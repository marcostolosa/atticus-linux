"""Control socket — how the global hotkey reaches the running app.

Wayland/Hyprland has no app-level global-hotkey API, so the idiomatic path is a
compositor keybind that runs a command. Atticus runs a small Unix-socket server;
Hyprland binds send one-word commands to it:

    bind  = CTRL ALT, SPACE, exec, atticus-ctl press     # key down: start listening
    bindr = CTRL ALT, SPACE, exec, atticus-ctl release   # key up:   send
    bind  = , ESCAPE, exec, atticus-ctl cancel           # (optional, scoped)

This gives true hold-to-talk through the compositor. Commands:
    press | release | toggle | cancel | ask <text> | ping
    dictate | dictate-press | dictate-release | dictate-text <text>   (typed at the cursor)
    settings | quit
    hide-for-capture | show-after-capture   (cli.py, around a --look/--text grab)
"""
from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional


def socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".config/atticus")
    return os.path.join(base, "atticus.sock")


class ControlServer:
    """Listens for hotkey commands and hands each to `handler` on the GTK thread."""

    def __init__(self, handler: Callable[[str, str], None]):
        self.handler = handler
        self.path = socket_path()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self._sock.listen(8)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        from gi.repository import GLib
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2)   # a client that connects and never writes must not stall the hotkey
                    data = conn.recv(65536).decode("utf-8", "replace").strip()
                except OSError:
                    continue
                if not data:
                    continue
                cmd, _, arg = data.partition(" ")
                GLib.idle_add(self._deliver, cmd.strip(), arg.strip())

    def _deliver(self, cmd: str, arg: str) -> bool:
        try:
            self.handler(cmd, arg)
        except Exception:
            import traceback
            traceback.print_exc()
        return False

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def send(cmd: str, retries: int = 6, delay: float = 0.15) -> bool:
    """Client side: deliver one command to a running Atticus. True on success.

    Retries briefly so a keybind that fires during startup still lands once the
    socket is up.
    """
    import time
    path = socket_path()
    for attempt in range(retries):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(2)
            s.connect(path)
            s.sendall(cmd.encode("utf-8"))
            return True
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay)
        finally:
            s.close()
    return False
