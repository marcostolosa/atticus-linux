"""Putting text at the cursor."""
import unittest

from atticus_shell import hands


class InsertText(unittest.TestCase):
    def setUp(self):
        self.ran, self.typed = [], []
        self.old = (hands.subprocess.run, hands.type_text, hands._front_class, hands.shutil.which, hands.time.sleep)

        class Done:
            returncode, stdout, stderr = 0, b"antes", ""
        hands.subprocess.run = lambda cmd, **kw: (self.ran.append((cmd, kw.get("input"))), Done())[1]
        hands.type_text = lambda text: (self.typed.append(text), (True, ""))[1]
        hands.shutil.which = lambda name: "/usr/bin/" + name
        hands.time.sleep = lambda s: None

    def tearDown(self):
        hands.subprocess.run, hands.type_text, hands._front_class, hands.shutil.which, hands.time.sleep = self.old

    def test_one_line_is_typed(self):
        self.assertEqual(hands.insert_text("Chego às oito."), (True, ""))
        self.assertEqual(self.typed, ["Chego às oito."])
        self.assertEqual(self.ran, [])

    def test_several_lines_are_pasted_and_the_clipboard_is_put_back(self):
        hands._front_class = lambda: "brave-browser"
        self.assertTrue(hands.insert_text("Lista:\n- Pão\n- Leite")[0])
        self.assertEqual(self.typed, [], "a typed line break would press Enter and send a chat message")
        self.assertIn((["wl-copy", "--"], "Lista:\n- Pão\n- Leite".encode()), self.ran)
        self.assertIn(["wtype", "-M", "ctrl", "v"], [c for c, _ in self.ran])
        self.assertEqual(self.ran[-1], (["wl-copy", "--"], b"antes"))

    def test_a_terminal_takes_another_paste_key(self):
        hands._front_class = lambda: "kitty"
        hands.insert_text("a\nb")
        self.assertIn(["wtype", "-M", "ctrl", "-M", "shift", "v"], [c for c, _ in self.ran])
