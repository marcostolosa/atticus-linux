"""The keyboard shortcuts, kept where Hyprland keeps them.

A global key belongs to the compositor: Atticus only listens on its control
socket, and a line in ~/.config/hypr/bindings.lua runs `atticus-ctl <command>`
when the keys are pressed. This module reads those lines and rewrites them
when the Settings window records new keys:

    o.bind("SUPER + ALT + A", "Atticus (voice)", "~/.local/bin/atticus-ctl toggle")
    o.bind_hold("SUPER + ALT + A", "Atticus (voice)", "… press", "Atticus (send)", "… release")

A write is checked: Hyprland reloads, and if it reports a config error the
file goes back to what it was.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULTS = {"talk": "SUPER + ALT + A", "dictate": "SUPER + ALT + D", "settings": "SUPER + CTRL + ALT + A"}
NAMES = {"talk": "Atticus (voice)", "dictate": "Atticus dictation", "settings": "Atticus settings",
         "meeting": "Atticus meeting", "move": "Atticus to background"}
# which shortcut a bound `atticus-ctl <command>` is
KINDS = {"toggle": "talk", "press": "talk", "release": "talk", "dictate": "dictate", "dictate-press": "dictate",
         "dictate-release": "dictate", "settings": "settings", "meeting": "meeting", "move": "move"}
MODS = (("SUPER", 64), ("CTRL", 4), ("ALT", 8), ("SHIFT", 1))
HEAD = "-- Atticus voice assistant. The Settings window rewrites these lines."


def path() -> Path:
    return Path(os.environ.get("ATTICUS_HYPR_BINDS", Path.home() / ".config/hypr/bindings.lua"))


def ctl() -> str:
    return shutil.which("atticus-ctl") or str(Path.home() / ".local/bin/atticus-ctl")


def clean(keys: str) -> str:
    """'super+alt + a' -> 'SUPER + ALT + A', modifiers first and in one order; '' when it is no shortcut."""
    parts = [p.strip().upper() for p in re.split(r"\s*\+\s*", keys.strip()) if p.strip()]
    names = {"CONTROL": "CTRL", "META": "SUPER", "WIN": "SUPER", "MOD4": "SUPER"}
    parts = [names.get(p, p) for p in parts]
    mods = [m for m, _ in MODS if m in parts]
    rest = [p for p in parts if p not in dict(MODS)]
    if len(rest) != 1 or not re.fullmatch(r"[A-Z0-9_]+", rest[0]):
        return ""
    if mods in ([], ["SHIFT"]) and not re.fullmatch(r"F\d{1,2}", rest[0]):   # a plain or capital letter would be taken from every app
        return ""
    return " + ".join(mods + rest)


def label(keys: str) -> str:
    """'SUPER + ALT + A' -> 'Super+Alt+A', what the pages show as key caps."""
    words = {"SUPER": "Super", "CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "SPACE": "Space", "RETURN": "Enter",
             "COMMA": ",", "PERIOD": ".", "SLASH": "/", "ESCAPE": "Esc"}
    return "+".join(words.get(p, p if len(p) < 4 else p.capitalize()) for p in keys.split(" + ") if p)


def _rows(text: str) -> list[tuple[int, str, str, bool]]:
    """(line number, kind, keys, held) for every line that binds an Atticus command."""
    out = []
    for n, line in enumerate(text.split("\n")):
        if line.lstrip().startswith("--"):
            continue
        m = re.search(r'\bo\.(bind|rebind|bind_hold)\(\s*"([^"]+)"', line)
        cmd = re.search(r"atticus-ctl\s+([a-z-]+)", line)
        if m and cmd and cmd[1] in KINDS:
            out.append((n, KINDS[cmd[1]], clean(m[2]), m[1] == "bind_hold"))
    return out


def read() -> dict:
    """{'talk': 'SUPER + ALT + A', 'dictate': …, 'hold': bool} from the bindings file."""
    try:
        rows = _rows(path().read_text())
    except OSError:
        rows = []
    found: dict = {kind: keys for _, kind, keys, _ in rows if keys}
    found["hold"] = any(held for _, kind, _, held in rows if kind == "talk")
    return found


def labels() -> dict[str, str]:
    return {k: label(v) for k, v in read().items() if isinstance(v, str)}


def can_hold() -> bool:
    """Press-and-release binding needs Omarchy's `o.bind_hold` helper."""
    try:
        return "function o.bind_hold" in Path("/usr/share/omarchy/default/hypr/helpers.lua").read_text()
    except OSError:
        return False


def _hyprctl(*args: str) -> str:
    try:
        return subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def taken(keys: str) -> str:
    """What these keys already do on this desktop ('' when free, or when they are Atticus's own)."""
    parts = keys.split(" + ")
    mask = sum(bit for name, bit in MODS if name in parts)
    try:
        rows = json.loads(_hyprctl("-j", "binds") or "[]")
    except ValueError:
        rows = []
    for b in rows if isinstance(rows, list) else []:
        if isinstance(b, dict) and b.get("modmask") == mask and str(b.get("key", "")).upper() == parts[-1] and not b.get("submap"):
            what = str(b.get("description") or b.get("arg") or "another action")
            if "tticus" not in what:
                return what
    return ""


def line(kind: str, keys: str, hold: bool = False) -> str:
    c = ctl()
    if kind == "talk" and hold:
        return f'o.bind_hold("{keys}", "{NAMES[kind]}", "{c} press", "Atticus (send)", "{c} release")'
    command = {"talk": "toggle"}.get(kind, kind)
    return f'o.bind("{keys}", "{NAMES[kind]}", "{c} {command}")'


def write(kind: str, keys: str | None, hold: bool | None = None) -> str:
    """Bind `kind` to `keys` (None removes it). Returns '' when done, or why not."""
    if kind not in NAMES:
        return "Unknown shortcut."
    file = path()
    try:
        before = file.read_text()
    except OSError:
        return f"{file} was not found. Add this line to your Hyprland config: {line(kind, keys or DEFAULTS.get(kind, ''))}"
    now = read()
    if keys is None and kind != "talk":
        keys = ""
    else:
        keys = clean(keys or now.get(kind) or DEFAULTS.get(kind, ""))
        if not keys:
            return "Use a key together with Super, Ctrl or Alt."
        other = next((k for k, v in now.items() if k != kind and v == keys), None)
        if other:
            return f"own:{other}"
        busy = taken(keys)
        if busy:
            return f"taken:{busy}"
    hold = (now.get("hold", False) if hold is None else hold) and can_hold()
    lines = before.split("\n")
    mine = [n for n, k, _, _ in _rows(before) if k == kind]
    for n in reversed(mine):
        del lines[n]
    if keys:
        at = mine[0] if mine else len(lines)
        if not mine:
            while lines and not lines[-1].strip():
                lines.pop()
            at = len(lines)
            if HEAD not in lines and not _rows(before):
                lines += ["", HEAD]
                at = len(lines)
        lines.insert(at, line(kind, keys, hold))
    after = "\n".join(lines).rstrip("\n") + "\n"
    if after == before:
        return ""
    file.write_text(after)
    _hyprctl("reload")
    errors = _hyprctl("configerrors").strip()
    if errors and errors != "ok":
        file.write_text(before)
        _hyprctl("reload")
        return "Hyprland refused the new shortcut, so the old one was kept."
    return ""


def install() -> list[str]:
    """Bind the usual keys for whatever has none yet. One line per shortcut, saying what happened."""
    said = []
    for kind, keys in DEFAULTS.items():
        have = read().get(kind)
        why = "" if have else write(kind, keys)
        said.append(f"{NAMES[kind]}: {label(have or keys)}" if not why else
                    f"{NAMES[kind]}: not bound ({why.split(':', 1)[-1]}). Pick keys in Settings.")
    return said


if __name__ == "__main__":
    print("\n".join(install()))
