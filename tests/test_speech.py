import subprocess
import sys
import time
import unittest
from unittest import mock

import numpy as np

from atticus_shell import speech


class Vocabulary(unittest.TestCase):
    """The names the recognizer is told to expect."""

    def names(self, own, devices, **kw):
        with mock.patch.object(speech, "_setting", lambda k, d=None: own if k == "asrVocabulary" else d), \
                mock.patch.object(speech, "spoken_names", lambda refresh=False: devices):
            return speech.vocabulary(**kw)

    def test_the_users_own_names_come_first(self):
        self.assertEqual(self.names(["Dom"], ["Luz da Cozinha"]), ["Dom", "Luz da Cozinha"])

    def test_a_name_with_no_new_word_is_left_out(self):
        self.assertEqual(self.names([], ["Luz da Sala", "Sala", "luz da sala"]), ["Luz da Sala"])

    def test_the_list_stays_short(self):
        many = [f"Tomada {i} da oficina" for i in range(200)]
        self.assertLessEqual(sum(len(n.split()) for n in self.names([], many, limit=20)), 20)


class HandedOnStream(unittest.TestCase):
    """A recorder that carries on from a stream someone else opened."""

    def test_what_was_heard_before_and_after_is_one_recording(self):
        after = np.arange(4000, dtype=np.int16)
        proc = subprocess.Popen([sys.executable, "-c", "import sys,time; sys.stdout.buffer.write(bytes(%r)); "
                                 "sys.stdout.flush(); time.sleep(5)" % after.tobytes()], stdout=subprocess.PIPE)
        before = np.full(16000, 7, dtype=np.int16)
        rec = speech.Recorder()
        self.assertTrue(rec.start(stream=(proc, before.tobytes())))
        self.assertEqual(rec.lead, 1.0)
        time.sleep(0.6)
        got = (rec.stop() * 32768).round().astype(np.int16)
        self.assertEqual(got[:16000].tolist(), before.tolist())
        self.assertEqual(got[16000:16000 + 2048].tolist(), after[:2048].tolist())
        self.assertIsNotNone(proc.poll())    # the stream is closed with the recording


class RecorderCommand(unittest.TestCase):
    def test_audio_is_asked_for_in_small_pieces(self):
        # Without a named latency parecord hands audio over two seconds at a time.
        with mock.patch.object(speech.shutil, "which", lambda name: "/usr/bin/" + name if name == "parecord" else None):
            self.assertIn(f"--latency-msec={speech.LATENCY_MS}", speech._record_cmd())
        self.assertLessEqual(speech.LATENCY_MS, 100)


if __name__ == "__main__":
    unittest.main()


class RumbleCutTest(unittest.TestCase):
    def test_takes_the_fan_and_leaves_the_voice(self):
        import numpy as np
        from atticus_shell import speech
        t = np.arange(16000) / 16000
        fan, voice = 0.3 * np.sin(2 * np.pi * 60 * t), 0.05 * np.sin(2 * np.pi * 400 * t)
        out = speech.cut_rumble((fan + voice).astype(np.float32))
        self.assertEqual(out.size, 16000)
        self.assertLess(float(np.sqrt(np.mean((out - voice) ** 2))), 0.002)   # what is left is the voice
        only_voice = speech.cut_rumble(voice.astype(np.float32))
        self.assertLess(float(np.max(np.abs(only_voice - voice))), 0.001)       # and a voice alone is untouched
