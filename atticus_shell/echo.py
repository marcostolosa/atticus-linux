"""Echo cancellation, so the microphone can stay open while Atticus talks.

PipeWire's echo-cancel module gives a virtual speaker and a virtual
microphone: whatever is played into the speaker is subtracted from what the
microphone hears. Atticus plays its voice there and listens there while it
speaks, so a person talking over it is heard and its own voice is not.
"""
from __future__ import annotations

import shutil
import subprocess

SOURCE, SINK = "atticus_aec_source", "atticus_aec_sink"


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ready() -> bool:
    return bool(shutil.which("pactl")) and SOURCE in _pactl("list", "short", "sources")


def ensure() -> bool:
    """Load the module once per audio session. False when it is unavailable."""
    if not shutil.which("pactl"):
        return False
    if ready():
        return True
    # No automatic gain: with nobody talking it turns up what is left of
    # Atticus's own voice until it sounds like someone speaking.
    _pactl("load-module", "module-echo-cancel", "aec_method=webrtc",
           f"source_name={SOURCE}", f"sink_name={SINK}",
           'aec_args="webrtc.gain_control=false"')
    return ready()


def own_words(heard: str, spoken: str) -> str:
    """What is left of a transcript after removing Atticus's own words. Some
    of its voice always leaks past the canceller; a transcript that is just a
    run of the sentence being spoken ("está ligado em 18 graus") is that
    leak, not the user. Returns "" for pure echo, otherwise the transcript
    with any leading echo cut off ("ligado em 18 graus liga a luz" →
    "liga a luz")."""
    import difflib
    import re
    import unicodedata

    def words(s):
        # numbers apart from letters: "17:12" spoken comes back written "17h12"
        s = unicodedata.normalize("NFD", s.lower())
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return [w for w in re.findall(r"\d+|[^\W\d_]+", s) if w != "h"]
    raw = re.findall(r"\S+", heard)
    h, s = words(heard), words(spoken)
    if not h:
        return ""
    if len(raw) != len(h):          # punctuation split differently: compare, do not cut
        raw = h
    match = difflib.SequenceMatcher(None, h, s, autojunk=False)
    m = match.find_longest_match(0, len(h), 0, len(s))
    if m.size >= 2 and m.size >= 0.7 * len(h):
        return ""
    # the sentence with a word or two heard wrong in the middle is still the sentence
    same = sum(b.size for b in match.get_matching_blocks())
    if len(h) >= 3 and same >= 0.75 * len(h) and not (m.size >= 3 and m.a == 0 and len(h) - m.size >= 2):
        return ""
    # One word that is in the sentence: its own voice, unless it is a short word
    # people really say over it ("não", "para"). A number alone is never that.
    if len(h) == 1 and len(s) > 1 and h[0] in s and (len(h[0]) > 3 or h[0].isdigit()):
        return ""
    if m.size >= 3 and m.a == 0:    # starts with a run of the spoken sentence
        return " ".join(raw[m.size:])
    return " ".join(raw) if raw is h else heard.strip()
