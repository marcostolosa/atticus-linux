"""Text-to-speech — the Linux replacement for macOS `say`.

Two engines, chosen automatically:

  * piper   (preferred): a small neural voice (.onnx from piper) streamed as raw
    PCM into a PipeWire/Pulse player. Natural, offline, fast on CPU. Used when a
    voice model exists (settings `ttsModel`, else models/<ttsVoice>.onnx, default
    pt_BR-faber-medium). Install: pip install piper-tts + download the voice.
  * espeak  (fallback): espeak-ng/espeak formant synth. Robotic but dependency-
    free, so the core loop never hard-depends on a model being present.

Only runs when the user's "speak answers" setting is on.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))
MODELS = RES / "models"

# Map detected/UI language codes to espeak-ng voices (fallback engine only).
_VOICE = {"en": "en-us", "pt": "pt-br", "pt-br": "pt-br",
          "it": "it", "es": "es", "fr": "fr", "de": "de"}


def spoken(answer: str) -> str:
    """Answer text without Markdown marks, roughly what should be read aloud."""
    s = answer
    s = re.sub(r"```.*?```", " ", s, flags=re.S)         # code fences
    s = re.sub(r"`([^`]*)`", r"\1", s)                    # inline code
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)           # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # links -> text
    s = re.sub(r"[*_#>~]", "", s)                          # emphasis/heading marks
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _piper_bin() -> Optional[str]:
    cand = Path(sys.executable).parent / "piper"
    if cand.exists():
        return str(cand)
    return shutil.which("piper")


def _piper_model() -> Optional[str]:
    """Resolve a piper voice: settings ttsModel, else models/<ttsVoice>.onnx."""
    p = _setting("ttsModel")
    if p and os.path.exists(os.path.expanduser(p)):
        return os.path.expanduser(p)
    voice = _setting("ttsVoice") or "pt_BR-faber-medium"
    cand = MODELS / f"{voice}.onnx"
    return str(cand) if cand.exists() else None


def _player_cmd(rate: int) -> Optional[list]:
    """A raw-PCM player: s16le mono at the voice's sample rate."""
    if shutil.which("paplay"):
        return ["paplay", "--raw", f"--rate={rate}", "--channels=1",
                "--format=s16le"]
    if shutil.which("pw-cat"):
        return ["pw-cat", "--playback", "--rate", str(rate), "--channels", "1",
                "--format", "s16", "-"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate),
                "-c", "1", "-"]
    return None


def _setting(key, default=None):
    p = Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "settings.json"
    try:
        return json.loads(p.read_text()).get(key, default)
    except (OSError, ValueError):
        return default


class Speaker:
    def __init__(self):
        self._piper = _piper_bin()
        self._model = _piper_model()
        self._rate = 22050
        self._player = _player_cmd(self._rate)
        if self._piper and self._model and self._player:
            try:
                cfg = json.loads(Path(self._model + ".json").read_text())
                self._rate = int(cfg.get("audio", {}).get("sample_rate", 22050))
                self._player = _player_cmd(self._rate)
            except Exception:
                pass
            self._engine = "piper"
        else:
            self._engine = ("espeak-ng" if shutil.which("espeak-ng")
                            else ("espeak" if shutil.which("espeak") else None))
        self._espeak = ("espeak-ng" if shutil.which("espeak-ng")
                        else ("espeak" if shutil.which("espeak") else None))
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()

    @property
    def engine(self) -> str:
        return self._engine or "none"

    def available(self) -> bool:
        return self._engine is not None

    def _enabled(self) -> bool:
        return bool(_setting("speak", True))   # default on, like the Mac read-aloud

    def _espeak_voice(self, override: Optional[str] = None) -> str:
        if override and override.lower() in _VOICE:
            return _VOICE[override.lower()]
        lang = str(_setting("uiLanguage", "") or "").lower()
        if lang in _VOICE:
            return _VOICE[lang]
        asr = str(_setting("asrLanguage", "auto")).lower()
        return _VOICE.get(asr, "en-us")

    def speak(self, text: str, language: Optional[str] = None, done=None):
        """Speak text. If `done` is given, it is called (on a worker thread) once
        playback finishes or cannot start, so the caller can, for example, open
        the mic to hear a reply only after the question has been read aloud."""
        if not self._engine or not self._enabled():
            if done:
                threading.Thread(target=done, daemon=True).start()
            return
        words = spoken(text)
        if not words:
            if done:
                threading.Thread(target=done, daemon=True).start()
            return
        self.stop()
        # The piper voice is pt-BR; read non-pt answers with espeak if we have it.
        lang = (language or "").lower()
        use_piper = (self._engine == "piper" and
                     (not lang or lang.startswith("pt") or not self._espeak))
        if use_piper:
            self._speak_piper(words)
        else:
            self._speak_espeak(words, language)
        if done:
            procs = list(self._procs)

            def _watch():
                for p in procs:
                    try:
                        p.wait()
                    except Exception:
                        pass
                try:
                    done()
                except Exception:
                    pass
            threading.Thread(target=_watch, daemon=True).start()

    def _speak_piper(self, words: str):
        with self._lock:
            try:
                synth = subprocess.Popen(
                    [self._piper, "-m", self._model, "--output-raw"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
                play = subprocess.Popen(
                    self._player, stdin=synth.stdout,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                synth.stdout.close()   # play owns the read end
                self._procs = [play, synth]
            except OSError:
                self._procs = []
                return
        # Feed the text on a worker so speak() returns immediately.
        def feed():
            try:
                synth.stdin.write(words.encode("utf-8"))
                synth.stdin.close()
            except Exception:
                pass
        threading.Thread(target=feed, daemon=True).start()

    def _speak_espeak(self, words: str, language: Optional[str]):
        engine = self._espeak or self._engine
        if engine not in ("espeak", "espeak-ng"):
            return
        voice = self._espeak_voice(language)
        with self._lock:
            try:
                self._procs = [subprocess.Popen(
                    [engine, "-v", voice, words],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
            except OSError:
                self._procs = []

    def stop(self):
        with self._lock:
            for p in self._procs:
                if p and p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
            self._procs = []
