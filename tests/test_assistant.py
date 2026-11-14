"""Lists and the alarm store's own logic (no systemd needed)."""
import os
import tempfile
import time
import unittest


class Lists(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="atticus-test-")
        os.environ["ATTICUS_DIR"] = self.dir

    def tearDown(self):
        os.environ.pop("ATTICUS_DIR", None)

    def test_roundtrip(self):
        from atticus_shell import assistant as A
        self.assertEqual(A.list_add("lista de compras", ["Leite", "pão"]), ["Leite", "pão"])
        self.assertEqual(A.list_add("compras", ["leite", "ovos"]), ["ovos"])        # no duplicates, any spelling of the name
        self.assertEqual(A.list_items("shopping list"), ["Leite", "pão", "ovos"])
        self.assertEqual(A.list_remove("compras", "pao"), ["pão"])
        self.assertEqual(A.list_remove("compras", "queijo"), [])
        self.assertEqual(A.list_add("tarefas", ["ligar pro banco"]), ["ligar pro banco"])
        self.assertEqual(sorted(A.list_names()), ["compras", "tarefas"])
        self.assertEqual(A.list_clear("compras"), 2)
        self.assertEqual(A.list_items("compras"), [])


class Repeats(unittest.TestCase):
    def test_next_repeat(self):
        from atticus_shell import assistant as A
        fri_8 = time.mktime((2026, 11, 13, 8, 0, 0, 0, 0, -1))
        after = time.mktime((2026, 11, 13, 8, 0, 1, 0, 0, -1))
        self.assertEqual(time.localtime(A._next_repeat(fri_8, "daily", after))[:5], (2026, 11, 14, 8, 0))
        self.assertEqual(time.localtime(A._next_repeat(fri_8, "weekdays", after))[:5], (2026, 11, 16, 8, 0))   # skips the weekend

    def test_fire_text(self):
        from atticus_shell import assistant as A
        at = time.mktime((2026, 11, 14, 7, 30, 0, 0, 0, -1))
        self.assertEqual(A.fire_text({"kind": "timer", "label": "macarrão", "at": at, "lang": "pt"}), "O timer de macarrão terminou.")
        self.assertEqual(A.fire_text({"kind": "alarm", "label": "", "at": at, "lang": "pt"}), "Alarme das 07:30.")
        self.assertEqual(A.fire_text({"kind": "reminder", "label": "call mom", "at": at, "lang": "en"}), "Reminder: call mom.")
        self.assertIn("desligado", A.fire_text({"kind": "reminder", "label": "x", "at": at, "lang": "pt"}, missed=True))

    def test_rejects_bad_times(self):
        from atticus_shell import assistant as A
        self.assertIsNone(A.add("timer", time.time() - 5)[0])
        self.assertIsNone(A.add("alarm", time.time() + 400 * 86400)[0])
        self.assertIsNone(A.add("party", time.time() + 60)[0])


if __name__ == "__main__":
    unittest.main()
