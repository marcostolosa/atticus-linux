"""Native callback helper — the Linux replacement for the `Atticus --<cmd>` calls
that atticus.py (the brain) makes into its host app.

The brain invokes `$ATTICUS_APP <cmd> ...` as a single executable (argv[0]), so a
one-file launcher (bin/atticus-native) execs `python3 -m atticus_shell.cli`.

v1 implements the commands the core loop needs:
  --screens                      JSON [[name,x,y,w,h,builtin], ...] (main first)
  --look <n> <out.jpg> [winid]   screenshot display n (1-based), ~1280px wide JPEG
  --text <app> [title]           OCR of that window's region (grim + tesseract)
  --calendar                     calendar request as JSON on stdin (agenda.py)
Everything else exits non-zero; the brain already degrades gracefully.
"""
from __future__ import annotations

import json
import os
import shutil
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
    """--hands <verb> ...: acting on the desktop. Controls are looked at and
    used by name through the accessibility bus (atticus_shell/atspi.py), keys
    go to one window through Hyprland, text through wtype. Raw mouse and
    keyboard input (ydotool, uinput) is off unless the setting `rawInput` is
    true. Prints nothing on success; an error line on failure, which the
    brain's own hands() reads and relays instead of claiming it worked."""
    import json as _json
    from . import atspi, hands, settings
    if not argv:
        return 1
    verb, rest = argv[0], argv[1:]
    raw = bool(settings.get("rawInput", False))
    if verb == "see":
        win = atspi.find_window(rest[0] if rest else "", " ".join(rest[1:]))
        if not win:
            print("There is no such window.")
            return 1
        rows, note = atspi.snapshot(win)
        atspi.save(win, rows)
        head = f'{win.get("class", "")} - {atspi.clean(win.get("title", ""), 80)}'
        print("\n".join(x for x in (head, atspi.lines(rows), note) if x))
        return 0 if rows else 1
    if verb == "describe":   # what the Allow card says, read from the program itself: {} fields, or {"error": ...}
        what = rest[0] if rest else ""
        if what in ("click", "fill") and len(rest) > 1 and rest[1].isdigit():
            print(_json.dumps(atspi.describe(int(rest[1]))))
        else:
            win = atspi.target_window() if what == "press" else atspi.front()   # typing goes to the focused window, keys to the one looked at
            cls = str(win.get("class", "")).lower()
            print(_json.dumps({"app": atspi.clean(win.get("class", ""), 40), "title": atspi.clean(win.get("title", ""), 60), "terminal": cls in atspi.TERMINALS}))
        return 0
    if verb == "click" and len(rest) == 1 and rest[0].isdigit():
        ok, msg = atspi.click(int(rest[0]))
        if not ok and msg:
            print(msg)
        return 0 if ok else 1
    if verb == "fill" and len(rest) >= 2 and rest[0].isdigit():
        ok, msg = atspi.fill(int(rest[0]), " ".join(rest[1:]))
        if not ok and msg:
            print(msg)
        return 0 if ok else 1
    if verb == "press" and not raw:
        if not rest:
            return 1
        times = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 1
        win = atspi.target_window()
        ok, msg = atspi.press(rest[0], str(win.get("address", "")), times)
        if not ok and msg:
            print(msg)
        return 0 if ok else 1
    if verb in ("click", "scroll", "drag", "press") and not raw:
        print("Mouse input by position is off. Look with --hands see, then use --hands click <number>; scroll with press PageDown.")
        return 1
    if verb == "type" and not hands.type_available():
        print("wtype is not installed")
        return 1
    if verb in ("click", "press", "scroll", "drag") and not hands.available():
        print("ydotool is not set up")
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
        # direction x y [amount]: x/y position the pointer first — a wheel
        # event only affects whatever is under the cursor, so without this a
        # scroll call could silently do nothing or hit the wrong thing.
        if len(rest) < 3:
            return 1
        try:
            x, y = int(float(rest[1])), int(float(rest[2]))
        except ValueError:
            return 1
        amount = int(rest[3]) if len(rest) > 3 and rest[3].lstrip("-").isdigit() else 3
        ok, msg = hands.scroll(rest[0], amount, x, y)
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


# --- everyday assistant: timers, alarms, reminders, media, volume ------------
def _parse_span(text: str) -> float | None:
    """ "90", "90s", "10m", "1h30m", "1h 30m" -> seconds."""
    text = text.strip().lower()
    if text.replace(".", "", 1).isdigit():
        return float(text)
    import re
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(h|m|s)", text)
    if not parts or re.sub(r"(\d+(?:\.\d+)?)\s*(h|m|s)|\s", "", text):
        return None
    return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)


def _row_line(r: dict) -> str:
    import time
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["at"]))
    left = max(0, int(r["at"] - time.time()))
    rep = f" repeats {r['repeat']}" if r.get("repeat") else ""
    return f"{r['id']} {r['kind']} at {when} (in {left // 3600}h{left % 3600 // 60:02d}m{left % 60:02d}s){rep} {r.get('label', '')}".rstrip()


def _lang() -> str:
    from . import settings
    lang = str(settings.get("asrLanguage", "auto")).lower()
    if lang.startswith("en"):
        return "en"
    if lang.startswith("pt"):
        return "pt"
    return "pt" if (os.environ.get("LANG") or "").lower().startswith("pt") else "en"


def assistant_main(cmd: str, rest: list[str]) -> int:
    """The model-facing side of atticus_shell/assistant.py. Replies are short
    English facts for the model to phrase; errors exit 1 with the reason."""
    import time
    from . import assistant as A
    if cmd == "--alarm-fire":
        return 0 if rest and A.fire(rest[0]) else 1
    if cmd == "--timer":
        secs = _parse_span(rest[0]) if rest else None
        if not secs or secs > 86400:
            print('usage: --timer <duration> [label]   e.g. --timer 10m "pasta", --timer 1h30m, --timer 45s')
            return 1
        row, err = A.add("timer", time.time() + secs, " ".join(rest[1:]), lang=_lang(), seconds=secs)
        print(f"timer set: {_row_line(row)}" if row else f"not set: {err}")
        return 0 if row else 1
    if cmd == "--alarm":
        import re
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", rest[0]) if rest else None
        if not m:
            print('usage: --alarm <HH:MM> [today|tomorrow|YYYY-MM-DD|daily|weekdays] [label]   24-hour clock')
            return 1
        h, mi = int(m[1]), int(m[2])
        day, label, repeat = (rest[1].lower() if len(rest) > 1 else ""), rest[2:], ""
        lt = time.localtime()
        if day in ("daily", "weekdays"):
            repeat, base = day, (lt.tm_year, lt.tm_mon, lt.tm_mday)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            base = tuple(int(x) for x in day.split("-"))
        elif day in ("today", "tomorrow"):
            base = (lt.tm_year, lt.tm_mon, lt.tm_mday + (day == "tomorrow"))
        else:
            label, base = rest[1:], None
        if base is None:   # no day: the next time the clock shows it
            at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
            if at <= time.time():
                at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, h, mi, 0, 0, 0, -1))
        else:
            at = time.mktime((*base, h, mi, 0, 0, 0, -1))
        row, err = A.add("alarm", at, " ".join(label), repeat, lang=_lang())
        print(f"alarm set: {_row_line(row)}" if row else f"not set: {err}")
        return 0 if row else 1
    if cmd == "--remind":
        import re
        usage = 'usage: --remind "<YYYY-MM-DD HH:MM>" <text> | --remind +<duration> <text> | add daily|weekdays before the text to repeat'
        if len(rest) < 2:
            print(usage)
            return 1
        when, words, repeat = rest[0], rest[1:], ""
        if when.startswith("+"):
            secs = _parse_span(when[1:])
            if not secs:
                print(usage)
                return 1
            at = time.time() + secs
        else:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when) and words and re.fullmatch(r"\d{1,2}:\d{2}", words[0]):
                when, words = when + " " + words[0], words[1:]   # date and time passed as two arguments
            try:
                at = time.mktime(time.strptime(when.replace("T", " ")[:16], "%Y-%m-%d %H:%M"))
            except ValueError:
                print(usage)
                return 1
        if words and words[0].lower() in ("daily", "weekdays"):
            repeat, words = words[0].lower(), words[1:]
        if not words:
            print(usage)
            return 1
        row, err = A.add("reminder", at, " ".join(words), repeat, lang=_lang())
        print(f"reminder set: {_row_line(row)}" if row else f"not set: {err}")
        return 0 if row else 1
    if cmd == "--alarms":
        if rest[:1] == ["cancel"]:
            gone = A.cancel(" ".join(rest[1:]))
            print("\n".join("cancelled: " + _row_line(r) for r in gone) or "nothing matched; run --alarms to list them")
            return 0 if gone else 1
        rows = A.alarms()
        print("\n".join(_row_line(r) for r in rows) or "no timers, alarms or reminders are set")
        return 0
    if cmd == "--media":
        action = rest[0] if rest else "status"
        if action != "status":
            ok, err = A.media(action)
            if not ok:
                print(err)
                return 1
            time.sleep(0.3)   # the player reports its new state a moment later
        p = A.now_playing()
        print(f"{p['status']}: {p['title']}" + (f" by {p['artist']}" if p["artist"] else "") + f" ({p['player']})"
              if p else "nothing is playing")
        return 0
    if cmd == "--volume":
        arg = rest[0].lower().rstrip("%") if rest else ""
        if arg in ("mute", "unmute"):
            v = A.set_volume(mute=arg == "mute")
        elif arg[:1] in ("+", "-") and arg[1:].isdigit():
            v = A.set_volume(delta=int(arg))
        elif arg.isdigit():
            v = A.set_volume(percent=int(arg))
        elif arg:
            print("usage: --volume [N | +N | -N | mute | unmute]   percent of the default output")
            return 1
        else:
            v = A.volume()
        print(f"volume {v[0]}%" + (" (muted)" if v[1] else "") if v else "could not reach the audio server (wpctl)")
        return 0 if v else 1
    if cmd == "--brightness":
        arg = rest[0].rstrip("%") if rest else ""
        if arg[:1] in ("+", "-") and arg[1:].isdigit():
            b = A.set_brightness(delta=int(arg))
        elif arg.isdigit():
            b = A.set_brightness(percent=int(arg))
        elif arg:
            print("usage: --brightness [N | +N | -N]   percent")
            return 1
        else:
            b = A.brightness()
        print(f"brightness {b}%" if b is not None else "no adjustable backlight found (brightnessctl)")
        return 0 if b is not None else 1
    if cmd == "--notify":
        ok = A.notify(" ".join(rest)[:2000]) if rest else False
        if not ok:
            print("could not show a notification (notify-send)")
        return 0 if ok else 1
    if cmd == "--list":
        usage = 'usage: --list | --list "<name>" | --list "<name>" add "<item>" ["<item>"…] | remove "<item>" | clear'
        if not rest:
            names = A.list_names()
            print("\n".join(f"{n}: {len(A.list_items(n))} item(s)" for n in names) or "no lists yet")
            return 0
        name, verb, items = rest[0], (rest[1] if len(rest) > 1 else "show"), rest[2:]
        if verb == "show":
            rows = A.list_items(name)
            print("\n".join(f"- {r}" for r in rows) or f"the {A.list_key(name)} list is empty")
        elif verb == "add" and items:
            added = A.list_add(name, items)
            print(f"added to {A.list_key(name)}: " + ", ".join(added) if added else "already on the list")
        elif verb == "remove" and items:
            gone = A.list_remove(name, " ".join(items))
            print(f"removed from {A.list_key(name)}: " + ", ".join(gone) if gone else "that is not on the list")
        elif verb == "clear":
            print(f"cleared {A.list_clear(name)} item(s) from {A.list_key(name)}")
        else:
            print(usage)
            return 1
        return 0
    if cmd == "--home":
        from . import home
        return home.main(rest)
    if cmd == "--apps":
        print(json.dumps(sorted({a["name"] for a in A.apps()})))
        return 0
    return 1


ASSISTANT_VERBS = ("--alarm-fire", "--timer", "--alarm", "--remind", "--alarms", "--media", "--volume",
                   "--brightness", "--notify", "--apps", "--list", "--home")


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    cmd = argv[0]
    if cmd in ASSISTANT_VERBS:
        return assistant_main(cmd, argv[1:])
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
        if not hands.type_available():
            print("neither wtype nor ydotool is set up for typing")
            return 1
        ok, msg = hands.type_text(text)
        if not ok:
            print(msg or "Could not type.")
        return 0 if ok else 1
    if cmd == "--draw-target":   # the window a guide is about: the one in use
        w = _active_window()
        if not w:
            return 1
        (x, y), (width, height) = w.get("at", [0, 0]), w.get("size", [0, 0])
        print(json.dumps({"pid": w.get("pid", 0), "windowID": str(w.get("address", "")), "windowFrame": [x, y, width, height]}))
        return 0
    if cmd == "--calendar":   # one JSON request on stdin, one JSON answer
        from . import agenda
        try:
            print(json.dumps(agenda.run(json.load(sys.stdin)), ensure_ascii=False))
            return 0
        except (OSError, ValueError, TypeError, KeyError) as e:
            print(json.dumps({"error": str(e)[:300] or "Calendar could not finish."}))
            return 0
    if cmd == "control" and len(argv) >= 2:
        from . import control
        rest = " ".join(argv[1:])
        if control.send(rest):
            return 0
        # A key bound in Hyprland has no terminal to print in: say it on the desktop too.
        text = "Atticus is not running. Start it with: systemctl --user start atticus"
        print(text, file=sys.stderr)
        if rest.split()[0] in ("press", "toggle", "dictate", "settings", "meeting", "setup") and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "Atticus", "-u", "low", "Atticus", text], capture_output=True, timeout=3)
        return 3
    # Unimplemented native command in v1: fail so the brain degrades.
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
