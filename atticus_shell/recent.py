"""The last few things said, kept where the brain reads them.

The brain writes a turn here when it finishes one. Two kinds of turn never
reach that point: a command answered on the spot (no model involved) and a
request cut short because the user called again. Both are written from the
shell, so "e a da cozinha?" or "continua" still has something to refer to.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

KEEP_SECONDS = 600   # the brain reads back ten minutes
KEEP_TURNS = 3


def _path() -> Path:
    return Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "recent.jsonl"


def add(question: str, answer: str, now: float | None = None) -> bool:
    question, answer = (question or "").strip(), (answer or "").strip()
    if not question or not answer:
        return False
    path, now = _path(), time.time() if now is None else now
    rows = []
    try:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("t"), (int, float)) and now - row["t"] < KEEP_SECONDS:
                rows.append(row)
    except OSError:
        pass
    rows = rows[-(KEEP_TURNS - 1):] + [{"t": now, "q": question, "a": answer}]
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:   # created private (0600)
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        os.replace(f.name, path)
        return True
    except OSError:
        return False
