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
    from .earcons import Earcons
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
            self._fix_mic_gain()
            self._wake_session = False
            self._speech_seen = False
            self._silence_since = 0.0
            self._listen_started = 0.0
            # Hands-free answering of an interactive card (choice / permission).
            self._answering = False
            self._prompt = None          # {"kind": "pick"|"perm", ...}
            self._answer_tries = 0
            # Bring the hotkey socket up first so it's ready before WebKit finishes init.
            self.control = ControlServer(self._on_control)
            self.control.start()
            self.overlay = Overlay(app, on_message=self._on_message,
                                   on_escape=self._cancel)
            self._autohide_source = 0
            self._floor = None
            self.recorder = Recorder()
            self.transcriber = Transcriber()
            self.speaker = Speaker()
            self.earcons = Earcons()
            # Speak each answer in the language the user just spoke (pt-BR / en-US).
            self.bridge = Bridge(
                self.overlay,
                speak=lambda text: self.speaker.speak(text, self.transcriber.last_language),
                on_done=self._on_done,
                on_prompt=self._on_prompt)
            self.transcriber.warm()

            # Wake word ("Atticus"), like Alexa — always listening, off by default.
            self.wake = None
            if cfg.get("wake", False):
                word = cfg.get("wakeWord") or "atticus"
                self.wake = WakeListener(self._on_wake, word=word)
                if self.wake.available():
                    self.wake.start()
                    eng = self.wake.engine
                    note = ("trained model" if eng == "livekit"
                            else "whisper fallback — train a model for best results: tools/train_wakeword.md")
                    print(f"[atticus] wake word on ({eng}, {note}): say '{word}'", flush=True)
                else:
                    print("[atticus] wake word enabled but no engine available (need faster-whisper or a trained model)", flush=True)
                    self.wake = None

            # Hold the app alive without a visible window until the hotkey fires.
            self._hold = Gio.Application.hold(app)
            self.overlay.window.set_visible(False)

        def _fix_mic_gain(self):
            # On PipeWire the mic level is the SOURCE node volume (pactl), not the
            # ALSA amixer "Capture". Some laptop codecs also default "Internal Mic
            # Boost" to a huge gain that rails the ADC. Reset the boost and set a
            # sane PipeWire capture volume at startup. micFix=false disables.
            if not cfg.get("micFix", True):
                return
            import shutil
            card = str(cfg.get("micCard", "0"))
            if shutil.which("amixer"):
                try:
                    subprocess.run(["amixer", "-c", card, "sset", "Internal Mic Boost", "0"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                except Exception:
                    pass
            if shutil.which("pactl"):
                vol = str(cfg.get("micVolume", "45")).rstrip("%")
                for args in (["set-source-mute", "@DEFAULT_SOURCE@", "0"],
                             ["set-source-volume", "@DEFAULT_SOURCE@", f"{vol}%"]):
                    try:
                        subprocess.run(["pactl", *args],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                    except Exception:
                        pass

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
            self._cancel_autohide()
            # A new session starts clean: if the box was hidden (idle / a
            # previous person walked away), wipe the thread so nobody ever sees
            # the last conversation. A quick follow-up (box still up) keeps it.
            was_hidden = not self.overlay.window.get_visible()
            self.overlay.present()
            self.earcons.play("start")   # rising blip: "I'm listening"
            if not self.recorder.start():
                self.overlay.call("msg", "No microphone available.")
                self._resume_wake()
                return
            self.listening = True
            self._wake_session = wake
            self._speech_seen = False
            self._silence_since = 0.0
            self._floor = None
            self._listen_started = time.monotonic()
            if was_hidden:
                self.overlay.call("reset")     # clear any previous conversation
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
                # Grace window: ignore the opening earcon bleeding into the mic.
                if now - self._listen_started < 0.5:
                    return True
                # Adaptive noise floor: track the quietest level, creep up slowly.
                if self._floor is None or level < self._floor:
                    self._floor = level
                else:
                    self._floor += (level - self._floor) * 0.02
                speech_gate = max(0.10, self._floor + 0.07)
                silence_gate = max(0.075, self._floor + 0.035)
                if level >= speech_gate:
                    self._speech_seen = True
                    self._silence_since = 0.0
                elif self._speech_seen and level <= silence_gate:
                    if not self._silence_since:
                        self._silence_since = now
                    elif now - self._silence_since > 0.9:
                        if os.environ.get("ATTICUS_DEBUG"):
                            print(f"[listen] endpoint: floor={self._floor:.3f} "
                                  f"gate={silence_gate:.3f}", flush=True)
                        self._end_listen()
                        return False
                # Nothing said after the keyword: don't hang open.
                if not self._speech_seen and now - self._listen_started > 5:
                    self._end_listen()
                    return False
                if now - self._listen_started > 12:   # hard cap
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
            self.earcons.play("end")     # falling blip: "got it"
            if self._answering:
                threading.Thread(target=self._transcribe_answer, args=(audio,),
                                 daemon=True).start()
                return
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
            # The answer finished streaming; start listening for the keyword again
            # and let the box fade away on its own after a reading pause.
            self._resume_wake()
            self._schedule_autohide()

        def _schedule_autohide(self, secs=14):
            self._cancel_autohide()
            self._autohide_source = GLib.timeout_add_seconds(secs, self._autohide)

        def _cancel_autohide(self):
            if self._autohide_source:
                GLib.source_remove(self._autohide_source)
                self._autohide_source = 0

        def _autohide(self):
            self._autohide_source = 0
            if not self.listening and not self.bridge.running():
                self.overlay.hide()
            return False

        def _resume_wake(self):
            if self.wake and not self.listening and not self.bridge.running():
                self.wake.resume()

        def _submit_typed(self, text):
            # a typed/relayed ask has no turn yet: create one (keep box open), then run.
            was_hidden = not self.overlay.window.get_visible()
            self.overlay.present()
            if was_hidden:
                self.overlay.call("reset")     # a fresh session: no stale conversation
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
            self._answering = False
            self._prompt = None
            self.bridge.cancel()
            self.overlay.call("stopped", "")
            self.earcons.play("cancel")
            self._cancel_autohide()
            self.overlay.hide()
            self._resume_wake()

        # --- interactive cards (choice / permission) ----------------------
        def _on_prompt(self, kind, data):
            # A card just appeared. Read it aloud, then open the mic so the user
            # can answer hands-free. The panel buttons stay live the whole time.
            self._prompt = {"kind": kind, **data}
            self._answer_tries = 0
            self._cancel_autohide()
            self.overlay.present()
            self._speak_prompt(first=True)

        def _speak_prompt(self, first=False):
            p = self._prompt
            if not p:
                return
            text = self._prompt_speech(p, first)
            lang = self.transcriber.last_language or None

            def after():
                GLib.idle_add(self._begin_answer_listen)
            # If voice is off or unavailable the callback still fires, so the
            # mic opens right away and the card can be answered by voice or click.
            self.speaker.speak(text, lang, done=after)

        def _prompt_speech(self, p, first):
            if p["kind"] == "perm":
                base = (p.get("q") or "").strip()
                tail = ("Tento de novo? Diga sim ou não."
                        if p.get("retry") else "Posso? Diga sim ou não.")
                return (base + (". " if base else "") + tail) if first else tail
            q = (p.get("q") or "").strip()
            opts = p.get("options") or []
            spoken_opts = "; ".join(
                f"opção {i + 1}: {self._plain(o)}" for i, o in enumerate(opts[:6]))
            tail = "Diga o número, ou toque na opção."
            if first:
                return "; ".join(x for x in (q, spoken_opts, tail) if x)
            return "Diga o número da opção, ou toque."

        @staticmethod
        def _plain(text):
            import re
            s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", str(text))
            s = re.sub(r"[*_`#>~]", "", s)
            return re.sub(r"\s+", " ", s).strip()

        def _begin_answer_listen(self):
            # Open the mic to hear the answer to the pending card.
            if not self._prompt or self.listening or not self.bridge.running():
                return False
            if self.wake:
                self.wake.pause()
            if not self.recorder.start():
                return False   # no mic: leave the card for clicking
            self.listening = True
            self._answering = True
            self._wake_session = True    # reuse trailing-silence endpointing
            self._speech_seen = False
            self._silence_since = 0.0
            self._floor = None
            self._listen_started = time.monotonic()
            self.earcons.play("start")   # same cue as a normal listen turn
            self._level_source = GLib.timeout_add(60, self._tick_level)
            return False

        def _stop_answer_listen(self):
            # A panel click (or giving up) ends any in-flight answer listen.
            if self.listening and self._answering:
                self.listening = False
                if self._level_source:
                    GLib.source_remove(self._level_source)
                    self._level_source = 0
                try:
                    self.recorder.stop()
                except Exception:
                    pass
            self._answering = False
            self._prompt = None
            self.speaker.stop()

        def _transcribe_answer(self, audio):
            try:
                text = self.transcriber.transcribe(audio)
            except Exception:
                import traceback
                traceback.print_exc()
                text = ""
            GLib.idle_add(self._resolve_prompt, text)

        def _resolve_prompt(self, text):
            self._answering = False
            p = self._prompt
            if not p or not self.bridge.running():
                self._resume_wake()
                return False
            value = self._match_answer(p, (text or "").strip())
            if value is None:
                # Not understood: say so and listen again a couple of times,
                # then fall back to the panel buttons (still on screen).
                self._answer_tries += 1
                if self._answer_tries <= 2:
                    self._speak_prompt(first=False)
                return False
            if p["kind"] == "perm":
                self.bridge.answer_perm(value)
                self.overlay.call("askResolved")
            else:
                self.bridge.answer_pick(value)
                self.overlay.call("pickResolved")
            self._prompt = None
            return False

        def _match_answer(self, p, text):
            # Map a spoken reply to a card answer, or None when it is unclear.
            import re
            t = re.sub(r"[.!?,;:]+", " ", text.lower())
            t = re.sub(r"\s+", " ", t).strip()
            if not t:
                return None
            yes = {"sim", "pode", "pode sim", "claro", "ok", "okay", "isso",
                   "permitir", "permito", "aprovar", "manda", "vai", "yes", "allow"}
            no = {"nao", "não", "cancelar", "cancela", "negar", "nega", "para",
                  "pare", "no", "deny", "nunca"}
            none = {"nenhum", "nenhuma", "nenhum deles", "nenhuma delas",
                    "outra", "outra coisa", "none", "nada disso"}
            words = set(t.split())
            if p["kind"] == "perm":
                if words & yes or t in yes:
                    return "r" if p.get("retry") else "y"
                if words & no or t in no:
                    return "n"
                return None
            # choice card
            if t in none or words & none:
                return ""
            ordinals = {"primeiro": 0, "primeira": 0, "um": 0, "uma": 0,
                        "segundo": 1, "segunda": 1, "dois": 1, "duas": 1,
                        "terceiro": 2, "terceira": 2, "tres": 2, "três": 2,
                        "quarto": 3, "quarta": 3, "quatro": 3,
                        "quinto": 4, "quinta": 4, "cinco": 4,
                        "sexto": 5, "sexta": 5, "seis": 5}
            opts = p.get("options") or []
            m = re.search(r"\b(\d+)\b", t)
            if m:
                i = int(m.group(1)) - 1
                if 0 <= i < len(opts):
                    return str(i)
            for w in words:
                if w in ordinals and ordinals[w] < len(opts):
                    return str(ordinals[w])
            # Fuzzy: the option whose label shares the most words with the reply.
            best, best_score = None, 0
            for i, o in enumerate(opts):
                label = set(re.sub(r"[^\w\s]", " ", self._plain(o).lower()).split())
                score = len(label & words)
                if score > best_score:
                    best, best_score = i, score
            if best is not None and best_score >= 1:
                return str(best)
            return None

        # --- overlay page -> shell ----------------------------------------
        def _on_message(self, body):
            if body.get("close"):
                self._cancel()   # the box's X: stop everything and hide
                return
            # Answer to an interactive card (clicked in the panel, or a voice
            # pick routed through the page). Permission posts {allow: bool};
            # a choice posts {pick: "<index>"|"=own text"|""}; a retry card
            # also posts {pick: "r"|"n"}. Route by which card is pending.
            if "allow" in body:
                self._stop_answer_listen()
                self.bridge.answer_perm("y" if body.get("allow") else "n")
                return
            if "pick" in body and isinstance(body["pick"], str):
                self._stop_answer_listen()
                if self.bridge.pending_perm:
                    self.bridge.answer_perm(body["pick"])
                else:
                    self.bridge.answer_pick(body["pick"])
                return
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
