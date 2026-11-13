"""Speech-to-text — the Linux replacement for Apple's on-device recognizer.

macOS used SFSpeechRecognizer for live, on-device transcription. We use
faster-whisper (CTranslate2) locally. v1 records while the hotkey is held and
transcribes on release; a live RMS level drives the orb. Streaming partial
words can be layered on later with chunked/VAD decoding.

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


def _record_cmd() -> Optional[list[str]]:
    if shutil.which("parecord"):
        return ["parecord", "--raw", f"--rate={SAMPLE_RATE}", "--channels=1", "--format=s16le"]
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1",
                "--format", "s16", "--raw", "-"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"]
    return None


class Recorder:
    """Capture the microphone to an in-memory PCM buffer with a live level."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._chunks: list[bytes] = []
        self._thread: Optional[threading.Thread] = None
        self._level = 0.0
        self._running = False

    @property
    def level(self) -> float:
        """0..1 RMS of the most recent chunk, for the orb."""
        return self._level

    def available(self) -> bool:
        return _record_cmd() is not None

    def start(self) -> bool:
        if self._running:
            return True
        cmd = _record_cmd()
        if not cmd:
            return False
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
        except OSError:
            return False
        self._chunks = []
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
                rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2)))
                # Perceptual-ish scaling so quiet speech still moves the orb.
                self._level = min(1.0, rms * 4.0)

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


class Transcriber:
    """faster-whisper, loaded lazily and kept warm."""

    def __init__(self, model_size: Optional[str] = None, language: Optional[str] = None):
        # "small" is multilingual and noticeably better at pt-BR than "base",
        # while staying fast with int8 on CPU.
        self.model_size = model_size or os.environ.get("ATTICUS_WHISPER_MODEL", "small")
        # Language is configurable (public repo): "auto" detects per utterance,
        # or pin it (e.g. "pt", "en") to remove detection errors on short clips
        # and decode that language far better. Default auto so any user is served;
        # this install pins pt in settings.json for best Brazilian-Portuguese.
        if language is None:
            language = _setting("asrLanguage", "auto")
        self.language = None if str(language).lower() == "auto" else language
        self.last_language: Optional[str] = None  # language used for the last utterance
        self._model = None
        self._lock = threading.Lock()

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
        threading.Thread(target=self.load, daemon=True).start()

    def transcribe(self, audio: np.ndarray, language: Optional[str] = None) -> str:
        if audio.size < SAMPLE_RATE // 4:   # < 0.25s: nothing useful was said
            return ""
        self.load()
        segments, info = self._model.transcribe(
            audio, language=language or self.language, vad_filter=True,
            beam_size=1, condition_on_previous_text=False)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        self.last_language = getattr(info, "language", None)
        return text
