"""Raw input by screen position, and window placement. Opt-in.

Clicking and typing by control name is atticus_shell/atspi.py. This module is
the position-driven fallback: click, drag and scroll at a pixel, through
ydotool (uinput), reachable only when the setting `rawInput` is true. It is
off by default because uinput types into any window and needs the user in the
`input` group, and a picture of the screen is easier to mislead than a
control's own name.

Window placement (`place`) and text insertion (`insert_text`, wtype) do not
use uinput and are always available.

ydotool, when used, needs:
  - the `ydotool`/`ydotoold` package installed
  - ydotoold running as the same user Atticus runs as (a systemd --user
    service), which needs that user in the `input` group
Every function here degrades to a clear (ok=False) failure, never an
exception, so the brain can say what didn't work instead of hanging.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

TIMEOUT = 10

# --- ydotool plumbing --------------------------------------------------------
def _ydotool(*args: str) -> tuple[bool, str]:
    if not shutil.which("ydotool"):
        return False, "ydotool is not installed"
    try:
        r = subprocess.run(["ydotool", *args], capture_output=True, text=True,
                           timeout=TIMEOUT)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or f"ydotool exited {r.returncode}").strip()
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "ydotool timed out"
    except OSError as e:
        return False, str(e)


def available() -> bool:
    """True once click/press/scroll/drag are actually likely to work: the
    ydotool binary is there and its daemon's socket is reachable (ydotool
    itself would print a clear connection error otherwise, but checking
    first avoids a confusing partial failure mid-sequence, e.g. after a
    key-down with no matching up). `type` is checked separately by
    `type_available()`, since it goes through wtype instead whenever that is
    installed, which needs no daemon at all."""
    if not shutil.which("ydotool"):
        return False
    sock = os.environ.get("YDOTOOL_SOCKET") or os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), ".ydotool_socket")
    return os.path.exists(sock) or os.path.exists("/tmp/.ydotool_socket")


def type_available() -> bool:
    return bool(shutil.which("wtype")) or available()


# --- mouse --------------------------------------------------------------
_BUTTON = {"left": 0x00, "right": 0x01, "middle": 0x02}
DOWN, UP = 0x40, 0x80


def _cursor_pos() -> tuple[int, int] | None:
    try:
        r = subprocess.run(["hyprctl", "cursorpos"], capture_output=True, text=True, timeout=2)
        if r.returncode:
            return None
        x, y = r.stdout.strip().split(",")
        return int(x.strip()), int(y.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


_AXIS_SCALE: tuple[float, float] | None = None   # measured once per process; see _axis_scale()


def _axis_scale() -> tuple[float, float]:
    """ydotool's absolute positioning does not always land 1:1 on Hyprland's
    own pixel coordinates (confirmed live on a scale-1.5 laptop panel: asking
    for x lands the pointer at 2x, clamped at the screen edge — a mismatch
    between the virtual input device's absolute axis range and Hyprland's,
    unrelated to the display's HiDPI scale number itself). The true ratio
    isn't a fixed constant, so instead of hardcoding one it is measured: move
    to a small, always-on-screen probe point and see where Hyprland says the
    pointer actually went. Picture coordinates ({LOOK}) and hyprctl's own
    coordinates (window positions) are both in the space this corrects to."""
    global _AXIS_SCALE
    if _AXIS_SCALE is not None:
        return _AXIS_SCALE
    probe = 32
    if not _ydotool("mousemove", "--absolute", "-x", str(probe), "-y", str(probe))[0]:
        _AXIS_SCALE = (1.0, 1.0)
        return _AXIS_SCALE
    time.sleep(0.05)
    pos = _cursor_pos()
    _AXIS_SCALE = (pos[0] / probe, pos[1] / probe) if pos and pos[0] > 0 and pos[1] > 0 else (1.0, 1.0)
    return _AXIS_SCALE


def move(x: int, y: int) -> tuple[bool, str]:
    sx, sy = _axis_scale()
    return _ydotool("mousemove", "--absolute", "-x", str(round(x / sx)), "-y", str(round(y / sy)))


def click(x: int, y: int, button: str = "left", double: bool = False) -> tuple[bool, str]:
    b = _BUTTON.get(button, _BUTTON["left"])
    ok, err = move(x, y)
    if not ok:
        return False, err
    clicks = 2 if double else 1
    for i in range(clicks):
        ok, err = _ydotool("click", f"0x{b | DOWN | UP:02x}")
        if not ok:
            return False, err
        if i < clicks - 1:
            time.sleep(0.08)
    return True, ""


def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 8) -> tuple[bool, str]:
    """Press, move in several steps while held (a single jump can read as a
    click-then-teleport rather than a drag to apps watching motion, e.g. to
    select text or move a slider), then release."""
    ok, err = move(x1, y1)
    if not ok:
        return False, err
    ok, err = _ydotool("click", f"0x{_BUTTON['left'] | DOWN:02x}")
    if not ok:
        return False, err
    for i in range(1, steps + 1):
        mx = x1 + (x2 - x1) * i // steps
        my = y1 + (y2 - y1) * i // steps
        move(mx, my)
        time.sleep(0.02)
    ok, err = _ydotool("click", f"0x{_BUTTON['left'] | UP:02x}")
    return (True, "") if ok else (False, err)


_WHEEL_UNITS_PER_NOTCH = 15   # empirically tuned live: 50 raw units visibly scrolled ~40 terminal lines


def scroll(direction: str, amount: int = 3, x: int | None = None, y: int | None = None) -> tuple[bool, str]:
    """A wheel event only affects whatever is under the pointer, so without
    x/y this scrolls wherever the mouse was last left — often nowhere useful.
    Pass the coordinate to scroll over (read off the same {LOOK} picture).
    `amount` is notches (like real clicks of a mouse wheel), not raw units —
    confirmed live that a handful of raw units is too small to register as a
    scroll at all in a real app, so this scales up to a size that does."""
    if x is not None and y is not None:
        ok, err = move(x, y)
        if not ok:
            return False, err
    # REL_WHEEL convention: positive y is up, negative is down — confirmed
    # live (the opposite mapping visibly scrolled the wrong way).
    dx, dy = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}.get(
        direction, (0, -1))
    units = amount * _WHEEL_UNITS_PER_NOTCH
    return _ydotool("mousemove", "--wheel", "-x", str(dx * units), "-y", str(dy * units))


# --- keyboard -----------------------------------------------------------
# Linux evdev keycodes (linux/input-event-codes.h), not keysyms: ydotool's
# `key` only takes raw codes, so there is no layout lookup — this table
# covers a US/ABNT-compatible QWERTY layout, which is what everything else
# in this port already assumes (the hotkey config, wtype usage).
_KEYS = {
    **{c: k for c, k in zip("qwertyuiop", (16, 17, 18, 19, 20, 21, 22, 23, 24, 25))},
    **{c: k for c, k in zip("asdfghjkl", (30, 31, 32, 33, 34, 35, 36, 37, 38))},
    **{c: k for c, k in zip("zxcvbnm", (44, 45, 46, 47, 48, 49, 50))},
    **{d: k for d, k in zip("1234567890", (2, 3, 4, 5, 6, 7, 8, 9, 10, 11))},
    "enter": 28, "return": 28, "esc": 1, "escape": 1, "tab": 15, "space": 57,
    "backspace": 14, "delete": 111, "up": 103, "down": 108, "left": 105,
    "right": 106, "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "minus": 12, "equal": 13, "-": 12, "=": 13,
    **{f"f{n}": c for n, c in zip(range(1, 11), range(59, 69))},
    "f11": 87, "f12": 88,
}
_MODS = {
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "opt": 56, "option": 56,
    "super": 125, "cmd": 125, "meta": 125, "win": 125,
}


def _combo_codes(combo: str) -> tuple[list[int], int | None, str | None]:
    parts = [p.strip().lower() for p in re.split(r"[+\-\s]+", combo) if p.strip()]
    mods, key = [], None
    for p in parts:
        if p in _MODS:
            mods.append(_MODS[p])
        elif key is None and p in _KEYS:
            key = _KEYS[p]
        else:
            return [], None, p
    return mods, key, None


def press(combo: str, times: int = 1) -> tuple[bool, str]:
    mods, key, bad = _combo_codes(combo)
    if bad or key is None:
        return False, f'unrecognized key "{bad or combo}"'
    down = [f"{m}:1" for m in mods] + [f"{key}:1"]
    up = [f"{key}:0"] + [f"{m}:0" for m in reversed(mods)]
    for _ in range(max(1, times)):
        ok, err = _ydotool("key", *down, *up)
        if not ok:
            return False, err
        time.sleep(0.03)
    return True, ""


def type_text(text: str) -> tuple[bool, str]:
    """Types real Unicode text, respecting the session's actual keyboard
    layout. ydotool's own `type` sends raw US-QWERTY key positions, so on
    any other layout (confirmed live on this br-abnt2 one) punctuation comes
    out wrong — every ';' typed as 'ç', for one. wtype goes through the
    compositor's virtual-keyboard protocol with the real keymap instead, so
    it is used here even though everything else in this module is ydotool."""
    if not text:
        return True, ""
    if shutil.which("wtype"):
        try:
            r = subprocess.run(["wtype", "--", text], capture_output=True,
                               text=True, timeout=TIMEOUT)
            if r.returncode == 0:
                return True, ""
            return False, (r.stderr or r.stdout or "wtype failed").strip()
        except subprocess.TimeoutExpired:
            return False, "wtype timed out"
        except OSError as e:
            return False, str(e)
    return _ydotool("type", "--", text)   # best effort if wtype isn't installed


TERMINALS = ("kitty", "foot", "footclient", "alacritty", "ghostty", "wezterm", "konsole", "gnome-terminal",
             "xterm", "st", "tilix", "terminator", "com.mitchellh.ghostty", "org.wezfurlong.wezterm")


def _front_class() -> str:
    try:
        raw = subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True, text=True, timeout=3).stdout
        return str((json.loads(raw or "{}") or {}).get("class", "")).lower()
    except Exception:
        return ""


def insert_text(text: str) -> tuple[bool, str]:
    """Put text where the cursor is. One line is typed. Several lines are
    pasted: typing a line break is pressing Enter, and in a chat window that
    sends what was written so far. The clipboard is put back afterwards."""
    if "\n" not in text or not (shutil.which("wl-copy") and shutil.which("wtype")):
        return type_text(text.replace("\n", " ") if "\n" in text else text)
    try:
        before = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=2)
        kept = before.stdout if before.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        kept = None
    try:
        subprocess.run(["wl-copy", "--"], input=text.encode(), timeout=3, check=True)
        time.sleep(0.08)
        keys = ["-M", "ctrl", "-M", "shift", "v"] if _front_class() in TERMINALS else ["-M", "ctrl", "v"]   # terminals keep ctrl+v for themselves
        r = subprocess.run(["wtype", *keys], capture_output=True, text=True, timeout=TIMEOUT)
        ok, err = r.returncode == 0, (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        ok, err = False, str(e)
    time.sleep(0.25)                       # let the window read it before it is replaced
    try:
        if kept is not None:
            subprocess.run(["wl-copy", "--"], input=kept, timeout=3)
        else:
            subprocess.run(["wl-copy", "--clear"], timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass
    return ok, err


# --- window placement -----------------------------------------------------
def _hl(expr: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["hyprctl", "eval", expr], capture_output=True,
                           text=True, timeout=5)
        return r.returncode == 0 and not r.stdout.strip().startswith("error"), r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _client(addr: str) -> dict | None:
    try:
        raw = subprocess.run(["hyprctl", "-j", "clients"], capture_output=True,
                             text=True, timeout=5).stdout
        for c in json.loads(raw or "[]"):
            if c.get("address") == addr:
                return c
    except Exception:
        pass
    return None


_RECT = {
    "left": lambda x, y, w, h, cw, ch: (x, y, w // 2, h),
    "right": lambda x, y, w, h, cw, ch: (x + w // 2, y, w - w // 2, h),
    "full": lambda x, y, w, h, cw, ch: (x, y, w, h),
    "center": lambda x, y, w, h, cw, ch: (x + (w - min(cw, w)) // 2,
                                          y + (h - min(ch, h)) // 2,
                                          min(cw, w), min(ch, h)),
}


def place(addr: str, where: str, mon: dict) -> tuple[bool, str]:
    """Float a window and put it at `where` ('left'/'right'/'full'/'center')
    within `mon` {x,y,w,h} (logical px). Verified live: resize before move,
    or a floating window's position drifts a bit after resizing it — the
    move must be the last word on where it ends up. Checks where it actually
    landed afterward rather than trusting the dispatch; retries once with a
    corrective delta if it's off, since floating-point scale rounding can
    leave it a pixel or two short even then."""
    where = "full" if where == "fullscreen" else where   # no separate Spaces concept here
    before = _client(addr)
    if not before:
        return False, "window not found"
    if before.get("fullscreen"):
        ok, _ = _hl(f'hl.dispatch(hl.dsp.window.fullscreen({{ window = "address:{addr}", action = "unset" }}))')
        if not ok:
            return False, "could not exit fullscreen first"
        time.sleep(0.3)
    fn = _RECT.get(where)
    if not fn:
        return False, "where must be left, right, full or center"
    cw, ch = before.get("size", [800, 600])
    x, y, w, h = fn(mon["x"], mon["y"], mon["w"], mon["h"], cw, ch)
    if not before.get("floating"):
        ok, msg = _hl(f'hl.dispatch(hl.dsp.window.float({{ window = "address:{addr}", action = "on" }}))')
        if not ok:
            return False, f"could not float the window: {msg}"
    for attempt in range(2):
        _hl(f'hl.dispatch(hl.dsp.window.resize({{ window = "address:{addr}", x = {w}, y = {h} }}))')
        _hl(f'hl.dispatch(hl.dsp.window.move({{ window = "address:{addr}", x = {x}, y = {y} }}))')
        time.sleep(0.15)
        after = _client(addr)
        if not after:
            return False, "window disappeared while moving it"
        ax, ay = after.get("at", [0, 0])
        aw, ah = after.get("size", [0, 0])
        if abs(ax - x) <= 2 and abs(ay - y) <= 2 and abs(aw - w) <= 2 and abs(ah - h) <= 2:
            return True, ""
        if attempt == 0:
            x, y = x - (ax - x), y - (ay - y)   # correct by the observed drift, once
    return False, f"moved, but it landed at {ax},{ay} {aw}x{ah} instead of {x},{y} {w}x{h}"
