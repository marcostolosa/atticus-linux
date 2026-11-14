"""Echo cancellation, so the microphone can stay open while Atticus talks.

PipeWire's echo-cancel module gives a virtual speaker and a virtual
microphone: whatever is played into the speaker is subtracted from what the
microphone hears. Atticus plays its voice there and listens there while it
speaks, so a person talking over it is heard and its own voice is not.
"""
from __future__ import annotations

import shutil
import subprocess

SOURCE, SINK = "atticus_aec_source", "atticus_aec_sink"


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ready() -> bool:
    return bool(shutil.which("pactl")) and SOURCE in _pactl("list", "short", "sources")


def ensure() -> bool:
    """Load the module once per audio session. False when it is unavailable."""
    if not shutil.which("pactl"):
        return False
    if ready():
        return True
    _pactl("load-module", "module-echo-cancel", "aec_method=webrtc",
           f"source_name={SOURCE}", f"sink_name={SINK}")
    return ready()
