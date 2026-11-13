"""Always-on wake word — say the keyword (default "Siri") to start listening.

The keyword is an out-of-vocabulary name, which small streaming recognizers
(Vosk, openWakeWord's stock models) don't handle. faster-whisper does — it is
multilingual (so "Siri" / pt-BR "SÍ-RI" both transcribe) and knows the token.

To keep an always-on spotter cheap we don't run Whisper continuously: a light
RMS voice-activity gate segments the mic, and only short speech segments (the
length of a wake phrase) are transcribed, with the fast "tiny" model. A fuzzy
match against the keyword and its homophones then fires on_wake().

Audio: PipeWire parecord, 16 kHz mono s16le (same as the command recorder).
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import unicodedata
from typing import Callable, Optional

import numpy as np

from .speech import _record_cmd, SAMPLE_RATE
from .settings import get as _setting

# Homophone variants per keyword: Whisper usually prints the name, but accents,
# pt-BR pronunciation and mishearings vary, so match a small set plus edit-distance 1.
VARIANTS = {
    "siri": {"siri", "siris", "siree", "siri", "ciri", "syria", "serie", "série",
             "cyrie", "cyree", "psyri", "hey siri", "siriy", "sirih", "sidi"},
    "atticus": {"atticus", "attic us", "attic", "atticus"},
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).strip()


def _lev1(a: str, b: str) -> bool:
    """True if edit distance(a, b) <= 1 (cheap early-out version)."""
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
            if la == lb:
                i += 1; j += 1
            else:
                j += 1
    return True


class WakeListener:
    def __init__(self, on_wake: Callable[[], None], word: str = "siri",
                 model: Optional[str] = None, variants: Optional[set] = None):
        self.on_wake = on_wake
        self.word = _norm(word) or "siri"
        self.targets = {_norm(v) for v in (variants or VARIANTS.get(self.word, {self.word}))}
        self.targets.add(self.word)
        self.model_size = model or os.environ.get("ATTICUS_WAKE_MODEL", "tiny")
        self._wake_lang = _setting("asrLanguage", "auto")
        self._model = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = False
        self._last_fire = 0.0

    def available(self) -> bool:
        if _record_cmd() is None:
            return False
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")

    def start(self) -> bool:
        if self._running:
            return True
        try:
            self._load()
        except Exception:
            import traceback
            traceback.print_exc()
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def _spawn(self):
        cmd = _record_cmd()
        try:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) if cmd else None
        except OSError:
            return None

    def _loop(self):
        from gi.repository import GLib
        frame = 640  # 20 ms at 16 kHz s16le (320 samples * 2 bytes)
        speech, seg, silence = False, bytearray(), 0
        self._proc = self._spawn()
        while self._running:
            if self._paused or not self._proc:
                time.sleep(0.1)
                if not self._paused and not self._proc:
                    self._proc = self._spawn()
                speech, seg, silence = False, bytearray(), 0
                continue
            data = self._proc.stdout.read(frame)
            if not data:
                time.sleep(0.02)
                continue
            samples = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2))) if samples.size else 0.0
            if rms > 0.02:
                speech = True
                silence = 0
                seg += data
            elif speech:
                silence += 1
                seg += data
                if silence > 18:   # ~0.36 s trailing silence -> end of phrase
                    secs = len(seg) / 2 / SAMPLE_RATE
                    buf = bytes(seg)
                    speech, seg, silence = False, bytearray(), 0
                    if 0.2 < secs < 3.0:
                        self._check(buf, GLib)
            if len(seg) > SAMPLE_RATE * 2 * 4:   # cap a runaway segment at 4 s
                speech, seg, silence = False, bytearray(), 0

    def _check(self, pcm: bytes, GLib):
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            # Bias to pt-BR (the user's language) so "Siri" / "SÍ-RI" decodes well.
            lang = os.environ.get("ATTICUS_WAKE_LANG", self._wake_lang)
            segments, _ = self._model.transcribe(
                audio, language=(None if lang == "auto" else lang),
                beam_size=1, vad_filter=False, condition_on_previous_text=False)
            text = _norm(" ".join(s.text for s in segments))
        except Exception:
            return
        if not text or not self._matches(text):
            return
        now = time.monotonic()
        if now - self._last_fire < 2.0:
            return
        self._last_fire = now
        GLib.idle_add(self._fire)

    def _matches(self, text: str) -> bool:
        words = text.split()
        for t in self.targets:
            if " " in t:
                if t in text:
                    return True
            else:
                for w in words:
                    if w == t or _lev1(w, t):
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
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def resume(self):
        self._paused = False

    def stop(self):
        self._running = False
        self.pause()
