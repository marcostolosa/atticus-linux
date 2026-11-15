"""The address book over vCard files in a folder of its own."""
import json
import os
import tempfile
import unittest
from pathlib import Path

CARDS = """BEGIN:VCARD
VERSION:3.0
FN:Ana Souza
N:Souza;Ana;;;
NICKNAME:Aninha
EMAIL;TYPE=WORK:ana@example.com
TEL;TYPE=CELL:+55 11 99999-0000
ORG:Padaria Central;
END:VCARD
BEGIN:VCARD
VERSION:2.1
N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Concei=C3=A7=C3=A3o;Jo=C3=A3o
TEL;CELL:11988887777
item1.X-ABRELATEDNAMES:Ana Souza
item1.X-ABLabel:_$!<Sister>!$_
END:VCARD
BEGIN:VCARD
VERSION:4.0
ORG:Oficina do Zé
TEL;VALUE=uri:tel:+551133334444
END:VCARD
"""


class PeopleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in ("ATTICUS_DIR", "HOME")}
        os.environ.update(ATTICUS_DIR=self.tmp.name, HOME=self.tmp.name)
        Path(self.tmp.name, "contacts").mkdir()
        Path(self.tmp.name, "contacts", "todos.vcf").write_text(CARDS)
        from atticus_shell import people
        self.people = people

    def tearDown(self):
        for k, v in self.old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def test_cards(self):
        ana, joao, shop = self.people.cards()
        self.assertEqual((ana["name"], ana["nickname"], ana["organization"]), ("Ana Souza", "Aninha", "Padaria Central"))
        self.assertEqual((ana["emails"], ana["phones"]), (["ana@example.com"], ["+55 11 99999-0000"]))
        self.assertEqual(joao["name"], "João Conceição")   # no FN: built from the name parts, old encoding decoded
        self.assertEqual(joao["related"], [{"label": "sister", "value": "Ana Souza"}])
        self.assertEqual((shop["name"], shop["phones"]), ("Oficina do Zé", ["+551133334444"]))   # a business has no person's name
        self.assertEqual([c["index"] for c in (ana, joao, shop)], [0, 1, 2])

    def test_own_card_gives_the_profile(self):
        Path(self.tmp.name, "settings.json").write_text(json.dumps({"myCard": "joão conceição"}))
        got = self.people.profile()
        self.assertEqual(got["contacts"]["name"], "João Conceição")
        self.assertEqual(got["contacts"]["phones"], [{"label": "", "value": "11988887777"}])
        self.assertEqual(got["contacts"]["related"], [{"label": "sister", "value": "Ana Souza"}])
        self.assertIn("mail", got["errors"])   # no mail program is set up in this folder


if __name__ == "__main__":
    unittest.main()
