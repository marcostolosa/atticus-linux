"""Meeting Copilot: cutting sound into utterances, the saved record, the brief's files."""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from atticus_shell import meeting

RATE = 16000


def tone(seconds, level=0.1):
    t = np.arange(int(seconds * RATE)) / RATE
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def quiet(seconds):
    return np.zeros(int(seconds * RATE), dtype=np.float32)


def loud_ago(audio):
    """Stand-in voice detector: a "voice" is any stretch louder than the floor."""
    loud = np.nonzero(np.abs(audio) > 0.02)[0]
    return audio.size / RATE if loud.size == 0 else (audio.size - 1 - loud[-1]) / RATE


class SegmenterTest(unittest.TestCase):
    def feed(self, seg, audio):
        out = []
        for i in range(0, audio.size, 640):   # 40 ms at a time, as the recorder hands it over
            out += seg.feed(audio[i:i + 640])
        return out

    def test_a_pause_ends_an_utterance(self):
        seg = meeting.Segmenter(loud_ago)
        got = self.feed(seg, np.concatenate([quiet(1), tone(2), quiet(1.5), tone(1), quiet(1.5)]))
        self.assertEqual(len(got), 2)
        self.assertAlmostEqual(got[0].size / RATE, 2.5, delta=0.5)   # the words, a little room around them
        self.assertAlmostEqual(got[1].size / RATE, 1.5, delta=0.6)

    def test_silence_and_noise_without_a_voice_give_nothing(self):
        seg = meeting.Segmenter(loud_ago)
        self.assertEqual(self.feed(seg, quiet(6)), [])
        self.assertLess(seg.buf.size, RATE)   # and nothing piles up
        music = meeting.Segmenter(lambda audio: audio.size / RATE)   # loud, and the detector hears no voice in it
        self.assertEqual(self.feed(music, tone(6)), [])
        self.assertLess(music.buf.size, RATE)

    def test_nobody_pauses(self):
        seg = meeting.Segmenter(loud_ago)
        got = self.feed(seg, tone(45))
        self.assertEqual(len(got), 2)
        self.assertTrue(all(abs(g.size / RATE - meeting.LONGEST) < 1 for g in got))

    def test_what_was_being_said_when_it_stopped(self):
        seg = meeting.Segmenter(loud_ago)
        self.assertEqual(self.feed(seg, np.concatenate([quiet(1), tone(1.5)])), [])
        self.assertGreater(seg.rest().size, RATE)
        self.assertIsNone(seg.rest())


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ATTICUS_DIR")
        os.environ["ATTICUS_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("ATTICUS_DIR", None) if self.old is None else os.environ.__setitem__("ATTICUS_DIR", self.old)
        self.tmp.cleanup()

    def test_archive_layout(self):
        a = meeting.Archive("Reunião de equipe", "Call", 1000.0)
        self.assertTrue(a.append([{"kind": "transcript", "text": "Olá, tudo bem?", "seconds": 3, "mic": False, "speaker": "Them"}]))
        self.assertTrue(a.append([{"kind": "ended", "duration": 60}]))
        self.assertEqual(json.loads((a.folder / "metadata.json").read_text()), {"app": "Call", "started": 1000.0, "title": "Reunião de equipe", "version": 1})
        rows = [json.loads(l) for l in (a.folder / "events.jsonl").read_text().splitlines()]
        self.assertEqual([r["kind"] for r in rows], ["transcript", "ended"])
        self.assertEqual(rows[0]["text"], "Olá, tudo bem?")
        self.assertEqual(oct((a.folder / "events.jsonl").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(a.folder.stat().st_mode & 0o777), "0o700")

    def test_material(self):
        d = Path(self.tmp.name)
        (d / "pauta.md").write_text("# Pauta\nOrçamento de 18 mil.")
        with zipfile.ZipFile(d / "ata.docx", "w") as z:
            z.writestr("word/document.xml", "<w:document><w:p><w:t>Primeira linha</w:t></w:p><w:p><w:t>Segunda</w:t></w:p></w:document>")
        (d / "foto.png").write_bytes(b"\x89PNG")
        (d / "vazio.txt").write_text("  ")
        text, names = meeting.read_material([str(d / n) for n in ("pauta.md", "ata.docx", "foto.png", "vazio.txt", "sumiu.txt")])
        self.assertEqual(names, ["pauta.md", "ata.docx"])
        self.assertIn("Orçamento de 18 mil.", text)
        self.assertIn("Primeira linha\nSegunda", text)

    def test_who_spoke(self):
        self.assertEqual(meeting.speaker_names(True, False, "Call"), ("You", "You"))
        self.assertEqual(meeting.speaker_names(False, False, "Call"), ("Them", "Someone else"))
        self.assertEqual(meeting.speaker_names(False, False, "Zoom"), ("Zoom", "Someone on Zoom"))
        self.assertEqual(meeting.speaker_names(True, True, "In person"), ("Room", "Someone in the room"))   # in a room the microphone hears everyone


if __name__ == "__main__":
    unittest.main()
