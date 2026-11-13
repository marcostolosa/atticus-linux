"""Native callback helper — the Linux replacement for the `Atticus --<cmd>` calls
that atticus.py (the brain) makes into its host app.

The brain invokes `$ATTICUS_APP <cmd> ...` as a single executable (argv[0]), so a
one-file launcher (bin/atticus-native) execs `python3 -m atticus_shell.cli`.

v1 implements the commands the core loop needs:
  --screens                      JSON [[name,x,y,w,h,builtin], ...] (main first)
  --look <n> <out.jpg> [winid]   screenshot display n (1-based), ~1280px wide JPEG
  --text <app> [title]           OCR of that window's region (grim + tesseract)
Everything else exits non-zero; the brain already degrades gracefully.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


def monitors() -> list[dict]:
    """Hyprland monitors in a stable order: the built-in panel first (if any),
    then by id — never by current focus, which can change between one call
    and the next (see look(), which is called once per monitor using indices
    resolved by an earlier, separate call to this same list).
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
    mons.sort(key=lambda m: (not m["built_in"], m["id"]))
    return mons


def screens_json() -> int:
    rows = [[m["name"], m["x"], m["y"], m["w"], m["h"], m["built_in"]] for m in monitors()]
    print(json.dumps(rows))
    return 0


# --- hiding Atticus's own overlay around a capture --------------------------
def _hide_for_capture() -> None:
    # Best-effort: if the shell isn't reachable, there's nothing to hide.
    # Keep this fast (no retry loop) so a missing shell never stalls a
    # screenshot the brain is waiting on.
    from . import control
    control.send("hide-for-capture", retries=1, delay=0)


def _show_after_capture() -> None:
    from . import control
    control.send("show-after-capture", retries=1, delay=0)


def look(idx: int, out: str, _winid: str | None = None) -> int:
    mons = monitors()
    if not (1 <= idx <= len(mons)):
        return 1
    name = mons[idx - 1]["name"]
    # Atticus's own box sits in a layer-shell OVERLAY surface above every
    # window, so a plain screenshot would capture it too. Hide it first (it
    # is restored right after, success or failure) so the model's picture
    # shows only what the user actually has on screen.
    _hide_for_capture()
    try:
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
    finally:
        _show_after_capture()


# --- window lookup (for --text) ---------------------------------------------
def _clients() -> list[dict]:
    try:
        raw = subprocess.run(["hyprctl", "-j", "clients"],
                             capture_output=True, text=True, timeout=5).stdout
        rows = json.loads(raw or "[]")
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        return []


def _active_window() -> dict | None:
    try:
        raw = subprocess.run(["hyprctl", "-j", "activewindow"],
                             capture_output=True, text=True, timeout=5).stdout
        w = json.loads(raw or "{}")
        return w if isinstance(w, dict) and w.get("class") else None
    except Exception:
        return None


def find_window(app: str, title_words: str = "") -> dict | None:
    """Best matching mapped, visible window for `app` (a window class, as
    front() reports it on Linux) and optional title words. No app: the
    active window. Several windows of the same app: prefer one whose title
    matches the given words, then the most recently focused."""
    app = (app or "").strip()
    if not app:
        return _active_window()
    candidates = [c for c in _clients()
                 if c.get("mapped") and not c.get("hidden") and c.get("class")]
    al = app.lower()
    exact = [c for c in candidates if c["class"].lower() == al]
    pool = exact or [c for c in candidates
                     if al in c["class"].lower() or c["class"].lower() in al]
    if not pool:
        return None
    words = [w for w in title_words.lower().split() if w]
    if words:
        def score(c):
            t = str(c.get("title", "")).lower()
            return sum(1 for w in words if w in t)
        best = max(score(c) for c in pool)
        if best > 0:
            pool = [c for c in pool if score(c) == best]
    pool.sort(key=lambda c: c.get("focusHistoryID", 1 << 30))
    return pool[0]


def _ocr_langs() -> str | None:
    """Pick installed language packs for tesseract: prefer Portuguese and
    English together (most of this port's users), fall back to whichever of
    those two is actually installed, else let tesseract use its own default
    rather than fail outright on a system with only one pack."""
    try:
        out = subprocess.run(["tesseract", "--list-langs"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    installed = set(out.splitlines()[1:]) if out else set()
    wanted = [l for l in ("por", "eng") if l in installed]
    return "+".join(wanted) if wanted else None


def text_ocr(app: str, title: str = "") -> int:
    """OCR of one window's visible region: find it, screenshot just that
    rectangle (Atticus's own box hidden for the moment), run tesseract. Empty
    output (rc 0) on anything we can't do — the brain already treats that as
    "no text found" and tries another way."""
    win = find_window(app, title)
    if not win:
        return 0
    at, size = win.get("at"), win.get("size")
    if not (isinstance(at, list) and isinstance(size, list) and len(at) == 2 and len(size) == 2):
        return 0
    x, y = at
    w, h = size
    if w <= 0 or h <= 0:
        return 0
    fd, path = tempfile.mkstemp(prefix="atticus-ocr-", suffix=".png")
    os.close(fd)
    _hide_for_capture()
    try:
        grim = subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", "-t", "png", path],
                              capture_output=True, timeout=15)
        if grim.returncode != 0:
            return 0
        # tesseract's CLI is strictly `imagename outputbase [options]`: every
        # option, including -l, must come after both positional arguments
        # (path, then "-" for stdout) or tesseract misparses them.
        cmd = ["tesseract", path, "-"]
        langs = _ocr_langs()
        if langs:
            cmd += ["-l", langs]
        cmd += ["--psm", "6"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        text = r.stdout.strip()
        if text:
            print(text)
        return 0
    except Exception:
        return 0
    finally:
        _show_after_capture()
        try:
            os.remove(path)
        except OSError:
            pass


def hands_main(argv: list[str]) -> int:
    """--hands <verb> ...: native click/type/key/scroll/placement, backed by
    ydotool and Hyprland's dispatchers (see atticus_shell/hands.py). Prints
    nothing on success; an error line on failure, which the brain's own
    hands() reads and relays instead of claiming the action worked."""
    from . import hands
    if not argv:
        return 1
    verb, rest = argv[0], argv[1:]
    if verb in ("click", "type", "press", "scroll", "drag") and not hands.available():
        print("ydotool is not set up (install ydotool, enable the ydotool "
              "user service, and make sure this user is in the input group)")
        return 1
    if verb == "click":
        if len(rest) < 2:
            return 1
        try:
            x, y = int(float(rest[0])), int(float(rest[1]))
        except ValueError:
            return 1
        opts = {o.lower() for o in rest[2:]}
        ok, msg = hands.click(x, y, button="right" if "right" in opts else "left",
                              double="double" in opts)
    elif verb == "type":
        ok, msg = hands.type_text(" ".join(rest))
    elif verb == "press":
        if not rest:
            return 1
        times = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 1
        ok, msg = hands.press(rest[0], times)
    elif verb == "scroll":
        if not rest:
            return 1
        amount = int(rest[1]) if len(rest) > 1 and rest[1].lstrip("-").isdigit() else 3
        ok, msg = hands.scroll(rest[0], amount)
    elif verb == "drag":
        if len(rest) < 4:
            return 1
        try:
            x1, y1, x2, y2 = (int(float(v)) for v in rest[:4])
        except ValueError:
            return 1
        ok, msg = hands.drag(x1, y1, x2, y2)
    elif verb == "place":
        if len(rest) < 3:
            return 1
        app, where, mon_name = rest[0], rest[1], rest[2]
        title = " ".join(rest[3:])
        win = find_window(app, title)
        if not win:
            print(f'no window found for "{app}" {title}'.strip())
            return 1
        mons = {m["name"]: m for m in monitors()}
        mon = mons.get(mon_name) or next(iter(mons.values()), None)
        if not mon:
            print("no display information available")
            return 1
        ok, msg = hands.place(win["address"], where, mon)
    else:
        return 1
    if not ok and msg:
        print(msg)
    return 0 if ok else 1


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
        app = argv[1] if len(argv) > 1 else ""
        title = argv[2] if len(argv) > 2 else ""
        return text_ocr(app, title)
    if cmd == "--hands":
        return hands_main(argv[1:])
    if cmd == "--type":
        from . import hands
        text = sys.stdin.read()
        if not hands.available():
            print("ydotool is not set up (install ydotool, enable the ydotool "
                  "user service, and make sure this user is in the input group)")
            return 1
        ok, msg = hands.type_text(text)
        if not ok:
            print(msg or "Could not type.")
        return 0 if ok else 1
    if cmd == "control" and len(argv) >= 2:
        from . import control
        rest = " ".join(argv[1:])
        return 0 if control.send(rest) else 3
    # Unimplemented native command in v1: fail so the brain degrades.
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
