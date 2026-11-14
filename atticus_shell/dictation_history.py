"""What dictation typed, kept for the Settings window: search, copy, delete,
and the stats the Dictation page shows (words today/all time, sessions, time
saved). Off by the `dictateHistory` setting (on by default); turning it off
keeps what is already saved.

Last 500 entries, 30 days — the same limit Atticus.swift used.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

KEEP_DAYS = 30
KEEP_ROWS = 500


def _path() -> Path:
    return Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "dictations.json"


def _read() -> list[dict]:
    try:
        rows = json.loads(_path().read_text())
    except (OSError, ValueError):
        return []
    cutoff = time.time() - KEEP_DAYS * 86400
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("timestamp"), (int, float)) and r["timestamp"] >= cutoff] \
        if isinstance(rows, list) else []


def _write(rows: list[dict]) -> None:
    path = _path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
            json.dump(rows[-KEEP_ROWS:], f, ensure_ascii=False)
        os.chmod(f.name, 0o600)
        os.replace(f.name, path)
    except OSError:
        pass


def add(text: str, app: str = "", raw: str = "", now: float | None = None) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    row = {"id": uuid.uuid4().hex, "text": text, "app": app or "",
           "timestamp": time.time() if now is None else now}
    if raw and raw.strip() != text:
        row["raw"] = raw.strip()
    rows = _read()
    rows.append(row)
    _write(rows)
    return row


def delete(identifier: str) -> bool:
    rows = _read()
    kept = [r for r in rows if r.get("id") != identifier]
    if len(kept) == len(rows):
        return False
    _write(kept)
    return True


def clear() -> None:
    _write([])


def stats(rows: list[dict] | None = None) -> dict:
    rows = _read() if rows is None else rows
    today = time.strftime("%Y-%m-%d")
    words = lambda r: len(str(r.get("text", "")).split())
    total = sum(words(r) for r in rows)
    today_words = sum(words(r) for r in rows if time.strftime("%Y-%m-%d", time.localtime(r["timestamp"])) == today)
    return {"today": today_words, "words": total, "sessions": len(rows),
            "minutes": round(total / 40)}   # roughly: typing the same words at 40 wpm


def load() -> dict:
    rows = sorted(_read(), key=lambda r: -r["timestamp"])
    return {"history": rows, "stats": stats(rows)}
