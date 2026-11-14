"""Text-to-speech, fully local.

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
import signal
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


def _player_cmd(rate: int, device: Optional[str] = None) -> Optional[list]:
    """A raw-PCM player: s16le mono at the voice's sample rate."""
    if shutil.which("paplay"):
        return ["paplay", "--raw", f"--rate={rate}", "--channels=1",
                "--format=s16le", "--latency-msec=60",   # little buffered: pause and stop act at once
                *([f"--device={device}"] if device else [])]
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
        try:
            import piper as _piper_api  # noqa: F401
            has_api = True
        except Exception:
            has_api = False
        if (self._piper or has_api) and self._model and self._player:
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
        self._voice = None
        self.echo_free = False      # use_device() routed the voice through echo cancellation
        self.last_echo_free = False  # ... and the sentence being spoken went that way
        self._voice_failed = False
        self._voice_lock = threading.Lock()

    def use_device(self, sink: str) -> bool:
        """Play the voice into this sink (the echo-cancelling one, so the
        microphone can stay open while Atticus talks). Only the piper voice
        goes through the player this applies to; False means it cannot."""
        if self._engine != "piper" or not shutil.which("paplay"):
            return False
        self._player = _player_cmd(self._rate, sink)
        self.echo_free = True
        return True

    @property
    def engine(self) -> str:
        return self._engine or "none"

    def available(self) -> bool:
        return self._engine is not None

    def _enabled(self) -> bool:
        return bool(_setting("speak", True))   # default on

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
        self.last_echo_free = bool(use_piper and self.echo_free)
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

    def _load_voice(self):
        """Load the piper voice once, in-process. Starting the piper CLI for
        every answer re-imports onnxruntime and reloads the model each time:
        measured 2.5 s before the first sample, against 0.1 s with the voice
        kept warm. Returns None if the Python API is unavailable, and the
        caller falls back to the CLI."""
        with self._voice_lock:
            if self._voice is None and not self._voice_failed:
                try:
                    from piper import PiperVoice
                    self._voice = PiperVoice.load(self._model)
                except Exception:
                    self._voice_failed = True
            return self._voice

    def warm(self):
        if self._engine == "piper":
            threading.Thread(target=self._load_voice, daemon=True).start()

    def _speak_piper(self, words: str):
        voice = self._load_voice()
        if voice is None:
            return self._speak_piper_cli(words)
        with self._lock:
            try:
                play = subprocess.Popen(
                    self._player, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._procs = []
                return
            self._procs = [play]

        # Synthesize sentence by sentence straight into the player, so the
        # first words are heard while the rest is still being generated.
        def feed():
            try:
                for chunk in voice.synthesize(words):
                    if play.poll() is not None:   # stop() ended playback
                        break
                    play.stdin.write(chunk.audio_int16_bytes)
            except Exception:
                pass
            finally:
                try:
                    play.stdin.close()
                except Exception:
                    pass
        threading.Thread(target=feed, daemon=True).start()

    def _speak_piper_cli(self, words: str):
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

    def _signal(self, sig):
        with self._lock:
            for p in self._procs:
                if p and p.poll() is None:
                    try:
                        p.send_signal(sig)
                    except Exception:
                        pass

    def pause(self):
        """Hold the voice mid-sentence (resume() carries on from there)."""
        self._signal(signal.SIGSTOP)

    def resume(self):
        self._signal(signal.SIGCONT)

    def stop(self):
        with self._lock:
            for p in self._procs:
                if p and p.poll() is None:
                    try:
                        p.terminate()
                        p.send_signal(signal.SIGCONT)   # a paused player only dies once continued
                    except Exception:
                        pass
            self._procs = []
