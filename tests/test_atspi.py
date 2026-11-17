"""Acting by control name: what is read, what is refused, what the user is asked (no real window is touched)."""
import os
import tempfile
import unittest
from unittest import mock

from atticus_shell import atspi


class Fake:
    """A control as the accessibility bus gives it: role, name, children."""
    def __init__(self, role, name, kids=()):
        self.role, self.name, self.kids = role, name, list(kids)

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.kids)

    def get_child_at_index(self, i):
        return self.kids[i]


class AtspiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [mock.patch.dict(os.environ, {"ATTICUS_DIR": self.tmp.name}),
                   mock.patch.object(atspi, "_role", lambda n: n.role),
                   mock.patch.object(atspi, "_states", lambda n: {"showing", "enabled"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.send = Fake("button", "Send")
        self.password = Fake("password text", "Password")
        self.window = Fake("dialog", "Mail", [Fake("panel", "", [self.send, Fake("entry", "To"), self.password, Fake("label", "Hello")])])
        self.app = Fake("application", "mail", [self.window])

    def look(self):
        with mock.patch.object(atspi, "Atspi", object()), mock.patch.object(atspi, "_app_of", lambda pid: (0, self.app)):
            rows, note = atspi.snapshot({"pid": 7, "title": "Mail", "class": "mail", "address": "0x1"})
        atspi.save({"pid": 7, "title": "Mail", "class": "mail", "address": "0x1"}, rows)
        return rows

    def test_lists_what_can_be_used_and_nothing_else(self):
        rows = self.look()
        self.assertEqual([(r["n"], r["kind"], r["name"]) for r in rows], [(1, "button", "Send"), (2, "field", "To"), (3, "secret", "Password")])
        self.assertEqual(atspi.lines(rows).split("\n")[0], '[1] button "Send"')

    def test_a_changed_window_is_refused_not_guessed(self):
        self.look()
        with mock.patch.object(atspi, "Atspi", object()), mock.patch.object(atspi, "_app_of", lambda pid: (0, self.app)):
            self.assertEqual(atspi.describe(1), {"kind": "button", "name": "Send", "app": "mail", "title": "Mail"})
            self.send.name = "Delete everything"   # the page changed after it was read
            self.assertIn("window changed", atspi.describe(1)["error"])
            self.assertFalse(atspi.click(1)[0])
            self.assertIn("no control [9]", atspi.describe(9)["error"])

    def test_passwords_are_never_filled_and_buttons_are_not_fields(self):
        self.look()
        with mock.patch.object(atspi, "Atspi", object()), mock.patch.object(atspi, "_app_of", lambda pid: (0, self.app)):
            self.assertIn("password", atspi.fill(3, "x")[1])
            self.assertIn("not a field", atspi.fill(1, "x")[1])

    def test_names_cannot_carry_control_characters_or_run_long(self):
        self.assertEqual(atspi.clean("Pay\n\x1b[31m now‮"), "Pay [31m now")
        self.assertEqual(len(atspi.clean("x" * 500)), 80)

    def test_key_combinations(self):
        self.assertEqual(atspi.parse_combo("ctrl+shift+t"), ("CTRL SHIFT", "t"))
        self.assertEqual(atspi.parse_combo("Return"), ("", "code:36"))   # by code: names fail on some keyboards' keymaps
        self.assertEqual(atspi.parse_combo("alt+F4"), ("ALT", "code:70"))
        self.assertEqual(atspi.parse_combo("PageDown"), ("", "code:117"))
        self.assertEqual(atspi.parse_combo("f12"), ("", "code:96"))
        for bad in ("", "ctrl", "ctrl+", "ctrl+alt+nonsense", "rm -rf"):
            self.assertIsNone(atspi.parse_combo(bad), bad)

    def test_keys_need_a_real_window_address(self):
        with mock.patch("subprocess.run") as run:
            self.assertFalse(atspi.press("ctrl+t", "")[0])
            self.assertFalse(atspi.press("ctrl+t", '0x1"); os.execute("x")')[0])
            run.assert_not_called()
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "ok", ""
            self.assertTrue(atspi.press("ctrl+t", "0xabc")[0])
            self.assertIn('address:0xabc', run.call_args[0][0][2])


class FindWindowTest(unittest.TestCase):
    def test_a_window_is_found_by_class_then_by_title(self):
        rows = [{"class": "chrome-__tmp_x-Default", "initialClass": "", "title": "AtticusForm", "pid": 1},
                {"class": "kitty", "initialClass": "kitty", "title": "htop", "pid": 2}]
        with mock.patch.object(atspi, "windows", lambda: rows):
            self.assertEqual(atspi.find_window("kitty")["pid"], 2)
            self.assertEqual(atspi.find_window("atticusform")["pid"], 1)
            self.assertEqual(atspi.find_window("chrome", "atticusform")["pid"], 1)
            self.assertEqual(atspi.find_window("nothing"), {})


class TargetWindowTest(unittest.TestCase):
    def test_keys_go_to_the_window_that_was_looked_at_while_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ATTICUS_DIR": tmp}):
            atspi.save({"address": "0xaaa", "pid": 1, "class": "a", "title": "A"}, [])
            wins = [{"address": "0xbbb"}, {"address": "0xaaa"}]
            with mock.patch.object(atspi, "windows", lambda: wins), mock.patch.object(atspi, "front", lambda: {"address": "0xbbb"}):
                self.assertEqual(atspi.target_window()["address"], "0xaaa")
                self.assertEqual(atspi.target_window(max_age=-1)["address"], "0xbbb")   # too old: the front one
            with mock.patch.object(atspi, "windows", lambda: [{"address": "0xbbb"}]), mock.patch.object(atspi, "front", lambda: {"address": "0xbbb"}):
                self.assertEqual(atspi.target_window()["address"], "0xbbb")   # it closed


if __name__ == "__main__":
    unittest.main()
