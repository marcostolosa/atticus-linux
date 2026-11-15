#!/usr/bin/env python3
"""Measure how well Atticus understands speech, before and after a change.

Every claim about the listening chain ("this makes it hear better") is checked
here first. The bench speaks a set of everyday commands with the piper voice,
puts each one in the conditions a real room adds (distance, echo off the
walls, the noise of this room, a whisper) and counts the words each
recognizer gets wrong.

    .venv/bin/python tools/stt_bench.py                       # every engine, every condition
    .venv/bin/python tools/stt_bench.py --engines local,local+vocab
    .venv/bin/python tools/stt_bench.py --noise room.raw      # 16 kHz mono s16le of the room
    .venv/bin/python tools/stt_bench.py --clips DIR           # real recordings instead

--clips reads DIR/*.wav (16 kHz mono) with the words said in a .txt of the
same name, and is the number that counts: a synthetic voice is one speaker
with perfect diction, so use it to compare two versions, not as the truth.

Engines: local (the model on this machine), local+vocab (the same, told the
names it is likely to hear), local+rumble (local+vocab with the low rumble cut
first, as the assistant does when the server is away), remote (the Home
Assistant server, when set up).
"""
import argparse
import re
import sys
import time
import unicodedata
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atticus_shell import speech  # noqa: E402
from atticus_shell.speech import SAMPLE_RATE  # noqa: E402

PHRASES = [
    "que horas são",
    "coloca um timer de dez minutos",
    "aumenta o volume",
    "abaixa o volume para trinta por cento",
    "qual é a previsão do tempo para amanhã",
    "me acorda amanhã às sete da manhã",
    "abre o navegador",
    "próxima música",
    "pausa a música",
    "quanto é quinze vezes doze",
    "me lembra de ligar para o dentista às três da tarde",
    "diminui o brilho da tela",
]
# {} takes each device name the user has (Home Assistant), or these when there is none.
NAMED = ["liga {}", "desliga {}", "como está {}"]
FALLBACK_NAMES = ["a luz da sala", "a luz do quarto", "o ar-condicionado", "o ventilador"]


# "10" and "dez", "30%" and "trinta por cento", "liga luz" and "liga a luz" are
# the same request: they are counted as heard right.
_SAME = {"um": "1", "uma": "1", "dois": "2", "duas": "2", "tres": "3", "quatro": "4", "cinco": "5", "seis": "6",
         "sete": "7", "oito": "8", "nove": "9", "dez": "10", "doze": "12", "quinze": "15", "vinte": "20",
         "trinta": "30", "2nd": "2"}
_SKIP = {"a", "o", "as", "os"}


def words(text: str) -> list[str]:
    t = unicodedata.normalize("NFD", text.lower().replace("%", " por cento"))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return [_SAME.get(w, w) for w in re.findall(r"[a-z0-9]+", t) if w not in _SKIP]


def errors(said: str, heard: str) -> tuple[int, int]:
    """Word errors (substituted, dropped, added) and the number of words said."""
    a, b = words(said), words(heard)
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1], len(a)


def resample(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE:
        return x.astype(np.float32)
    # Cut what the lower rate cannot carry, then read the curve at the new rate.
    n = 129
    t = np.arange(n) - n // 2
    cut = 0.5 * min(SAMPLE_RATE, rate) / rate * 0.95
    fir = 2 * cut * np.sinc(2 * cut * t) * np.hamming(n)
    x = np.convolve(x, fir / fir.sum(), mode="same")
    at = np.arange(0, len(x), rate / SAMPLE_RATE)
    return np.interp(at, np.arange(len(x)), x).astype(np.float32)


def synthesize(voice, text: str) -> np.ndarray:
    pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(text))
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return resample(x, voice.config.sample_rate)


def room(x: np.ndarray, rng, rt60: float = 0.6, direct_db: float = 0.0) -> np.ndarray:
    """The same voice from across a room: the direct sound plus its echoes
    dying away over rt60 seconds, the echoes together as strong as the direct
    sound (direct_db 0), which is what a few metres from the microphone does."""
    n = int(rt60 * SAMPLE_RATE)
    tail = rng.standard_normal(n) * np.exp(-6.9 * np.arange(n) / n)
    tail[: int(0.004 * SAMPLE_RATE)] = 0
    tail *= 10 ** (-direct_db / 20) / np.sqrt((tail ** 2).sum())
    tail[0] = 1.0
    return np.convolve(x, tail)[: len(x) + n // 2].astype(np.float32)


def with_noise(x: np.ndarray, noise: np.ndarray, snr_db: float, rng) -> np.ndarray:
    lead = SAMPLE_RATE // 2          # the microphone opens before the first word and closes after the last
    x = np.concatenate([np.zeros(lead, np.float32), x, np.zeros(lead, np.float32)])
    start = int(rng.integers(0, max(1, len(noise) - len(x))))
    bed = np.resize(noise[start:], len(x))
    loud = x[np.abs(x) > 0.02 * np.abs(x).max()]
    gain = np.sqrt((loud ** 2).mean() / max((bed ** 2).mean(), 1e-12)) / 10 ** (snr_db / 20)
    return (x + bed * gain).astype(np.float32)


CONDITIONS = {
    # name: (across the room?, speech-to-noise in dB, level of the whole thing)
    "close": (False, 25.0, 0.3),
    "across the room": (True, 12.0, 0.08),
    "whisper, noisy": (True, 4.0, 0.02),
}


def degrade(x: np.ndarray, noise: np.ndarray, name: str, rng) -> np.ndarray:
    far, snr, level = CONDITIONS[name]
    if far:
        x = room(x, rng)
    x = with_noise(x, noise, snr, rng)
    return (x * level / max(np.abs(x).max(), 1e-9)).astype(np.float32)


def load_clips(folder: Path):
    for wav in sorted(folder.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.exists():
            continue
        with wave.open(str(wav), "rb") as w:
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
            if w.getnchannels() > 1:
                raw = raw.reshape(-1, w.getnchannels()).mean(axis=1)
            yield txt.read_text().strip(), resample(raw, w.getframerate())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", default="local,local+vocab,remote")
    ap.add_argument("--model", default=None, help="local model size (default: the one in settings)")
    ap.add_argument("--noise", help="16 kHz mono s16le recording of the room (default: a steady hiss and rumble)")
    ap.add_argument("--clips", help="folder of real recordings: NAME.wav + NAME.txt")
    ap.add_argument("--language", default="pt")
    ap.add_argument("--show", action="store_true", help="print every sentence that was heard wrong")
    args = ap.parse_args()
    rng = np.random.default_rng(7)

    tr = speech.Transcriber(model_size=args.model, language=args.language)
    vocab = speech.vocabulary(refresh=True)
    print(f"vocabulary given to local+vocab: {', '.join(vocab) or '(none)'}\n")

    if args.clips:
        sets = {"recordings": list(load_clips(Path(args.clips)))}
    else:
        from piper import PiperVoice
        from atticus_shell.tts import _piper_model
        voice = PiperVoice.load(_piper_model())
        names = speech.spoken_names() or FALLBACK_NAMES
        said = PHRASES + [t.format(n.lower()) for n in names[:6] for t in NAMED]
        if args.noise:
            noise = np.fromfile(args.noise, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            hiss = rng.standard_normal(SAMPLE_RATE * 20)
            noise = (np.convolve(hiss, np.ones(40) / 40, mode="same") * 4 + hiss * 0.3).astype(np.float32)
        clean = [(s, synthesize(voice, s)) for s in said]
        sets = {c: [(s, degrade(x, noise, c, rng)) for s, x in clean] for c in CONDITIONS}

    def run(engine, audio):
        if engine == "local+rumble":   # what the assistant does when the server is away: rumble cut, then levelled
            return tr.transcribe_local(speech.level_audio(speech.cut_rumble(audio)), args.language, vocab)
        audio = speech.level_audio(audio)
        if engine == "remote":
            return tr.remote.transcribe(audio, args.language)
        return tr.transcribe_local(audio, args.language, vocab if engine == "local+vocab" else [])

    engines = [e for e in args.engines.split(",") if e]
    if "remote" in engines and not tr.remote.entity():
        print("remote: no Home Assistant speech-to-text reachable, skipped\n")
        engines.remove("remote")
    print(f"{'':18}" + "".join(f"{e:>22}" for e in engines))
    for cond, clips in sets.items():
        cells, wrong = [], []
        for e in engines:
            bad = total = 0
            took = time.monotonic()
            for said, audio in clips:
                heard = run(e, audio) or ""
                b, n = errors(said, heard)
                bad, total = bad + b, total + n
                if b:
                    wrong.append((e, said, heard))
            took = (time.monotonic() - took) / max(len(clips), 1)
            cells.append(f"{100 * bad / max(total, 1):5.1f}% wrong {took:4.1f} s")
        print(f"{cond:18}" + "".join(f"{c:>22}" for c in cells), flush=True)
        if args.show:
            for e, said, heard in wrong:
                print(f"    [{e}] said: {said!r}  heard: {heard!r}")


if __name__ == "__main__":
    main()
