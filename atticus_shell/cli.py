"""Native callback helper — the Linux replacement for the `Atticus --<cmd>` calls
that atticus.py (the brain) makes into its host app.

The brain invokes `$ATTICUS_APP <cmd> ...` as a single executable (argv[0]), so a
one-file launcher (bin/atticus-native) execs `python3 -m atticus_shell.cli`.

v1 implements the commands the core loop needs:
  --screens                      JSON [[name,x,y,w,h,builtin], ...] (main first)
  --look <n> <out.jpg> [winid]   screenshot display n (1-based), ~1280px wide JPEG
  --text <app> [title]           window text (unavailable on Wayland -> empty)
Everything else exits non-zero; the brain already degrades gracefully.
"""
from __future__ import annotations

import json
import subprocess
import sys


def monitors() -> list[dict]:
    """Hyprland monitors in a stable order: focused first, then by id.

    Returns dicts with logical name/x/y/w/h and a built_in guess.
    """
    try:
        raw = subprocess.run(["hyprctl", "-j", "monitors"],
                             capture_output=True, text=True, timeout=5).stdout
        mons = json.loads(raw or "[]")
    except Exception:
        return []
    def logical(m):
        scale = m.get("scale") or 1.0
        transform = m.get("transform", 0)
        w, h = m.get("width", 0), m.get("height", 0)
        if transform in (1, 3, 5, 7):   # 90/270 rotations swap dimensions
            w, h = h, w
        return {
            "name": m.get("name", ""),
            "x": int(m.get("x", 0)),
            "y": int(m.get("y", 0)),
            "w": int(round(w / scale)) if scale else int(w),
            "h": int(round(h / scale)) if scale else int(h),
            "built_in": str(m.get("name", "")).lower().startswith("edp"),
            "focused": bool(m.get("focused")),
            "id": m.get("id", 0),
        }
    mons = [logical(m) for m in mons if isinstance(m, dict)]
    mons.sort(key=lambda m: (not m["focused"], m["id"]))
    return mons


def screens_json() -> int:
    rows = [[m["name"], m["x"], m["y"], m["w"], m["h"], m["built_in"]] for m in monitors()]
    print(json.dumps(rows))
    return 0


def look(idx: int, out: str, _winid: str | None = None) -> int:
    mons = monitors()
    if not (1 <= idx <= len(mons)):
        return 1
    name = mons[idx - 1]["name"]
    # grim one output to PNG on stdout, then scale to <=1280px wide JPEG via ffmpeg.
    try:
        grim = subprocess.run(["grim", "-o", name, "-t", "png", "-"],
                              capture_output=True, timeout=15)
        if grim.returncode != 0 or not grim.stdout:
            return 1
        ff = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
             "-vf", "scale='min(1280,iw)':-1", "-q:v", "5", out],
            input=grim.stdout, capture_output=True, timeout=20)
        return 0 if ff.returncode == 0 else 1
    except Exception:
        return 1


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    cmd = argv[0]
    if cmd == "--screens":
        return screens_json()
    if cmd == "--look" and len(argv) >= 3:
        try:
            idx = int(argv[1])
        except ValueError:
            return 2
        return look(idx, argv[2], argv[3] if len(argv) > 3 else None)
    if cmd == "--text":
        # No accessibility window-text read on Wayland yet: empty -> brain uses OCR.
        return 0
    if cmd == "control" and len(argv) >= 2:
        from . import control
        rest = " ".join(argv[1:])
        return 0 if control.send(rest) else 3
    # Unimplemented native command in v1: fail so the brain degrades.
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
