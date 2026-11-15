"""The shortcut lines in Hyprland's bindings file, in a file of its own (the real one is never touched)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atticus_shell import shortcuts

START = '''-- my keys
o.bind("SUPER + H", nil, "voxtype record toggle")
-- o.bind("CTRL + ALT + SPACE", "old", "/x/atticus-ctl toggle")

-- Atticus voice assistant
o.bind("SUPER + ALT + A", "Atticus (voice)", "/home/u/.local/bin/atticus-ctl toggle")
o.bind("SUPER + ALT + D", "Atticus dictation", "/home/u/.local/bin/atticus-ctl dictate")
'''


class ShortcutsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name, "bindings.lua")
        self.file.write_text(START)
        self.calls = []
        self.errors = ""
        self.live = "[]"
        patches = [mock.patch.dict(os.environ, {"ATTICUS_HYPR_BINDS": str(self.file)}),
                   mock.patch.object(shortcuts, "_hyprctl", self.hyprctl),
                   mock.patch.object(shortcuts, "ctl", lambda: "/bin/atticus-ctl"),
                   mock.patch.object(shortcuts, "can_hold", lambda: True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def hyprctl(self, *args):
        self.calls.append(args)
        return {"configerrors": self.errors, "-j": self.live}.get(args[0], "ok")

    def test_reads_only_live_lines(self):
        self.assertEqual(shortcuts.read(), {"talk": "SUPER + ALT + A", "dictate": "SUPER + ALT + D", "hold": False})
        self.assertEqual(shortcuts.labels(), {"talk": "Super+Alt+A", "dictate": "Super+Alt+D"})

    def test_names(self):
        self.assertEqual(shortcuts.clean("alt+super + a"), "SUPER + ALT + A")
        self.assertEqual(shortcuts.clean("F9"), "F9")
        for bad in ("A", "SHIFT + A", "SUPER + ALT", "SUPER + A + B", 'SUPER + "'):
            self.assertEqual(shortcuts.clean(bad), "", bad)
        self.assertEqual(shortcuts.label("SUPER + CTRL + SPACE"), "Super+Ctrl+Space")
        self.assertEqual(shortcuts.label("ALT + F9"), "Alt+F9")

    def test_new_keys_replace_the_line_in_place(self):
        self.assertEqual(shortcuts.write("talk", "SUPER + CTRL + J"), "")
        text = self.file.read_text()
        self.assertIn('o.bind("SUPER + CTRL + J", "Atticus (voice)", "/bin/atticus-ctl toggle")\no.bind("SUPER + ALT + D"', text)
        self.assertNotIn('"SUPER + ALT + A"', text)
        self.assertIn('o.bind("SUPER + H", nil, "voxtype record toggle")', text)   # the rest of the file is left alone
        self.assertIn(("reload",), self.calls)

    def test_hold_and_back(self):
        self.assertEqual(shortcuts.write("talk", None, hold=True), "")
        self.assertIn('o.bind_hold("SUPER + ALT + A", "Atticus (voice)", "/bin/atticus-ctl press", "Atticus (send)", "/bin/atticus-ctl release")',
                      self.file.read_text())
        self.assertEqual(shortcuts.read()["hold"], True)
        self.assertEqual(shortcuts.write("talk", "SUPER + ALT + J"), "")   # new keys keep the way it is pressed
        self.assertIn('o.bind_hold("SUPER + ALT + J"', self.file.read_text())
        self.assertEqual(shortcuts.write("talk", None, hold=False), "")
        self.assertEqual(shortcuts.read(), {"talk": "SUPER + ALT + J", "dictate": "SUPER + ALT + D", "hold": False})

    def test_refuses_keys_in_use(self):
        self.assertEqual(shortcuts.write("dictate", "SUPER + ALT + A"), "own:talk")
        self.live = '[{"modmask": 64, "key": "SPACE", "submap": "", "description": "Omarchy menu"}]'
        self.assertEqual(shortcuts.write("talk", "SUPER + SPACE"), "taken:Omarchy menu")
        self.assertEqual(shortcuts.write("talk", "J"), "Use a key together with Super, Ctrl or Alt.")
        self.assertEqual(self.file.read_text(), START)

    def test_a_config_error_puts_the_file_back(self):
        self.errors = "bindings.lua:7: unexpected symbol"
        self.assertIn("refused", shortcuts.write("talk", "SUPER + CTRL + J"))
        self.assertEqual(self.file.read_text(), START)

    def test_remove_and_add(self):
        self.assertEqual(shortcuts.write("dictate", None), "")
        self.assertNotIn("dictate", self.file.read_text())
        self.assertEqual(shortcuts.write("settings", "SUPER + CTRL + ALT + A"), "")
        self.assertTrue(self.file.read_text().endswith('o.bind("SUPER + CTRL + ALT + A", "Atticus settings", "/bin/atticus-ctl settings")\n'))
        self.assertEqual(shortcuts.install(), ["Atticus (voice): Super+Alt+A", "Atticus dictation: Super+Alt+D", "Atticus settings: Super+Ctrl+Alt+A"])

    def test_no_file(self):
        self.file.unlink()
        self.assertIn("was not found", shortcuts.write("talk", "SUPER + ALT + J"))
        self.assertEqual(shortcuts.read(), {"hold": False})


if __name__ == "__main__":
    unittest.main()
