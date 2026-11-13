"""Short UI sounds (earcons) so the user hears when Atticus starts and stops
listening — like Alexa's open/close chimes.

Tones are synthesized once into a cache dir (no asset files to ship) and played
fire-and-forget through the same PipeWire/Pulse player the voice uses. Gated by
the `sounds` setting (default on); independent of the `speak` (TTS) setting.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Optional

import numpy as np

RATE = 22050
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "atticus" / "earcons"

# Each earcon is a list of (freq, seconds); freq can be (f0, f1) for a glide.
_SPECS = {
    "start": [(660, 0.085), (990, 0.11)],    # rising two-note: "listening"
    "end":   [(880, 0.075), (620, 0.10)],    # falling two-note: "got it"
    "cancel": [(520, 0.10)],                  # single low blip: dismissed
}


def _render(segments) -> np.ndarray:
    parts = []
    for freq, dur in segments:
        n = max(1, int(RATE * dur))
        t = np.arange(n) / RATE
        if isinstance(freq, (tuple, list)):
            f = np.linspace(freq[0], freq[1], n)
            wave_ = np.sin(2 * np.pi * np.cumsum(f) / RATE)
        else:
            wave_ = np.sin(2 * np.pi * freq * t)
        env = np.ones(n)
        a, d = int(RATE * 0.012), int(RATE * 0.035)
        if a:
            env[:a] = np.linspace(0, 1, a)
        if d:
            env[-d:] = np.linspace(1, 0, d)
        parts.append(wave_ * env)
    sig = np.concatenate(parts) if parts else np.zeros(1)
    return (sig * 0.26 * 32767).astype(np.int16)


def _player() -> Optional[list]:
    if shutil.which("paplay"):
        return ["paplay"]
    if shutil.which("pw-play"):
        return ["pw-play"]
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    return None


def _setting(key, default=None):
    p = Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "settings.json"
    try:
        return json.loads(p.read_text()).get(key, default)
    except (OSError, ValueError):
        return default


class Earcons:
    def __init__(self):
        self._player = _player()
        self._files: dict[str, str] = {}
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            for name, spec in _SPECS.items():
                path = CACHE / f"{name}.wav"
                if not path.exists():
                    self._write_wav(path, _render(spec))
                self._files[name] = str(path)
        except Exception:
            self._files = {}

    @staticmethod
    def _write_wav(path: Path, samples: np.ndarray):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(samples.tobytes())

    def enabled(self) -> bool:
        return bool(_setting("sounds", True))

    def play(self, name: str):
        if not self._player or not self.enabled():
            return
        path = self._files.get(name)
        if not path:
            return
        try:
            subprocess.Popen(self._player + [path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
