"""What the shell writes down for the brain to read back."""
import json
import os
import tempfile
import unittest

from atticus_shell import recent


class Recent(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="atticus-test-")
        self.old = os.environ.get("ATTICUS_DIR")
        os.environ["ATTICUS_DIR"] = self.dir

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ATTICUS_DIR", None)
        else:
            os.environ["ATTICUS_DIR"] = self.old

    def rows(self):
        with open(os.path.join(self.dir, "recent.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_a_turn_is_kept_in_the_shape_the_brain_reads(self):
        self.assertTrue(recent.add("o ar está ligado", "Ar-condicionado está ligado.", now=1000))
        self.assertEqual(self.rows(), [{"t": 1000, "q": "o ar está ligado", "a": "Ar-condicionado está ligado."}])
        self.assertEqual(os.stat(os.path.join(self.dir, "recent.jsonl")).st_mode & 0o077, 0)

    def test_only_the_last_few_minutes_stay(self):
        recent.add("um", "1", now=1000)
        for i, at in enumerate((2000, 2010, 2020, 2030)):
            recent.add(f"q{i}", f"a{i}", now=at)
        self.assertEqual([r["q"] for r in self.rows()], ["q1", "q2", "q3"])

    def test_nothing_said_is_nothing_kept(self):
        self.assertFalse(recent.add("", "x"))
        self.assertFalse(recent.add("x", " "))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "recent.jsonl")))
