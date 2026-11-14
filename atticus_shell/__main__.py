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
            # Bumped at the start of every recording session (and on cancel) so
            # a transcription that finishes after the session it belongs to was
            # superseded or cancelled is recognized as stale and dropped instead
            # of running a discarded command or discarding a fresh one.
            self._gen = 0
            self._speak_gen = 0
            # Hide-for-screenshot bookkeeping: a helper process (cli.py, run
            # by the brain for --look/--text) asks the running shell to hide
            # its own overlay for the moment of a grim capture, so a screen
            # read never sees Atticus's own box. Nested/overlapping requests
            # (depth) restore to whatever the box's visibility actually was
            # before the first one, not to a hardcoded "show".
            self._capture_depth = 0
            self._pre_capture_visible = False
            # True while an answer is actually being read aloud (TTS playback in
            # flight), independent of whether the brain process has exited — the
            # brain usually exits right after printing the final answer, well
            # before playback finishes.
            self._speaking = False
            # Hands-free answering of an interactive card (choice / permission).
            self._answering = False
            self._followup = False
            self._barge_source = 0
            self._barge_hits = []
            self._barge_started = 0.0
            self._barge_peak = 0.0
            self._aec = False
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
                speak=self._speak_answer,
                on_done=self._on_done,
                on_prompt=self._on_prompt)
            self.transcriber.warm()
            self.speaker.warm()
            if cfg.get("bargeIn", True):
                threading.Thread(target=self._setup_echo, daemon=True).start()
            self._last_answer = ""
            # Timers/alarms/reminders live in systemd transient timers, which a
            # reboot wipes: re-arm what is still ahead and fire what was missed.
            threading.Thread(target=self._rearm_alarms, daemon=True).start()

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

        def _rearm_alarms(self):
            try:
                from . import assistant
                time.sleep(3)   # let the overlay and the control socket come up first
                assistant.rearm_all()
            except Exception:
                import traceback
                traceback.print_exc()

        def _pop_routines(self):
            # A scheduled routine is due ("every weekday at 8, tell me the
            # news"): the brain queued it; take the queue and ask each one as
            # if the user had just said it, so the answer is shown and spoken.
            import json as _json
            from .bridge import ATTICUS_PY, _brain_env
            try:
                env = _brain_env()
                ids = _json.loads(subprocess.run([sys.executable, str(ATTICUS_PY), "--routine-pop"], env=env,
                                                 capture_output=True, text=True, timeout=20).stdout or "[]")
                from pathlib import Path
                rows = _json.loads((Path(env["ATTICUS_DIR"]) / "routines.json").read_text())
                asks = [r["ask"] for i in ids for r in rows if isinstance(r, dict) and r.get("id") == i and r.get("ask")]
            except Exception:
                import traceback
                traceback.print_exc()
                return
            for ask in asks:
                GLib.idle_add(self._run_routine, ask, 0)

        def _run_routine(self, ask, tries):
            # Never talk over the user: wait for a conversation to end (up to ~10 min).
            busy = self.listening or self.bridge.running() or self._speaking or self._prompt
            if busy and tries < 40:
                GLib.timeout_add_seconds(15, lambda: (self._run_routine(ask, tries + 1), False)[1])
                return False
            if not busy:
                self.earcons.play("start")
                self._submit_typed(ask)
            return False

        def _on_alarm(self, arg):
            # A timer, alarm or reminder just went off (assistant.fire, run by
            # its systemd timer). Chime always; show and say it unless the
            # user is mid-conversation, where it must not cut them off.
            try:
                import json as _json
                data = _json.loads(arg)
                text, lang = str(data.get("text", "")), str(data.get("lang", "pt"))
            except (ValueError, AttributeError):
                return
            if not text:
                return
            self.earcons.play("alarm")
            if self.listening or self.bridge.running() or self._prompt:
                return   # the notification is already on screen
            was_hidden = not self.overlay.window.get_visible()
            self._cancel_autohide()
            self.overlay.present()
            if was_hidden:
                self.overlay.call("reset")
            self.overlay.call("listen", True)
            self.overlay.call("words", "⏰")
            self.overlay.call("line", "✓ " + text)
            if self.wake:
                self.wake.pause()   # its own voice must not wake it
            self._speaking = True
            self._speak_gen = self._gen
            self._last_answer = ""
            # after the chime (about 1.9 s), not on top of it
            GLib.timeout_add(1900, lambda: (self.speaker.speak(text, lang, done=self._on_speech_done), False)[1])

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
            elif cmd == "alarm" and arg:
                self._on_alarm(arg)
            elif cmd == "routine":
                threading.Thread(target=self._pop_routines, daemon=True).start()
            elif cmd == "ping":
                pass
            elif cmd == "hide-for-capture":
                if self._capture_depth == 0:
                    self._pre_capture_visible = self.overlay.window.get_visible()
                self._capture_depth += 1
                self.overlay.window.set_visible(False)
            elif cmd == "show-after-capture":
                self._capture_depth = max(0, self._capture_depth - 1)
                if self._capture_depth == 0:
                    self.overlay.window.set_visible(self._pre_capture_visible)
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

        @staticmethod
        def _log(event, **details):
            # One line per voice event in the service journal (journalctl
            # --user -u atticus), so "it ignored me" can be traced afterwards.
            print("[voice] " + event + "".join(f" {k}={v!r}" for k, v in details.items()), flush=True)

        # --- talking over Atticus ------------------------------------------
        def _setup_echo(self):
            # Route the voice through PipeWire's echo canceller, so the mic
            # can stay open while Atticus speaks and hear only the person.
            from . import echo
            if echo.ensure() and self.speaker.use_device(echo.SINK):
                self._aec = True
            self._log("echo-cancel", on=self._aec)

        def _arm_barge(self):
            """Atticus just started speaking: listen for the user talking over
            it. A voice held for a quarter of a second stops the speech and
            becomes the next request (or the answer to the card on screen)."""
            if (not self._aec or not self.speaker.last_echo_free or self.listening
                    or self._barge_source or not cfg.get("bargeIn", True)):
                return
            from . import echo
            if not self.recorder.start(device=echo.SOURCE):
                return
            self._barge_hits = []
            self._barge_peak = 0.0
            self._barge_started = time.monotonic()
            self._barge_source = GLib.timeout_add(60, self._tick_barge)

        def _disarm_barge(self):
            if not self._barge_source:
                return
            GLib.source_remove(self._barge_source)
            self._barge_source = 0
            if os.environ.get("ATTICUS_DEBUG"):
                self._log("not interrupted", loudest=round(self._barge_peak, 3))
            if not self.listening:
                try:
                    self.recorder.stop()
                except Exception:
                    pass

        def _tick_barge(self):
            hits = self._barge_hits
            self._barge_peak = max(self._barge_peak, self.recorder.level)
            hits.append(self.recorder.level >= float(cfg.get("bargeLevel", 0.12)))
            del hits[:-6]
            # the first moments are the canceller still adapting to the room
            if time.monotonic() - self._barge_started > 0.5 and sum(hits) >= 4:
                self._barge_source = 0
                self._barge_in()
                return False
            return True

        def _barge_in(self):
            self.recorder.keep_last(1.0)     # the words that interrupted, not the echo before them
            answering = bool(self._prompt and self.bridge.running())
            self._log("interrupted", answering_card=answering)
            self._gen += 1                   # the interrupted speech must not reopen the mic when it ends
            self.speaker.stop()
            self._speaking = False
            self._last_answer = ""
            if not answering:
                self._prompt = None
                if self.bridge.running():
                    self.bridge.cancel()
            self._cancel_autohide()
            self.listening = True
            self._answering = answering
            self._followup = False
            self._wake_session = True        # ends on trailing silence
            self._speech_seen = True
            self._silence_since = 0.0
            self._floor = 0.02
            self._listen_started = time.monotonic()
            if not answering:
                self.overlay.call("listen")  # a fresh voice turn
            self._level_source = GLib.timeout_add(60, self._tick_level)

        def _begin_listen(self, wake=False, followup=False):
            if self.listening:
                return
            self._disarm_barge()
            # followup: the mic opened by itself after an answer. If nobody
            # speaks it closes without a sound and nothing is transcribed.
            self._followup = followup
            self._log("listening", wake=wake, followup=followup)
            # Barge-in: starting a new recording interrupts whatever Atticus is
            # doing right now — speaking an answer, or waiting on an interactive
            # card from a now-abandoned ask — so the mic never records its own
            # voice and no stale card state corrupts the next voice turn.
            self.speaker.stop()
            if self.bridge.running():
                self.bridge.cancel()
            self._answering = False
            self._prompt = None
            self._gen += 1
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
                self._schedule_autohide()
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
                # Gates sit just above the room's own noise so a whisper
                # still counts as speech in a quiet room (the floors used to
                # be fixed at a normal speaking level).
                speech_gate = max(0.06, self._floor * 1.6 + 0.035)
                silence_gate = max(0.045, self._floor * 1.3 + 0.012)
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
                if not self._speech_seen and now - self._listen_started > (float(cfg.get("followUpSeconds", 3)) if self._followup else 5):
                    if self._followup:
                        self._close_followup()
                    else:
                        self._end_listen()
                    return False
                if now - self._listen_started > 12:   # hard cap
                    self._end_listen()
                    return False
            return True

        def _stop_recording(self):
            """Stop the mic and level ticker, if running. Returns the captured
            audio (or None). Shared by every path that tears down a recording
            session, so they can never disagree about `listening`/the timer."""
            if not self.listening:
                return None
            self.listening = False
            if self._level_source:
                GLib.source_remove(self._level_source)
                self._level_source = 0
            try:
                return self.recorder.stop()
            except Exception:
                return None

        def _close_followup(self):
            # The follow-up window passed in silence: back to the wake word,
            # with no blip and without sending room noise to be transcribed.
            self._stop_recording()
            self._followup = False
            self._log("follow-up closed: silence")
            self.overlay.call("msg", "")
            self._resume_wake()
            self._schedule_autohide()

        def _end_listen(self):
            if not self.listening:
                return
            answering = self._answering
            gen = self._gen   # this session's generation; _begin_listen/_cancel bump it
            audio = self._stop_recording()
            self.earcons.play("end")     # falling blip: "got it"
            if answering:
                threading.Thread(target=self._transcribe_answer, args=(audio, gen),
                                 daemon=True).start()
                return
            self.overlay.call("think")
            threading.Thread(target=self._transcribe_and_run, args=(audio, gen), daemon=True).start()

        def _transcribe_and_run(self, audio, gen):
            try:
                text = self.transcriber.transcribe(audio)
            except Exception:
                import traceback
                traceback.print_exc()
                text = ""
            GLib.idle_add(self._after_transcribe, text, gen)

        def _after_transcribe(self, text, gen):
            if gen != self._gen:
                return False   # superseded by a newer session or a cancel; drop it
            text = (text or "").strip()
            self._log("heard", text=text, by=self.transcriber.last_engine)
            if not text:
                self.overlay.call("msg", "")
                self._resume_wake()   # nothing heard: go back to waiting for the keyword
                self._schedule_autohide()
                return False
            # the voice turn already exists (from ui.listen()); fill it and run.
            self.overlay.call("words", text)
            self._start_brain(text)
            return False

        def _speak_answer(self, text):
            # The final answer: speak it, and hold off on resuming the wake
            # word / arming auto-hide until playback actually finishes — the
            # brain process usually exits right after printing this line,
            # well before the audio is done, so doing it on process-exit alone
            # would open the mic (and risk hiding the box) over Atticus's own
            # voice.
            self._speaking = True
            self._speak_gen = self._gen   # tag this playback to the current turn
            self._last_answer = text
            self._log("answer", text=text[:200])
            self.speaker.speak(text, self.transcriber.last_language,
                               done=self._on_speech_done)
            self._arm_barge()

        def _on_speech_done(self):
            # Runs on the TTS watcher thread; hop back to the GLib main thread.
            GLib.idle_add(self._speech_finished)

        def _speech_finished(self):
            if self._speak_gen == self._gen:
                self._disarm_barge()
            self._speaking = False
            if self._speak_gen != self._gen:
                # A cancel or a new _begin_listen happened while this answer
                # was still playing; that call already did its own wake/hide
                # handling, so finishing late must not redo or override it
                # (it would otherwise re-arm auto-hide on top of a new turn).
                return False
            if not self.bridge.running():
                # Conversation mode: when Atticus ends on a question, open the
                # mic for the reply instead of making the user say the wake
                # word again. Trailing silence closes it, as after the keyword.
                # Conversation mode: after an answer the mic opens again for a
                # few seconds, so a reply ("liga", "e a da cozinha?") needs no
                # wake word. followUp: "always" (default), "question" (only
                # when the answer ends on a question) or false.
                mode = cfg.get("followUp", "always")
                asked = self._last_answer.rstrip().rstrip("*_`)\"” ").endswith("?")
                spoke, self._last_answer = bool(self._last_answer), ""
                if mode and not self.listening and (asked or (spoke and mode in (True, "always"))):
                    self._begin_listen(wake=True, followup=True)
                    return False
                self._resume_wake()
                self._schedule_autohide()
            return False

        def _on_done(self):
            # The brain process exited. If its answer is still being read
            # aloud, let _speech_finished() do the wake/auto-hide handoff
            # once playback actually ends.
            if self._speaking:
                return
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
            if (not self.listening and not self.bridge.running()
                    and not self._speaking and not self._prompt):
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
            # Everyday commands (timer, volume, next song, open an app…) are
            # recognized and run right here, with no AI round trip. Anything
            # the parser is not sure about falls through to the model.
            intent = None
            if cfg.get("instant", True):
                try:
                    from . import intents
                    intent = intents.parse(text)
                except Exception:
                    import traceback
                    traceback.print_exc()
            if intent is None:
                return self._ask_brain(text)
            gen = self._gen

            def work():
                try:
                    reply = intents.run(intent)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    reply = None
                GLib.idle_add(self._instant_done, text, intent, reply, gen)
            threading.Thread(target=work, daemon=True).start()

        def _instant_done(self, text, intent, reply, gen):
            if gen != self._gen:
                return False   # cancelled or superseded while it ran
            self._log("instant", kind=intent.kind, reply=reply)
            if reply is None:
                self._ask_brain(text)   # could not do it locally after all
                return False
            if not reply:           # "stop": nothing to say, just go quiet
                self.speaker.stop()
                self.overlay.hide()
                self._resume_wake()
                return False
            self.overlay.call("line", "✓ " + reply)
            self._speaking = True
            self._speak_gen = self._gen
            # a state answer invites a reply ("liga"); a plain confirmation does not
            self._last_answer = reply if intent.kind == "home_state" or reply.rstrip().endswith("?") else ""
            self.speaker.speak(reply, intent.lang, done=self._on_speech_done)
            self._arm_barge()
            return False

        def _ask_brain(self, text):
            front = self._front_app()
            self.bridge.ask(text, front_app=front)

        def _cancel(self):
            self._gen += 1   # invalidate any transcription still in flight
            self._disarm_barge()
            self._stop_recording()
            self.speaker.stop()
            self._speaking = False
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
            self._arm_barge()

        def _prompt_speech(self, p, first):
            en = str(self.transcriber.last_language or cfg.get("asrLanguage", "")).lower().startswith("en")
            if p["kind"] == "perm":
                base = (p.get("q") or "").strip()
                if en:
                    tail = ("Try again? Say yes or no." if p.get("retry") else "May I? Say yes or no.")
                else:
                    tail = ("Tento de novo? Diga sim ou não."
                            if p.get("retry") else "Posso? Diga sim ou não.")
                return (base + (". " if base else "") + tail) if first else tail
            q = (p.get("q") or "").strip()
            opts = p.get("options") or []
            spoken_opts = "; ".join(
                f"{'option' if en else 'opção'} {i + 1}: {self._plain(o)}" for i, o in enumerate(opts[:6]))
            tail = "Say the number, or tap the option." if en else "Diga o número, ou toque na opção."
            if first:
                return "; ".join(x for x in (q, spoken_opts, tail) if x)
            return "Say the option number, or tap it." if en else "Diga o número da opção, ou toque."

        @staticmethod
        def _plain(text):
            import re
            s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", str(text))
            s = re.sub(r"[*_`#>~]", "", s)
            return re.sub(r"\s+", " ", s).strip()

        def _begin_answer_listen(self):
            # Open the mic to hear the answer to the pending card.
            if self.listening:
                return False     # the user already talked over the question
            self._disarm_barge()
            if not self._prompt or not self.bridge.running():
                return False
            self._gen += 1
            if self.wake:
                self.wake.pause()
            if not self.recorder.start():
                return False   # no mic: leave the card for clicking
            self.listening = True
            self._answering = True
            self._followup = False
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
            self._disarm_barge()
            self._stop_recording()
            self._answering = False
            self._prompt = None
            self.speaker.stop()

        def _transcribe_answer(self, audio, gen):
            try:
                text = self.transcriber.transcribe(audio)
            except Exception:
                import traceback
                traceback.print_exc()
                text = ""
            GLib.idle_add(self._resolve_prompt, text, gen)

        def _resolve_prompt(self, text, gen):
            if gen != self._gen:
                return False   # superseded by a newer session or a cancel; drop it
            self._answering = False
            p = self._prompt
            if not p or not self.bridge.running():
                self._resume_wake()
                self._schedule_autohide()
                return False
            said = (text or "").strip()
            value = self._match_answer(p, said)
            self._log("card answer", heard=said, matched=value)
            if value is None and p["kind"] != "perm" and said:
                # Not one of the options, but the user did say something:
                # that is their answer in their own words ("liga o ar").
                value = "=" + said[:300]
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
            from . import answers
            return answers.match(p, text, self.transcriber.last_language or cfg.get("asrLanguage", ""))

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
                # user started typing: drop a live mic without sending. If
                # that mic was listening for a card's spoken answer, clear
                # that whole voice-answer state too — otherwise it stays
                # "answering" and the *next* recording (a fresh command) gets
                # misrouted to _resolve_prompt and silently discarded.
                self._cancel_autohide()
                if self._answering:
                    self._stop_answer_listen()
                else:
                    self._stop_recording()
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
