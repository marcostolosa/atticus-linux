"""Atticus Linux shell — core loop.

Wires the overlay, control socket (hotkey), microphone recorder, faster-whisper
transcriber, TTS and the brain bridge into one hold-to-talk assistant:

    press   -> show box, start listening, live orb level
    release -> transcribe, show the question, run the brain, stream + speak
    cancel  -> stop everything, dismiss
    ask T   -> typed request (also used by the box's text field via `send`)

gtk4-layer-shell must load before libgtk-4, so we LD_PRELOAD and re-exec first.
"""
import ctypes.util
import os
import subprocess
import sys
import threading

_PRELOAD_LIB = "libgtk4-layer-shell.so"


def _ensure_preload():
    if os.environ.get("ATTICUS_PRELOADED"):
        return
    lib = None
    for cand in ("/usr/lib/libgtk4-layer-shell.so", "/usr/lib64/libgtk4-layer-shell.so"):
        if os.path.exists(cand):
            lib = cand
            break
    if lib is None:
        lib = ctypes.util.find_library("gtk4-layer-shell") or _PRELOAD_LIB
    pre = os.environ.get("LD_PRELOAD", "")
    if _PRELOAD_LIB not in pre:
        os.environ["LD_PRELOAD"] = lib + (":" + pre if pre else "")
    os.environ["ATTICUS_PRELOADED"] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "atticus_shell", *sys.argv[1:]])


def main():
    _ensure_preload()

    from . import platform
    platform.require_v1_platform()

    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk

    import time
    from .overlay import Overlay
    from .bridge import Bridge
    from .control import ControlServer
    from .speech import Recorder, Transcriber
    from .tts import Speaker
    from .wakeword import WakeListener
    from . import settings as cfg

    app = Gtk.Application(application_id="com.atticus.app",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    if "--demo" in sys.argv[1:]:
        def on_demo(a):
            box = Overlay(a, on_message=lambda b: print("page->shell:", b))
            box.present()
            seq = [
                (250, lambda: box.call("reset")),
                (350, lambda: box.call("visible", True)),
                (500, lambda: box.call("listen", "hold")),
                (800, lambda: box.call("words", "what's on my screen")),
                (1400, lambda: box.call("think")),
                (1700, lambda: box.call("line", "· Looking at your screen")),
                (2300, lambda: box.call("line", "▸ Reading the window")),
                (3100, lambda: box.call("line", "✓ Your editor shows atticus_shell/overlay.py with a terminal below it.")),
            ]
            for d, fn in seq:
                GLib.timeout_add(d, lambda fn=fn: (fn(), False)[1])
        app.connect("activate", on_demo)
        app.run([])
        return

    class Atticus:
        def __init__(self, app):
            self.app = app
            self.listening = False
            self._level_source = 0
            self._wake_session = False
            self._speech_seen = False
            self._silence_since = 0.0
            self._listen_started = 0.0
            # Bring the hotkey socket up first so it's ready before WebKit finishes init.
            self.control = ControlServer(self._on_control)
            self.control.start()
            self.overlay = Overlay(app, on_message=self._on_message)
            self.recorder = Recorder()
            self.transcriber = Transcriber()
            self.speaker = Speaker()
            # Speak each answer in the language the user just spoke (pt-BR / en-US).
            self.bridge = Bridge(
                self.overlay,
                speak=lambda text: self.speaker.speak(text, self.transcriber.last_language),
                on_done=self._on_done)
            self.transcriber.warm()

            # Wake word ("Atticus"), like Alexa — always listening, off by default.
            self.wake = None
            if cfg.get("wake", False):
                word = cfg.get("wakeWord") or "atticus"
                self.wake = WakeListener(self._on_wake, word=word)
                if self.wake.available():
                    self.wake.start()
                    print(f"[atticus] wake word on: say '{word}'", flush=True)
                else:
                    print("[atticus] wake word enabled but Vosk model missing; run build.sh", flush=True)
                    self.wake = None

            # Hold the app alive without a visible window until the hotkey fires.
            self._hold = Gio.Application.hold(app)
            self.overlay.window.set_visible(False)

        # --- hotkey / control ---------------------------------------------
        def _on_control(self, cmd, arg):
            if cmd == "press":
                self._begin_listen()
            elif cmd == "release":
                self._end_listen()
            elif cmd == "toggle":
                (self._end_listen if self.listening else self._begin_listen)()
            elif cmd == "cancel":
                self._cancel()
            elif cmd == "ask" and arg:
                self._submit_typed(arg)
            elif cmd == "ping":
                pass
            elif cmd == "probe":
                self.overlay.eval_js(
                    "console.log('PROBE ' + JSON.stringify({"
                    "cardH: document.getElementById('card')?.offsetHeight,"
                    "threadH: document.getElementById('thread')?.offsetHeight,"
                    "turns: document.querySelectorAll('.turn').length,"
                    "q: document.querySelector('.q')?.textContent,"
                    "a: document.querySelector('.a')?.textContent,"
                    "qH: document.querySelector('.q')?.offsetHeight,"
                    "bodyCls: document.body.className,"
                    "threadDisp: getComputedStyle(document.getElementById('thread')).display,"
                    "threadOvf: getComputedStyle(document.getElementById('thread')).overflow,"
                    "cardRect: JSON.stringify(document.getElementById('card')?.getBoundingClientRect())"
                    "}))")

        def _on_wake(self):
            # Ignore the keyword while already listening or mid-answer.
            if self.listening or self.bridge.running():
                return
            self._begin_listen(wake=True)

        def _begin_listen(self, wake=False):
            if self.listening:
                return
            if self.wake:
                self.wake.pause()   # don't let the spotter fight the command mic
            self.overlay.present()
            if not self.recorder.start():
                self.overlay.call("msg", "No microphone available.")
                self._resume_wake()
                return
            self.listening = True
            self._wake_session = wake
            self._speech_seen = False
            self._silence_since = 0.0
            self._listen_started = time.monotonic()
            self.overlay.call("listen")        # creates a fresh voice turn (q/steps/answer)
            self._level_source = GLib.timeout_add(60, self._tick_level)

        def _tick_level(self):
            if not self.listening:
                return False
            level = self.recorder.level
            self.overlay.call("level", round(level, 3))
            # Wake-word sessions have no key release, so end on a trailing silence.
            if self._wake_session:
                now = time.monotonic()
                if level > 0.07:
                    self._speech_seen = True
                    self._silence_since = 0.0
                elif self._speech_seen:
                    if not self._silence_since:
                        self._silence_since = now
                    elif now - self._silence_since > 1.3:
                        self._end_listen()
                        return False
                if now - self._listen_started > 15:   # hard cap
                    self._end_listen()
                    return False
            return True

        def _end_listen(self):
            if not self.listening:
                return
            self.listening = False
            if self._level_source:
                GLib.source_remove(self._level_source)
                self._level_source = 0
            audio = self.recorder.stop()
            self.overlay.call("think")
            threading.Thread(target=self._transcribe_and_run, args=(audio,), daemon=True).start()

        def _transcribe_and_run(self, audio):
            try:
                text = self.transcriber.transcribe(audio)
            except Exception:
                import traceback
                traceback.print_exc()
                text = ""
            GLib.idle_add(self._after_transcribe, text)

        def _after_transcribe(self, text):
            text = (text or "").strip()
            if not text:
                self.overlay.call("msg", "")
                self._resume_wake()   # nothing heard: go back to waiting for the keyword
                return False
            # the voice turn already exists (from ui.listen()); fill it and run.
            self.overlay.call("words", text)
            self._start_brain(text)
            return False

        def _on_done(self):
            # The answer finished streaming; start listening for the keyword again.
            self._resume_wake()

        def _resume_wake(self):
            if self.wake and not self.listening and not self.bridge.running():
                self.wake.resume()

        def _submit_typed(self, text):
            # a typed/relayed ask has no turn yet: create one (keep box open), then run.
            self.overlay.present()
            self.overlay.call("listen", True)
            self.overlay.call("words", text)
            self._start_brain(text)

        def _start_brain(self, text):
            self.overlay.present()
            front = self._front_app()
            self.bridge.ask(text, front_app=front)

        def _cancel(self):
            self.listening = False
            if self._level_source:
                GLib.source_remove(self._level_source)
                self._level_source = 0
            try:
                self.recorder.stop()
            except Exception:
                pass
            self.speaker.stop()
            self.bridge.cancel()
            self.overlay.call("stopped", "")
            self._resume_wake()

        # --- overlay page -> shell ----------------------------------------
        def _on_message(self, body):
            if "send" in body and isinstance(body["send"], str):
                self._submit_typed(body["send"])
            if body.get("type"):
                # user started typing: drop a live mic without sending
                if self.listening:
                    self.listening = False
                    if self._level_source:
                        GLib.source_remove(self._level_source)
                        self._level_source = 0
                    try:
                        self.recorder.stop()
                    except Exception:
                        pass
            if isinstance(body.get("copy"), str) and body["copy"]:
                self._clipboard(body["copy"])
            if isinstance(body.get("link"), str):
                subprocess.Popen(["xdg-open", body["link"]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for key in ("fold", "comp", "boxH"):
                if key in body:
                    h = body.get("h") if key == "fold" else body.get(key)
                    if isinstance(h, (int, float)) and h > 0:
                        self.overlay.resize(None, int(h))
                    if key == "fold":
                        self.overlay.call("foldSettled", body["fold"])

        def _clipboard(self, text):
            try:
                p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
                p.communicate(text.encode("utf-8"), timeout=3)
                self.overlay.call("copied")
            except Exception:
                pass

        def _front_app(self):
            try:
                from .platform import hyprctl
                w = hyprctl("activewindow", json=True)
                return (w or {}).get("class", "") or ""
            except Exception:
                return ""

    holder = {}

    def on_activate(a):
        if not holder:
            holder["app"] = Atticus(a)

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
