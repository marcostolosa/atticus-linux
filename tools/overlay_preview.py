"""Draws the overlay in each of its states and saves a picture of each.

    LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so .venv/bin/python tools/overlay_preview.py OUT_DIR

A second overlay is opened in its own process (the running one is left alone),
fed the same calls the shell makes, and the top centre of the screen is
captured with grim after each. The pictures show whatever is behind the box,
so run it over something ordinary.
"""
from __future__ import annotations

import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from atticus_shell.overlay import Overlay  # noqa: E402

LONG = ("Claro. O comando `ls -la` lista todos os arquivos da pasta atual, inclusive os ocultos, com permissões, dono e tamanho. "
        "Se quiser só os nomes, use `ls`. Para ordenar por data, acrescente `-t`.")


def main(out: str) -> None:
    os.makedirs(out, exist_ok=True)
    app = Gtk.Application(application_id="com.atticus.preview", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def go(a):
        box = Overlay(a, on_message=lambda b: None)
        box.present()
        steps: list[tuple[int, object, str]] = [
            (1200, lambda: (box.call("reset"), box.call("visible", True), box.call("listen", "hold")), "listening"),
            (2800, lambda: (box.call("level", 0.4), box.call("words", "que horas são em Tóquio")), "hearing"),
            (5800, lambda: box.call("think"), "thinking"),
            (5600, lambda: box.call("line", "▸ Searching the web"), "working"),
            (7200, lambda: box.call("line", "✓ São 21h42 em Tóquio, onze horas à frente de Brasília."), "answer"),
            (9200, lambda: (box.call("reset"), box.call("listen", True), box.call("words", "liste os arquivos"), box.call("think"),
                            box.call("line", "✓ " + LONG)), "long-answer"),
            (11600, lambda: (box.call("reset"), box.call("listen", True), box.call("words", "apague a pasta de testes"), box.call("think"),
                            box.call("ask", ["Apagar a pasta de testes?", "outside rmdir ~/testes", False, ""])), "ask"),
            (14000, lambda: (box.call("reset"), box.call("listen", "dict")), "dictation"),
        ]
        t = 0
        for at, fn, name in steps:
            GLib.timeout_add(at, lambda fn=fn: (fn(), False)[1])
            GLib.timeout_add(at + 1100, lambda name=name: (subprocess.run(["grim", "-g", "380,0 560x420", os.path.join(out, name + ".png")]), False)[1])
            t = at + 1100
        GLib.timeout_add(t + 400, lambda: (app.quit(), False)[1])

    app.connect("activate", go)
    app.run([])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "overlay-preview")
