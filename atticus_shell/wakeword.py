"""Always-on wake word — say the keyword (default "Siri") to start listening.

Two engines, chosen automatically:

  * livekit  (preferred): a small trained .onnx model (from `livekit-wakeword
    train`) streamed frame-by-frame. Real streaming detection, ~1.7% of one CPU
    core, low false-positive rate. Used when a model file exists
    (settings `wakeModel`, else models/<word>.onnx). See tools/train_wakeword.md.
  * whisper  (fallback, no training): an RMS voice-activity gate segments the
    mic and faster-whisper transcribes each short segment; a fuzzy match on the
    keyword fires it. Works immediately but higher latency / CPU than a trained
    model. Lets the feature work before the user trains a model.

Audio: PipeWire parecord, 16 kHz mono s16le (same as the command recorder).
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .speech import _record_cmd, SAMPLE_RATE
from .settings import get as _setting

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))
MODELS = RES / "models"

# Whisper-fallback homophone variants per keyword (Whisper usually prints the name).
VARIANTS = {
    "siri": {"siri", "siris", "siree", "ciri", "syria", "serie", "série",
             "cyrie", "cyree", "psyri", "hey siri", "ei siri", "siriy", "sidi"},
    # The "ei" prefix is transcribed wildly ("a siri", "eiz siri", "pc"), so for
    # recall also accept the distinctive "siri" word on its own + close variants.
    "ei siri": {"ei siri", "hey siri", "siri", "ciri", "sirih", "siree", "psiri", "cyri"},
    "atticus": {"atticus", "attic us", "attic"},
}


# faster-whisper hallucinates these stock phrases on silence/noise (pt + en).
_HALLUCINATIONS = {
    "legendas pela comunidade de amara org", "legendas pela comunidade amara org",
    "amara org", "obrigado por assistir", "inscreva se no canal", "ate o proximo video",
    "thanks for watching", "subtitles by the amara org community", "you",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).strip()


def _lev1(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            edits += 1
            if edits > 1:
                return False
            i, j = (i + 1, j + 1) if la == lb else (i, j + 1)
    return True


def model_path(word: str) -> Optional[str]:
    """Resolve a trained wake-word model: settings wakeModel, else models/<word>.onnx."""
    p = _setting("wakeModel")
    if p and os.path.exists(os.path.expanduser(p)):
        return os.path.expanduser(p)
    cand = MODELS / f"{re.sub(r'[^a-z0-9]+', '_', word.lower()).strip('_')}.onnx"
    return str(cand) if cand.exists() else None


class WakeListener:
    def __init__(self, on_wake: Callable[[], None], word: str = "siri",
                 model: Optional[str] = None, variants: Optional[set] = None):
        self.on_wake = on_wake
        self.word = _norm(word) or "siri"
        self.model_file = model or model_path(self.word)
        self.threshold = float(_setting("wakeThreshold", 0.5) or 0.5)
        # whisper fallback config
        self.targets = {_norm(v) for v in (variants or VARIANTS.get(self.word, {self.word}))}
        self.targets.add(self.word)
        self.wake_model_size = os.environ.get("ATTICUS_WAKE_MODEL", "small")
        self._wake_lang = _setting("asrLanguage", "auto")
        self._vad = float(_setting("wakeVad", 0.045) or 0.045)   # speech gate for the whisper fallback
        self._engine = "livekit" if self.model_file else "whisper"
        self._lk = None
        self._whisper = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = False
        self._last_fire = 0.0

    @property
    def engine(self) -> str:
        return self._engine

    def available(self) -> bool:
        if _record_cmd() is None:
            return False
        if self._engine == "livekit":
            try:
                import livekit.wakeword  # noqa: F401
                return True
            except Exception:
                self._engine = "whisper"   # library missing: fall back
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def _load(self) -> bool:
        try:
            if self._engine == "livekit":
                from livekit.wakeword import WakeWordModel
                # One thread, no spin-waiting: onnxruntime's default pool busy-
                # waits between the 80 ms frames, which measured ~22% of a core
                # while idle. A frame takes ~5 ms on one thread, so this is all
                # the model needs and idle cost drops to a few percent.
                opts = None
                try:
                    import onnxruntime as ort
                    opts = ort.SessionOptions()
                    opts.intra_op_num_threads = 1
                    opts.inter_op_num_threads = 1
                    opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
                    opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
                except Exception:
                    opts = None
                self._lk = WakeWordModel(models=[self.model_file], sess_options=opts,
                                         sample_rate=SAMPLE_RATE)
            else:
                from faster_whisper import WhisperModel
                self._whisper = WhisperModel(self.wake_model_size, device="cpu", compute_type="int8")
            return True
        except Exception:
            import traceback
            traceback.print_exc()
            return False

    def start(self) -> bool:
        if self._running or not self._load():
            return self._running
        self._running = True
        target = self._loop_livekit if self._engine == "livekit" else self._loop_whisper
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        return True

    def _spawn(self):
        cmd = _record_cmd()
        try:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) if cmd else None
        except OSError:
            return None

    def _fire_if_ready(self):
        from gi.repository import GLib
        now = time.monotonic()
        if now - self._last_fire < 2.0:
            return
        self._last_fire = now
        GLib.idle_add(self._fire)

    # --- livekit engine: stream fixed frames through the ONNX model ---------
    def _loop_livekit(self):
        chunk = 1280 * 2   # 80 ms frame (FRAME_SAMPLES * int16)
        dbg = bool(os.environ.get("ATTICUS_DEBUG"))
        peak, hb_t = 0.0, time.monotonic()
        empty_reads = 0
        self._proc = self._spawn()
        while self._running:
            # Take a local reference: pause() may set self._proc = None from
            # another thread between this check and the read below, but the
            # Popen object we already hold stays valid to read from (it just
            # returns EOF), so this never dereferences None.
            proc = self._proc
            if self._paused or proc is None:
                time.sleep(0.1)
                if not self._paused and self._proc is None:
                    self._proc = self._spawn()
                empty_reads = 0
                continue
            try:
                data = proc.stdout.read(chunk)
            except (OSError, ValueError):
                data = b""
            if not data:
                # EOF: parecord exited (PipeWire restart, mic unplugged, the
                # machine resumed from suspend, ...). Reading a dead pipe
                # returns b"" forever, so without this the wake word would
                # stay silently dead until the service is restarted by hand.
                empty_reads += 1
                if proc.poll() is not None and proc is self._proc:
                    self._proc = None
                time.sleep(min(0.5, 0.02 * empty_reads))
                continue
            empty_reads = 0
            samples = np.frombuffer(data, dtype=np.int16)
            try:
                scores = self._lk.predict(samples)
            except Exception:
                continue
            top = max(scores.values()) if scores else 0.0
            if dbg:
                peak = max(peak, top)
                if top >= 0.30:   # log anything that looks remotely like the word
                    print(f"[wake] score={top:.3f} (thr={self.threshold:.2f}) {'FIRE' if top >= self.threshold else ''}", flush=True)
                if time.monotonic() - hb_t > 3.0:
                    print(f"[wake] alive, peak_score_3s={peak:.3f} paused={self._paused}", flush=True)
                    peak, hb_t = 0.0, time.monotonic()
            if top >= self.threshold:
                self._fire_if_ready()

    # --- whisper fallback engine: VAD-segment, transcribe, fuzzy match ------
    def _loop_whisper(self):
        frame = 640
        speech, seg, silence = False, bytearray(), 0
        dbg = bool(os.environ.get("ATTICUS_DEBUG"))
        hb_n, hb_max, hb_t = 0, 0.0, time.monotonic()
        empty_reads = 0
        self._proc = self._spawn()
        while self._running:
            # See _loop_livekit: hold a local reference so a concurrent
            # pause() (which sets self._proc = None) can't make this
            # dereference None.
            proc = self._proc
            if self._paused or proc is None:
                time.sleep(0.1)
                if not self._paused and self._proc is None:
                    self._proc = self._spawn()
                speech, seg, silence = False, bytearray(), 0
                empty_reads = 0
                continue
            try:
                data = proc.stdout.read(frame)
            except (OSError, ValueError):
                data = b""
            if not data:
                # EOF: the recorder died. Respawn it instead of spinning on a
                # closed pipe forever (see _loop_livekit for the same fix).
                empty_reads += 1
                if proc.poll() is not None and proc is self._proc:
                    self._proc = None
                time.sleep(min(0.5, 0.02 * empty_reads))
                continue
            empty_reads = 0
            s = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean((s.astype(np.float32) / 32768.0) ** 2))) if s.size else 0.0
            if dbg:
                hb_n += 1; hb_max = max(hb_max, rms)
                if time.monotonic() - hb_t > 2.0:
                    print(f"[wake] audio alive: frames={hb_n} max_rms={hb_max:.3f} paused={self._paused}", flush=True)
                    hb_n, hb_max, hb_t = 0, 0.0, time.monotonic()
            if rms > self._vad:
                speech, silence = True, 0
                seg += data
            elif speech:
                silence += 1
                seg += data
                if silence > 18:
                    secs = len(seg) / 2 / SAMPLE_RATE
                    buf = bytes(seg)
                    speech, seg, silence = False, bytearray(), 0
                    if 0.2 < secs < 3.0:
                        self._check_whisper(buf)
            if len(seg) > SAMPLE_RATE * 2 * 4:
                speech, seg, silence = False, bytearray(), 0

    def _check_whisper(self, pcm: bytes):
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            lang = os.environ.get("ATTICUS_WAKE_LANG", self._wake_lang)
            segs, _ = self._whisper.transcribe(
                audio, language=(None if lang == "auto" else lang),
                beam_size=1, vad_filter=False, condition_on_previous_text=False)
            text = _norm(" ".join(s.text for s in segs))
        except Exception:
            return
        if text in _HALLUCINATIONS:   # Whisper invents these on silence/noise
            text = ""
        if os.environ.get("ATTICUS_DEBUG"):
            print(f"[wake] heard={text!r} match={self._matches(text) if text else False}", flush=True)
        if text and self._matches(text):
            self._fire_if_ready()

    def _matches(self, text: str) -> bool:
        words = text.split()
        for t in self.targets:
            toks = t.split()
            if len(toks) == 1:
                if any(w == toks[0] or _lev1(w, toks[0]) for w in words):
                    return True
            else:
                # Multi-word phrase: each token may be mis-transcribed a little
                # ("ei siri" -> "eiz siri", "ei ciri"), so fuzzy-match each word
                # of a sliding window in order.
                for i in range(len(words) - len(toks) + 1):
                    if all(words[i + j] == toks[j] or _lev1(words[i + j], toks[j])
                           for j in range(len(toks))):
                        return True
        return False

    def _fire(self) -> bool:
        try:
            self.on_wake()
        except Exception:
            import traceback
            traceback.print_exc()
        return False

    def pause(self):
        self._paused = True
        # Swap to a local reference before acting on it: the loop thread reads
        # self._proc without a lock, so clearing it first (then terminating
        # the local reference) avoids a window where self._proc is non-None
        # but already closing under it.
        proc, self._proc = self._proc, None
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def resume(self):
        if self._lk is not None:
            try:
                self._lk.reset()
            except Exception:
                pass
        self._paused = False

    def stop(self):
        self._running = False
        self.pause()
