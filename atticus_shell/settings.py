"""Read/write Atticus settings (shared with atticus.py and the Settings window).

Settings live in ~/.config/atticus/settings.json. Reads fall back to defaults
when the file or a key is missing; `set()` persists a single key so a spoken
"turn X on/off" takes effect (the brain relays it as a validated "⚙" card).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    base = os.environ.get("ATTICUS_DIR", str(Path.home() / ".config/atticus"))
    return Path(base) / "settings.json"


def all() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


def get(key: str, default=None):
    return all().get(key, default)


def set(key: str, value) -> bool:
    """Persist one setting, preserving the rest. Returns True on success."""
    p = _path()
    data = all()
    data[key] = value
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(p)   # atomic: a concurrent reader never sees half a file
        return True
    except OSError:
        return False


def migrate_wake_word_keys() -> None:
    """One-time fixup: before the Settings window existed, "wake" held the
    on/off switch and "wakeWord" held the word itself. The Settings window
    uses "wakeWord"
    for on/off and "wakeWordName" for the word — matching names, swapped
    meaning. Without this, opening the window once and saving anything would
    silently replace a real word like "siri" with a plain True/False. Runs
    once at startup; harmless (a no-op) once "wakeWordName" exists."""
    data = all()
    if isinstance(data.get("wakeWord"), str) and "wakeWordName" not in data:
        set("wakeWordName", data["wakeWord"])
        set("wakeWord", bool(data.get("wake", True)))
