"""Marks drawn over the desktop: "where do I click", or an idea explained as a diagram.

The brain sends one guide (`✎ {"steps": [...]}`): each step is a few shapes
and a sentence. Every monitor gets a see-through layer above everything else
that takes no input, so the desktop underneath is used as usual; only the
caption (its Back / Next / close buttons) can be clicked.

Two kinds of guide, as the brain marks them:
  teaching   an explanation built step by step: earlier steps stay, dimmed,
             and the guide stays until closed
  locating   "click here": one step at a time; a step that waits moves on when
             the user clicks inside its place, and the guide leaves when a
             step's time runs out

A click is seen by reading the mouse buttons from /dev/input, which needs the
user to be in the `input` group (Atticus does not ask for it). The layer
itself cannot see it: a surface that takes the click would also keep it from
the application underneath. Without that access the Next button, or saying
"next", moves the guide on.

Coordinates are the desktop's own (logical pixels, origin at the top-left of
the layout), the same ones `hyprctl monitors` and the brain's screens() use.
"""
from __future__ import annotations

import glob
import json
import math
import os
import select
import struct
import subprocess
import threading
import time

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

COLORS = {"red": (1.0, 0.27, 0.23), "blue": (0.04, 0.52, 1.0), "green": (0.19, 0.82, 0.35),
          "yellow": (1.0, 0.84, 0.04), "white": (1.0, 1.0, 1.0)}
LABEL_PX = {"s": 13, "m": 16, "l": 20}
TEXT_PX = {"s": 16, "m": 22, "l": 30}
WORDS = {"pt": ("Voltar", "Próximo", "Concluir", "Fechar"), "en": ("Back", "Next", "Done", "Close")}
EVENT = struct.Struct("llHHi")   # one input event: seconds, microseconds, type, code, value
EV_KEY, BTN_LEFT = 1, 0x110


def smooth(points: list[list[float]]) -> list[tuple]:
    """A curve through every point (Catmull-Rom) as cubic segments:
    [(c1x, c1y, c2x, c2y, x, y), ...] starting from points[0]."""
    out = []
    for i in range(len(points) - 1):
        p0, p1, p2 = points[max(i - 1, 0)], points[i], points[i + 1]
        p3 = points[min(i + 2, len(points) - 1)]
        out.append((p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6,
                    p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6, p2[0], p2[1]))
    return out


def bounds(shapes: list[dict]) -> tuple[float, float, float, float] | None:
    """The box around a step's marks, to find its monitor and keep the caption off it."""
    xs, ys = [], []
    for s in shapes:
        if s.get("rect"):
            x, y, w, h = s["rect"]
            xs += [x, x + w]; ys += [y, y + h]
        if s.get("at"):
            xs.append(s["at"][0]); ys.append(s["at"][1])
        for p in s.get("points") or []:
            xs.append(p[0]); ys.append(p[1])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def plain_math(text: str) -> str:
    """Formula marks the layer cannot typeset are taken off, leaving the
    formula readable as written."""
    return text.replace("$$", "").replace("$", "").replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")


def paint(cr, shapes: list[dict], ox: float, oy: float, dim: bool = False, phase: float = 0.0) -> None:
    """Draw shapes on a cairo context whose origin is the monitor's corner."""
    alpha = 0.45 if dim else 1.0

    def stroke(color, width=3.0):
        cr.set_line_cap(cairo.LINE_CAP_ROUND); cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_source_rgba(0, 0, 0, 0.35 * alpha); cr.set_line_width(width + 2.5); cr.stroke_preserve()   # a dark edge: the mark reads on any background
        cr.set_source_rgba(*color, alpha); cr.set_line_width(width); cr.stroke()

    def words(text, px, bold=False, width=None):
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription("sans")
        font.set_absolute_size(px * Pango.SCALE)
        if bold: font.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(font)
        if width:
            layout.set_width(int(width * Pango.SCALE)); layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_text(plain_math(text), -1)
        return layout

    for s in shapes:
        kind, color = s.get("kind"), COLORS.get(s.get("color", "red"), COLORS["red"])
        cr.new_path()
        if kind in ("rect", "circle") and s.get("rect"):
            x, y, w, h = s["rect"]; x -= ox; y -= oy
            if kind == "rect":
                r = min(7.0, w / 2, h / 2)
                cr.new_sub_path()
                cr.arc(x + w - r, y + r, r, -math.pi / 2, 0); cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
                cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi); cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
                cr.close_path()
            else:
                cr.save(); cr.translate(x + w / 2, y + h / 2); cr.scale(max(w, 1) / 2, max(h, 1) / 2)
                cr.arc(0, 0, 1, 0, 2 * math.pi); cr.restore()
            if s.get("fill"):
                cr.set_source_rgba(*color, 0.2 * alpha); cr.fill_preserve()
            stroke(color)
        elif kind in ("line", "arrow", "path") and s.get("points"):
            pts = [[p[0] - ox, p[1] - oy] for p in s["points"]]
            cr.move_to(*pts[0])
            if kind == "path" and s.get("curve"):
                for i in range(1, len(pts) - 2, 3):
                    cr.curve_to(*pts[i], *pts[i + 1], *pts[i + 2])
            elif kind == "path" and s.get("smooth") and len(pts) > 2:
                for seg in smooth(pts):
                    cr.curve_to(*seg)
            else:
                for p in pts[1:]:
                    cr.line_to(*p)
            if kind == "path" and s.get("fill"):
                cr.close_path(); cr.set_source_rgba(*color, 0.2 * alpha); cr.fill_preserve()
            stroke(color)
            if kind == "arrow":
                (x1, y1), (x2, y2) = pts[0], pts[1]
                a, size = math.atan2(y2 - y1, x2 - x1), 14.0
                cr.move_to(x2 - size * math.cos(a - 0.45), y2 - size * math.sin(a - 0.45)); cr.line_to(x2, y2)
                cr.line_to(x2 - size * math.cos(a + 0.45), y2 - size * math.sin(a + 0.45))
                stroke(color)
        elif kind == "pointer" and s.get("at"):
            x, y = s["at"][0] - ox, s["at"][1] - oy
            grow = 1 + 0.25 * math.sin(phase * 2 * math.pi)   # breathes, so the eye finds it
            cr.arc(x, y, 20 * grow, 0, 2 * math.pi); stroke(color, 3.5)
            cr.arc(x, y, 4.5, 0, 2 * math.pi); cr.set_source_rgba(*color, alpha); cr.fill()
        elif kind == "label" and s.get("at") and s.get("text"):
            x, y = s["at"][0] - ox, s["at"][1] - oy
            layout = words(s["text"], LABEL_PX.get(s.get("size", "m"), 16), bold=True, width=360)
            w, h = layout.get_pixel_size()
            px, py = 9, 5
            cr.new_sub_path()
            r = min(9.0, (h + 2 * py) / 2)
            bx, by, bw, bh = x, y, w + 2 * px, h + 2 * py
            cr.arc(bx + bw - r, by + r, r, -math.pi / 2, 0); cr.arc(bx + bw - r, by + bh - r, r, 0, math.pi / 2)
            cr.arc(bx + r, by + bh - r, r, math.pi / 2, math.pi); cr.arc(bx + r, by + r, r, math.pi, 1.5 * math.pi)
            cr.close_path()
            cr.set_source_rgba(*color, 0.95 * alpha); cr.fill()
            dark = s.get("color") in ("yellow", "white")
            cr.set_source_rgba(*((0.07, 0.07, 0.07) if dark else (1, 1, 1)), alpha)
            cr.move_to(bx + px, by + py); PangoCairo.show_layout(cr, layout)
        elif kind == "text" and s.get("at") and s.get("text"):
            x, y = s["at"][0] - ox, s["at"][1] - oy
            layout = words(s["text"], TEXT_PX.get(s.get("size", "m"), 22), bold=True, width=620)
            cr.move_to(x, y); PangoCairo.layout_path(cr, layout)
            cr.set_source_rgba(0, 0, 0, 0.6 * alpha); cr.set_line_width(4); cr.set_line_join(cairo.LINE_JOIN_ROUND); cr.stroke()
            cr.set_source_rgba(*color, alpha); cr.move_to(x, y); PangoCairo.show_layout(cr, layout)


class Clicks:
    """Left-button presses, from every mouse-like device, on a thread."""

    def __init__(self, on_click):
        self.on_click, self._stop = on_click, threading.Event()
        self._files = []
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                self._files.append(open(path, "rb", buffering=0))
            except OSError:
                continue
        if self._files:
            threading.Thread(target=self._run, daemon=True).start()

    def available(self) -> bool:
        return bool(self._files)

    def _run(self):
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(self._files, [], [], 0.5)
            except (OSError, ValueError):
                break
            for f in ready:
                try:
                    data = f.read(EVENT.size * 32)
                except OSError:
                    data = b""
                for at in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, kind, code, value = EVENT.unpack_from(data, at)
                    if kind == EV_KEY and code == BTN_LEFT and value == 1:
                        GLib.idle_add(self.on_click)
        for f in self._files:
            try: f.close()
            except OSError: pass

    def stop(self):
        self._stop.set()


def cursor() -> tuple[float, float] | None:
    try:
        out = subprocess.run(["hyprctl", "cursorpos"], capture_output=True, text=True, timeout=1).stdout
        x, y = out.replace(",", " ").split()[:2]
        return float(x), float(y)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class ScreenDrawing:
    def __init__(self, app, speak=None, language=lambda: "en"):
        self.app, self.speak, self.language = app, speak or (lambda text: None), language
        self.request, self.index = None, 0
        self.layers: list[dict] = []     # one per monitor: window, area, caption widgets, geometry
        self._clicks, self._tick, self._deadline, self._ready = None, 0, 0.0, 0.0
        self._phase, self._hidden, self._point_source = 0.0, False, 0
        css = Gtk.CssProvider()
        css.load_from_data(b"""
window.atticus-draw, window.atticus-draw > * { background: transparent; }
.atticus-caption { background: rgba(22,22,24,0.94); color: #fff; border-radius: 14px; padding: 12px 14px;
                   border: 1px solid rgba(255,255,255,0.14); }
.atticus-caption label { color: #fff; font-size: 15px; }
.atticus-caption .count { color: rgba(255,255,255,0.55); font-size: 12px; }
.atticus-caption button { background: rgba(255,255,255,0.12); color: #fff; border: none; border-radius: 8px;
                          padding: 4px 12px; min-height: 0; box-shadow: none; }
.atticus-caption button:hover { background: rgba(255,255,255,0.22); }
.atticus-caption button.go { background: #0a84ff; }
""")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # --- what the shell calls ------------------------------------------------
    @property
    def active(self) -> bool:
        return self.request is not None

    def show(self, request: dict) -> str:
        """Put a guide on screen. 'shown', or why not (the brain reads it)."""
        steps = request.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) and isinstance(s.get("shapes"), list) for s in steps):
            return "Invalid drawing"
        self.close()
        self.request, self.index = request, 0
        if not self._open_layers():
            self.request = None
            return "No display to draw on"
        if any(s.get("wait") and s.get("hit") for s in steps):
            self._clicks = Clicks(self._clicked)
        self._tick = GLib.timeout_add(50, self._on_tick)
        self._show_step()
        return "shown"

    def point(self, x: float, y: float, hold: float = 6.0) -> None:
        """A ring on one spot for a few seconds: "it is here"."""
        if self.active and not self.request.get("_point"):
            return   # a guide is up: it already says where
        self.close()
        self.request, self.index = {"steps": [{"shapes": [{"kind": "pointer", "at": [x, y], "color": "blue"}]}], "teaching": True, "_point": True}, 0
        if not self._open_layers():
            self.request = None
            return
        self._tick = GLib.timeout_add(50, self._on_tick)
        self._show_step()
        self._point_source = GLib.timeout_add(int(hold * 1000), self._point_over)

    def _point_over(self):
        self._point_source = 0
        if self.active and self.request.get("_point"):
            self.close()
        return False

    def next(self) -> bool:
        """Next step; on the last one the guide is done. False when nothing was up."""
        if not self.active:
            return False
        if self.index + 1 < len(self.request["steps"]):
            self.index += 1
            self._show_step()
        else:
            self.close()
        return True

    def back(self) -> bool:
        if not self.active or self.index == 0:
            return False
        self.index -= 1
        self._show_step()
        return True

    def close(self) -> bool:
        was = self.active
        self.request = None
        for source in ("_tick", "_point_source"):
            if getattr(self, source):
                GLib.source_remove(getattr(self, source)); setattr(self, source, 0)
        if self._clicks:
            self._clicks.stop(); self._clicks = None
        for layer in self.layers:
            layer["window"].destroy()
        self.layers = []
        return was

    def set_hidden(self, hidden: bool) -> None:
        """Out of the way while the screen is photographed, so a picture of
        the screen never contains the guide."""
        self._hidden = hidden
        for layer in self.layers:
            layer["window"].set_visible(not hidden)

    # --- internals -----------------------------------------------------------
    def _open_layers(self) -> bool:
        monitors = Gdk.Display.get_default().get_monitors()
        for i in range(monitors.get_n_items()):
            mon = monitors.get_item(i)
            geo = mon.get_geometry()
            win = Gtk.Window(application=self.app)
            win.set_decorated(False)
            win.add_css_class("atticus-draw")
            LayerShell.init_for_window(win)
            LayerShell.set_namespace(win, "atticus-draw")
            LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
            LayerShell.set_monitor(win, mon)
            LayerShell.set_exclusive_zone(win, -1)   # over the bar too: the picture's pixels include it
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(win, edge, True)
            area = Gtk.DrawingArea()
            layer = {"window": win, "area": area, "x": geo.x, "y": geo.y, "w": geo.width, "h": geo.height}
            area.set_draw_func(self._draw, layer)
            over = Gtk.Overlay()
            over.set_child(area)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.add_css_class("atticus-caption")
            box.set_halign(Gtk.Align.CENTER); box.set_valign(Gtk.Align.END); box.set_margin_bottom(56)
            text = Gtk.Label(wrap=True, xalign=0, max_width_chars=58)
            row = Gtk.Box(spacing=8)
            count = Gtk.Label(hexpand=True, xalign=0)
            count.add_css_class("count")
            back, go, shut = Gtk.Button(), Gtk.Button(), Gtk.Button(label="✕")
            go.add_css_class("go")
            back.connect("clicked", lambda *_: self.back())
            go.connect("clicked", lambda *_: self.next())
            shut.connect("clicked", lambda *_: self.close())
            for w in (count, back, go, shut):
                row.append(w)
            box.append(text); box.append(row)
            box.set_visible(False)
            over.add_overlay(box)
            win.set_child(over)
            layer.update(caption=box, text=text, count=count, back=back, go=go, shut=shut)
            win.connect("map", lambda *_a, l=layer: self._input_region(l))
            win.present()
            self.layers.append(layer)
        return bool(self.layers)

    def _input_region(self, layer: dict) -> bool:
        """Only the caption takes the mouse; everything else goes through to
        the desktop."""
        surface = layer["window"].get_surface()
        if surface is None:
            return False
        region = cairo.Region()
        box = layer["caption"]
        if box.get_visible():
            ok, rect = box.compute_bounds(layer["window"])
            if ok and rect.get_width() > 1:
                region = cairo.Region(cairo.RectangleInt(int(rect.get_x()), int(rect.get_y()),
                                                         int(math.ceil(rect.get_width())), int(math.ceil(rect.get_height()))))
        surface.set_input_region(region)
        return False

    def _home(self, shapes: list[dict]) -> dict:
        """The monitor a step is on: where its marks are, else where the pointer is."""
        box = bounds(shapes)
        spot = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) if box else cursor()
        if spot:
            for layer in self.layers:
                if layer["x"] <= spot[0] < layer["x"] + layer["w"] and layer["y"] <= spot[1] < layer["y"] + layer["h"]:
                    return layer
        return self.layers[0]

    def _show_step(self) -> None:
        r, i = self.request, self.index
        step, total = r["steps"][i], len(r["steps"])
        say = str(step.get("say") or "")
        home = self._home(step["shapes"])
        back, nxt, done, shut = WORDS["pt" if str(self.language()).startswith("pt") else "en"]
        for layer in self.layers:
            show = layer is home and bool(say or total > 1) and not r.get("_point")
            layer["caption"].set_visible(show)
            if show:
                layer["text"].set_text(say); layer["text"].set_visible(bool(say))
                layer["count"].set_text(f"{i + 1} / {total}" if total > 1 else "")
                layer["back"].set_label(back); layer["back"].set_visible(i > 0)
                layer["go"].set_label(nxt if i + 1 < total else done)
                layer["shut"].set_tooltip_text(shut)
                marks = bounds(step["shapes"])   # the caption moves to the top when the marks are at the bottom
                low = bool(marks) and marks[3] - layer["y"] > layer["h"] * 0.62
                layer["caption"].set_valign(Gtk.Align.START if low else Gtk.Align.END)
                layer["caption"].set_margin_top(56 if low else 0); layer["caption"].set_margin_bottom(0 if low else 56)
            layer["area"].queue_draw()
            GLib.timeout_add(60, self._input_region, layer)   # after the caption has its new size
        now = time.monotonic()
        self._ready = now + 0.35   # the click that started this step is not the click it waits for
        self._deadline = 0.0 if r.get("teaching") else now + float(step.get("timeout", 60))
        if say:
            try: self.speak(say)
            except Exception: pass

    def _draw(self, area, cr, width, height, layer) -> None:
        r = self.request
        if not r or self._hidden:
            return
        steps, i = r["steps"], self.index
        if r.get("teaching"):
            for earlier in steps[:i]:
                paint(cr, earlier["shapes"], layer["x"], layer["y"], dim=True)
        paint(cr, steps[i]["shapes"], layer["x"], layer["y"], phase=self._phase)

    def _on_tick(self) -> bool:
        if not self.active:
            self._tick = 0
            return False
        if self._deadline and time.monotonic() > self._deadline:
            self._tick = 0
            self.close()
            return False
        if any(s.get("kind") == "pointer" for s in self.request["steps"][self.index]["shapes"]):
            self._phase = (time.monotonic() % 1.4) / 1.4
            for layer in self.layers:
                layer["area"].queue_draw()
        return True

    def _clicked(self) -> bool:
        """A left click somewhere: does it land in the place this step waits on?"""
        if not self.active or time.monotonic() < self._ready:
            return False
        step = self.request["steps"][self.index]
        hit = step.get("hit")
        if not step.get("wait") or not hit:
            return False
        at = cursor()
        if at and hit[0] <= at[0] <= hit[0] + hit[2] and hit[1] <= at[1] <= hit[1] + hit[3]:
            GLib.timeout_add(350, lambda: self.next() and False)   # the application answers the click first
        return False


def preview(request: dict, width: int, height: int, path: str, step: int | None = None) -> None:
    """The guide as a picture file, with no desktop: for tests and for seeing
    what the model asked for."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.16, 0.17, 0.2); cr.paint()
    steps = request["steps"]
    last = len(steps) - 1 if step is None else step
    for n, s in enumerate(steps[:last + 1]):
        if n == last or request.get("teaching"):
            paint(cr, s["shapes"], 0, 0, dim=n < last)
    surface.write_to_png(path)


if __name__ == "__main__":   # python -m atticus_shell.drawing '<the ✎ JSON>' out.png
    import sys
    preview(json.loads(sys.argv[1]), 1280, 800, sys.argv[2])
