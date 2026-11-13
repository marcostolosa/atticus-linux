"""Read Atticus settings (shared with atticus.py and the Settings window).

Settings live in ~/.config/atticus/settings.json. This is read-only here; the
Settings UI writes them. Missing file / keys fall back to defaults.
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
