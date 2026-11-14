"""Everything the Settings window's {do: "..."} messages need, with no GTK
import, so it can be tested without a display. settings_window.py is the
thin GTK/WebKit shell around this: it owns the window and the threading,
this owns what each action actually does.

Every brain-backed action (usage stats, rules, routines, the profile,
models) runs `atticus.py` as a subprocess, the same process boundary every
other tool call in this shell crosses (atticus_shell/bridge.py) — never an
in-process import of the brain.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import dictation_history
from . import settings as cfg
from .bridge import ATTICUS_PY, _brain_env

RES = Path(os.environ.get("ATTICUS_RESOURCES_DIR", Path(__file__).resolve().parent.parent))


def brain(*args: str, input: Optional[str] = None, timeout: float = 20) -> str:
    """Run one `atticus.py --flag ...` call and return its stdout, stripped.
    '' on any failure — every caller here treats that as "nothing to show",
    never as a crash: the page must stay usable even when one action the
    brain owns (usage stats, a saved rule) can't be reached right now."""
    try:
        r = subprocess.run([sys.executable, str(ATTICUS_PY), *args], input=input, env=_brain_env(),
                           capture_output=True, text=True, timeout=timeout, cwd=str(RES))
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def brain_json(*args: str, input: Optional[str] = None, timeout: float = 20) -> Any:
    try:
        return json.loads(brain(*args, input=input, timeout=timeout) or "null")
    except ValueError:
        return None


# --- real, checkable facts about this machine (the macOS "Permissions" rows map to these) ---
def mic_ok() -> bool:
    return bool(shutil.which("parecord") or shutil.which("pw-record") or shutil.which("arecord"))


def speech_ok() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def input_ok() -> bool:
    from . import hands
    return hands.available()


def screen_ok() -> bool:
    return shutil.which("grim") is not None


PERM_CHECKS = {"mic": mic_ok, "speech": speech_ok, "ax": input_ok, "screen": screen_ok}
PERM_FIX = {
    "ax": "To click, type and move windows, ydotool needs to be set up: install ydotool, add this "
          "user to the input group, and enable the ydotoold service — see the README.",
    "screen": "Install grim and slurp to let Atticus see the screen.",
    "mic": "No way to record audio was found. Install pipewire-pulse (parecord) or alsa-utils (arecord).",
    "speech": "faster-whisper is not installed in Atticus's own virtual environment; re-run build.sh.",
}


def perms_payload() -> dict:
    return {k: fn() for k, fn in PERM_CHECKS.items()}


def mic_list() -> list[dict]:
    """Real PipeWire/PulseAudio capture devices (not a sink's monitor, not
    Atticus's own echo-cancel source), for the microphone picker."""
    try:
        r = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=3)
        rows = json.loads(r.stdout) if r.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        rows = []
    out = []
    for s in rows if isinstance(rows, list) else []:
        name = s.get("name", "") if isinstance(s, dict) else ""
        props = s.get("properties", {}) if isinstance(s, dict) and isinstance(s.get("properties"), dict) else {}
        if not name or name.startswith("atticus_") or props.get("device.class") != "sound":
            continue
        out.append({"name": props.get("device.description") or name, "uid": name})
    return out


def set_microphone(uid: str) -> None:
    if uid and uid != "automatic":
        try:
            subprocess.run(["pactl", "set-default-source", uid], capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def hyprland_binds() -> dict[str, str]:
    """The press-to-talk and dictation shortcuts, read from Hyprland's live
    bind table — there is no in-window recorder here: compositor-level
    binds are set in ~/.config/hypr/, not by holding a key inside Atticus
    (see the README)."""
    try:
        rows = json.loads(subprocess.run(["hyprctl", "-j", "binds"], capture_output=True, text=True, timeout=3).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        rows = []
    found: dict[str, str] = {}
    for b in rows if isinstance(rows, list) else []:
        arg = str(b.get("arg", "")) if isinstance(b, dict) else ""
        combo = "+".join(x for x in (b.get("mods", ""), b.get("key", "")) if x) if isinstance(b, dict) else ""
        if re.search(r"atticus-ctl\s+(press|toggle)\b", arg):
            found["talk"] = combo
        elif "atticus-ctl dictate" in arg:
            found["dictate"] = combo
    return found


def state_payload() -> dict:
    return brain_json("--state") or {"today": {}, "week": {}, "all": {}, "asks": [], "rules": [], "skills": [],
                                      "me": {"facts": []}, "plan": {"ok": False, "text": "Could not reach the brain."}}


def dictation_payload() -> dict:
    d = dictation_history.load()
    return {"words": [w for w in (cfg.get("dictateWords", []) or []) if isinstance(w, str)],
            "history": d["history"], "stats": d["stats"]}


# The real effective default for every setting the window edits that has one
# elsewhere in the code (atticus.py's pref(), or this shell's cfg.get()/
# tts.py's _setting()) — not just what the page itself assumes. Without this,
# a key nobody has ever changed shows the wrong state (e.g. a switch painted
# off for something that is really on by default) until it is first touched.
# Checked against every call site with `grep -n 'pref(\|cfg\.get(\|_setting('`
# across atticus.py and atticus_shell/ (keep this in step with those).
DEFAULTS: dict[str, object] = {
    "safe": True, "model": "sonnet", "draw": True, "improve": False,
    "saveChats": True, "screen": "never", "showAnswers": "stream",
    "paused": False, "agentUserTools": True, "meetingSummary": True,
    "speak": True, "dictateMode": "verbatim", "dictateHistory": True,
    "wakeWord": False, "wakeWordName": "atticus", "talkMode": "hold",
    "uiLanguage": "auto", "microphone": "automatic", "whisperModel": "turbo",
    "whisperLanguage": "auto", "meetingSave": True, "client": "claude",
}


def prefs_with_defaults() -> dict:
    return {**DEFAULTS, **cfg.all()}


def initial_state() -> dict:
    icon_path = RES / "ui" / "AppIcon.icns"
    version = ""
    try:
        version = (RES / "VERSION").read_text().strip()
    except OSError:
        pass
    binds = hyprland_binds()
    return {
        "prefs": {**prefs_with_defaults(),
                  "hotkeyLabel": binds.get("talk", ""), "dictateHotkeyLabel": binds.get("dictate", "")},
        "routines": brain_json("--routine-list") or [],
        "icon": str(icon_path) if icon_path.exists() else "",
        "version": version,
        "managedUpdates": True,     # no in-app updater here: updates come from re-running build.sh
        "update": "Updates are applied by re-running the installer (build.sh), not checked automatically.",
        "sharingUnavailable": False,
        "hotkeyTaken": False,
        "dictateHotkeyTaken": False,
        "state": state_payload(),
        "binds": binds,
    }


def dispatch(body: dict) -> Optional[tuple[str, Any]]:
    """One {do: "..."} message -> (st.<name> to call back, its payload), or
    None when the page already applied the change itself and nothing need
    be echoed back (most plain settings). "__quit__" asks the caller to end
    the whole application instead of calling a page function."""
    do = body.get("do")
    if do == "set":
        k, v = body.get("k"), body.get("v")
        if isinstance(k, str):
            cfg.set(k, v)
            if k == "microphone" and isinstance(v, str):
                set_microphone(v)
        return None
    if do == "copy" and isinstance(body.get("text"), str):
        try:
            p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
            p.communicate(body["text"].encode("utf-8"), timeout=3)
        except Exception:
            pass
        return None
    if do == "remember" and isinstance(body.get("line"), str):
        brain("--remember", body["line"])
        return "state", state_payload()
    if do == "rule" and isinstance(body.get("old"), str) and isinstance(body.get("text"), str):
        brain("--rule", body["old"], body["text"])
        return "state", state_payload()
    if do in ("forget", "unlearn"):
        line = body.get("line") if do == "forget" else body.get("say")
        if isinstance(line, str):
            brain("--forget", line)
        return "state", state_payload()
    if do == "clearRecent":
        try:
            (Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus")) / "recent.jsonl").unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if do == "routineRemove" and isinstance(body.get("id"), str):
        brain("--routine", "remove", body["id"])
        return "load", initial_state()
    if do == "me" and isinstance(body.get("action"), str):
        action = body["action"]
        if action == "set" and isinstance(body.get("id"), str) and isinstance(body.get("text"), str):
            brain("--me", "set", body["id"], body["text"])
        elif action == "drop" and isinstance(body.get("id"), str):
            brain("--me", "drop", body["id"], "Settings")
        elif action == "add" and isinstance(body.get("category"), str) and isinstance(body.get("text"), str):
            brain("--me", "add", body["category"], body["text"])
        elif action == "forget-source" and isinstance(body.get("source"), str):
            brain("--me", "forget-source", body["source"], "Settings")
        elif action == "import":
            return "profileImport", {"errors": ["contacts", "mail"], "errorReasons": {}, "counts": {}}
        return "state", state_payload()
    if do == "dictationWord":
        words = [w for w in (cfg.get("dictateWords", []) or []) if isinstance(w, str)]
        old, word = body.get("old"), str(body.get("word", "")).strip()
        if old in words:
            words = [word if w == old else w for w in words] if word else [w for w in words if w != old]
        elif word and word not in words:
            words.append(word)
        cfg.set("dictateWords", words[:500])
        return "dictation", dictation_payload()
    if do == "dictationWordDelete" and isinstance(body.get("word"), str):
        cfg.set("dictateWords", [w for w in (cfg.get("dictateWords", []) or []) if w != body["word"]])
        return "dictation", dictation_payload()
    if do == "dictationDelete" and isinstance(body.get("id"), str):
        dictation_history.delete(body["id"])
        return "dictation", dictation_payload()
    if do == "dictationClear":
        dictation_history.clear()
        return "dictation", dictation_payload()
    if do == "models":
        return "models", {"base": cfg.get("apiBase", ""), "key": cfg.get("apiKey", ""), "list": brain_json("--models") or []}
    if do == "localServers":
        return "local", brain_json("--local-servers") or []
    if do == "connectors":
        return "connectors", brain_json("--connectors") or {"connected": [], "signin": []}
    if do == "agents":
        return "agents", {"count": 0}   # background agents are not wired into this shell yet
    if do == "apps":
        from . import assistant
        try:
            names = sorted({a["name"] for a in assistant.apps() if a.get("name")})
        except Exception:
            names = []
        return "apps", [{"id": n, "name": n, "status": "allowed", "suggested": False} for n in names]
    if do == "grant" and isinstance(body.get("id"), str):
        kind = body["id"]
        if kind in PERM_CHECKS:
            return ("perms", perms_payload()) if PERM_CHECKS[kind]() else ("notice", {"text": PERM_FIX[kind]})
        return "notice", {"text": "There is no per-app permission to grant on Linux: one setup "
                                  "(Accessibility, above) covers every app ydotool can reach."}
    if do in ("allow", "stopAllow", "enableAll"):
        return "notice", {"text": "Background agents are not available in this build yet."}
    if do == "automation":
        return "notice", {"text": "There is no Automation permission on Linux; see Accessibility, above."}
    if do == "setup":
        return "notice", {"text": "Run `claude /login` in a terminal, or set a custom server address below."}
    if do == "resetHotkey":
        return "notice", {"text": "Shortcuts are set in ~/.config/hypr/, not recorded in this window on Linux "
                                  "— see the README for the lines to bind."}
    if do == "globeKey":
        return None
    if do == "checkUpdate":
        return "load", initial_state()
    if do in ("record", "recordDictate", "cancelRecord"):
        return "notice", {"text": "Shortcuts are set in ~/.config/hypr/ (see the README), not recorded here."}
    if do in ("whisperInstall", "whisperDownload", "whisperCancel", "whisperDelete"):
        return "notice", {"text": "The speech-model manager is not wired into this window yet. Set `asrModel` "
                                  "in ~/.config/atticus/settings.json for now."}
    if do in ("meetings", "meetingOpen", "meetingCopy", "meetingExport", "meetingDelete"):
        return "meetings", {"notice": "Meetings are not wired into this window yet."}
    if do == "feedbackPreview":
        result = brain_json("--feedback", input=json.dumps({"text": body.get("text", ""), "include": bool(body.get("include"))}))
        if isinstance(result, dict):
            result["revision"] = body.get("revision")
        return "feedback", result or {"error": "Could not prepare the preview."}
    if do == "feedbackOpen":
        result = brain_json("--feedback", input=json.dumps({"text": "", "include": False}))
        url = (result or {}).get("url") if isinstance(result, dict) else None
        if url:
            try:
                subprocess.run(["xdg-open", url], capture_output=True, timeout=3)
            except OSError:
                pass
        return None
    if do == "quit":
        return "__quit__", None
    return None
