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
    # Tell the brain an overlay is listening on stdout, so relay() emits the
    # choice ("◇"), permission ("?") and setting ("⚙") cards to us instead of
    # the macOS-only AppleScript dialogs (which are no-ops on Linux, so a pick
    # would silently resolve to "none" and the brain would wrongly report that
    # the user did not choose). Background-agent hand-off is not wired in this
    # shell, so force main-window delivery to keep the model from moving a task
    # to an agent that never runs.
    env["ATTICUS_UI"] = "1"
    env["ATTICUS_MAIN_ONLY"] = "1"
    # The launcher runs `python -m atticus_shell.cli`, so the package must resolve.
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(RES) + (os.pathsep + pp if pp else "")
    # bin/ holds the `open` shim (and launcher); it must win over anything else.
    env["PATH"] = str(RES / "bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    return env


class Bridge:
    """Owns the running brain process for one ask and fans its output to the UI."""

    def __init__(self, overlay, speak: Optional[Callable[[str], None]] = None,
                 on_done: Optional[Callable[[], None]] = None,
                 on_prompt: Optional[Callable[[str, dict], None]] = None):
        self.overlay = overlay
        self.speak = speak or (lambda text: None)
        self.on_done = on_done or (lambda: None)
        # Called on the GLib main thread when an interactive card appears:
        #   on_prompt("pick", {"q": str, "options": [label, ...]})
        #   on_prompt("perm", {"q": str, "cmd": str, "retry": bool})
        self.on_prompt = on_prompt or (lambda kind, data: None)
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        # Answer files for the card currently shown, so the shell can write the
        # user's choice back to the waiting brain tool (click or voice).
        self.pending_pick: Optional[str] = None            # answer-file path
        self.pending_perm: Optional[dict] = None           # {"a": path, "retry": bool}
        self.pick_options: list = []                       # labels, for voice matching

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
        self._clear_pending()
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    # --- interactive answers ------------------------------------------------
    def answer_pick(self, value: str):
        """Write the user's choice (index string, "=own text", or "" for none)
        to the waiting --pick tool. No-op if no choice card is pending."""
        a, self.pending_pick, self.pick_options = self.pending_pick, None, []
        if a:
            self._write_answer(a, value)

    def answer_perm(self, value: str):
        """Write a permission/retry answer ("y"/"n"/"r") to the waiting gate."""
        info, self.pending_perm = self.pending_perm, None
        if info and info.get("a"):
            self._write_answer(info["a"], value)

    def _clear_pending(self):
        self.pending_pick = None
        self.pending_perm = None
        self.pick_options = []

    @staticmethod
    def _write_answer(path: str, value: str):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(str(value))
            os.rename(tmp, path)   # whole, or the waiting side reads it half-written
        except OSError:
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
            # Tag each queued callback with the process that produced it: ask()
            # may already have cancelled this proc and started a new one before
            # this line is dispatched on the main thread, and a stale line must
            # never render/speak into the new ask's turn or overwrite its
            # pending card's answer file.
            GLib.idle_add(self._dispatch, proc, line, answer_spoken)
        proc.wait()
        GLib.idle_add(self._finished, proc, answer_spoken)

    def _drain_err(self, proc: subprocess.Popen):
        if not proc.stderr:
            return
        for raw in proc.stderr:
            sys.stderr.write("[brain] " + raw)

    def _dispatch(self, proc: subprocess.Popen, line: str, answer_spoken: list) -> bool:
        if proc is not self.proc:
            return False   # superseded by a newer ask; drop this stale line
        tag = line[:1]
        rest = line[2:] if len(line) > 1 and line[1] == " " else line[1:]
        try:
            if tag == "!":
                self.overlay.call("msg", rest)
            elif tag == "◇":
                try:
                    card = json.loads(rest)
                except ValueError:
                    return False
                self.pending_perm = None
                self.pending_pick = card.get("a")   # where the user's choice is written
                self.pick_options = [
                    (o.get("label", "") if isinstance(o, dict) else str(o))
                    for o in card.get("options", [])]
                self.overlay.call("pick", card)
                self._prompt("pick", {"q": card.get("q", ""),
                                      "options": self.pick_options})
            elif tag == "?":
                # relay format: "? <q>\t<cmd>\t<answer-file>" then optionally
                # "\t\t\t<need-json>" (approval + connector) or
                # "\t\tretry\t<details>" (a failure offer: Not now / Retry).
                parts = rest.split("\t")
                q = parts[0] if parts else ""
                cmd = parts[1] if len(parts) > 1 else ""
                a = parts[2] if len(parts) > 2 else ""
                retry = len(parts) > 4 and parts[4] == "retry"
                details = parts[5] if retry and len(parts) > 5 else ""
                self.pending_pick = None
                self.pick_options = []
                self.pending_perm = {"a": a, "retry": retry}
                self.overlay.call("ask", [q, cmd, retry, details])
                self._prompt("perm", {"q": q, "cmd": cmd, "retry": retry})
            elif tag == "⚙":
                # "⚙ <key> <value>": a spoken settings change, already validated
                # by the brain (never a server address, key, client, or Safe mode
                # off). Persist it so it survives, like the macOS app flipping it.
                parts = rest.split(" ", 1)
                key = parts[0]
                raw = parts[1] if len(parts) > 1 else ""
                value = True if raw == "true" else False if raw == "false" else raw
                try:
                    from . import settings as cfg
                    if key:
                        cfg.set(key, value)
                except Exception:
                    import traceback
                    traceback.print_exc()
            elif tag in ("▣", "@", "▤", "▥", "⤴", "⇢"):
                # mini-screen / agents / movable: not in v1 core — ignore.
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

    def _prompt(self, kind: str, data: dict) -> None:
        try:
            self.on_prompt(kind, data)
        except Exception:
            import traceback
            traceback.print_exc()

    def _finished(self, proc, answer_spoken) -> bool:
        if proc is not self.proc:
            # This proc was already cancelled/superseded (ask() or cancel()
            # moved self.proc on). Its own cancel/ask call already cleared
            # pending state and will own on_done() for whatever replaced it;
            # finishing late must not wipe the new ask's pending card answer
            # file or fire on_done() (wake/auto-hide) out of turn.
            return False
        self.proc = None
        self._clear_pending()
        if proc.returncode and not answer_spoken:
            self.overlay.call("msg", "")
        try:
            self.on_done()
        except Exception:
            import traceback
            traceback.print_exc()
        return False
