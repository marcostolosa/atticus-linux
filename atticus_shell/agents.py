"""Background agents: tasks that go on working while the box is free for the next request.

The work itself is done by the brain's supervisor (`atticus.py --agents`), one
long-lived process that starts, steers and records each task. This module is
its face on the desktop:

  the shelf   a small column of chips at the top centre, one per task
              that is working, asking something or just finished
  the view    a window with every task, its steps, its result and saved chats

Both are the same page (ui/agents.html) told which of the two it is. The
supervisor and the page speak JSON: one object per line with the supervisor,
`agents.<name>(value)` calls into the page, and `{action: ...}` messages back.

A task gets here in two ways: the brain moves a long request of the main box
to the background (a "⤴" line with its brief), or a saved routine comes due.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, GLib, Gtk, WebKit  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

from . import settings as cfg
from . import stack
from .overlay import load_page

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))
LIVE = ("running", "needs-you", "waiting-screen", "queued")
SHELF_W = 260
CONNECTORS = "https://claude.ai/settings/connectors"


def lingers(row: dict, now: float | None = None) -> bool:
    """Finished or last looked at within ten minutes: its chip is still news."""
    now = time.time() if now is None else now
    return row.get("status") in ("done", "failed") and now - max(row.get("ended") or 0, row.get("seenAt") or 0) < 600


def badge(rows: dict) -> dict:
    """What the box's "N agents" pill shows; it turns amber when one waits for the user."""
    return {"count": sum(r.get("status") == "running" for r in rows.values()),
            "attention": any(r.get("status") == "needs-you" for r in rows.values())}


def slots_full(rows: dict) -> bool:
    """Three tasks work at once; the next one waits its turn."""
    return sum(r.get("status") in ("running", "needs-you", "waiting-screen") and r.get("ended") is None for r in rows.values()) >= 3


def newest_first(rows: dict) -> list[dict]:
    return sorted(rows.values(), key=lambda r: r.get("created") or 0, reverse=True)


def opens(row: dict | None, target: str | None, reveal: bool) -> tuple[str, str] | None:
    """What a click on a task's card opens: ("url", address), ("file", path),
    ("folder", path), or None for the task's own page in the view."""
    if row is None:
        return ("url", target) if target and target.startswith(("http://", "https://")) else None
    made = row.get("made") or []
    raw = target or next((m.get("url") for m in made if m.get("url")), None) or next(iter(row.get("links") or []), None)
    root = row.get("directory") or ""
    if raw:
        if raw.startswith(("http://", "https://")):
            return ("url", raw)
        path = raw[len("file://"):] if raw.startswith("file://") else os.path.expanduser(raw)
        path = os.path.normpath(path if path.startswith("/") else os.path.join(root, path))
        if os.path.exists(path):
            return ("folder", os.path.dirname(path)) if reveal else ("file", path)
    if reveal and root:
        return ("folder", root)
    return None


class Page:
    """One WebKit view of a page of ui/ (agents.html unless told otherwise).
    The page posts objects to its handler and is called as `<obj>.<name>(value)`.
    Calls made before the page says it is ready wait for it."""

    def __init__(self, on_message: Callable[[dict], None], page: str = "agents.html", handler: str = "agents", obj: str = "agents"):
        self.ready, self._pending, self.obj = False, [], obj
        ucm = WebKit.UserContentManager()   # the handler must exist before the view is made with it
        ucm.register_script_message_handler(handler, None)
        ucm.connect("script-message-received::" + handler, self._received)
        self.on_message = on_message
        self.web = WebKit.WebView(user_content_manager=ucm)
        clear = Gdk.RGBA()
        clear.parse("rgba(0,0,0,0)")   # a bare Gdk.RGBA() is not see-through here: measured, the view painted #1e1e1e
        self.web.set_background_color(clear)
        try:
            self.web.get_settings().set_enable_developer_extras(bool(os.environ.get("ATTICUS_DEVTOOLS")))
        except Exception:
            pass
        self.web.load_html(load_page(page), None)

    def _received(self, _manager, value):
        try:
            body = json.loads(value.to_json(0))
        except (ValueError, AttributeError, TypeError):
            return
        said = body.get("action", body.get("do")) if isinstance(body, dict) else None   # the pages name it either way
        if isinstance(said, str):
            if said == "ready":
                self.ready = True
            self.on_message(body)
            if said == "ready":
                pending, self._pending = self._pending, []
                for js in pending:
                    self.web.evaluate_javascript(js, -1, None, None, None, None, None)

    def call(self, name: str, value: Any = None):
        js = f"window.{self.obj} && {self.obj}.{name} && {self.obj}.{name}({json.dumps({} if value is None else value)})"
        if self.ready:
            self.web.evaluate_javascript(js, -1, None, None, None, None, None)
        elif name not in ("visible", "pointer", "level", "clock"):
            self._pending.append(js)


class AgentPanels:
    def __init__(self, app, box_js: Callable[[str, Any], None], box_state: Callable[[], dict],
                 show_box: Callable[[], None], hide_box: Callable[[float], None], sound: Callable[[str], None] | None = None):
        self.app, self.box_js, self.box_state = app, box_js, box_state
        self.show_box, self.hide_box, self.sound = show_box, hide_box, sound or (lambda name: None)
        self.rows: dict[str, dict] = {}
        self.named: list = []
        self.offers: dict[str, dict] = {}
        self.dismissed = set(x for x in (cfg.get("agentDismissed", []) or []) if isinstance(x, str))
        self.lingering = False
        self.ping = ""          # a finished task whose card is shown in the box
        self.process = None
        self._restarts, self._last_box, self._closed = 0, None, False
        self._write = threading.Lock()
        self.shelf = self.shelf_page = self.view = self.view_page = None
        GLib.timeout_add(500, self._tick)
        GLib.idle_add(self.start)   # accepted work is picked up again even if the view is never opened

    # --- the supervisor ------------------------------------------------------
    def start(self) -> bool:
        if self.process is not None or self._closed:
            return False
        from .bridge import ATTICUS_PY, _brain_env
        try:
            p = subprocess.Popen([sys.executable, str(ATTICUS_PY), "--agents"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1, env=_brain_env(), cwd=str(RES))
        except OSError:
            self._retry()
            return False
        self.process = p
        threading.Thread(target=self._read, args=(p,), daemon=True).start()
        self.send({"action": "list"})
        return False

    def _retry(self):
        self._restarts += 1
        GLib.timeout_add(int(min(60, 2 ** self._restarts) * 1000), self.start)

    def _read(self, p):
        for line in p.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                GLib.idle_add(self._deliver, p, event)
        p.wait()
        GLib.idle_add(self._ended, p)

    def _deliver(self, p, event) -> bool:
        if p is self.process:
            try:
                self.receive(event)
            except Exception:
                import traceback
                traceback.print_exc()
        return False

    def _ended(self, p) -> bool:
        if p is self.process and not self._closed:
            # A supervisor that died lost its half-read line and what it knew of
            # the box; a new one lists its saved tasks again.
            self.process, self._last_box = None, None
            self._retry()
        return False

    def send(self, data: dict) -> None:
        if self.process is None:
            self.start()
        p = self.process
        if p is None:
            return
        try:
            with self._write:
                p.stdin.write(json.dumps(data) + "\n")
                p.stdin.flush()
        except (OSError, ValueError):
            try: p.terminate()
            except OSError: pass

    def close(self) -> None:
        self._closed = True
        p, self.process = self.process, None
        if p and p.poll() is None:
            try: p.terminate()
            except OSError: pass

    # --- the two surfaces ----------------------------------------------------
    def js(self, surface: str, name: str, value: Any = None) -> None:
        page = self.view_page if surface == "view" else self.shelf_page
        if page:
            page.call(name, value)

    def _ensure_shelf(self) -> None:
        if self.shelf is not None:
            return
        win = Gtk.Window(application=self.app)
        win.set_decorated(False)
        win.add_css_class("atticus-overlay")   # the box's see-through window style
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "atticus-agents")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.ON_DEMAND)   # an answer can be typed into a chip
        stack.anchor(win)   # top centre, below the assistant's box (stack.py)
        stack.update("shelf", window=win, height=52, visible=False)
        win.connect("notify::visible", lambda w, _p: stack.update("shelf", visible=w.get_visible()))
        self.shelf_page = Page(lambda body: self._message("shelf", body))
        self.shelf_page.web.set_size_request(SHELF_W, 52)
        win.set_child(self.shelf_page.web)   # the layer takes the view's size, and shrinks with it
        win.set_visible(False)
        self.shelf = win

    def open_view(self, identifier: str = "") -> None:
        self.start()
        if self.view is None:
            win = Gtk.ApplicationWindow(application=self.app, title="Atticus Agents")
            win.set_default_size(760, 580)
            win.add_css_class("atticus-agents")
            from .overlay import style_windows
            style_windows()
            win.add_css_class("atticus-window")
            win.connect("close-request", lambda *_: (self._close_view(), True)[1])
            self.view_page = Page(lambda body: self._message("view", body))
            win.set_child(self.view_page.web)
            self.view = win
        self.js("view", "visible", True)
        self.view.set_visible(True)
        self.view.present()
        if not identifier:   # the menu and the box's pill land on the task that is working
            identifier = next((r["id"] for r in newest_first(self.rows) if r.get("status") in LIVE), "")
        if identifier:
            self.js("view", "select", identifier)
        self.js("view", "list", newest_first(self.rows))

    def _close_view(self) -> None:
        if self.view:
            self.view.set_visible(False)
        self.js("view", "visible", False)
        if self.has_chips and not self.quiet:
            self.js("shelf", "resume")

    @property
    def quiet(self) -> bool:
        """Chips stay off the screen (a presentation): results arrive as notifications."""
        return bool(cfg.get("agentsHidden", False))

    @property
    def has_chips(self) -> bool:
        return self.lingering or any(r.get("status") in LIVE and r.get("id") not in self.dismissed for r in self.rows.values())

    def _set_dismissed(self, identifier: str, gone: bool) -> None:
        before = set(self.dismissed)
        (self.dismissed.add if gone else self.dismissed.discard)(identifier)
        if self.dismissed != before:   # kept over a restart only while the task could still show
            cfg.set("agentDismissed", sorted(i for i in self.dismissed if i in self.rows and (self.rows[i].get("status") in LIVE or lingers(self.rows[i]))))

    # --- what the supervisor says ---------------------------------------------
    def receive(self, event: dict) -> None:
        kind = event.get("event", "")
        if kind == "list" and isinstance(event.get("agents"), list):
            self._restarts = 0
            self.rows = {r["id"]: r for r in event["agents"] if isinstance(r, dict) and isinstance(r.get("id"), str)}
            self.named = event.get("named") or []
            if self.ping and self.ping not in self.rows:
                self.ping = ""
                self.box_js("agentCancelled", None)
                self.hide_box(1)
            if any(lingers(r) for r in self.rows.values()):
                self._ensure_shelf()
            rows = newest_first(self.rows)
            self.js("shelf", "list", rows); self.js("view", "list", rows); self.js("view", "named", self.named)
        elif kind == "chats":
            self.js("view", "chats", event.get("chats") or [])
        elif kind == "named":
            self.named = event.get("named") or []
            self.js("view", "named", self.named)
            if isinstance(event.get("reply"), str):
                self.js("view", "namedReply", event["reply"])
        row = event.get("agent")
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            identifier, old = row["id"], self.rows.get(row["id"]) or {}
            asked = lambda r: (r.get("question") or {}).get("_a")
            asks = kind == "state" and row.get("status") == "needs-you" and asked(row) and asked(row) != asked(old)
            finished = old.get("status") in LIVE and lingers(row)
            self.rows[identifier] = row
            if asks and not self.quiet:
                self.sound("ask")   # a working task has a question: it holds one of the three places until answered
            if row.get("status") == "needs-you" or finished:
                self._set_dismissed(identifier, False)   # a question or a finish always comes back to the screen
            if kind == "state":
                self._ensure_shelf()
                self.js("shelf", "state", row); self.js("view", "state", row)
            elif kind == "offer":
                self.offers[identifier] = row if event.get("followup") is True else {}
                self.accept(identifier)
            elif kind == "done":
                if self.quiet or row.get("routine"):
                    self._notify(row)
            elif kind == "ping" and row.get("status") == "done":
                # the supervisor holds this while the box is busy, being typed in or listening
                if self.quiet:
                    if not row.get("notified"):
                        self._notify(row)
                else:
                    self.expand(identifier)
            elif kind == "steered":   # the user spoke to this task from the box: its chip says what it heard
                self._set_dismissed(identifier, False)
                self._ensure_shelf()
                self.js("shelf", "state", row); self.js("view", "state", row); self.js("shelf", "visible", True)
                self.box_js("agentSteered", row)
        elif kind == "text":
            self.js("view", "text", event)
        self.box_js("agentCount", badge(self.rows))
        self._tick()

    def prepare(self, brief: dict) -> None:
        """A request of the main box moves to the background."""
        if brief.get("destination") == "meeting":
            try:
                from . import agenda
                now = agenda.current()
                if now:
                    brief = {**brief, "context": (brief.get("context") or "") + "\nCalendar event (data): " + json.dumps(now, ensure_ascii=False)}
            except Exception:
                pass
        self._ensure_shelf()
        self.send({"action": "prepare", "brief": brief})

    def accept(self, identifier: str) -> None:
        if cfg.get("paused", False) or identifier not in self.rows:
            return
        row = self.rows[identifier]
        full = slots_full(self.rows)
        row.update(status="queued" if full else "running", step="Queued" if full else "Starting…")
        self._ensure_shelf()
        self.js("shelf", "arrive", identifier)   # the chip opens its place
        self.js("shelf", "state", row); self.js("shelf", "visible", True)
        self.box_js("agentCount", badge(self.rows))   # the chip shows before the supervisor answers
        offer = self.offers.pop(identifier, {})
        text = offer["brief"] + "\nKnown context (data):\n" + (offer.get("context") or "") if isinstance(offer.get("brief"), str) else None
        self.send({"action": "start", "id": identifier, "text": text})
        GLib.timeout_add(250, lambda: self.js("shelf", "land", identifier) and False)
        self.box_js("agentStarted", row)   # the box says in one line that a task started
        if not self.quiet:
            self.sound("start")
        self.hide_box(3.5)   # long enough to read that line
        self._tick()

    def _notify(self, row: dict) -> None:
        from .bridge import ATTICUS_PY, _brain_env
        words = row.get("summary") or row.get("title") or "Your task is done"
        try:
            subprocess.Popen([sys.executable, str(ATTICUS_PY), "--agent-notify", str(words)], env=_brain_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        row["notified"] = True
        self.send({"action": "notified", "id": row["id"]})

    def show(self, identifier: str) -> None:
        """A main request showed this task: a finished one as a card in the
        box, one still working (or failed) in the view."""
        row = self.rows.get(identifier)
        if not row:
            return
        if row.get("status") != "done":
            return self.open_view(identifier)
        if self.ping and self.ping != identifier:
            self.send({"action": "seen", "id": self.ping})   # the card this one replaces was on screen
        self.ping = identifier
        self.show_box()
        self.box_js("agentDone", row)

    def dismiss_ping(self, identifier: str) -> None:
        self.send({"action": "seen", "id": identifier})
        self.ping = ""
        self.hide_box(1)

    def expand(self, identifier: str) -> None:
        """A finish shows on its own chip, opened with the result; the box stays as it was."""
        self._set_dismissed(identifier, False)
        self._ensure_shelf()
        self.js("shelf", "expand", identifier)
        self.sound("done")
        self._tick()

    def open(self, identifier: str, target: str | None = None, reveal: bool = False) -> None:
        what = opens(self.rows.get(identifier), target, reveal)
        if what is None:
            if identifier in self.rows:
                self.open_view(identifier)
            return
        try:
            subprocess.Popen(["xdg-open", what[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def stop_all(self) -> None:
        """Pause: the waiting ones first, so stopping a working one cannot start them."""
        live = [r for r in self.rows.values() if r.get("status") in LIVE]
        for row in sorted(live, key=lambda r: r.get("status") != "queued"):
            self.send({"action": "stop", "id": row["id"]})

    # --- what the pages say -----------------------------------------------------
    def _message(self, surface: str, body: dict) -> None:
        action, identifier = body["action"], body.get("id") if isinstance(body.get("id"), str) else ""
        done = self.rows.get(identifier, {}).get("status") == "done"
        if action == "ready":
            self.js(surface, "mode", surface)
            self.js(surface, "prefs", {"dismissed": sorted(self.dismissed), "stack": cfg.get("agentStack", "") or "", "tab": cfg.get("agentTab", "") or ""})
            self.js(surface, "list", newest_first(self.rows))
            if surface == "view":
                self.js(surface, "named", self.named)
            else:
                self.js(surface, "visible", bool(self.shelf and self.shelf.get_visible()))
        elif action == "size":
            h = body.get("h")
            if surface == "shelf" and self.shelf and isinstance(h, (int, float)) and 0 < h < 4000:
                height = max(52, int(h))
                self.shelf_page.web.set_size_request(SHELF_W, height)
                stack.update("shelf", height=height)
        elif action == "dismiss":
            if done:
                self.send({"action": "seen", "id": identifier})
                self.rows[identifier]["seenAt"] = time.time()   # as the supervisor saves it
            self._set_dismissed(identifier, True)
        elif action == "stack":
            cfg.set("agentStack", body.get("value") if isinstance(body.get("value"), str) else "")
        elif action == "tab":
            cfg.set("agentTab", "chats" if body.get("value") == "chats" else "")
        elif action in ("view", "show"):
            self.open_view(identifier)
            if done:
                self.send({"action": "seen", "id": identifier})
        elif action == "lingering":
            self.lingering = bool(body.get("value"))
            self._tick()
        elif action == "close":
            if surface == "view":
                self._close_view()
        elif action in ("hover", "miniRect", "drag", "attention"):
            pass   # live window previews and dragging the shelf are not part of this desktop's panel
        elif action == "open":
            self.open(identifier, body.get("target") if isinstance(body.get("target"), str) else None)
            if done:
                self.send({"action": "seen", "id": identifier})
        elif action == "reveal":
            self.open(identifier, reveal=True)
        elif action == "connect":
            subprocess.Popen(["xdg-open", CONNECTORS], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif action == "answer":
            self.send(body)
            question = self.rows.get(identifier, {}).get("question") or {}
            if body.get("answer") == "y" and question.get("need") and question.get("_a") == body.get("question"):
                self._allow(identifier, question)
        else:
            self.send(body)

    def _allow(self, identifier: str, question: dict) -> None:
        """Allow on a task's setup card. Setup that is a system permission
        elsewhere needs no grant on this desktop, so the task is told at once
        that it may go on; a connector opens its sign-in page and the
        supervisor watches for it."""
        need = question.get("need")
        if need == "page-js":
            cfg.set("pageJS", True)
        elif need == "connector":
            subprocess.Popen(["xdg-open", CONNECTORS], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.send({"action": "granted", "id": identifier})

    def _tick(self) -> bool:
        if self._closed:
            return False
        if not self.rows:
            if self.shelf and self.shelf.get_visible():
                self.shelf.set_visible(False)
            return True
        box = {"action": "box", **self.box_state(), "quiet": self.quiet}
        if box != self._last_box:
            self._last_box = box
            self.send(box)
        if self.shelf:
            want = self.has_chips and not self.quiet
            if want != self.shelf.get_visible():
                self.shelf.set_visible(want)
                self.js("shelf", "visible", want)
        if self.ping and not self.box_state().get("visible", True):   # the user closed the box over the card
            self.send({"action": "seen", "id": self.ping})
            self.ping = ""
            self.box_js("agentCancelled", None)
        return True
