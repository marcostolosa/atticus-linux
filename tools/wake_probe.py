#!/usr/bin/env python3
"""Real-voice wake-word probe.

Runs the live mic through the same VAD + Whisper pipeline the wake listener
uses, and prints what it heard for every speech segment plus whether it would
have fired. Say the wake word ("siri" / "ei siri") several times, and mix in
normal talking, to measure true hits and false positives.

Usage (from the repo root, venv active):
    python tools/wake_probe.py                # model=small, lang=pt
    python tools/wake_probe.py base auto      # try another model / language
Each segment is also saved as a .wav under tools/wake_clips/ so good/bad
recordings can later seed a dedicated wake model if we train one.

Ctrl+C to stop; it prints a summary.
"""
import os
import sys
import time
import wave
import subprocess
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atticus_shell.speech import _record_cmd, SAMPLE_RATE  # noqa: E402
from atticus_shell.wakeword import _norm, WakeListener      # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "small"
LANG = sys.argv[2] if len(sys.argv) > 2 else "pt"
CLIPS = Path(__file__).resolve().parent / "wake_clips"
CLIPS.mkdir(exist_ok=True)


def save(pcm: bytes, tag: str, n: int) -> str:
    p = CLIPS / f"{tag}-{n:03d}.wav"
    w = wave.open(str(p), "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
    w.writeframes(pcm); w.close()
    return p.name


def main():
    from faster_whisper import WhisperModel
    print(f"loading whisper {MODEL} (lang={LANG})…")
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    matcher = WakeListener(lambda: None, word="siri")
    print("targets:", sorted(matcher.targets))
    print("\nSpeak now. Say 'siri' / 'ei siri' a few times, then some normal\n"
          "sentences. Ctrl+C when done.\n")

    cmd = _record_cmd()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame = 640
    speech, seg, silence = False, bytearray(), 0
    hits = misses = segs = 0
    try:
        while True:
            data = proc.stdout.read(frame)
            if not data:
                break
            s = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean((s.astype(np.float32) / 32768.0) ** 2))) if s.size else 0.0
            if rms > 0.02:
                speech = True; silence = 0; seg += data
            elif speech:
                silence += 1; seg += data
                if silence > 18:
                    secs = len(seg) / 2 / SAMPLE_RATE
                    buf = bytes(seg); speech, seg, silence = False, bytearray(), 0
                    if 0.2 < secs < 3.0:
                        segs += 1
                        audio = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                        t0 = time.monotonic()
                        parts, _ = model.transcribe(
                            audio, language=(None if LANG == "auto" else LANG),
                            beam_size=1, vad_filter=False, condition_on_previous_text=False)
                        heard = _norm(" ".join(p.text for p in parts))
                        dt = time.monotonic() - t0
                        match = matcher._matches(heard)
                        if match: hits += 1
                        fn = save(buf, "hit" if match else "seg", segs)
                        mark = "  >>> WAKE <<<" if match else ""
                        print(f"[{secs:.1f}s {dt*1000:4.0f}ms] heard={heard!r:30} match={match}{mark}  ({fn})")
            if len(seg) > SAMPLE_RATE * 2 * 4:
                speech, seg, silence = False, bytearray(), 0
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    print(f"\nsegments={segs} wake-matches={hits}  clips in {CLIPS}")


if __name__ == "__main__":
    main()
