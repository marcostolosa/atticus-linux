"""The Atticus overlay box.

A top-left, always-on-top surface that never takes the keyboard focus: a
wlr-layer-shell layer (gtk4-layer-shell) holding a WebKit view of ui/ui.html.
The brain's output is turned into calls on the page's `ui.*` functions.

The page talks back on two channels:
  - "h": content height (or [w,h] in dictation) -> resize the box
  - "c": a dict of UI events (send, copy, link, focus, fold, model, ...)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))
UI = RES / "ui"

BOX_W_DEFAULT = 400
BOX_H_DEFAULT = 80   # the page reports its real height as soon as it loads
MARGIN = 12


def _settings() -> dict:
    p = Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "settings.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _ui_language() -> str:
    s = _settings()
    lang = s.get("uiLanguage") or "auto"
    if lang and lang != "auto":
        return lang
    # No explicit choice: someone who pinned speech recognition to a language
    # wants the box in it too (a pt-BR speaker on an en_US system locale).
    asr = str(s.get("asrLanguage") or "").lower()[:2]
    if asr in ("pt", "it", "es", "fr", "de", "en"):
        return asr
    loc = (os.environ.get("LANG") or "en").split(".")[0].split("_")[0].lower()
    return loc if loc in ("en", "pt", "it", "es", "fr", "de") else "en"


def load_page(name: str) -> str:
    """A page as one document: orbs.js and i18n.js go in at their markers, the shared look (theme.css) after the page's own styles."""
    html = (UI / name).read_text(encoding="utf-8")
    orbs = _read(UI / "orbs.js")
    i18n = _read(UI / "i18n.js")
    lang = _ui_language()
    auto = _ui_language()  # resolve("auto") == resolved default
    header = f"window.atticusLanguage = {json.dumps(lang)}; window.atticusAutoLanguage = {json.dumps(auto)};\n"
    theme = "<style id=\"atticus-theme\">" + _read(UI / "theme.css") + "</style>"
    page = html.replace("/*ORBS*/", orbs).replace("/*I18N*/", header + i18n)
    return page.replace("</head>", theme + "</head>", 1) if "</head>" in page else theme + page


_WINDOW_CSS = False


def style_windows() -> None:
    """The three ordinary windows (Settings, Setup, Agents) share one background, the same as theme.css's --a-surface-solid."""
    global _WINDOW_CSS
    if _WINDOW_CSS:
        return
    _WINDOW_CSS = True
    css = Gtk.CssProvider()
    css.load_from_data(b"window.atticus-window { background: #1e1e20; }")
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


class Overlay:
    """One overlay box. Create inside a running Gtk.Application."""

    def __init__(self, app: Gtk.Application,
                 on_message: Optional[Callable[[dict], None]] = None,
                 corner: str = "top-left",
                 on_escape: Optional[Callable[[], None]] = None):
        self.on_message = on_message or (lambda b: None)
        self.on_escape = on_escape or (lambda: None)
        self.corner = corner
        self._ready = False
        self._pending: list[str] = []   # JS queued until the page loads

        self.window = Gtk.ApplicationWindow(application=app)
        self.window.set_decorated(False)

        # Transparent: the card draws its own rounded background and shadow.
        self.window.add_css_class("atticus-overlay")
        css = Gtk.CssProvider()
        css.load_from_data(
            b"window.atticus-overlay, window.atticus-overlay > * { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        ucm = WebKit.UserContentManager()
        for name in ("c", "h"):
            ucm.register_script_message_handler(name, None)
        ucm.connect("script-message-received::c", self._on_c)
        ucm.connect("script-message-received::h", self._on_h)

        self.web = WebKit.WebView(user_content_manager=ucm)
        bg = Gdk.RGBA()
        bg.parse("rgba(0,0,0,0)")   # a bare Gdk.RGBA() paints #1e1e1e here; the card draws its own glass
        self.web.set_background_color(bg)
        self.web.set_size_request(BOX_W_DEFAULT, BOX_H_DEFAULT)
        try:
            # Let localhost / file content and media autoplay behave for the UI.
            s = self.web.get_settings()
            s.set_enable_developer_extras(bool(os.environ.get("ATTICUS_DEVTOOLS")))
            s.set_media_playback_requires_user_gesture(False)
            if os.environ.get("ATTICUS_DEBUG"):
                s.set_enable_write_console_messages_to_stdout(True)
        except Exception:
            pass
        self.web.connect("load-changed", self._on_load)
        self.window.set_child(self.web)

        # Esc anywhere in the box dismisses it (keyboard is ON_DEMAND: arrives
        # once the surface has focus). The shell also hides it via control/cancel.
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.window.add_controller(key)

        self._init_layer_shell()
        self.load("ui.html")

    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            try:
                self.on_escape()
            except Exception:
                import traceback
                traceback.print_exc()
            return True
        return False

    # --- layer-shell placement ---------------------------------------------
    def _init_layer_shell(self):
        w = self.window
        LayerShell.init_for_window(w)
        LayerShell.set_namespace(w, "atticus")
        LayerShell.set_layer(w, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(w, LayerShell.KeyboardMode.ON_DEMAND)
        top = self.corner.startswith("top")
        right = self.corner.endswith("right")
        LayerShell.set_anchor(w, LayerShell.Edge.TOP, top)
        LayerShell.set_anchor(w, LayerShell.Edge.BOTTOM, not top)
        LayerShell.set_anchor(w, LayerShell.Edge.LEFT, not right)
        LayerShell.set_anchor(w, LayerShell.Edge.RIGHT, right)
        LayerShell.set_margin(w, LayerShell.Edge.TOP, MARGIN)
        LayerShell.set_margin(w, LayerShell.Edge.LEFT, MARGIN)
        LayerShell.set_margin(w, LayerShell.Edge.RIGHT, MARGIN)

    # --- page lifecycle -----------------------------------------------------
    def load(self, name: str):
        self._ready = False
        self.web.load_html(load_page(name), None)

    def _on_load(self, web, event):
        if os.environ.get("ATTICUS_DEBUG"):
            print(f"[overlay] load-changed: {event.value_nick}", flush=True)
        if event == WebKit.LoadEvent.FINISHED:
            # window.ui is defined by a deferred <script type="module"> that runs
            # just after FINISHED, so poll until it exists before flushing.
            self._await_ui()

    def _await_ui(self, tries: int = 0):
        def done(web, result, _):
            ok = False
            try:
                v = web.evaluate_javascript_finish(result)
                ok = (v is not None and v.to_string() == "true")
            except Exception:
                ok = False
            if ok:
                self._ready = True
                if os.environ.get("ATTICUS_DEBUG"):
                    print(f"[overlay] window.ui ready; flushing {len(self._pending)}", flush=True)
                pending, self._pending = self._pending, []
                for js in pending:
                    self._eval(js)
            elif tries < 100:
                GLib.timeout_add(30, lambda: (self._await_ui(tries + 1), False)[1])
        self.web.evaluate_javascript(
            "typeof window.ui === 'object' && typeof window.ui.listen === 'function'",
            -1, None, None, None, done, None)

    # --- shell -> page ------------------------------------------------------
    def call(self, fn: str, *args: Any):
        """Call window.ui.<fn>(...args) on the page, JSON-encoding arguments."""
        try:
            payload = json.dumps(list(args))
        except (TypeError, ValueError):
            return
        self._eval(f"window.ui && ui.{fn} && ui.{fn}(...{payload})")

    def eval_js(self, script: str):
        self._eval(script)

    def _eval(self, script: str):
        if not self._ready:
            self._pending.append(script)
            if os.environ.get("ATTICUS_DEBUG"):
                print(f"[overlay] queued (not ready): {script[:70]}", flush=True)
            return
        if os.environ.get("ATTICUS_DEBUG"):
            print(f"[overlay] eval: {script[:70]}", flush=True)
            self.web.evaluate_javascript(script, -1, None, None, None, self._eval_done, script)
        else:
            self.web.evaluate_javascript(script, -1, None, None, None, None, None)

    def _eval_done(self, web, result, script):
        try:
            web.evaluate_javascript_finish(result)
        except Exception as e:
            print(f"[overlay] eval ERROR on {script[:50]!r}: {e}", flush=True)

    # --- page -> shell ------------------------------------------------------
    def _value_to_py(self, value) -> Any:
        if hasattr(value, "get_js_value"):   # WebKit2 4.x compatibility
            value = value.get_js_value()
        try:
            return json.loads(value.to_json(0))
        except Exception:
            try:
                return value.to_string()
            except Exception:
                return None

    def _on_c(self, ucm, value):
        body = self._value_to_py(value)
        if isinstance(body, dict):
            try:
                self.on_message(body)
            except Exception:
                import traceback
                traceback.print_exc()

    def _on_h(self, ucm, value):
        body = self._value_to_py(value)
        if isinstance(body, list) and len(body) == 2:   # dictation pill: [w, h]
            self.resize(int(body[0]), int(body[1]))
        elif isinstance(body, (int, float)):
            self.resize(None, int(body))

    def resize(self, width: Optional[int], height: int):
        height = max(1, min(height, 1200))
        cur_w = self.web.get_size_request().width if hasattr(self.web, "get_size_request") else BOX_W_DEFAULT
        w = width if width else (cur_w if cur_w and cur_w > 0 else BOX_W_DEFAULT)
        self.web.set_size_request(w, height)

    # --- visibility ---------------------------------------------------------
    def present(self):
        self.window.present()

    def hide(self):
        self.window.set_visible(False)
