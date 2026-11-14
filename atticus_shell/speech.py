"""Speech-to-text, fully local.

faster-whisper (CTranslate2) transcribes on this machine: nothing is sent
anywhere. A recording runs from the hotkey / wake word until release or
trailing silence and is transcribed at the end; a live RMS level drives the orb.

Audio is captured with PipeWire's parecord/pw-record as 16 kHz mono s16le — no
Python audio C-extension (portaudio) required.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from .settings import get as _setting

SAMPLE_RATE = 16000


LATENCY_MS = 40   # how often the recorder hands over what it heard


def _record_cmd(device: Optional[str] = None) -> Optional[list[str]]:
    # The latency is asked for by name. Left to itself parecord hands audio
    # over two seconds at a time (measured: the first sample 2034 ms after it
    # starts, then 64000 bytes every 2 s), so the level, the end of a
    # sentence and the wake word were all being judged up to two seconds late.
    if shutil.which("parecord"):
        return ["parecord", "--raw", f"--rate={SAMPLE_RATE}", "--channels=1", "--format=s16le",
                f"--latency-msec={LATENCY_MS}", *([f"--device={device}"] if device else [])]
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1",
                "--format", "s16", "--latency", f"{LATENCY_MS}ms", "--raw", "-"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"]
    return None


def _end(proc) -> None:
    try:
        proc.terminate()
    except Exception:
        pass


class Recorder:
    """Capture the microphone to an in-memory PCM buffer with a live level."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._chunks: list[bytes] = []
        self._thread: Optional[threading.Thread] = None
        self._level = 0.0
        self._running = False
        self.lead = 0.0   # seconds of audio a handed-on stream arrived with

    @property
    def level(self) -> float:
        """0..1 RMS of the most recent chunk, for the orb."""
        return self._level

    def available(self) -> bool:
        return _record_cmd() is not None

    def start(self, device: Optional[str] = None, stream=None) -> bool:
        """stream: (process, audio so far) of a recorder that is already
        running — the one that just heard the wake word. Carrying on with it
        loses nothing of what is said straight after the word; starting a new
        one would miss the time it takes to open."""
        if self._running:
            if stream:
                _end(stream[0])
            return True
        self._chunks, self.lead = [], 0.0
        if stream:
            self._proc, pcm = stream
            self._chunks = [pcm[i:i + 2048] for i in range(0, len(pcm), 2048)]
            self.lead = len(pcm) / 2 / SAMPLE_RATE
        else:
            cmd = _record_cmd(device)
            if not cmd:
                return False
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL)
            except OSError:
                return False
        self._running = True
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True

    def _pump(self):
        assert self._proc and self._proc.stdout
        while self._running:
            data = self._proc.stdout.read(2048)
            if not data:
                break
            self._chunks.append(data)
            samples = np.frombuffer(data, dtype=np.int16)
            if samples.size:
                rms = voice_rms(samples.astype(np.float32) / 32768.0)
                # Perceptual-ish scaling so quiet speech still moves the orb.
                self._level = min(1.0, rms * 4.0)

    def keep_last(self, seconds: float) -> None:
        """Forget all but the most recent audio (what came before was only
        being watched for a voice, not recorded for its words)."""
        keep = max(1, int(seconds * SAMPLE_RATE * 2 / 2048))
        del self._chunks[:-keep]

    def tail(self, seconds: float) -> np.ndarray:
        """The most recent audio, while still recording."""
        keep = max(1, int(seconds * SAMPLE_RATE * 2 / 2048))
        pcm = b"".join(self._chunks[-keep:])
        return np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0

    def stop(self) -> np.ndarray:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=2)
        self._level = 0.0
        pcm = b"".join(self._chunks)
        if not pcm:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def speech_seconds(audio: np.ndarray, threshold: float = 0.5, gain: float = 0.0) -> float:
    """How much of this audio is a person talking, by a voice detector (the
    Silero model that ships with faster-whisper) rather than by loudness: an
    air conditioner is loud and is not speech, a whisper is faint and is.
    gain 0 brings the audio to a steady level first; a fixed gain keeps quiet
    things quiet. Returns -1 when the detector is unavailable."""
    if audio.size < SAMPLE_RATE // 4:
        return 0.0
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        x = level_audio(audio) if not gain else np.clip(audio * gain, -1.0, 1.0).astype(np.float32)
        spans = get_speech_timestamps(x, VadOptions(threshold=threshold, min_speech_duration_ms=150,
                                                    min_silence_duration_ms=200, speech_pad_ms=0))
        return sum(t["end"] - t["start"] for t in spans) / SAMPLE_RATE
    except Exception:
        return -1.0


def voice_ago(audio: np.ndarray, threshold: float = 0.5) -> float:
    """Seconds since a voice was last heard in this audio: near 0 while
    someone is still talking at its end, its whole length when nobody spoke
    in it. This is what ends a sentence: a pause is told from a fan by what
    it is, not by how loud it is. Returns -1 when the detector is unavailable."""
    total = audio.size / SAMPLE_RATE
    if audio.size < SAMPLE_RATE // 4:
        return total
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        spans = get_speech_timestamps(level_audio(audio), VadOptions(
            threshold=threshold, min_speech_duration_ms=120, min_silence_duration_ms=100, speech_pad_ms=0))
        return total - spans[-1]["end"] / SAMPLE_RATE if spans else total
    except Exception:
        return -1.0


# What Whisper writes when handed noise with no words in it. From a microphone
# that opened by itself these are never taken as something the user asked.
_PHANTOMS = {"tchau", "tchau tchau", "obrigado", "obrigada", "e ai", "ate a proxima", "o que e isso",
             "legendas pela comunidade amara org", "thank you", "thanks for watching", "bye", "you"}


def is_phantom(text: str) -> bool:
    import re
    import unicodedata
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join(re.findall(r"\w+", t)) in _PHANTOMS


RUMBLE_HZ = 150   # fans and air conditioners live below this; voices do not


def voice_rms(samples: np.ndarray) -> float:
    """Loudness of one short chunk counting only the band voices use. An air
    conditioner puts several times more energy into the microphone than a
    quiet voice does, nearly all of it as low rumble; measured across the
    whole band, that rumble reads as speech and a whisper as nothing."""
    n = samples.size
    if n < 64:
        return float(np.sqrt(np.mean(samples ** 2))) if n else 0.0
    power = np.abs(np.fft.rfft(samples)) ** 2
    first = int(np.ceil(RUMBLE_HZ * n / SAMPLE_RATE))
    return float(np.sqrt(2.0 * power[first:].sum()) / n)


# The rumble is cut only for measuring loudness. The recognizer gets the
# recording as it is: on real recordings mixed with real air-conditioner
# noise, every cleaning tried before Whisper (rumble cut, voice band-pass,
# a neural denoiser) made it understand fewer commands than leaving the
# noise in, as studies of denoising in front of Whisper also report.


def level_audio(audio: np.ndarray, target: float = 0.25) -> np.ndarray:
    """Bring quiet speech up to a steady level before recognition. A whisper or
    a distant voice reaches the recognizer many times fainter than the audio
    it was trained on; scaling by the loudest moments (not the single loudest
    sample, so one click does not set the gain) fixes that without clipping."""
    if audio.size == 0:
        return audio
    peak = float(np.percentile(np.abs(audio), 99.9)) or 1e-6
    return np.clip(audio * min(target / peak, 60.0), -1.0, 1.0).astype(np.float32)


# Whisper spells an unusual name the way a common word sounds ("Luz do Dom"
# comes back as "luz do dó"). Told beforehand which names to expect, it writes
# them right. These are the names: the user's own list (settings
# `asrVocabulary`) and what their smart-home devices are called.
_NAMED_ALOUD = ("light", "climate", "fan", "cover", "lock", "media_player", "vacuum", "scene", "humidifier")
_SOMETIMES = ("switch", "input_boolean", "script")   # plugs have spoken names; dozens of option toggles do not
_names = {"at": 0.0, "list": [], "busy": False}
_names_lock = threading.Lock()


def _fetch_names() -> None:
    try:
        from . import home
        rows = home.entities() if home.configured() else []
        named = [r["name"] for d in _NAMED_ALOUD for r in rows if r["domain"] == d]
        extra = [r["name"] for d in _SOMETIMES for r in rows if r["domain"] == d]
        _names["list"] = named + (extra if len(extra) <= 12 else [])
    except Exception:
        pass
    finally:
        import time
        _names["at"], _names["busy"] = time.monotonic(), False


def spoken_names(refresh: bool = False) -> list[str]:
    """What the user's devices are called. Never waits on the network unless
    asked to (refresh): it answers with what it last knew and looks again in
    the background every ten minutes."""
    import time
    if refresh:
        _fetch_names()
    elif time.monotonic() - _names["at"] > 600 or not _names["at"]:
        with _names_lock:
            start, _names["busy"] = not _names["busy"], True
        if start:
            threading.Thread(target=_fetch_names, daemon=True).start()
    return list(_names["list"])


def vocabulary(refresh: bool = False, limit: int = 40) -> list[str]:
    """The names worth telling the recognizer about, the user's own first. A
    name that adds no new word is left out, and the list stays short: a long
    one starts putting its words where they were not said."""
    own = _setting("asrVocabulary") or []
    out, seen, size = [], set(), 0
    for name in [*(own if isinstance(own, list) else []), *spoken_names(refresh)]:
        said = str(name).split()
        if not {w.lower() for w in said} - seen or size + len(said) > limit:
            continue
        seen |= {w.lower() for w in said}
        size += len(said)
        out.append(" ".join(said))
    return out


class RemoteWhisper:
    """Speech-to-text on the user's own Home Assistant server, when it offers
    one (its Wyoming faster-whisper service usually runs a much larger model on
    a much stronger machine than this laptop). Measured on real recordings from
    this desk: 12% word errors in 1.3 s there, against 28% in 3.4 s locally —
    and 13% against 45% when whispering. The audio goes to that server and
    nowhere else; if it does not answer, the local model takes over."""

    def __init__(self):
        self._entity: Optional[str] = None
        self._checked = 0.0
        self._down_until = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _conf():
        mode = str(_setting("sttRemote", "auto")).lower()
        url, token = str(_setting("haUrl") or "").rstrip("/"), _setting("haToken")
        return (url, token) if mode not in ("off", "false", "local", "none") and url and token else (None, None)

    def _request(self, path: str, data: Optional[bytes] = None, headers: Optional[dict] = None, timeout: float = 5):
        import json as _json
        import urllib.request
        url, token = self._conf()
        req = urllib.request.Request(url + path, data=data, headers={"Authorization": f"Bearer {token}", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read() or b"null")

    def entity(self) -> Optional[str]:
        """The server's speech-to-text entity: settings `sttEntity`, else the
        first Whisper one it lists. Looked up at most once a minute."""
        import time
        url, _ = self._conf()
        if not url or time.monotonic() < self._down_until:
            return None
        with self._lock:
            if self._entity and time.monotonic() - self._checked < 600:
                return self._entity
            try:
                want = _setting("sttEntity")
                if not want:
                    names = [s["entity_id"] for s in self._request("/api/states") if str(s.get("entity_id", "")).startswith("stt.")]
                    want = next((n for n in names if "whisper" in n), None)
                if want:
                    self._request("/api/stt/" + want, timeout=4)   # it answers: usable
                self._entity, self._checked = want, time.monotonic()
            except Exception:
                self._entity, self._down_until = None, time.monotonic() + 60
            return self._entity

    def transcribe(self, audio: np.ndarray, language: str) -> Optional[str]:
        """Text, or None when the server could not be used (the caller falls back)."""
        import io
        import time
        import wave
        entity = self.entity()
        if not entity:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        try:
            out = self._request("/api/stt/" + entity, data=buf.getvalue(), timeout=float(_setting("sttRemoteTimeout", 8) or 8),
                                headers={"X-Speech-Content": "format=wav; codec=pcm; sample_rate=16000; bit_rate=16; "
                                                             f"channel=1; language={language}"})
            if isinstance(out, dict) and out.get("result") == "success":
                return str(out.get("text") or "").strip()
        except Exception:
            pass
        self._down_until = time.monotonic() + 60   # do not make the next minute of requests wait on a dead server
        return None


class Transcriber:
    """faster-whisper, loaded lazily and kept warm."""

    def __init__(self, model_size: Optional[str] = None, language: Optional[str] = None):
        # "small" is multilingual and noticeably better at pt-BR than "base",
        # while staying fast with int8 on CPU.
        # Settings "asrModel": "base" answers about 2 s sooner on a laptop CPU
        # but mishears more; "small" is the default; a GPU host can go larger.
        self.model_size = (model_size or os.environ.get("ATTICUS_WHISPER_MODEL")
                           or _setting("asrModel") or "small")
        # Language is configurable (public repo): "auto" detects per utterance,
        # or pin it (e.g. "pt", "en") to remove detection errors on short clips
        # and decode that language far better. Default auto so any user is served;
        # this install pins pt in settings.json for best Brazilian-Portuguese.
        if language is None:
            language = _setting("asrLanguage", "auto")
        self.language = None if str(language).lower() == "auto" else language
        self.last_language: Optional[str] = None  # language used for the last utterance
        self.last_engine = ""                       # "remote" or "local", for diagnostics
        self._model = None
        self._lock = threading.Lock()
        self.remote = RemoteWhisper()

    def load(self):
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel
            # int8 on CPU is fast and light; a GPU host can override via env later.
            device = os.environ.get("ATTICUS_WHISPER_DEVICE", "cpu")
            compute = os.environ.get("ATTICUS_WHISPER_COMPUTE", "int8")
            self._model = WhisperModel(self.model_size, device=device, compute_type=compute)

    def warm(self):
        # With a working server the local model (about 500 MB of RAM) is only
        # loaded if it is ever needed as the fallback.
        def go():
            if not self.remote.entity():
                self.load()
        threading.Thread(target=go, daemon=True).start()

    def _remote_language(self, language: Optional[str]) -> str:
        lang = (language or self.language or "").lower()[:2]
        if lang:
            return lang
        return "pt" if (os.environ.get("LANG") or "").lower().startswith("pt") else "en"

    def transcribe(self, audio: np.ndarray, language: Optional[str] = None) -> str:
        if audio.size < SAMPLE_RATE // 4:   # < 0.25s: nothing useful was said
            return ""
        audio = level_audio(audio)
        lang = self._remote_language(language)
        text = self.remote.transcribe(audio, lang)
        if text is not None:
            self.last_language, self.last_engine = lang, "remote"
            return text
        self.last_engine = "local"
        return self.transcribe_local(audio, language, vocabulary() if _setting("asrBias", True) else [])

    def transcribe_local(self, audio: np.ndarray, language: Optional[str] = None,
                         names: Optional[list[str]] = None) -> str:
        """The model on this machine. `names` are the ones it should expect."""
        self.load()
        # No voice-activity filter: it is tuned for normal speech and drops whispers.
        segments, info = self._model.transcribe(
            audio, language=language or self.language, vad_filter=False,
            beam_size=5, condition_on_previous_text=False,
            hotwords=", ".join(names) if names else None)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        self.last_language = getattr(info, "language", None)
        return text
