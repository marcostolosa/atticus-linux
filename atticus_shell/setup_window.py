"""The first-run window: what Atticus is, what it still needs, and a first try.

It hosts ui/onboarding.html the way the Settings window hosts its page, and
answers it with the same checks (settings_backend): is there a way to record,
a recognizer, ydotool for the mouse and keyboard, grim for the screen, and is
the AI client installed and signed in. Nothing here is a permission the system
grants: each row is something installed or not, so a row's button says what
is missing, in a notification, instead of opening a system pane.

`atticus-ctl setup` opens it; it opens by itself only when there is no
settings file yet (a first run).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import browser, settings as cfg
from . import settings_backend as backend
from .agents import Page

CONNECTORS = "https://claude.ai/settings/connectors"
CHOICES = {"routing": ("cheapest", "balanced", "capable"), "dictateMode": ("verbatim", "cleanup")}
SWITCHES = ("meeting", "draw", "improve")


def needs() -> dict:
    """Each row of the page: True when that thing is in place."""
    return {**backend.perms_payload(),
            "auto": True, "calendar": True,          # nothing to allow: other programs are reached without a grant, the calendar is files
            "tools": bool(cfg.get("agentUserTools", True)),
            "chrome": bool(browser.command())}       # a Chromium browser for reading and clicking pages


def prefs(binds: dict) -> dict:
    p = backend.prefs_with_defaults()
    return {"meeting": bool(p.get("meeting", True)), "draw": bool(p.get("draw", True)), "routing": p.get("routing", "balanced"),
            "hotkeyLabel": binds.get("talk", ""), "safe": bool(p.get("safe", True)), "dictateMode": p.get("dictateMode", "verbatim"),
            "dictateHotkeyLabel": binds.get("dictate", ""), "globeTaken": False}


def apply(key: str, value: str) -> bool:
    """One switch or choice of the feature pages. False when it is not one of them."""
    if key in SWITCHES:
        return cfg.set(key, value == "true")
    if value in CHOICES.get(key, ()):
        return cfg.set(key, value)
    return False


def sign_in_command(plan: dict) -> str | None:
    """What to run in a terminal for the AI client's row, or None when a web page (or nothing) is the fix."""
    need = plan.get("need")
    if need == "install":
        return "curl -fsSL https://claude.ai/install.sh | bash && ~/.local/bin/claude auth login"
    if need == "login":
        return "claude auth login"
    return None


class SetupWindow:
    def __init__(self, app, on_done=None, open_settings=None):
        self.app, self.on_done, self.open_settings = app, on_done or (lambda: None), open_settings or (lambda: None)
        self.window = Gtk.ApplicationWindow(application=app, title="Atticus Setup")
        self.window.set_default_size(640, 640)
        self.window.connect("close-request", lambda *_: (self.close(), True)[1])
        self.page = Page(self._message, page="onboarding.html", handler="ob", obj="ob")
        self.window.set_child(self.page.web)
        self._ticks, self._checking, self._closed = 0, False, False
        GLib.timeout_add(1000, self._tick)
        self._tick()

    def present(self):
        self.window.set_visible(True)
        self.window.present()

    def close(self):
        self._closed = True
        self.window.set_visible(False)
        self.window.destroy()
        self.on_done()

    def tried(self):
        """The talk key was pressed while this window is up: the try page goes green."""
        self.page.call("tried")

    def _tick(self) -> bool:
        if self._closed:
            return False
        self._ticks += 1
        threading.Thread(target=self._push, daemon=True).start()
        if self._ticks % 5 == 1 and not self._checking:   # it starts the AI client: too slow for every second
            self._checking = True
            threading.Thread(target=self._plan, daemon=True).start()
        return True

    def _shortcut(self, keys):
        """The dictation keys the page recorded: bound in Hyprland, or refused with what already has them."""
        answer = backend.set_shortcut("dictate", keys if isinstance(keys, str) else None)
        if answer and answer[0] in ("taken", "notice"):
            payload = answer[1] if answer[0] == "taken" else {"own": False, "what": answer[1]["text"]}
            GLib.idle_add(lambda: (self.page.call("taken", payload), False)[1])
        else:
            label = backend.hyprland_binds().get("dictate", "")
            GLib.idle_add(lambda: (self.page.call("recorded", label), False)[1])

    def _push(self):
        state, binds, p = needs(), backend.hyprland_binds(), backend.prefs_with_defaults()

        def back():
            if not self._closed:
                self.page.call("set", state)
                self.page.call("mode", p.get("talkMode", "toggle") == "toggle")
                self.page.call("prefs", prefs(binds))
                self.page.call("server", {"apiBase": p.get("apiBase", "") or "", "apiModel": p.get("apiModel", "") or ""})
            return False
        GLib.idle_add(back)

    def _plan(self):
        plan = backend.brain_json("--check", timeout=40) or {"ok": False, "need": "install", "text": "Could not run Atticus's helper."}
        self._last_plan = plan

        def back():
            self._checking = False
            if not self._closed:
                self.page.call("plan", plan)
            return False
        GLib.idle_add(back)

    def _say(self, text: str) -> None:
        try:
            subprocess.Popen(["notify-send", "-a", "Atticus", "Atticus", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _grant(self, kind: str) -> None:
        if kind == "claude":
            plan = getattr(self, "_last_plan", {}) or {}
            if plan.get("need") == "upgrade":
                subprocess.Popen(["xdg-open", "https://claude.ai/upgrade"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            command = sign_in_command(plan)
            terminal = shutil.which("xdg-terminal-exec") or shutil.which("alacritty") or shutil.which("kitty") or shutil.which("foot")
            if not command:
                return self.open_settings()
            if not terminal:
                return self._say("Run in a terminal: " + command)
            args = [terminal] if terminal.endswith("xdg-terminal-exec") else [terminal, "-e"]
            subprocess.Popen([*args, "bash", "-lc", command + "; echo; read -n1 -rsp 'Done. Press a key to close.'"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == "connectors":
            subprocess.Popen(["xdg-open", CONNECTORS], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == "tools":
            cfg.set("agentUserTools", True)
        elif kind == "chrome":
            if not browser.command():
                self._say("Install a Chromium browser (Brave, Chromium or Chrome) so Atticus can read and click web pages.")
        elif kind in backend.PERM_CHECKS and not backend.PERM_CHECKS[kind]():
            self._say(backend.PERM_FIX[kind])

    def _message(self, b: dict) -> None:
        do = b.get("do")
        if do == "ready":
            return
        if do == "done":
            cfg.set("setupDone", True)
            return self.close()
        if do == "feedback":
            return self.open_settings()
        if do == "set" and isinstance(b.get("k"), str) and isinstance(b.get("v"), str):
            apply(b["k"], b["v"])
            return
        if do in ("setHotkey", "clearDictate"):
            threading.Thread(target=self._shortcut, args=(b.get("keys") if do == "setHotkey" else None,), daemon=True).start()
            return
        if do in ("enableAll", "kofi", "globeKey", "recordDictate", "cancelRecord"):
            if do == "enableAll":   # everything a task needs is already there or shown as missing; nothing is switched on elsewhere
                cfg.set("agentUserTools", True)
                self.page.call("walk", "")
            return
        if do in ("probe", "localServers", "connectors", "provider"):
            threading.Thread(target=self._slow, args=(b,), daemon=True).start()
            return
        if isinstance(b.get("id"), str):
            self._grant(b["id"])

    def _slow(self, b: dict) -> None:
        do = b["do"]
        if do == "connectors":
            name, value = "connectors", backend.brain_json("--connectors", timeout=30) or {"connected": [], "signin": []}
        elif do == "localServers":
            name, value = "local", backend.brain_json("--local-servers") or []
        elif do == "provider":
            if b.get("client") in ("claude", "devin", "pi"):
                cfg.set("client", b["client"])
                for k in ("apiBase", "apiKey", "apiModel"):
                    if isinstance(b.get(k), str):
                        cfg.set(k, b[k])
            self._checking = True
            return self._plan()
        else:   # probe: the models of a server typed in, not saved yet
            import os
            import sys
            from .bridge import ATTICUS_PY, _brain_env
            key = b.get("key") or (cfg.get("apiKey", "") if b.get("base") == cfg.get("apiBase") else "")
            try:
                out = subprocess.run([sys.executable, str(ATTICUS_PY), "--models"], capture_output=True, text=True, timeout=20,
                                     env={**_brain_env(), "ATTICUS_PROBE_BASE": str(b.get("base") or ""), "ATTICUS_PROBE_KEY": str(key or "")}).stdout
                value = json.loads(out or "[]")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                value = []
            name, value = "models", value if isinstance(value, list) else []
        GLib.idle_add(lambda: (self.page.call(name, value), False)[1])
