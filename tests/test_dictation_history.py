"""The Settings window's Dictation history: add, search by filtering client-side, delete, clear, stats."""
import json
import os
import tempfile
import time
import unittest

from atticus_shell import dictation_history as dh


class History(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="atticus-test-")
        self.old = os.environ.get("ATTICUS_DIR")
        os.environ["ATTICUS_DIR"] = self.dir

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ATTICUS_DIR", None)
        else:
            os.environ["ATTICUS_DIR"] = self.old

    def test_a_dictation_is_kept_with_its_app_and_time(self):
        row = dh.add("Chego às oito.", app="kitty", now=time.time())
        self.assertEqual(row["text"], "Chego às oito.")
        self.assertEqual(row["app"], "kitty")
        self.assertNotIn("raw", row)
        self.assertEqual(dh.load()["history"], [row])

    def test_the_heard_words_are_kept_too_when_they_differ(self):
        row = dh.add("Chego às oito.", raw="chego as oito hã")
        self.assertEqual(row["raw"], "chego as oito hã")

    def test_nothing_said_is_nothing_kept(self):
        self.assertIsNone(dh.add(""))
        self.assertIsNone(dh.add("   "))
        self.assertEqual(dh.load()["history"], [])

    def test_delete_and_clear(self):
        now = time.time()
        a = dh.add("um", now=now - 2)
        dh.add("dois", now=now - 1)
        self.assertTrue(dh.delete(a["id"]))
        self.assertFalse(dh.delete("not-a-real-id"))
        self.assertEqual(len(dh.load()["history"]), 1)
        dh.clear()
        self.assertEqual(dh.load()["history"], [])

    def test_only_the_last_30_days_are_kept(self):
        dh.add("velho", now=time.time() - 40 * 86400)
        dh.add("novo", now=time.time())
        self.assertEqual([r["text"] for r in dh.load()["history"]], ["novo"])

    def test_stats_count_words_today_and_all_time(self):
        now = time.time()
        dh.add("uma frase com cinco palavras", now=now)
        dh.add("outra com quatro", now=now - 90000)   # more than a day ago
        s = dh.load()["stats"]
        self.assertEqual(s["words"], 8)
        self.assertEqual(s["sessions"], 2)
        self.assertGreaterEqual(s["today"], 0)
        self.assertEqual(s["minutes"], round(8 / 40))
