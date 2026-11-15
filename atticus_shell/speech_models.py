"""The speech models kept on this computer: which are here, download, delete.

They are the faster-whisper models, stored by the Hugging Face cache under
~/.cache/huggingface/hub. A download runs in its own process so Cancel can
stop it; how far it got is the folder's size against the model's size (sizes
in bytes, read from the folders and, for Medium, from the hub's file list).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import settings as cfg

MODELS = (("tiny", "Tiny", 78_203_619), ("base", "Base", 147_882_941), ("small", "Small", 486_212_372),
          ("medium", "Medium", 1_530_575_217), ("large-v3-turbo", "Large v3 Turbo", 1_621_665_983))
DEFAULT = "small"

_jobs: dict[str, subprocess.Popen] = {}
_errors: dict[str, str] = {}
_lock = threading.Lock()


def size_text(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{round(n / 1e6)} MB"


def folder(model: str) -> Path | None:
    try:
        from faster_whisper.utils import _MODELS
        repo = _MODELS[model]
    except (ImportError, KeyError):
        return None
    hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub")
    return hub / ("models--" + repo.replace("/", "--"))


def stored(model: str) -> int:
    """Bytes of this model on disk (following the cache's links)."""
    root, total = folder(model), 0
    if not root or not (root / "snapshots").is_dir():
        return 0
    for p in (root / "snapshots").rglob("*"):
        try:
            total += p.stat().st_size if p.is_file() else 0
        except OSError:
            pass
    return total


def have(model: str) -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model(model, local_files_only=True)
        return stored(model) > 0.95 * dict((m, s) for m, _, s in MODELS).get(model, 0)
    except Exception:
        return False


def chosen() -> str:
    want = str(cfg.get("asrModel", DEFAULT) or DEFAULT)
    return want if want in [m for m, _, _ in MODELS] else DEFAULT


def status() -> dict:
    """What the Settings page paints: every model, whether it is here, and a download's progress."""
    rows = []
    for model, name, size in MODELS:
        row = {"id": model, "name": name, "size": size_text(size), "have": have(model)}
        with _lock:
            running = model in _jobs
        if running:
            row["have"], row["progress"] = False, min(0.99, stored(model) / size)
        rows.append(row)
    with _lock:
        error = _errors.get(chosen(), "")
    return {"server": True, "installing": False, "models": rows, "error": error}


def download(model: str, changed=lambda: None) -> None:
    """Start fetching `model`; `changed` is called about once a second while it runs, and once at the end."""
    if model not in [m for m, _, _ in MODELS]:
        return
    with _lock:
        if model in _jobs:
            return
        _errors.pop(model, None)
        _jobs[model] = job = subprocess.Popen(
            [sys.executable, "-c", "import sys; from faster_whisper.utils import download_model; download_model(sys.argv[1])", model],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def watch():
        while job.poll() is None:
            changed()
            time.sleep(1)
        with _lock:
            stopped = _jobs.pop(model, None) is None
            if job.returncode and not stopped:
                last = (job.stderr.read() or "").strip().splitlines()[-1:] if job.stderr else []
                _errors[model] = "Download failed. " + (last[0][:120] if last else "Check the connection and try again.")
        changed()
    threading.Thread(target=watch, daemon=True).start()


def cancel(model: str) -> None:
    with _lock:
        job = _jobs.pop(model, None)
    if job and job.poll() is None:
        job.terminate()


def delete(model: str) -> None:
    cancel(model)
    root = folder(model)
    if root and root.is_dir() and root.name.startswith("models--"):
        shutil.rmtree(root, ignore_errors=True)
