"""Platform detection and backend selection.

The Linux desktop is not one thing, so every OS-specific capability goes through a backend chosen
here. v1 targets Omarchy (Hyprland on Wayland); the seams below are where
GNOME / X11 support slots in later.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    session: str          # "wayland" | "x11" | "unknown"
    compositor: str       # "hyprland" | "gnome" | "sway" | "other"
    desktop: str          # raw XDG_CURRENT_DESKTOP

    @property
    def is_wayland(self) -> bool:
        return self.session == "wayland"

    @property
    def is_hyprland(self) -> bool:
        return self.compositor == "hyprland"


def detect() -> Platform:
    session = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if session not in ("wayland", "x11"):
        session = "wayland" if os.environ.get("WAYLAND_DISPLAY") else (
            "x11" if os.environ.get("DISPLAY") else "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    d = desktop.lower()
    if "hyprland" in d or os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        compositor = "hyprland"
    elif "gnome" in d:
        compositor = "gnome"
    elif "sway" in d:
        compositor = "sway"
    else:
        compositor = "other"
    return Platform(session=session, compositor=compositor, desktop=desktop)


CURRENT = detect()


class UnsupportedPlatform(RuntimeError):
    pass


def require_v1_platform() -> Platform:
    """v1 supports Omarchy only. Fail loud and early with a useful message."""
    p = CURRENT
    problems = []
    if not p.is_wayland:
        problems.append(f"needs a Wayland session (found session={p.session or 'unknown'})")
    if not p.is_hyprland:
        problems.append(f"v1 targets Hyprland/Omarchy (found compositor={p.compositor})")
    for tool in ("hyprctl", "grim"):
        if not shutil.which(tool):
            problems.append(f"missing required tool: {tool}")
    if problems:
        raise UnsupportedPlatform(
            "Atticus (Linux v1) only runs on Omarchy / Hyprland right now:\n  - "
            + "\n  - ".join(problems))
    return p


def hyprctl(*args: str, json: bool = False):
    """Thin wrapper over hyprctl. Returns parsed JSON or stripped text."""
    cmd = ["hyprctl"]
    if json:
        cmd.append("-j")
    cmd += list(args)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    if json:
        import json as _json
        return _json.loads(out.stdout or "null")
    return out.stdout.strip()
