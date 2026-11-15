"""The guide drawn over the desktop: the parts that need no display."""
import os
import struct
import tempfile
import unittest

from atticus_shell import drawing, intents


class DrawingTest(unittest.TestCase):
    def test_curve_goes_through_every_point(self):
        pts = [[0, 0], [10, 20], [30, 20], [40, 0]]
        segs = drawing.smooth(pts)
        self.assertEqual([list(s[4:]) for s in segs], pts[1:])
        self.assertEqual(len(segs), 3)

    def test_bounds_cover_all_kinds(self):
        shapes = [{"kind": "rect", "rect": [10, 20, 30, 40]}, {"kind": "label", "at": [5, 100], "text": "x"},
                  {"kind": "path", "points": [[0, 0], [200, 50]]}]
        self.assertEqual(drawing.bounds(shapes), (0, 0, 200, 100))
        self.assertIsNone(drawing.bounds([]))

    def test_formula_marks_come_off(self):
        self.assertEqual(drawing.plain_math("área: $a^2$ e $$b$$"), "área: a^2 e b")

    def test_every_shape_paints(self):
        guide = {"teaching": True, "steps": [
            {"say": "um", "shapes": [{"kind": "rect", "rect": [10, 10, 80, 40], "fill": True}, {"kind": "circle", "rect": [100, 10, 40, 40]},
                                     {"kind": "line", "points": [[0, 0], [50, 50]]}, {"kind": "arrow", "points": [[0, 90], [90, 90]], "color": "green"}]},
            {"say": "dois", "shapes": [{"kind": "path", "points": [[0, 0], [10, 30], [40, 30]], "smooth": True},
                                       {"kind": "path", "points": [[0, 0], [0, 30], [30, 30], [30, 0]], "curve": True, "fill": True},
                                       {"kind": "label", "at": [20, 120], "text": "rótulo", "color": "yellow"},
                                       {"kind": "text", "at": [20, 150], "text": "c² = a² + b²", "size": "l"}, {"kind": "pointer", "at": [150, 150]}]}]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "g.png")
            drawing.preview(guide, 200, 200, path)
            with open(path, "rb") as f:
                head = f.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", head[16:24]), (200, 200))

    def test_guide_words(self):
        for said, verb in (("Próximo.", "next"), ("pronto", "next"), ("next please", "next"), ("Já fiz!", "next"),
                           ("volta", "back"), ("go back", "back"), ("fecha", "close"), ("pode tirar", "close")):
            self.assertEqual(intents.guide_word(said), verb, said)
        for said in ("próximo passo do bolo é qual", "fecha a janela do navegador", "que horas são"):
            self.assertIsNone(intents.guide_word(said), said)


if __name__ == "__main__":
    unittest.main()
