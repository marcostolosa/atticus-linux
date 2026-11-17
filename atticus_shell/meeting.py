"""Meeting Copilot: listens to a call, writes down who said what, and suggests
what to answer when someone asks a question.

Two things are heard at once and kept apart:
  them   what the computer is playing (the sink's monitor): the other people
  you    the microphone, through the echo canceller when it is set up, so the
         call coming out of the speakers is not written down a second time
In a room with no call, only the microphone is heard and every voice is "Room".

There is no recognizer here that writes while someone is still talking, so the
transcript grows one utterance at a time: a voice detector finds the pause
that ends it (Segmenter) and the utterance goes to the same recognizer the
assistant uses (speech.Transcriber). A line shows up about 3 s after the person
stops (measured with three synthetic sentences played into a silent sink and
read from its monitor: 3.2 to 4.0 s, of which 0.7 s is the pause being waited
for and the rest is recognition); partial words never do.

Everything that needs a model is the brain's (`atticus.py --meeting-live`
answers one question, `--meeting-prep` and `--meeting-gather` make the brief,
`--meetings` reads the saved ones). This module is the window (ui/meeting.html),
the two listeners, the queue of questions and the saved record under
~/.config/atticus/meetings/<id>/ (metadata.json and events.jsonl, the layout
the brain reads back).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import stack

import numpy as np

from . import settings as cfg
from .speech import LATENCY_MS, SAMPLE_RATE

BRIEF_LIMIT = 6000
PAUSE = 0.7          # seconds without a voice that end an utterance
LONGEST = 20.0       # an utterance is cut here even if nobody paused
CHECK = 0.4          # how often the voice detector looks
QUIET_RMS = 0.004    # below this a stretch of audio is not worth asking the detector about
MATERIAL = ("pdf", "docx", "txt", "md")


def _dir() -> Path:
    return Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus"))


# --- cutting a stream into utterances ----------------------------------------
class Segmenter:
    """Audio in, whole utterances out. `voice_ago(audio)` says how long ago a
    voice was last heard in it (speech.voice_ago; a stand-in in tests)."""

    def __init__(self, voice_ago: Callable[[np.ndarray], float], rate: int = SAMPLE_RATE):
        self.voice_ago, self.rate = voice_ago, rate
        self.buf = np.zeros(0, dtype=np.float32)
        self._checked = 0   # samples of buf already looked at

    def feed(self, chunk: np.ndarray) -> list[np.ndarray]:
        self.buf = np.concatenate([self.buf, chunk])
        out = []
        while self.buf.size - self._checked >= int(CHECK * self.rate):
            self._checked = self.buf.size
            total = self.buf.size / self.rate
            if float(np.sqrt(np.mean(self.buf ** 2))) < QUIET_RMS:
                self._drop(keep=0.3)   # nothing but room: keep a little, so a first syllable is not lost
                continue
            ago = self.voice_ago(self.buf)
            if ago < 0:                # no detector: fall back to fixed pieces
                ago = 0.0
            if ago >= total - 0.05:    # loud, but no voice in it (music, a fan)
                self._drop(keep=0.3)
            elif ago >= PAUSE or total >= LONGEST:
                end = self.buf.size - int(max(0.0, ago - 0.2) * self.rate)
                out.append(self.buf[:end].copy())
                self.buf = self.buf[end:]
                self._checked = self.buf.size
        return out

    def _drop(self, keep: float) -> None:
        self.buf = self.buf[-int(keep * self.rate):]
        self._checked = self.buf.size

    def rest(self) -> np.ndarray | None:
        """What was being said when listening stopped."""
        left, self.buf, self._checked = self.buf, np.zeros(0, dtype=np.float32), 0
        if left.size < self.rate // 2 or self.voice_ago(left) >= left.size / self.rate - 0.05:
            return None
        return left


def monitor_source() -> str | None:
    """The recording side of whatever the computer is playing through."""
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sink + ".monitor" if sink else None


class Listener:
    """One source of sound, written down utterance by utterance on a thread."""

    def __init__(self, device: str | None, transcribe: Callable[[np.ndarray], str],
                 on_text: Callable[[str], None], on_level: Callable[[float], None] | None = None):
        self.device, self.transcribe, self.on_text, self.on_level = device, transcribe, on_text, on_level
        self.proc, self._stop = None, threading.Event()
        self.heard_samples = 0

    def start(self) -> bool:
        if not shutil.which("parecord"):
            return False
        cmd = ["parecord", "--raw", f"--rate={SAMPLE_RATE}", "--channels=1", "--format=s16le",
               f"--latency-msec={LATENCY_MS}",   # asked for by name, or the audio comes two seconds at a time
               *([f"--device={self.device}"] if self.device else [])]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        from .speech import is_phantom, voice_ago
        seg, last_level, queue = Segmenter(voice_ago), 0.0, []
        done = threading.Event()

        def write():   # recognition is slower than listening: it must never hold the reading up
            while not (done.is_set() and not queue):
                if not queue:
                    time.sleep(0.05)
                    continue
                audio = queue.pop(0)
                try:
                    text = (self.transcribe(audio) or "").strip()
                except Exception:
                    text = ""
                if text and not is_phantom(text) and not self._stop.is_set():
                    self.on_text(text)
        threading.Thread(target=write, daemon=True).start()
        try:
            while not self._stop.is_set():
                raw = self.proc.stdout.read(SAMPLE_RATE * 2 * LATENCY_MS // 1000 * 2)
                if not raw:
                    break
                chunk = np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
                self.heard_samples += chunk.size
                if self.on_level and time.monotonic() - last_level >= 0.1 and chunk.size:
                    last_level = time.monotonic()
                    self.on_level(float(np.max(np.abs(chunk))))
                queue.extend(seg.feed(chunk))
            left = seg.rest()
            if left is not None and not self._stop.is_set():
                queue.append(left)
        finally:
            done.set()

    def stop(self):
        self._stop.set()
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try: p.terminate()
            except OSError: pass


# --- the saved record ----------------------------------------------------------
class Archive:
    def __init__(self, title: str, app: str, started: float):
        self.id = str(uuid.uuid4()).upper()
        root = _dir() / "meetings"
        if root.is_symlink():
            raise OSError("meetings folder is a link")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.folder = root / self.id
        self.folder.mkdir(mode=0o700)
        self._write("metadata.json", json.dumps({"app": app, "started": started, "title": title, "version": 1}, sort_keys=True))
        self._write("events.jsonl", "")
        self._lock = threading.Lock()

    def _write(self, name: str, text: str) -> None:
        fd = os.open(self.folder / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    def append(self, events: list[dict]) -> bool:
        try:
            with self._lock:
                fd = os.open(self.folder / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                with os.fdopen(fd, "w") as f:
                    f.write("".join(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in events))
                    f.flush()
                    os.fsync(f.fileno())
            return True
        except OSError:
            return False


def read_material(paths: list[str]) -> tuple[str, list[str]]:
    """Text of the files handed in for the brief -> (text, names that were readable)."""
    text, names = "", []
    for raw in paths[:8]:
        path = Path(raw)
        ext = path.suffix.lower().lstrip(".")
        try:
            if ext not in MATERIAL or path.stat().st_size >= 10_000_000:
                continue
            if ext == "pdf":
                part = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=20).stdout
            elif ext == "docx":
                with zipfile.ZipFile(path) as z:
                    xml = z.read("word/document.xml").decode("utf-8", "replace")
                part = re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n"))
            else:
                part = path.read_text(errors="replace")
        except (OSError, KeyError, zipfile.BadZipFile, subprocess.TimeoutExpired):
            continue
        if part.strip():
            names.append(path.name)
            text += path.name + "\n" + part[:20000] + "\n"
    return text[:20000], names


def speaker_names(mic: bool, room: bool, call_app: str) -> tuple[str, str]:
    """(the name in the saved transcript, who the page says asked)."""
    you = mic and not room
    if you:
        return "You", "You"
    if room:
        return "Room", "Someone in the room"
    return ("Them", "Someone else") if call_app == "Call" else (call_app, "Someone on " + call_app)


class MeetingCopilot:
    def __init__(self, app, transcribe: Callable[[np.ndarray], str], mic_device: Callable[[], str | None] = lambda: None,
                 pause_wake: Callable[[bool], None] = lambda paused: None, open_settings: Callable[[], None] = lambda: None):
        self.app, self.transcribe, self.mic_device = app, transcribe, mic_device
        self.pause_wake, self.open_settings = pause_wake, open_settings
        self.window = self.page = None
        self.loaded = self.running = self.minimized = self.gathering = self.voice = self.brief_edited = False
        self.generation = self.serial = 0
        self.elapsed, self.began = 0.0, 0.0
        self.brief = self.meeting_title = ""
        self.call_app = "Call"
        self.transcript: list[dict] = []
        self.queue: list[dict] = []
        self.answering: int | None = None
        self.answer_proc = self.process = None
        self.answer_question = ""
        self.pending_brief: str | None = None
        self.listeners: list[Listener] = []
        self.archive: Archive | None = None
        self.archive_ended = False
        self.voice_text, self.voice_tail = "", 0.0
        self.expanded = (340, 650)
        self._timer = 0
        self._lock = threading.Lock()   # one recognition at a time: the local model is not shared safely

    @property
    def room(self) -> bool:
        return self.call_app == "In person"

    @property
    def style(self) -> str:
        pick = cfg.get("meetingStyle", "") or ""
        if pick == "Custom…":
            return str(cfg.get("meetingCustom", "") or "")
        return next((str(s.get("text", "")) for s in (cfg.get("meetingStyles", []) or []) if isinstance(s, dict) and s.get("name") == pick), "")

    def js(self, name: str, value: Any = None) -> None:
        if self.page:
            self.page.call(name, value)

    # --- the window --------------------------------------------------------------
    def open(self) -> None:
        if not cfg.get("meeting", True) or cfg.get("paused", False):
            return
        if self.window is not None:
            self.window.set_visible(True)
            return
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk
        from gi.repository import Gtk4LayerShell as LayerShell
        from .agents import Page
        self.loaded, self.minimized = False, False
        self.generation += 1
        self.transcript, self.brief, self.elapsed, self.serial = [], "", 0.0, 0
        self.archive, self.archive_ended, self.meeting_title, self.call_app = None, False, "", "Call"
        win = Gtk.Window(application=self.app)
        win.set_decorated(False)
        win.add_css_class("atticus-meeting")
        css = Gtk.CssProvider()
        css.load_from_data(b"window.atticus-meeting, window.atticus-meeting > * { background: transparent; }")   # the page draws the glass
        from gi.repository import Gdk
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "atticus-meeting")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.ON_DEMAND)
        stack.anchor(win)   # top centre, above the assistant's box (stack.py)
        stack.update("meeting", window=win, height=470, visible=True)
        win.connect("notify::visible", lambda w, _p: stack.update("meeting", visible=w.get_visible()))
        self.page = Page(self._message, page="meeting.html", handler="meeting", obj="meeting")
        self.page.web.set_size_request(340, 470)
        win.set_child(self.page.web)
        self.window = win
        win.present()

    def _most_height(self) -> int:
        try:
            from gi.repository import Gdk
            mon = Gdk.Display.get_default().get_monitors().get_item(0)
            return max(200, mon.get_geometry().height - 24)
        except Exception:
            return 696

    def _size(self, h: float, reveal=None) -> None:
        """The page asks for its height (0 = the whole live view); the top edge stays where it is."""
        if not self.page:
            return
        most = self._most_height()
        want = min(self.expanded[1], most) if h <= 0 else int(min(most, max(44 if self.minimized else 80, h)))
        self.page.web.set_size_request(116 if self.minimized else self.expanded[0], want)
        stack.update("meeting", height=want)
        if reveal is not None:
            self.js("settled", reveal)

    def _ready(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        self.js("motion", False)
        self.js("privacy", bool(cfg.get("meetingSave", True)))
        self.js("calendar", True)   # the calendar is files here: nothing to allow
        self.js("styles", {"list": cfg.get("meetingStyles", []) or [], "pick": cfg.get("meetingStyle", "") or "", "custom": cfg.get("meetingCustom", "") or ""})
        self.js("width", 340)
        self.js("savedPrep", cfg.get("meetingPrep", "") or "")
        self.js("mode", "call" if call_apps() else "room")
        self.js("prep")
        self.js("focus")

    # --- saving --------------------------------------------------------------------
    def _save(self, events: list[dict]) -> None:
        if not cfg.get("meetingSave", True) or not self.archive:
            return
        if not self.archive.append(events):
            self.archive = None
            self.js("saving", False)
            self.js("error", "Transcript could not save. Check available disk space.")

    def _begin_archive(self) -> None:
        if not cfg.get("meetingSave", True) or self.archive is not None:
            self.js("saving", self.archive is not None)
            return
        try:
            title = self.meeting_title or self.call_app + " · " + time.strftime("%d/%m/%Y %H:%M", time.localtime(self.began))
            self.archive = Archive(title, self.call_app, self.began)
            self.archive_ended = False
            self._save([{"kind": "notes", "text": self.brief}])
            self.js("saving", True)
        except OSError:
            self.js("saving", False)
            self.js("error", "Could not save this transcript. Check available disk space.")

    def _finish_archive(self, summarize: bool = True) -> None:
        archive = self.archive
        if not archive or self.archive_ended:
            return
        self.archive_ended = True
        if not archive.append([{"kind": "ended", "at": time.time(), "duration": self.elapsed}]):
            self.archive, self.archive_ended = None, False
            self.js("saving", False)
            return
        if summarize and not self.running:
            g = self.generation
            self._worker("--meetings", {"action": "finish", "id": archive.id}, lambda result: (
                self.js("recap", result["meeting"]) if g == self.generation and isinstance(result.get("meeting"), dict) and not self.running else None),
                tracked=False)

    # --- listening -------------------------------------------------------------------
    def start(self, context: str) -> None:
        if not self.loaded or self.running or not cfg.get("meeting", True) or cfg.get("paused", False):
            return
        self._cancel_work()
        self.brief, self.brief_edited, self.running = context[:BRIEF_LIMIT], False, True
        self.began = time.time() - self.elapsed
        if self.call_app == "Call":
            self.call_app = next(iter(sorted(call_apps())), "Call")
        self._begin_archive()
        if self.archive_ended:
            self._save([{"kind": "resumed", "duration": self.elapsed}])
            self.archive_ended = False
        self.js("listen")
        self._warm_answer()
        # Short notes are the brief as they are; long notes or a link are boiled down while it listens.
        if len(context) > BRIEF_LIMIT or re.fullmatch(r"https?://\S+", context.strip()):
            self._prepare(context)
        g = self.generation   # stop() moves it on: whatever this start set going is then ignored
        self.pause_wake(True)   # the wake word would fire on the call's own voices
        from gi.repository import GLib

        def said(mic: bool):
            return lambda text: GLib.idle_add(self._speech, text, mic, g)

        def level(mic: bool):
            return lambda value: GLib.idle_add(self._level, value, mic, g)

        def write(audio):
            with self._lock:
                return self.transcribe(audio)
        sources = [(self.mic_device(), True)] + ([] if self.room else [(monitor_source(), False)])
        self.listeners = []
        for device, mic in sources:
            if not mic and not device:
                continue
            listener = Listener(device, write, said(mic), level(mic))
            if listener.start():
                self.listeners.append(listener)
        if not self.listeners:
            self.stop()
            self.js("error", "Audio could not be recorded (parecord is missing or no device answered).")
            return
        cfg.set("meetingPrep", "")
        self.js("capturing")
        self._timer = GLib.timeout_add(1000, self._tick, g)

    def _tick(self, g: int) -> bool:
        if g != self.generation or not self.running:
            return False
        seconds = int(time.time() - self.began)
        self.js("clock", seconds)
        if seconds % 2 == 0:
            self._save([{"kind": "checkpoint", "duration": seconds}])
        return True

    def _level(self, value: float, mic: bool, g: int) -> bool:
        if g == self.generation and self.running and (mic if self.voice else self.minimized and not mic or self.room):
            self.js("level", value)
        return False

    def _speech(self, text: str, mic: bool, g: int) -> bool:
        """One whole utterance from one of the two sources."""
        if g != self.generation or not self.running:
            return False
        if mic and self.voice:   # talking to Atticus: the microphone goes to the ask box, not the transcript
            self.voice_text = (self.voice_text + " " + text).strip()
            self.js("heard", self.voice_text)
            return False
        if mic and time.time() < self.voice_tail:
            return False         # the last words of a spoken ask: already in the ask
        self._line(text, mic)
        return False

    def _line(self, text: str, mic: bool, answer: bool = True) -> None:
        at, you = time.time(), mic and not self.room
        self.serial += 1
        identifier, seconds = self.serial, int(at - self.began)
        speaker, asked_by = speaker_names(mic, self.room, self.call_app)
        self.transcript.append({"at": at, "text": text, "seconds": seconds, "mic": you})
        self._save([{"kind": "transcript", "at": at, "text": text, "seconds": seconds, "mic": you, "speaker": speaker}])
        row = {"id": identifier, "text": text, "seconds": seconds, "mic": you, "speaker": asked_by}
        self.js("line", row)
        if answer and not you and text.rstrip().endswith("?"):
            automatic = cfg.get("meetingAuto", "auto") != "ask"
            self.js("question", {**row, "auto": automatic, "heard": True})
            if automatic:
                self._enqueue(identifier, text, best=cfg.get("meetingAuto", "auto") == "always", at=at)

    # --- answering -----------------------------------------------------------------------
    def _enqueue(self, identifier: int, question: str, best: bool, at: float | None = None, image: str | None = None) -> None:
        if not self.running or not question.strip() or self.answering == identifier or any(q["id"] == identifier for q in self.queue):
            return
        now = time.time()
        data = {"id": identifier, "transcript": list(self.transcript), "now": now, "question": question, "best": best,
                "at": at or now, "finalAt": at or now, "detectedAt": now}
        if image:
            data["image"] = image
        self.queue.append(data)
        self._next()

    def _prepare(self, context: str) -> None:
        self.pending_brief, self.brief_edited = context[:20000], False
        self._next()

    def _next(self) -> None:
        if not self.running or self.process is not None or self.answering is not None:
            return
        if self.pending_brief is not None:
            context, self.pending_brief = self.pending_brief, None

            def prepared(result):
                if not self.running:
                    return
                if not self.brief_edited and self.pending_brief is None:
                    if isinstance(result.get("brief"), str) and result["brief"]:
                        self.brief = result["brief"][:BRIEF_LIMIT]
                        self._save([{"kind": "notes", "text": self.brief}])
                    self.js("prepared", result)
                self._next()
            return self._worker("--meeting-prep", {"text": context}, prepared)
        if not self.queue:
            return
        data = self.queue.pop(0)
        data.update(brief=self.brief, style=self.style)
        self.answering = data["id"]
        self.js("thinking", data["id"])
        self._send_answer(data)

    def _brain(self, flag: str):
        from .bridge import ATTICUS_PY, _brain_env
        return subprocess.Popen([sys.executable, str(ATTICUS_PY), flag], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None if os.environ.get("ATTICUS_DEBUG") else subprocess.DEVNULL,   # debugging: the helper's own errors reach the journal
                                text=True, env=_brain_env(), cwd=str(ATTICUS_PY.parent))

    def _warm_answer(self) -> None:
        """The answering process is started ahead: its startup is paid while people talk, not after the question."""
        if not self.running or self.answer_proc is not None:
            return
        try:
            p = self._brain("--meeting-live")
        except OSError:
            return
        self.answer_proc = p
        g = self.generation
        from gi.repository import GLib

        def read():
            for line in p.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                GLib.idle_add(self._answer_event, p, g, event)
            p.wait()
            GLib.idle_add(self._answer_event, p, g, None)
        threading.Thread(target=read, daemon=True).start()

    def _answer_event(self, p, g: int, event: dict | None) -> bool:
        if p is not self.answer_proc:
            return False
        if event is None:   # it ended
            if self.answering is not None:
                self._finish_answer({"error": "Could not finish"}, p)
            else:
                self.answer_proc = None
            return False
        if g != self.generation or not self.running or self.answering is None:
            return False
        if event.get("event") == "text" and isinstance(event.get("text"), str):
            self.js("answerChunk", {"id": self.answering, "text": event["text"], "timing": {}})
        elif event.get("event") == "result":
            self._finish_answer(event.get("result") if isinstance(event.get("result"), dict) else {"error": "Could not finish"}, p)
        return False

    def _send_answer(self, data: dict) -> None:
        if self.answer_proc is None:
            self._warm_answer()
        p = self.answer_proc
        self.answer_question = data.get("question", "")
        if p is None or p.poll() is not None:
            return self._finish_answer({"error": "Could not start"}, p)
        try:
            p.stdin.write(json.dumps(data) + "\n")
            p.stdin.close()
        except (OSError, ValueError):
            self._finish_answer({"error": "Could not start"}, p)

    def _finish_answer(self, result: dict, p) -> None:
        if p is not None and p is not self.answer_proc or self.answering is None:
            return
        identifier, self.answering, self.answer_proc = self.answering, None, None
        if result.get("kind") == "answer":
            self._save([{"kind": "answer", "question": self.answer_question, "lines": result.get("lines") or [], "at": time.time()}])
        self.js("answer", {**result, "id": identifier})
        self._warm_answer()
        self._next()

    def _worker(self, flag: str, data: dict, done: Callable[[dict], None], tracked: bool = True) -> None:
        """One brain helper: JSON in, JSON out, the answer back on the main thread."""
        from gi.repository import GLib
        try:
            p = self._brain(flag)
        except OSError:
            return done({"error": "Could not start"})
        if tracked:
            if self.process and self.process.poll() is None:
                self.process.terminate()   # the one tracked helper; an older one is never left running
            self.process = p
        g = self.generation

        def run():
            try:
                out, _ = p.communicate(json.dumps(data), timeout=180)
                result = json.loads(out)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                p.kill()
                result = {"error": "Could not finish"}

            def back():
                if tracked:
                    if g != self.generation or self.process is not p:
                        return False
                    self.process = None
                done(result if isinstance(result, dict) else {"error": "Could not finish"})
                return False
            GLib.idle_add(back)
        threading.Thread(target=run, daemon=True).start()

    def _gather(self, context: str) -> None:
        if not self.loaded or self.running or self.gathering:
            return
        self.gathering = True
        self.js("gathering", "Reading your current or next calendar event…")
        event, notice = None, None
        try:
            from . import agenda
            event = agenda.current()
        except Exception:
            pass
        if event is None:
            notice = "No current or upcoming calendar event was found; using your notes and connected sources."
        else:
            self.meeting_title = event.get("title", "")
        self.js("gathering", "Searching related email and documents…")

        def gathered(result):
            if not self.gathering:
                return
            self.gathering = False
            if notice:
                result = {**result, "notice": (notice + " " + result["notice"]) if result.get("notice") else notice}
            self.js("gathered", result)
        self._worker("--meeting-gather", {"event": event or {}, "text": context[:20000]}, gathered)

    def _cancel_work(self) -> None:
        for p in (self.process, self.answer_proc):
            if p and p.poll() is None:
                try: p.terminate()
                except OSError: pass
        self.process = self.answer_proc = None
        self.queue, self.pending_brief, self.answering, self.gathering = [], None, None, False

    def stop(self, paused: bool = False) -> None:
        if self.window is None:
            return
        if not self.running:
            if not self.transcript:
                self.discard()
            return
        self.voice, self.voice_text = False, ""
        self.elapsed = time.time() - self.began
        self.running, self.minimized = False, False
        self.generation += 1
        for listener in self.listeners:
            listener.stop()
        self.listeners = []
        self._cancel_work()
        self.pause_wake(False)
        self.js("stopped", paused)
        self.js("saving", self.archive is not None)
        if paused:
            self._save([{"kind": "checkpoint", "duration": self.elapsed}])
        else:
            self._finish_archive()
        self.js("focus")

    def discard(self) -> None:
        if self.running:
            self.stop(paused=True)
        self._finish_archive(summarize=False)
        self.archive = None
        self.generation += 1
        self._cancel_work()
        self.transcript, self.brief = [], ""
        win, self.window, self.page = self.window, None, None
        if win:
            win.destroy()

    def _material(self, paths: list[str]) -> None:
        g = self.generation
        from gi.repository import GLib

        def run():
            text, names = read_material(paths)

            def back():
                if g != self.generation:
                    return False
                if not names:
                    self.js("error", "Add a readable PDF, Word (.docx), text or Markdown file under 10 MB")
                    return False
                self.js("material", text)
                for path in paths:
                    if Path(path).name in names:
                        self.js("attachment", {"name": Path(path).name, "folder": str(Path(path).parent), "img": ""})
                return False
            GLib.idle_add(back)
        threading.Thread(target=run, daemon=True).start()

    def _choose_files(self) -> None:
        from gi.repository import Gio, Gtk
        dialog = Gtk.FileDialog()
        pick = Gtk.FileFilter()
        for ext in MATERIAL:
            pick.add_suffix(ext)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(pick)
        dialog.set_filters(filters)

        def chosen(d, result):
            try:
                files = d.open_multiple_finish(result)
            except Exception:
                return   # closed without choosing
            self._material([files.get_item(i).get_path() for i in range(files.get_n_items())])
        dialog.open_multiple(self.window, None, chosen)

    def _screen(self, question: str) -> None:
        """A question about what is on the screen right now (a shared slide)."""
        g = self.generation
        from gi.repository import GLib

        def run():
            image = ""
            try:
                shot = subprocess.run(["grim", "-t", "jpeg", "-q", "65", "-s", "0.67", "-"], capture_output=True, timeout=10)
                image = base64.b64encode(shot.stdout).decode() if shot.returncode == 0 and shot.stdout else ""
            except (OSError, subprocess.TimeoutExpired):
                pass

            def back():
                if g != self.generation or not self.running:
                    return False
                if not image:
                    self.js("error", "Could not look at screen")
                    return False
                self.serial += 1
                self.js("question", {"id": self.serial, "text": question, "seconds": int(time.time() - self.began), "auto": True})
                self._enqueue(self.serial, question, best=True, image=image)
                return False
            GLib.idle_add(back)
        if self.window:
            self.window.set_visible(False)   # the panel itself must not be in the picture
            GLib.timeout_add(150, lambda: threading.Thread(target=run, daemon=True).start() and False)
            GLib.timeout_add(900, lambda: self.window and self.window.set_visible(True) and False)

    # --- what the page says ----------------------------------------------------------------
    def _message(self, b: dict) -> None:
        action = b.get("do")
        text = lambda key: b.get(key) if isinstance(b.get(key), str) else ""
        if action == "ready":
            self._ready()
        elif action == "start":
            if b.get("room") is True:
                self.call_app = "In person"
            elif self.room:
                self.call_app = "Call"
            if isinstance(b.get("style"), str):   # this call's pick is the next call's default
                cfg.set("meetingStyle", b["style"])
                cfg.set("meetingCustom", text("custom")[:300])
            self.start(text("context"))
        elif action == "size":
            h = b.get("h")
            self._size(h if isinstance(h, (int, float)) else 0, b.get("reveal") if isinstance(b.get("reveal"), int) else None)
        elif action == "resize":
            if self.running and not self.minimized and all(isinstance(b.get(k), (int, float)) for k in ("w", "h")):
                self.expanded = (int(max(280, min(900, b["w"]))), int(max(380, min(self._most_height(), b["h"]))))
                self._size(0)
        elif action == "compact":
            if self.running:
                self.minimized = b.get("on") is True
                self.js("compact", self.minimized)
        elif action == "focus":
            if self.window:
                self.window.present()
            self.js("focus")
        elif action == "savePrep":
            if not self.running:
                cfg.set("meetingPrep", text("text")[:20000])
        elif action in ("gather", "allowCalendar"):
            self._gather(text("context"))
        elif action == "cancelGather":
            if self.gathering:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                self.process, self.gathering = None, False
                self.js("gatherCancelled")
        elif action == "prepare":
            if self.running:
                self._prepare(text("context"))
        elif action == "brief":
            if self.running:
                self.brief_edited, self.brief = True, text("text")[:BRIEF_LIMIT]
                self._save([{"kind": "notes", "text": self.brief}])
        elif action == "stop":
            self.stop()
        elif action == "voice":
            if not self.running:
                return
            if b.get("on") is True:
                self.voice, self.voice_text = True, ""
            elif self.voice:
                self.voice, self.voice_text, self.voice_tail = False, "", time.time() + 1.5
        elif action == "pause":
            self.stop(paused=True)
        elif action == "resume":
            self.start(self.brief)
        elif action == "meetings":
            self.open_settings()
        elif action in ("delete", "save"):
            if action == "delete" or not self.running:
                self.discard()
        elif action == "deleteSaved":
            if not self.running and self.archive:
                archive = self.archive

                def deleted(result):
                    if result.get("deleted"):
                        self.archive = None
                        self.discard()
                self._worker("--meetings", {"action": "delete", "id": archive.id}, deleted, tracked=False)
        elif action == "chooseFiles":
            self._choose_files()
        elif action in ("ask", "answer"):
            if not self.running:
                return
            identifier = b.get("id")
            identifier = int(identifier) if isinstance(identifier, (int, str)) and str(identifier).isdigit() else None
            if identifier is None:
                self.serial += 1
                identifier = self.serial
            if action == "ask":
                self.js("question", {"id": identifier, "text": text("text"), "seconds": int(time.time() - self.began), "auto": True})
            self._enqueue(identifier, text("text"), best=True)
        elif action == "screen":
            if self.running:
                self._screen(text("text") or "Explain the shared slide.")
        elif action == "copy":
            if text("text"):
                try:
                    subprocess.run(["wl-copy"], input=text("text").encode(), timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        # drag, resetFrame, rememberFrame, measured: the panel stays in its corner here


def call_apps() -> set[str]:
    """Programs recording from a microphone right now (a call in progress),
    other than Atticus's own listeners."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "source-outputs"], capture_output=True, text=True, timeout=3).stdout
        rows = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return set()
    names = set()
    for row in rows if isinstance(rows, list) else []:
        props = row.get("properties") or {}
        name = props.get("application.name") or props.get("application.process.binary") or ""
        if name and name.lower() not in ("parecord", "pw-record", "atticus", "python", "python3", "pipewire", "wireplumber") \
                and "echo-cancel" not in str(props.get("node.name", "")) and "monitor" not in str(props.get("media.name", "")).lower():
            names.add(str(name))
    return names
