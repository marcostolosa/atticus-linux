"""The brain started while the person is still talking: handed the request when it is known, stopped when it never is."""
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from atticus_shell import bridge

FAKE = textwrap.dedent("""
    import sys
    if sys.argv[1:] == ["--wait"]:
        front, _, task = sys.stdin.readline().rstrip("\\n").rpartition("\\t")
        print("✓ got:" + front + ":" + task, flush=True)
    else:
        print("✓ cold:" + " ".join(sys.argv[1:]), flush=True)
""")


class WarmBrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        script = Path(self.tmp.name, "brain.py")
        script.write_text(FAKE)
        for p in (mock.patch.object(bridge, "ATTICUS_PY", script), mock.patch.object(bridge, "_brain_env", lambda: dict(os.environ)),
                  mock.patch.object(bridge.GLib, "idle_add", lambda *a, **k: 0)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.b = bridge.Bridge.__new__(bridge.Bridge)
        self.b.proc, self.b._lock, self.b._warm, self.b._warm_timer = None, __import__("threading").Lock(), None, None
        self.b.move_file, self.b.pending_pick, self.b.pending_perm, self.b.pick_options = "", None, None, []

    def out(self):
        return self.b.proc.stdout.read()

    def test_the_request_goes_to_the_waiting_brain(self):
        self.b.warm("kitty")
        warm = self.b._warm
        self.assertIsNotNone(warm)
        with mock.patch.object(bridge.threading, "Thread"):   # no reader threads: the test reads the output itself
            self.b.ask("que dia é hoje", "kitty")
        self.assertIs(self.b.proc, warm)
        self.assertIsNone(self.b._warm)
        self.assertEqual(self.out().strip(), "✓ got:kitty:que dia é hoje")

    def test_without_a_waiting_brain_it_starts_one_as_before(self):
        with mock.patch.object(bridge.threading, "Thread"):
            self.b.ask("oi", "")
        self.assertEqual(self.out().strip(), "✓ cold:oi")

    def test_an_unused_brain_is_stopped(self):
        self.b.warm("")
        warm = self.b._warm
        self.b.drop_warm()
        self.assertIsNone(self.b._warm)
        time.sleep(0.3)
        self.assertIsNotNone(warm.poll())

    def test_a_dead_waiting_brain_is_not_used(self):
        self.b.warm("")
        self.b._warm.kill()
        self.b._warm.wait()
        with mock.patch.object(bridge.threading, "Thread"):
            self.b.ask("oi", "")
        self.assertEqual(self.out().strip(), "✓ cold:oi")


if __name__ == "__main__":
    unittest.main()
