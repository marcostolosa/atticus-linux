"""Acting on a window's own controls, by name, through the accessibility bus (AT-SPI).

    see      the controls of a window, numbered ("[3] button "Send"")
    click N  the program runs that control's own action
    fill N   the text goes into that one field
    press    keys go to one window, through Hyprland, not to whatever has focus

Nothing here is synthesized at the keyboard or mouse (uinput), so a click
cannot land on another window and the user needs no access to /dev/input.
What the user is asked to allow is built from what this module read from the
control itself (its role and name, the program, the window), never from the
model's description of it: text on a page can be written by an attacker.

A number is only good for the look that produced it. The control is found
again by its place in the tree and must still have the same role and name,
otherwise nothing is done.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
except (ImportError, ValueError):   # no accessibility libraries: every function says so instead of failing
    Atspi = None

LIMIT = 160            # controls listed per look
SEARCH_NODES = 4000    # a huge page is cut off here
SEARCH_SECONDS = 2.5
# what can be clicked or filled; the value is the word the user is shown
CLICKABLE = {"button": "button", "push button": "button", "toggle button": "button", "check box": "checkbox", "radio button": "option",
             "menu item": "menu item", "check menu item": "menu item", "radio menu item": "menu item", "page tab": "tab",
             "link": "link", "combo box": "list", "list item": "item", "tree item": "item", "table cell": "cell",
             "tool bar button": "button", "icon": "icon"}
FIELDS = {"entry": "field", "text": "field", "spin button": "field", "terminal": "terminal"}
SECRET = {"password text"}   # never read, never filled
TERMINALS = ("kitty", "foot", "footclient", "alacritty", "ghostty", "wezterm", "konsole", "gnome-terminal",
             "xterm", "st", "tilix", "terminator", "com.mitchellh.ghostty", "org.wezfurlong.wezterm")


def state_file() -> Path:
    base = os.environ.get("ATTICUS_HANDS_STATE") or str(Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "hands.json")
    return Path(base + ".atspi")


def available() -> bool:
    return Atspi is not None


def enable() -> bool:
    """Programs only publish their controls once the bus says accessibility is wanted."""
    try:
        r = subprocess.run(["busctl", "--user", "set-property", "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled", "b", "true"],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def clean(text: str, n: int = 80) -> str:
    """A name as shown and read aloud: one line, no control characters, cut short."""
    return re.sub(r"[\x00-\x1f\x7f​-‏‪-‮]+", " ", str(text or "")).strip()[:n]


def windows() -> list[dict]:
    try:
        raw = subprocess.run(["hyprctl", "-j", "clients"], capture_output=True, text=True, timeout=3).stdout
        return [c for c in json.loads(raw or "[]") if c.get("mapped", True)]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def front() -> dict:
    try:
        return json.loads(subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True, text=True, timeout=3).stdout or "{}") or {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def find_window(app: str = "", title: str = "") -> dict:
    """The window asked for by program and title words, else the one in front ({} when none)."""
    if not app:
        return front()
    want, words = app.casefold(), [w for w in title.casefold().split() if w]
    rows = [w for w in windows() if want in str(w.get("class", "")).casefold() or want in str(w.get("initialClass", "")).casefold()]
    if not rows:   # said by what the window shows ("the form"), not by its program's class name
        rows = [w for w in windows() if want in str(w.get("title", "")).casefold()]
    rows = [w for w in rows if all(x in str(w.get("title", "")).casefold() for x in words)] or ([] if words else rows)
    return rows[0] if rows else {}


def _app_of(pid: int):
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        try:
            node = desktop.get_child_at_index(i)
            if node is not None and node.get_process_id() == pid:
                return i, node
        except Exception:
            continue
    return None, None


def _role(node) -> str:
    """The role as its fixed English name ('push button'); get_role_name() is translated."""
    return node.get_role().value_nick.replace("-", " ")


def _states(node) -> set:
    try:
        s = node.get_state_set()
        return {name for name, t in (("showing", Atspi.StateType.SHOWING), ("enabled", Atspi.StateType.ENABLED), ("enabled", Atspi.StateType.SENSITIVE), ("editable", Atspi.StateType.EDITABLE),
                                     ("checked", Atspi.StateType.CHECKED), ("focused", Atspi.StateType.FOCUSED)) if s.contains(t)}
    except Exception:
        return set()


def _window_node(app, title: str):
    """The top-level window of the program whose name is the Hyprland title (a program can have several)."""
    best = None
    for i in range(app.get_child_count()):
        w = app.get_child_at_index(i)
        if w is None:
            continue
        best = best or (i, w)
        if title and clean(w.get_name(), 200) == clean(title, 200):
            return i, w
    return best if best else (None, None)


def snapshot(win: dict) -> tuple[list[dict], str]:
    """(controls, note). Each control: n, role, kind, name, state, path (child indexes from the program down)."""
    if Atspi is None:
        return [], "The accessibility libraries (at-spi2, python-gobject) are not available."
    pid = int(win.get("pid") or 0)
    if not pid:
        return [], "There is no window to look at."
    ai, app = _app_of(pid)
    if app is None:
        enable()
        return [], (f"{win.get('class', 'That program')} does not publish its controls yet. Accessibility was just switched on: "
                    "programs started from now on do, and a program already open may need to be restarted. Use a screenshot meanwhile.")
    wi, node = _window_node(app, str(win.get("title", "")))
    if node is None:
        return [], "That program has no window on the accessibility bus."
    out, seen, deadline = [], 0, time.monotonic() + SEARCH_SECONDS
    stack = [(node, [wi])]
    while stack and len(out) < LIMIT and seen < SEARCH_NODES and time.monotonic() < deadline:
        cur, path = stack.pop()
        seen += 1
        try:
            role, name, st = _role(cur), cur.get_name() or "", _states(cur)
        except Exception:
            continue
        if "showing" not in st:
            continue
        kind = CLICKABLE.get(role) or FIELDS.get(role) or ("secret" if role in SECRET else "")
        if kind and ("enabled" in st or role in FIELDS) and (name or kind in ("field", "terminal", "secret")):
            out.append({"role": role, "kind": kind, "name": clean(name), "state": sorted(st & {"checked", "focused"}), "path": path})
        try:
            kids = [(cur.get_child_at_index(i), path + [i]) for i in range(cur.get_child_count())]
        except Exception:
            kids = []
        stack.extend(reversed([k for k in kids if k[0] is not None]))
    for n, row in enumerate(out, 1):
        row["n"] = n
    note = "" if out else "No controls with names were found in that window. Use a screenshot."
    if seen >= SEARCH_NODES or len(out) >= LIMIT:
        note = "Only the first controls are listed; the window has more."
    return out, note


def save(win: dict, rows: list[dict]) -> None:
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"window": {k: win.get(k) for k in ("address", "pid", "class", "title")}, "pid": int(win.get("pid") or 0), "rows": rows, "at": time.time()}))
    os.chmod(f, 0o600)


def load() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def lines(rows: list[dict]) -> str:
    return "\n".join(f'[{r["n"]}] {r["kind"]} "{r["name"]}"' + (f' ({", ".join(r["state"])})' if r["state"] else "") for r in rows)


def _resolve(n: int):
    """(row, live node, window) for number n of the last look, or (None, None, why)."""
    if Atspi is None:
        return None, None, "The accessibility libraries are not available."
    saved = load()
    row = next((r for r in saved.get("rows", []) if r.get("n") == n), None)
    if not row:
        return None, None, f"There is no control [{n}] in the last look. Look again with --hands see."
    ai, app = _app_of(int(saved.get("pid") or 0))
    if app is None:
        return None, None, "That program is no longer on the accessibility bus. Look again."
    node = app
    try:
        for i in row["path"]:
            node = node.get_child_at_index(i)
            if node is None:
                raise IndexError
        same = _role(node) == row["role"] and clean(node.get_name()) == row["name"] and "showing" in _states(node)
    except Exception:
        same = False
    if not same:
        return None, None, "The window changed since it was read. Nothing was done; look again with --hands see."
    return row, node, saved.get("window", {})


def describe(n: int) -> dict:
    """What the user is asked about: control and program, from the live control. {'error': why} when it cannot be found."""
    row, node, win = _resolve(n)
    if row is None:
        return {"error": win}
    return {"kind": row["kind"], "name": row["name"], "app": clean(win.get("class", ""), 40), "title": clean(win.get("title", ""), 60)}


def click(n: int) -> tuple[bool, str]:
    row, node, win = _resolve(n)
    if row is None:
        return False, win
    if row["kind"] in ("field", "terminal", "secret"):
        try:
            node.grab_focus()
            return True, ""
        except Exception as e:
            return False, f"Could not focus it: {e}"
    try:
        action = node.get_action_iface()
        names = [action.get_action_name(i) for i in range(action.get_n_actions())] if action else []
    except Exception:
        action, names = None, []
    if not action or not names:
        return False, "That control has no action to run. Use a screenshot."
    pick = next((i for i, a in enumerate(names) if a in ("click", "press", "activate", "toggle", "select", "jump", "open")), 0)
    return (True, "") if action.do_action(pick) else (False, "The program refused the action.")


def fill(n: int, text: str) -> tuple[bool, str]:
    row, node, win = _resolve(n)
    if row is None:
        return False, win
    if row["kind"] == "secret":
        return False, "That is a password field. Atticus does not fill passwords; tell the user to type it."
    if row["kind"] not in ("field",):
        return False, f'[{n}] is a {row["kind"]}, not a field.'
    try:
        editable = node.get_editable_text_iface()
        if editable and editable.set_text_contents(text):
            return True, ""
    except Exception:
        pass
    return False, "The field would not take the text. Click it, then use --hands type."


KEYS = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "space", "backspace": "BackSpace",
        "delete": "Delete", "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
        "pageup": "Prior", "pagedown": "Next", "page_up": "Prior", "page_down": "Next"}
MODS = {"ctrl": "CTRL", "control": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "SUPER", "meta": "SUPER", "win": "SUPER"}


def parse_combo(combo: str) -> tuple[str, str] | None:
    """'ctrl+shift+t' -> ('CTRL SHIFT', 't'); None when it is not a chord of modifiers and one key."""
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods, key = [MODS[p.lower()] for p in parts[:-1] if p.lower() in MODS], parts[-1]
    if len(mods) != len(parts) - 1 or key.lower() in MODS:
        return None
    key = KEYS.get(key.lower(), key if len(key) > 1 and key[0] == "F" and key[1:].isdigit() else key.lower() if len(key) == 1 else "")
    return (" ".join(mods), key) if key else None


def press(combo: str, address: str, times: int = 1) -> tuple[bool, str]:
    """Send keys to one window by its address. Another window, even the one in front, does not get them."""
    parsed = parse_combo(combo)
    if not parsed or not re.fullmatch(r"0x[0-9a-f]+", address or ""):
        return False, "Use a key combination like ctrl+t or Return, and a window."
    mods, key = parsed
    for _ in range(max(1, min(times, 50))):
        for state in ("down", "up"):
            expr = f'hl.dispatch(hl.dsp.send_key_state({{ mods = "{mods.replace(" ", "_")}", key = "{key}", state = "{state}", window = "address:{address}" }}))'
            r = subprocess.run(["hyprctl", "eval", expr], capture_output=True, text=True, timeout=4)
            if r.returncode or "not found" in (r.stdout + r.stderr) or r.stdout.strip().startswith("error"):
                return False, "That window is gone." if "not found" in (r.stdout + r.stderr) else (r.stdout + r.stderr).strip()[:160]
        time.sleep(0.03)
    return True, ""
