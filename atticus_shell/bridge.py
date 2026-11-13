"""Run the brain (atticus.py) and stream its tagged output into the overlay.

The macOS shell parsed atticus.py's stdout line protocol and drove the WKWebView.
This does the same for the GTK/WebKit overlay. The line tags (from atticus.py's
say()/print()):
    "· note"   live status
    "▸ step"   a step row
    "… text"   streamed answer, replaced by ✓
    "✓ answer" final answer  (also spoken)
    "! msg"    a short message / cancel
    "◇ {json}" a choice card
    "? q\tcmd\tretry[\t\t\tjson]"  a permission gate
    "i text"   a tip / "Learned:"
Most tags the page's ui.line() already dispatches by leading glyph, so we mostly
forward whole lines to ui.line and special-case the few the shell must act on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))
ATTICUS_PY = RES / "atticus.py"
# Single-file launcher the brain runs as $ATTICUS_APP (argv[0]); it execs the CLI.
NATIVE_LAUNCHER = RES / "bin" / "atticus-native"


def _brain_env() -> dict:
    env = dict(os.environ)
    # The brain invokes $ATTICUS_APP as one executable, so point it at the launcher.
    env["ATTICUS_APP"] = str(NATIVE_LAUNCHER)
    env["ATTICUS_PYTHON"] = sys.executable
    env.setdefault("ATTICUS_RESOURCES_DIR", str(RES))
    env.setdefault("ATTICUS_DIR", str(Path.home() / ".config/atticus"))
    # The launcher runs `python -m atticus_shell.cli`, so the package must resolve.
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(RES) + (os.pathsep + pp if pp else "")
    # bin/ holds the `open` shim (and launcher); it must win over anything else.
    env["PATH"] = str(RES / "bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    return env


class Bridge:
    """Owns the running brain process for one ask and fans its output to the UI."""

    def __init__(self, overlay, speak: Optional[Callable[[str], None]] = None,
                 on_done: Optional[Callable[[], None]] = None):
        self.overlay = overlay
        self.speak = speak or (lambda text: None)
        self.on_done = on_done or (lambda: None)
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # --- public -------------------------------------------------------------
    def ask(self, task: str, front_app: str = ""):
        """Start a new ask. Cancels any in-flight one."""
        task = (task or "").strip()
        if not task:
            return
        self.cancel()
        env = _brain_env()
        if front_app:
            env["ATTICUS_FRONT"] = front_app
        cmd = [sys.executable, str(ATTICUS_PY), task]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env, cwd=str(RES))
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._drain_err, args=(self.proc,), daemon=True).start()

    def cancel(self):
        with self._lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # --- internals ----------------------------------------------------------
    def _pump(self, proc: subprocess.Popen):
        answer_spoken = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            GLib.idle_add(self._dispatch, line, answer_spoken)
        proc.wait()
        GLib.idle_add(self._finished, proc, answer_spoken)

    def _drain_err(self, proc: subprocess.Popen):
        if not proc.stderr:
            return
        for raw in proc.stderr:
            sys.stderr.write("[brain] " + raw)

    def _dispatch(self, line: str, answer_spoken: list) -> bool:
        tag = line[:1]
        rest = line[2:] if len(line) > 1 and line[1] == " " else line[1:]
        try:
            if tag == "!":
                self.overlay.call("msg", rest)
            elif tag == "◇":
                try:
                    card = json.loads(rest)
                    self.overlay.call("pick", card)
                except ValueError:
                    pass
            elif tag == "?":
                parts = rest.split("\t")
                q = parts[0] if parts else ""
                cmd = parts[1] if len(parts) > 1 else ""
                retry = parts[2] if len(parts) > 2 else ""
                details = parts[5] if len(parts) > 5 else ""
                self.overlay.call("ask", [q, cmd, retry, details])
            elif tag in ("▣", "@", "▤", "▥", "⤴", "⇢", "⚙", "⤴"):
                # mini-screen / agents / movable / settings: not in v1 core — ignore.
                pass
            elif tag == "✓":
                self.overlay.call("line", line)
                answer_spoken.append(rest)
                self.speak(rest)
            else:
                # ·  ▸  …  i  ✎  and anything else: the page dispatches by glyph.
                self.overlay.call("line", line)
        except Exception:
            import traceback
            traceback.print_exc()
        return False

    def _finished(self, proc, answer_spoken) -> bool:
        if proc is self.proc:
            self.proc = None
        if proc.returncode and not answer_spoken:
            self.overlay.call("msg", "")
        try:
            self.on_done()
        except Exception:
            import traceback
            traceback.print_exc()
        return False
