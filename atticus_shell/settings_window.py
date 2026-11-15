"""The Settings window: hosts ui/settings.html, the same way the main
overlay (atticus_shell/overlay.py) hosts ui/ui.html. The page posts
{do: "..."} messages on channel "st"; atticus_shell/settings_backend.py
decides what each one does and this file only owns the window, the
WebKit/postMessage plumbing and the threading (nothing here blocks the GTK
thread: every action runs on its own daemon thread, the reply crosses back
with GLib.idle_add — see AGENTS.md rule 6).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from gi.repository import GLib, Gtk, WebKit

from . import settings_backend as backend
from .overlay import load_page

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))


class SettingsWindow:
    def __init__(self, app: Gtk.Application, on_quit=None):
        self.app = app
        self.on_quit = on_quit or (lambda: app.quit())
        self.window = Gtk.ApplicationWindow(application=app, title="Atticus Settings")
        self.window.set_default_size(920, 640)
        self.window.connect("close-request", self._on_close)
        self._ready = False
        self._pending: list[str] = []

        # The UserContentManager must carry the handler registration *before*
        # the WebView is constructed with it — registering on the one that
        # web.get_user_content_manager() hands back afterwards (as a plain
        # WebKit.WebView() would have created on its own) silently never
        # delivers postMessage calls on this WebKitGTK (confirmed live:
        # window.webkit.messageHandlers.st.postMessage() ran, with no JS
        # error, but nothing reached Python). Mirrors overlay.py exactly.
        ucm = WebKit.UserContentManager()
        ucm.register_script_message_handler("st", None)
        ucm.connect("script-message-received::st", self._on_message)
        self.web = WebKit.WebView(user_content_manager=ucm)
        try:
            self.web.get_settings().set_enable_developer_extras(bool(os.environ.get("ATTICUS_DEBUG")))
        except Exception:
            pass
        self.web.connect("load-changed", self._on_load)
        self.window.set_child(self.web)
        backend.push = lambda name, payload: GLib.idle_add(self.call, name, payload)
        self.web.load_html(load_page("settings.html"), None)

    # --- lifecycle -------------------------------------------------------
    def present(self):
        self.window.set_visible(True)
        self.window.present()

    def _on_close(self, *_a):
        self.window.set_visible(False)
        return True   # the process (and any in-flight action thread) stays up; reopening shows the same window

    def _on_load(self, _web, event):
        if event == WebKit.LoadEvent.FINISHED:
            self._await_ui()

    def _await_ui(self, tries: int = 0):
        def done(web, result, _):
            try:
                v = web.evaluate_javascript_finish(result)
                ok = v is not None and v.to_string() == "true"
            except Exception:
                ok = False
            if ok:
                self._ready = True
                pending, self._pending = self._pending, []
                for js in pending:
                    self._eval(js)
                threading.Thread(target=self._initial_load, daemon=True).start()
            elif tries < 100:
                GLib.timeout_add(30, lambda: (self._await_ui(tries + 1), False)[1])
        self.web.evaluate_javascript("typeof window.st === 'object' && typeof window.st.load === 'function'",
                                     -1, None, None, None, done, None)

    def _eval(self, js: str):
        if not self._ready:
            self._pending.append(js)
            if os.environ.get("ATTICUS_DEBUG"):
                print(f"[settings] queued (not ready): {js[:90]}", flush=True)
            return
        if os.environ.get("ATTICUS_DEBUG"):
            print(f"[settings] eval: {js[:90]}", flush=True)
            self.web.evaluate_javascript(js, -1, None, None, None, self._eval_done, js)
        else:
            self.web.evaluate_javascript(js, -1, None, None, None, None, None)

    def _eval_done(self, web, result, js):
        try:
            web.evaluate_javascript_finish(result)
        except Exception as e:
            print(f"[settings] eval ERROR on {js[:90]!r}: {e}", flush=True)

    def call(self, fn: str, payload: Any):
        try:
            encoded = json.dumps(payload)
        except (TypeError, ValueError):
            return
        self._eval(f"window.st && st.{fn} && st.{fn}({encoded})")

    # --- page -> shell ----------------------------------------------------
    def _on_message(self, _manager, value):
        try:
            body = json.loads(value.to_string())
        except (ValueError, AttributeError):
            return
        if os.environ.get("ATTICUS_DEBUG"):
            print(f"[settings] message: {body}", flush=True)
        threading.Thread(target=self._handle, args=(body,), daemon=True).start()

    def _handle(self, body: dict):
        try:
            result = backend.dispatch(body)
        except Exception:
            import traceback
            traceback.print_exc()
            return
        if not result:
            return
        name, payload = result
        if name == "__quit__":
            GLib.idle_add(self.on_quit)
            return
        GLib.idle_add(self.call, name, payload)

    def _initial_load(self):
        state = backend.initial_state()
        GLib.idle_add(self.call, "load", state)
        GLib.idle_add(self.call, "mics", backend.mic_list())
        GLib.idle_add(self.call, "perms", backend.perms_payload())
        GLib.idle_add(self.call, "whisper", backend.speech_models.status())
