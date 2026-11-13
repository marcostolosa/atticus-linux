#!/usr/bin/env python3
"""Interactive mic calibration + validation (run it in YOUR terminal).

Removes the "speak exactly now" sync problem: it sets a sane mic gain, shows a
countdown and a LIVE level bar while you speak, then reports whether real speech
was captured (energy contrast, clipping), transcribes it (pt-BR), and tests the
wake-word match. Use it to confirm the whole mic -> Whisper chain works and to
pick a gain.

    cd ~/atticus-linux && source .venv/bin/activate && python tools/mic_test.py
    python tools/mic_test.py --capture 70      # try a louder capture gain (%)
"""
import argparse
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atticus_shell.speech import _record_cmd, SAMPLE_RATE  # noqa: E402
from atticus_shell.wakeword import _norm, WakeListener      # noqa: E402

CARD = "0"


def amixer(*args):
    subprocess.run(["amixer", "-c", CARD, *args],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_gain(pw_volume_pct: int):
    # On PipeWire the mic LEVEL is the source node volume (pactl), not the ALSA
    # "Capture". Keep "Internal Mic Boost" at 0 (it rails the ADC when high).
    amixer("sset", "Internal Mic Boost", "0")
    subprocess.run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{pw_volume_pct}%"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"gain: PipeWire source volume={pw_volume_pct}%, Internal Mic Boost=0")


def record(seconds: float) -> np.ndarray:
    cmd = _record_cmd()
    if not cmd:
        print("no recorder (parecord/pw-record) found"); sys.exit(1)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks, peak_seen = [], 0.0
    frames = int(seconds * SAMPLE_RATE * 2)
    got = 0
    while got < frames:
        data = proc.stdout.read(1600 * 2)  # 0.1s
        if not data:
            break
        chunks.append(data); got += len(data)
        s = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(s ** 2))) if s.size else 0.0
        peak_seen = max(peak_seen, float(np.abs(s).max()) if s.size else 0.0)
        bar = "#" * min(40, int(rms * 200))
        clip = "  <<< CLIPPING!" if peak_seen > 0.98 else ""
        print(f"\r  level |{bar:<40}| rms={rms:.3f} peak={peak_seen:.2f}{clip}", end="", flush=True)
    proc.terminate()
    print()
    return np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--volume", "--capture", type=int, default=32, dest="volume",
                    help="PipeWire mic volume %% (default 45)")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--model", default="small")
    args = ap.parse_args()

    print("=== Atticus mic test ===")
    set_gain(args.volume)
    print("\nGet ready to say a sentence AND the wake word, e.g.:")
    print("    'teste um dois três, ei siri'\n")
    for n in (3, 2, 1):
        print(f"  recording in {n}…", end="\r", flush=True); time.sleep(1)
    print("  SPEAK NOW!                 ")
    audio = record(args.seconds)

    # contrast analysis
    fr = np.array([np.sqrt(np.mean(audio[i:i+1600] ** 2))
                   for i in range(0, max(0, len(audio) - 1600), 1600)])
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    speech_fr = float(fr.max()) if fr.size else 0.0
    quiet_fr = float(np.percentile(fr, 20)) if fr.size else 0.0
    print(f"\nanalysis: peak={peak:.3f} loudest-0.1s={speech_fr:.3f} quiet-floor={quiet_fr:.3f}")
    if peak > 0.98:
        print("  ⚠ CLIPPING — lower volume: rerun with --volume", max(5, args.volume - 15))
    elif speech_fr < 0.04:
        print("  ⚠ too quiet / no speech caught — raise volume: --volume", min(100, args.volume + 20),
              "and speak during 'SPEAK NOW'")
    else:
        print("  ✓ good speech/silence contrast")

    print("\ntranscribing (faster-whisper, pt)…")
    from faster_whisper import WhisperModel
    m = WhisperModel(args.model, device="cpu", compute_type="int8")
    seg, info = m.transcribe(audio, language="pt", beam_size=1, vad_filter=False,
                             condition_on_previous_text=False)
    text = " ".join(s.text for s in seg).strip()
    print(f"  TRANSCRIPTION: {text!r}")

    wl = WakeListener(lambda: None, word="ei siri")
    matched = wl._matches(_norm(text))
    print(f"  wake-word ('ei siri') match in that text: {matched}")
    print("\nDone. If the transcription matches what you said, the mic chain works.")
    print("Tell Claude the printed peak/contrast and the TRANSCRIPTION line.")


if __name__ == "__main__":
    main()
