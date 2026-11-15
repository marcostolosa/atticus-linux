"""The agents panel: the decisions that need no window and no supervisor."""
import os
import tempfile
import unittest

from atticus_shell import agents


class AgentsTest(unittest.TestCase):
    def test_finished_task_is_news_for_ten_minutes(self):
        now = 10_000.0
        self.assertTrue(agents.lingers({"status": "done", "ended": now - 30}, now))
        self.assertTrue(agents.lingers({"status": "failed", "ended": now - 3000, "seenAt": now - 100}, now))   # looked at a moment ago
        self.assertFalse(agents.lingers({"status": "done", "ended": now - 601}, now))
        self.assertFalse(agents.lingers({"status": "running"}, now))
        self.assertFalse(agents.lingers({"status": "stopped", "ended": now}, now))

    def test_badge_counts_working_and_flags_a_question(self):
        rows = {"a": {"status": "running"}, "b": {"status": "queued"}, "c": {"status": "done"}}
        self.assertEqual(agents.badge(rows), {"count": 1, "attention": False})
        rows["d"] = {"status": "needs-you"}
        self.assertEqual(agents.badge(rows), {"count": 1, "attention": True})

    def test_three_at_once(self):
        rows = {str(i): {"status": s} for i, s in enumerate(["running", "needs-you", "queued", "done"])}
        self.assertFalse(agents.slots_full(rows))
        rows["x"] = {"status": "waiting-screen"}
        self.assertTrue(agents.slots_full(rows))
        rows["x"]["ended"] = 1.0   # it finished while asking: its place is free
        self.assertFalse(agents.slots_full(rows))

    def test_newest_first(self):
        rows = {"a": {"id": "a", "created": 1}, "b": {"id": "b", "created": 3}, "c": {"id": "c"}}
        self.assertEqual([r["id"] for r in agents.newest_first(rows)], ["b", "a", "c"])

    def test_what_a_card_opens(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "relatorio.md"), "w").close()
            row = {"directory": d, "made": [{"url": "relatorio.md"}], "links": ["https://example.com/x"]}
            self.assertEqual(agents.opens(row, None, False), ("file", os.path.join(d, "relatorio.md")))
            self.assertEqual(agents.opens(row, None, True), ("folder", d))
            self.assertEqual(agents.opens(row, "https://example.com/y", False), ("url", "https://example.com/y"))
            self.assertEqual(agents.opens({"directory": d, "links": ["https://example.com/x"]}, None, False), ("url", "https://example.com/x"))
            self.assertIsNone(agents.opens({"directory": d, "made": [{"url": "sumiu.txt"}]}, None, False))   # the file is gone: the task's own page
            self.assertEqual(agents.opens({"directory": d}, None, True), ("folder", d))
        self.assertEqual(agents.opens(None, "https://example.com", False), ("url", "https://example.com"))
        self.assertIsNone(agents.opens(None, "javascript:alert(1)", False))


if __name__ == "__main__":
    unittest.main()
