"""The boxes stack from the top centre, each below the ones showing above it."""
import unittest
from unittest import mock

from atticus_shell import stack


class StackTest(unittest.TestCase):
    def setUp(self):
        stack._items.clear()
        p = mock.patch.object(stack.LayerShell, "set_margin")
        self.set_margin = p.start()
        self.addCleanup(p.stop)

    def test_boxes_showing_are_stacked_in_order_and_hidden_ones_take_no_room(self):
        stack.update("shelf", window="s", height=52, visible=True)
        stack.update("box", window="b", height=200, visible=True)
        self.assertEqual(stack.margins(), {"box": 8, "shelf": 8 + 200 + 8})
        stack.update("meeting", window="m", height=470, visible=True)
        self.assertEqual(stack.margins(), {"meeting": 8, "box": 8 + 470 + 8, "shelf": 8 + 470 + 8 + 200 + 8})
        stack.update("box", visible=False)
        self.assertEqual(stack.margins(), {"meeting": 8, "shelf": 8 + 470 + 8})
        stack.update("box", visible=True, height=120)   # growing moves what is under it
        self.assertEqual(stack.margins()["shelf"], 8 + 470 + 8 + 120 + 8)

    def test_new_margins_reach_the_windows(self):
        stack.update("box", window="b", height=100, visible=True)
        stack.update("shelf", window="s", height=50, visible=True)
        self.assertIn(mock.call("s", stack.LayerShell.Edge.TOP, 116), self.set_margin.call_args_list)


if __name__ == "__main__":
    unittest.main()
