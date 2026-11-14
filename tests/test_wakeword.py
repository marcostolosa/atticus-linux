import unittest

from atticus_shell.wakeword import said


class Said(unittest.TestCase):
    """The request, without the wake word the recording began with."""

    def test_the_word_is_taken_off(self):
        for heard, asked in (("Siri, que horas são?", "Que horas são?"),
                             ("Hey Siri, desliga", "Desliga"),
                             ("Ciri, liga a luz", "Liga a luz"),          # how the recognizer often spells it
                             ("Série, liga o ar", "Liga o ar"),
                             ("e aí Siri que horas são", "Que horas são"),  # caught before the word
                             ("Siri.", "")):
            self.assertEqual(said(heard, "siri"), asked, heard)

    def test_two_word_wake_phrase(self):
        self.assertEqual(said("Ei, Siri. Liga a luz da cozinha.", "ei siri"), "Liga a luz da cozinha.")

    def test_text_without_the_word_is_left_alone(self):
        for heard in ("que horas são", "Cidade, para.", "liga a luz e fala com a siri depois", ""):
            self.assertEqual(said(heard, "siri"), heard, heard)


if __name__ == "__main__":
    unittest.main()
