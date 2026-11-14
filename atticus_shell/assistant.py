"""Everyday-assistant backends for Linux: timers, alarms and reminders, desktop
notifications, media, volume, brightness, radios, the lock screen and app
launching.

Everything in this module is plain local tooling, no cloud service:

  timers/alarms/reminders  systemd --user transient timers (survive the shell
                           restarting; re-armed from a JSON store after a
                           reboot) -> a notification, a chime and a spoken line
  notifications            notify-send
  media                    playerctl (MPRIS: Spotify, browsers, mpv, ...)
  volume                   wpctl (PipeWire)
  brightness               brightnessctl
  radios                   nmcli / bluetoothctl
  apps                     the XDG .desktop index + gtk-launch

Every function returns plain data and never raises for a missing tool, so the
callers (instant voice intents, the CLI the model uses) can say what is
missing instead of failing silently.
"""
from __future__ import annotations

import configparser
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))
UNIT_PREFIX = "atticus-alarm-"


def _dir() -> Path:
    return Path(os.environ.get("ATTICUS_DIR") or Path.home() / ".config/atticus")


def _run(cmd: list, timeout: float = 5, **kw) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _spawn(cmd: list) -> bool:
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


# --- notifications ----------------------------------------------------------
def notify(body: str, title: str = "Atticus", urgent: bool = False) -> bool:
    if not shutil.which("notify-send"):
        return False
    cmd = ["notify-send", "-a", "Atticus", "-u", "critical" if urgent else "normal", title, body]
    r = _run(cmd)
    return bool(r and r.returncode == 0)


# --- timers, alarms, reminders ----------------------------------------------
def _store() -> Path:
    return _dir() / "alarms.json"


class _Locked:
    """The alarm list, read and written under one lock: the shell, the CLI the
    model calls and a firing timer can all touch it at the same moment."""

    def __enter__(self):
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        self._lock = open(d / "alarms.lock", "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            rows = json.loads(_store().read_text())
            self.rows = [r for r in rows if isinstance(r, dict) and "id" in r and "at" in r]
        except (OSError, ValueError):
            self.rows = []
        return self

    def save(self):
        tmp = _store().with_name("alarms.json.tmp")
        tmp.write_text(json.dumps(self.rows, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(_store())

    def __exit__(self, *exc):
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()


def alarms() -> list[dict]:
    """Pending timers, alarms and reminders, soonest first."""
    with _Locked() as s:
        return sorted(s.rows, key=lambda r: r["at"])


def _unit(row: dict) -> str:
    return f"{UNIT_PREFIX}{row['id']}-{int(row.get('seq', 0))}"


def _arm(row: dict) -> tuple[bool, str]:
    """Schedule one row's next firing as a transient systemd user timer."""
    if not shutil.which("systemd-run"):
        return False, "systemd-run is not available"
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["at"]))
    cmd = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={_unit(row)}",
           f"--on-calendar={when}", "--timer-property=AccuracySec=500ms",
           f"--setenv=PYTHONPATH={RES}", f"--setenv=ATTICUS_RESOURCES_DIR={RES}"]
    if os.environ.get("ATTICUS_DIR"):
        cmd.append(f"--setenv=ATTICUS_DIR={os.environ['ATTICUS_DIR']}")
    cmd += [sys.executable, "-m", "atticus_shell.cli", "--alarm-fire", row["id"]]
    r = _run(cmd, timeout=10)
    if r is None or r.returncode:
        return False, ((r.stderr or r.stdout).strip() if r else "systemd-run did not answer")
    return True, ""


def _disarm(row: dict):
    unit = _unit(row)
    _run(["systemctl", "--user", "stop", unit + ".timer"])
    _run(["systemctl", "--user", "reset-failed", unit + ".timer", unit + ".service"])


def _armed(row: dict) -> bool:
    r = _run(["systemctl", "--user", "is-active", _unit(row) + ".timer"])
    return bool(r and r.stdout.strip() in ("active", "activating"))


def _next_repeat(at: float, repeat: str, now: float) -> float:
    """The next firing of a daily / weekdays row strictly after `now`."""
    t = time.localtime(at)
    day = time.localtime(now)
    for add in range(0, 9):
        cand = time.mktime((day.tm_year, day.tm_mon, day.tm_mday + add, t.tm_hour, t.tm_min, t.tm_sec, 0, 0, -1))
        if cand <= now:
            continue
        if repeat == "weekdays" and time.localtime(cand).tm_wday >= 5:
            continue
        return cand
    return now + 86400


def add(kind: str, at: float, label: str = "", repeat: str = "", lang: str = "pt",
        seconds: float = 0) -> tuple[dict | None, str]:
    """Create and arm a timer / alarm / reminder. -> (row, error)."""
    if kind not in ("timer", "alarm", "reminder"):
        return None, "kind must be timer, alarm or reminder"
    if repeat not in ("", "daily", "weekdays"):
        return None, "repeat must be daily or weekdays"
    now = time.time()
    if repeat:   # the first firing must itself be a valid day, and in the future
        at = _next_repeat(at, repeat, max(now, at - 1))
    if at <= now:
        return None, "that time has already passed"
    if at - now > 366 * 86400:
        return None, "that is more than a year away"
    row = {"id": uuid.uuid4().hex[:8], "kind": kind, "at": float(at), "label": " ".join(str(label).split())[:300],
           "repeat": repeat, "lang": lang if lang in ("pt", "en") else "pt", "seq": 0,
           "seconds": float(seconds), "created": now}
    with _Locked() as s:
        if len(s.rows) >= 200:
            return None, "too many alarms are already set"
        ok, err = _arm(row)
        if not ok:
            return None, err
        s.rows.append(row)
        s.save()
    return row, ""


def cancel(spec: str = "") -> list[dict]:
    """Cancel by id, kind ("timer"), "all", or words from the label. With no
    spec: the soonest one. -> the rows that were cancelled."""
    spec = " ".join(str(spec).lower().split())
    with _Locked() as s:
        rows = sorted(s.rows, key=lambda r: r["at"])
        if not rows:
            return []
        if spec in ("all", "todos", "todas", "tudo"):
            gone = rows
        elif spec in ("timer", "alarm", "reminder"):
            gone = [r for r in rows if r["kind"] == spec]
        elif spec:
            gone = [r for r in rows if r["id"] == spec]
            if not gone:
                words = [w for w in _fold(spec).split() if len(w) > 2]
                gone = [r for r in rows if words and all(w in _fold(r["label"]) for w in words)]
        else:
            gone = rows[:1]
        ids = {r["id"] for r in gone}
        for r in gone:
            _disarm(r)
        s.rows = [r for r in s.rows if r["id"] not in ids]
        s.save()
        return gone


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(s).lower()) if not unicodedata.combining(c))


def fire_text(row: dict, missed: bool = False) -> str:
    """What is shown and spoken when a row goes off."""
    pt = row.get("lang", "pt") == "pt"
    label = row.get("label", "")
    if row["kind"] == "timer":
        text = (f"O timer {('de ' + label) if label else ''} terminou." if pt else
                f"Your {label + ' ' if label else ''}timer is done.")
    elif row["kind"] == "alarm":
        clock = time.strftime("%H:%M", time.localtime(row["at"]))
        text = (f"Alarme das {clock}{': ' + label if label else ''}." if pt else
                f"{clock} alarm{': ' + label if label else ''}.")
    else:
        text = (f"Lembrete: {label}." if pt else f"Reminder: {label}.")
    text = re.sub(r"\s+", " ", text).replace(" .", ".").replace("..", ".")
    if missed:
        clock = time.strftime("%H:%M", time.localtime(row["at"]))
        text += (f" Era para as {clock}, quando o computador estava desligado." if pt else
                 f" It was due at {clock}, while the computer was off.")
    return text


def fire(ident: str, missed: bool = False) -> bool:
    """A timer went off: drop it (or schedule the next repeat), then tell the
    user three ways — notification, chime and voice — so it is noticed even
    with the screen locked or the sound muted."""
    with _Locked() as s:
        row = next((r for r in s.rows if r["id"] == ident), None)
        if not row:
            return False
        fired = dict(row)
        if row.get("repeat"):
            row["at"] = _next_repeat(row["at"], row["repeat"], time.time())
            row["seq"] = int(row.get("seq", 0)) + 1   # a new unit name: this one is still running us
            _arm(row)
        else:
            s.rows = [r for r in s.rows if r["id"] != ident]
        s.save()
    text = fire_text(fired, missed)
    notify(text, urgent=True)
    from . import control
    if not control.send("alarm " + json.dumps({"text": text, "lang": fired.get("lang", "pt"),
                                               "kind": fired["kind"]}, ensure_ascii=False),
                        retries=2, delay=0.2):
        _chime()   # the shell is not running: at least make a sound
    return True


def _chime():
    path = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "atticus" / "earcons" / "alarm.wav"
    player = shutil.which("paplay") or shutil.which("pw-play")
    if player and path.exists():
        _spawn([player, str(path)])


def rearm_all() -> int:
    """After a reboot or a crash the transient timers are gone but the store
    is not: re-arm what is still ahead, and fire what was missed so a reminder
    is late rather than lost. Returns how many rows were touched."""
    now, touched, missed = time.time(), 0, []
    with _Locked() as s:
        for row in s.rows:
            if row["at"] <= now:
                missed.append(row["id"])
            elif not _armed(row):
                _arm(row)
                touched += 1
    for ident in missed:
        fire(ident, missed=True)
    return touched + len(missed)


# --- volume -----------------------------------------------------------------
SINK = "@DEFAULT_AUDIO_SINK@"


def volume() -> tuple[int, bool] | None:
    """(percent, muted) of the default output, or None."""
    r = _run(["wpctl", "get-volume", SINK])
    m = re.search(r"Volume:\s*([\d.]+)", r.stdout) if r and r.returncode == 0 else None
    if not m:
        return None
    return int(round(float(m[1]) * 100)), "MUTED" in r.stdout


def set_volume(percent: int | None = None, delta: int | None = None, mute: bool | None = None) -> tuple[int, bool] | None:
    if mute is not None:
        _run(["wpctl", "set-mute", SINK, "1" if mute else "0"])
    if percent is not None:
        _run(["wpctl", "set-volume", "-l", "1.0", SINK, f"{max(0, min(100, int(percent)))}%"])
    elif delta:
        _run(["wpctl", "set-volume", "-l", "1.0", SINK, f"{abs(int(delta))}%{'+' if delta > 0 else '-'}"])
    if (percent is not None or (delta or 0) > 0) and mute is None:
        _run(["wpctl", "set-mute", SINK, "0"])   # turning it up while muted is asking to hear it
    return volume()


# --- brightness -------------------------------------------------------------
def brightness() -> int | None:
    r = _run(["brightnessctl", "-m"])
    m = re.search(r",(\d+)%", r.stdout) if r and r.returncode == 0 else None
    return int(m[1]) if m else None


def set_brightness(percent: int | None = None, delta: int | None = None) -> int | None:
    if percent is not None:
        _run(["brightnessctl", "set", f"{max(1, min(100, int(percent)))}%"])
    elif delta:
        _run(["brightnessctl", "set", f"{abs(int(delta))}%{'+' if delta > 0 else '-'}"])
    return brightness()


# --- media (MPRIS) ----------------------------------------------------------
def media(action: str) -> tuple[bool, str]:
    """play | pause | toggle | next | previous | stop. -> (ok, error)."""
    verb = {"toggle": "play-pause", "prev": "previous"}.get(action, action)
    if verb not in ("play", "pause", "play-pause", "next", "previous", "stop"):
        return False, "unknown media action"
    if not shutil.which("playerctl"):
        return False, "playerctl is not installed"
    r = _run(["playerctl", verb])
    if r is None or r.returncode:
        return False, "nothing is playing"
    return True, ""


def now_playing() -> dict | None:
    r = _run(["playerctl", "metadata", "--format", "{{status}}\t{{artist}}\t{{title}}\t{{playerName}}"])
    if not r or r.returncode or not r.stdout.strip():
        return None
    status, artist, title, player = (r.stdout.strip().split("\t") + ["", "", "", ""])[:4]
    return {"status": status, "artist": artist, "title": title, "player": player}


# --- radios, lock -----------------------------------------------------------
def radio(which: str, on: bool) -> bool:
    if which == "wifi":
        r = _run(["nmcli", "radio", "wifi", "on" if on else "off"])
    elif which == "bluetooth":
        r = _run(["bluetoothctl", "power", "on" if on else "off"], timeout=8)
    else:
        return False
    return bool(r and r.returncode == 0)


def lock() -> bool:
    for cmd in (["omarchy-lock-screen"], ["hyprlock"], ["loginctl", "lock-session"]):
        if shutil.which(cmd[0]):
            return _spawn(cmd)
    return False


# --- apps ---------------------------------------------------------------
APP_DIRS = [
    os.path.expanduser("~/.local/share/applications"),
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
    "/var/lib/flatpak/exports/share/applications",
]
_APPS: tuple[float, list] = (0.0, [])


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(s))


def apps() -> list[dict]:
    """Launchable desktop entries: [{id, name, names:{folded keys}}]. Cached
    for a minute; an earlier directory wins (a user entry overrides a system
    one of the same id, as the desktop spec says)."""
    global _APPS
    if _APPS[1] and time.monotonic() - _APPS[0] < 60:
        return _APPS[1]
    seen, out = set(), []
    for d in APP_DIRS:
        for f in sorted(glob.glob(os.path.join(d, "*.desktop"))):
            ident = os.path.basename(f)[:-8]
            if ident in seen:
                continue
            seen.add(ident)
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("Type", "Application") != "Application" or e.get("NoDisplay", "").lower() == "true" \
                    or e.get("Hidden", "").lower() == "true":
                continue
            name = e.get("Name", ident)
            names = {_key(name), _key(ident), _key(ident.split(".")[-1])}
            for k in ("Name[pt_BR]", "Name[pt]", "GenericName", "GenericName[pt_BR]"):
                if e.get(k):
                    names.add(_key(e[k]))
            exe = os.path.basename((e.get("Exec", "").split() or [""])[0])
            if exe and exe not in ("env", "sh", "bash", "flatpak", "omarchy-launch-webapp", "omarchy-launch-or-focus-webapp"):
                names.add(_key(exe))
            out.append({"id": ident, "name": name, "names": {n for n in names if n}})
    _APPS = (time.monotonic(), out)
    return out


def find_app(spoken: str) -> dict | None:
    """A spoken name -> one desktop entry, or None when nothing or more than
    one thing matches (an ambiguous name is the model's job, not a guess)."""
    k = _key(spoken)
    if len(k) < 2:
        return None
    rows = apps()
    exact = [a for a in rows if k in a["names"]]
    if exact:
        # several entries can share a generic name ("Web Browser"): prefer the one whose own name matches
        own = [a for a in exact if _key(a["name"]) == k or _key(a["id"]) == k]
        pick = own or exact
        return pick[0] if len(pick) == 1 or own else None
    if len(k) >= 4:
        starts = [a for a in rows if _key(a["name"]).startswith(k)]
        if len(starts) == 1:
            return starts[0]
    return None


def launch(app: dict) -> bool:
    if shutil.which("gtk-launch"):
        return _spawn(["gtk-launch", app["id"]])
    return False


def open_url(url: str) -> bool:
    return _spawn(["xdg-open", url])


# --- lists (shopping, to-do, anything) ---------------------------------------
def _lists_path() -> Path:
    return _dir() / "lists.json"


def _lists_edit(fn):
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "lists.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(_lists_path().read_text())
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        out = fn(data)
        tmp = _lists_path().with_name("lists.json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(_lists_path())
        return out


def list_key(name: str) -> str:
    """ "lista de compras", "compras", "shopping list" -> one key."""
    k = _fold(name).strip()
    k = re.sub(r"^(?:a |o |the |my |minha |meu )?(?:lista|list)(?: de| do| da| of)? ", "", k)
    k = re.sub(r" (?:list|lista)$", "", k).strip()
    alias = {"compra": "compras", "mercado": "compras", "supermercado": "compras", "shopping": "compras", "grocery": "compras",
             "groceries": "compras", "todo": "tarefas", "to do": "tarefas", "to-do": "tarefas", "tasks": "tarefas",
             "tarefa": "tarefas", "afazeres": "tarefas"}
    return alias.get(k, k) or "compras"


def list_add(name: str, items: list[str]) -> list[str]:
    items = [" ".join(i.split())[:120] for i in items if i and i.strip()]

    def fn(data):
        cur = data.setdefault(list_key(name), [])
        have = {_fold(x) for x in cur}
        added = [i for i in items if _fold(i) not in have and not have.add(_fold(i))]
        cur.extend(added)
        del cur[200:]
        return added
    return _lists_edit(fn)


def list_remove(name: str, item: str) -> list[str]:
    def fn(data):
        cur = data.get(list_key(name), [])
        want = _fold(item).strip()
        gone = [x for x in cur if _fold(x) == want] or [x for x in cur if want and want in _fold(x)]
        if len(gone) > 1 and not any(_fold(x) == want for x in gone):
            gone = gone[:1]
        data[list_key(name)] = [x for x in cur if x not in gone]
        return gone
    return _lists_edit(fn)


def list_clear(name: str) -> int:
    def fn(data):
        return len(data.pop(list_key(name), []))
    return _lists_edit(fn)


def list_items(name: str) -> list[str]:
    try:
        return list(json.loads(_lists_path().read_text()).get(list_key(name), []))
    except (OSError, ValueError, AttributeError):
        return []


def list_names() -> list[str]:
    try:
        return [k for k, v in json.loads(_lists_path().read_text()).items() if v]
    except (OSError, ValueError, AttributeError):
        return []
