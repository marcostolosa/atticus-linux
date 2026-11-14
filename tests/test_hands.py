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


class AxisScale(unittest.TestCase):
    """ydotool's absolute positioning doesn't always land 1:1 on Hyprland's
    own pixel coordinates (measured live on a scale-1.5 panel: asking for x
    lands the pointer at 2x). move() corrects for whatever the true ratio
    turns out to be on this machine, found by a one-time probe move."""

    def setUp(self):
        hands._AXIS_SCALE = None
        self.old = (hands._ydotool, hands._cursor_pos, hands.time.sleep)
        self.sent = []
        hands._ydotool = lambda *a: (self.sent.append(a), (True, ""))[1]
        hands.time.sleep = lambda s: None

    def tearDown(self):
        hands._ydotool, hands._cursor_pos, hands.time.sleep = self.old
        hands._AXIS_SCALE = None

    def test_a_mismatch_is_corrected(self):
        hands._cursor_pos = lambda: (64, 64)   # the probe (32,32) landed at double
        hands.move(900, 600)
        self.assertEqual(hands._axis_scale(), (2.0, 2.0))
        self.assertIn(("mousemove", "--absolute", "-x", "450", "-y", "300"), self.sent)

    def test_a_1to1_device_needs_no_correction(self):
        hands._cursor_pos = lambda: (32, 32)
        hands.move(500, 400)
        self.assertEqual(hands._axis_scale(), (1.0, 1.0))
        self.assertIn(("mousemove", "--absolute", "-x", "500", "-y", "400"), self.sent)

    def test_an_unreadable_cursor_position_falls_back_to_no_correction(self):
        hands._cursor_pos = lambda: None
        hands.move(500, 400)
        self.assertIn(("mousemove", "--absolute", "-x", "500", "-y", "400"), self.sent)

    def test_measured_only_once_per_process(self):
        calls = []
        hands._cursor_pos = lambda: (calls.append(1), (64, 64))[1]
        hands.move(100, 100)
        hands.move(200, 200)
        self.assertEqual(len(calls), 1)
