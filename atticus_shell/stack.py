"""Where the boxes sit: top centre, just under the bar's clock, in one column.

The assistant's box, the agent shelf and the meeting panel all hang from the
top edge, centred. They would cover each other there, so each one reports its
height and whether it is showing, and the ones showing are stacked from the top
in a fixed order (meeting, box, shelf). A layer surface with no exclusive zone
is placed below the bar by the compositor, so the first margin is only a gap.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

ORDER = ("meeting", "box", "shelf")
TOP_GAP = 8
BETWEEN = 8

_items: dict[str, dict] = {}


def anchor(window) -> None:
    """Hang a layer window from the top edge, centred (no left or right anchor)."""
    LayerShell.set_anchor(window, LayerShell.Edge.TOP, True)
    for edge in (LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT, LayerShell.Edge.BOTTOM):
        LayerShell.set_anchor(window, edge, False)
    LayerShell.set_margin(window, LayerShell.Edge.TOP, TOP_GAP)


def update(name: str, window=None, height: int | None = None, visible: bool | None = None) -> None:
    item = _items.setdefault(name, {"window": None, "height": 0, "visible": False})
    if window is not None:
        item["window"] = window
    if height is not None:
        item["height"] = int(height)
    if visible is not None:
        item["visible"] = bool(visible)
    layout()


def margins() -> dict[str, int]:
    """The top margin each showing box gets."""
    out, y = {}, TOP_GAP
    for name in ORDER:
        item = _items.get(name)
        if item and item["window"] is not None and item["visible"] and item["height"] > 0:
            out[name] = y
            y += item["height"] + BETWEEN
    return out


def layout() -> None:
    for name, top in margins().items():
        window = _items[name]["window"]
        try:
            LayerShell.set_margin(window, LayerShell.Edge.TOP, top)
        except Exception:
            pass
