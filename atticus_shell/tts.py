"""Text-to-speech — the Linux replacement for macOS `say`.

v1 uses espeak-ng when present (and is a no-op otherwise, so the core loop never
depends on it). A nicer piper voice can slot in behind the same interface later.
Only runs when the user's "speak answers" setting is on.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

# espeak-ng language codes for the UI languages Atticus supports.
_VOICE = {"en": "en", "it": "it", "es": "es", "fr": "fr", "de": "de"}


def spoken(answer: str) -> str:
    """Answer text without Markdown marks, roughly what should be read aloud."""
    s = answer
    s = re.sub(r"```.*?```", " ", s, flags=re.S)         # code fences
    s = re.sub(r"`([^`]*)`", r"\1", s)                    # inline code
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)           # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # links -> text
    s = re.sub(r"[*_#>~]", "", s)                          # emphasis/heading marks
    s = s.replace(" ", "\n")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


class Speaker:
    def __init__(self):
        self._engine = "espeak-ng" if shutil.which("espeak-ng") else (
            "espeak" if shutil.which("espeak") else None)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self._engine is not None

    def _enabled(self) -> bool:
        p = Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "settings.json"
        try:
            s = json.loads(p.read_text())
        except (OSError, ValueError):
            s = {}
        return bool(s.get("speak", True))   # default on, like the Mac read-aloud

    def _language(self) -> str:
        p = Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "settings.json"
        try:
            lang = (json.loads(p.read_text()).get("uiLanguage") or "").lower()
        except (OSError, ValueError):
            lang = ""
        return _VOICE.get(lang, "en")

    def speak(self, text: str):
        if not self._engine or not self._enabled():
            return
        words = spoken(text)
        if not words:
            return
        self.stop()
        voice = self._language()
        with self._lock:
            try:
                self._proc = subprocess.Popen(
                    [self._engine, "-v", voice, words],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None

    def stop(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None
