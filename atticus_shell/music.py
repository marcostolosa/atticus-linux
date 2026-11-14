"""Music by name.

Two ways to play something the user names:

  * Spotify, when its app is installed: the item's address is handed to the
    app through the desktop's media interface (MPRIS, via playerctl).
  * otherwise mpv, playing the sound of the best match on YouTube (yt-dlp
    finds it). mpv carries the same media interface, so "pausa", "próxima"
    and "o que está tocando" work on it like on any other player.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time

SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "atticus-mpv.sock")
MIX = 15   # how many results "mix" lines up


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _status(player: str) -> tuple[str, str]:
    """(Playing|Paused|Stopped|'', "artist - title") of one player."""
    r = _run(["playerctl", "-p", player, "metadata", "--format", "{{status}}\t{{artist}}\t{{title}}"], 3)
    if not r or r.returncode or not r.stdout.strip():
        return "", ""
    status, artist, title = (r.stdout.strip().split("\t") + ["", ""])[:3]
    return status, " - ".join(x for x in (artist, title) if x)


def _wait_playing(player: str, seconds: float, not_title: str = "") -> str:
    """The title once the player is playing (something other than not_title), '' if it never does."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        status, title = _status(player)
        if status == "Playing" and title and title != not_title:
            return title
        time.sleep(0.25)
    return ""


# --- Spotify ----------------------------------------------------------------
def spotify_command() -> str | None:
    return shutil.which("spotify-launcher") or shutil.which("spotify")


def spotify_play(uri: str, label: str) -> str:
    """Play one Spotify address (spotify:track:…) in the app, starting it if needed."""
    cmd = spotify_command()
    if not cmd or not shutil.which("playerctl"):
        return "Spotify is not installed."
    listed = lambda: "spotify" in ((_run(["playerctl", "-l"], 3) or subprocess.CompletedProcess([], 1, "", "")).stdout)
    if not listed():
        starting = _run(["pgrep", "-f", "spotify"], 3)
        if not (starting and starting.stdout.strip()):     # never a second copy while one is coming up
            try:
                subprocess.Popen([cmd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                return f"Could not start Spotify: {e}."
        end = time.monotonic() + 25
        while time.monotonic() < end and not listed():
            time.sleep(0.5)
        if not listed():
            # the first start downloads the app and asks the user to sign in
            return ("Spotify is still starting; the first time it downloads itself and asks to sign in. "
                    "Tell the user to finish that in the Spotify window and ask again.")
        time.sleep(1.5)                # it answers a moment before it can take an address
    before = _status("spotify")[1]
    r = _run(["playerctl", "-p", "spotify", "open", uri], 8)
    if not r or r.returncode:
        return f"Could not start {label} in Spotify: the app did not take it (is it open and signed in?)."
    title = _wait_playing("spotify", 8, not_title=before) or _wait_playing("spotify", 2)
    return f"Playing {title or label} on Spotify." if title or _status("spotify")[0] == "Playing" else \
        f"Could not start {label} in Spotify: it did not begin to play (signed in?)."


# --- mpv ---------------------------------------------------------------------
def _mpv(command: list) -> dict | None:
    """One command to the mpv Atticus started, over its socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(SOCK)
            s.sendall((json.dumps({"command": command}) + "\n").encode())
            for line in s.makefile():
                reply = json.loads(line)
                if "error" in reply:
                    return reply
    except (OSError, ValueError):
        return None
    return None


def running() -> bool:
    reply = _mpv(["get_property", "pid"])
    return bool(reply and reply.get("error") == "success")


def _prop(name: str):
    reply = _mpv(["get_property", name])
    return reply.get("data") if reply and reply.get("error") == "success" else None


def _wait_sound(seconds: float) -> str:
    """The title once sound is really coming out: the search has been
    answered (the title is no longer the search itself) and time is passing."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        title, at = _prop("media-title"), _prop("time-pos")
        if isinstance(title, str) and title and not title.startswith(("ytsearch", "ytdl://")) \
                and isinstance(at, (int, float)) and at > 0:
            return title
        if not running():
            return ""          # the player gave up: nothing was found
        time.sleep(0.3)
    return ""


def _target(name: str, mix: bool) -> str:
    return f"ytdl://ytsearch{MIX if mix else 1}:{name}"


def play(name: str, mix: bool = False) -> str:
    """Play what was named: the best match, or with mix a run of matches."""
    name = " ".join((name or "").split())
    if not name:
        return "Name a song, an artist or an album."
    if not shutil.which("mpv") or not shutil.which("yt-dlp"):
        return "Playing music by name needs mpv and yt-dlp (or the Spotify app)."
    if running():
        _mpv(["quit"])
        time.sleep(0.3)
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    try:
        subprocess.Popen(["mpv", "--no-video", "--really-quiet", "--no-terminal", "--force-window=no",
                          "--ytdl-format=bestaudio/best", f"--input-ipc-server={SOCK}", _target(name, mix)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return f"Could not start the player: {e}."
    title = _wait_sound(25)
    if not title:
        if not running():
            return f'Found nothing to play for "{name}".'
        return f'Started "{name}"; it is still loading.'
    return f"Playing {title}." + (" More like it follow." if mix else "")


def queue(name: str) -> str:
    """Add one more after what is playing (in the player Atticus started)."""
    name = " ".join((name or "").split())
    if not name:
        return "Name the song to add."
    if not running():
        return play(name)
    reply = _mpv(["loadfile", _target(name, False), "append-play"])
    if not reply or reply.get("error") != "success":
        return f'Could not add "{name}" to the queue.'
    return f'Added "{name}" to the queue.'
