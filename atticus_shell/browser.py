"""Atticus's own browser window, driven through its page instead of the screen.

Reading a page as text with numbered controls, then clicking or filling one
by number, is quicker and surer than a picture and a mouse click. A Chromium
browser lets another program do that only on a profile that is not the
everyday one, so Atticus keeps a profile of its own (data dir below): the
user's everyday browser, its tabs and its sign-ins are never touched, and a
site that needs an account is signed into once in Atticus's window.

The browser is told to listen on a local port (the DevTools protocol) and
each command is one JSON message over a websocket, written here with the
standard library alone.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .settings import get as _setting

BROWSERS = ("brave", "chromium", "google-chrome-stable", "google-chrome", "brave-browser", "vivaldi-stable")


def port() -> int:
    try:
        return int(_setting("browserPort", 9247))
    except (TypeError, ValueError):
        return 9247


def profile() -> Path:
    return Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "browser"


def command() -> str | None:
    chosen = _setting("browser")
    if chosen and shutil.which(str(chosen)):
        return shutil.which(str(chosen))
    return next((shutil.which(b) for b in BROWSERS if shutil.which(b)), None)


def _http(path: str, method: str = "GET", timeout: float = 2.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port()}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")


def alive() -> bool:
    try:
        return isinstance(_http("/json/version", timeout=0.6), dict)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def ensure(wait: float = 12.0) -> str:
    """Make sure Atticus's browser is running. '' when it is, else why not."""
    if alive():
        return ""
    cmd = command()
    if not cmd:
        return "No Chromium browser is installed (Brave, Chromium or Chrome)."
    data = profile()
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        subprocess.Popen([cmd, f"--remote-debugging-port={port()}", "--remote-debugging-address=127.0.0.1",
                          f"--user-data-dir={data}", "--no-first-run", "--no-default-browser-check", "about:blank"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return f"Could not start the browser: {e}"
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if alive():
            return ""
        time.sleep(0.2)
    return "The browser started but does not answer."


def tabs() -> list[dict]:
    """The open pages, as the browser lists them (the one used last first)."""
    try:
        rows = _http("/json/list")
    except (OSError, ValueError, urllib.error.URLError):
        return []
    return [t for t in rows or [] if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def tab(identifier: str) -> dict | None:
    return next((t for t in tabs() if t.get("id") == identifier), None)


def open_tab(url: str) -> dict | None:
    try:
        made = _http("/json/new?" + urllib.parse.quote(url, safe=":/?&=%#+,;@!$'()*~-._"), method="PUT", timeout=5)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return made if isinstance(made, dict) and made.get("webSocketDebuggerUrl") else None


def show_tab(identifier: str) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port()}/json/activate/{identifier}", timeout=2).read()
        return True
    except (OSError, urllib.error.URLError):
        return False


# --- one message over a websocket --------------------------------------------
def _frame(payload: bytes) -> bytes:
    """A masked text frame, as a client must send."""
    head = bytearray([0x81])
    n = len(payload)
    if n < 126:
        head.append(0x80 | n)
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("the browser closed the connection")
        buf += chunk
    return bytes(buf)


def _message(sock: socket.socket) -> str:
    """The next whole text message (frames put back together; pings skipped)."""
    parts = bytearray()
    while True:
        b1, b2 = _read_exact(sock, 2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _read_exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _read_exact(sock, 8))[0]
        data = _read_exact(sock, n)
        op = b1 & 0x0F
        if op == 0x8:
            raise ConnectionError("the browser closed the connection")
        if op in (0x9, 0xA):
            continue
        parts += data
        if b1 & 0x80:
            return parts.decode("utf-8", "replace")


def call(ws_url: str, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
    """One DevTools command on one page; its result, or RuntimeError."""
    u = urllib.parse.urlsplit(ws_url)
    with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout) as sock:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += _read_exact(sock, 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("the browser refused the connection")
        sock.sendall(_frame(json.dumps({"id": 1, "method": method, "params": params or {}}).encode()))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            sock.settimeout(max(0.05, end - time.monotonic()))
            try:
                reply = json.loads(_message(sock))
            except socket.timeout:
                break
            if reply.get("id") == 1:
                if "error" in reply:
                    raise RuntimeError(str(reply["error"].get("message", "the page did not answer")))
                return reply.get("result", {})
    raise RuntimeError("the page did not answer in time")


def evaluate(ws_url: str, expression: str, timeout: float = 15.0) -> str:
    """Run one expression in the page; its value as text."""
    result = call(ws_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, timeout)
    if result.get("exceptionDetails"):
        detail = result["exceptionDetails"]
        raise RuntimeError(str((detail.get("exception") or {}).get("description") or detail.get("text") or "the page raised an error")[:300])
    value = (result.get("result") or {}).get("value")
    return value if isinstance(value, str) else json.dumps(value)
