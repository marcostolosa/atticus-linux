import unittest
from atticus_shell.answers import match

PERM = {"kind": "perm"}
PICK = {"kind": "pick", "options": ["Thiago Ventura (canal oficial)", "Tiago Ventura Cortes", "Outro canal"]}


class Permission(unittest.TestCase):
    def test_yes(self):
        for t in ("Sim.", "pode", "Pode sim!", "claro", "ok", "yes", "pode abrir no Chrome", "pode mandar para mim"):
            self.assertEqual(match(PERM, t, "pt"), "y", t)

    def test_no(self):
        for t in ("Não.", "não pode", "claro que não", "ok, não", "cancela", "para", "no", "nunca"):
            self.assertEqual(match(PERM, t, "pt"), "n", t)
        self.assertEqual(match(PERM, "no, don't do that", "en"), "n")

    def test_unclear(self):
        for t in ("", "hum", "talvez depois"):
            self.assertIsNone(match(PERM, t, "pt"), t)

    def test_retry(self):
        self.assertEqual(match({"kind": "perm", "retry": True}, "sim", "pt"), "r")


class Choice(unittest.TestCase):
    def test_number_and_ordinal(self):
        self.assertEqual(match(PICK, "2", "pt"), "1")
        self.assertEqual(match(PICK, "a segunda", "pt"), "1")
        self.assertEqual(match(PICK, "opção três", "pt"), "2")

    def test_label_words(self):
        self.assertEqual(match(PICK, "o dos cortes", "pt"), "1")

    def test_none(self):
        self.assertEqual(match(PICK, "nenhuma delas", "pt"), "")
        self.assertIsNone(match(PICK, "sei lá", "pt"))

    def test_out_of_range(self):
        self.assertIsNone(match(PICK, "7", "pt"))


if __name__ == "__main__":
    unittest.main()
