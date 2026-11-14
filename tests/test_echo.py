import unittest

from atticus_shell.echo import own_words

SPOKEN = "Ar-condicionado está ligado, em 18 graus. O ambiente está em 30 graus."


class OwnWords(unittest.TestCase):
    """Telling the user's words from a leak of the sentence being spoken."""

    def test_leak_of_the_spoken_sentence_is_nothing(self):
        for heard in ("O ar-condicionado está ligado.", "está ligado em 18 graus", "ambiente está em 30 graus.",
                      "", "graus"):
            self.assertEqual(own_words(heard, SPOKEN), "", heard)

    def test_the_user_talking_over_it(self):
        for heard in ("Desliga.", "liga o ar", "Para.", "Coloca em 23 graus", "que horas são"):
            self.assertEqual(own_words(heard, SPOKEN), heard, heard)

    def test_numbers_written_another_way_are_still_the_leak(self):
        self.assertEqual(own_words("São 17h12.", "São 17:12."), "")

    def test_a_word_heard_wrong_in_the_middle_is_still_the_leak(self):
        self.assertEqual(own_words("O ar condicionado está ligado em 18 graus. O ambiente está em 30 graus.", SPOKEN), "")

    def test_leak_then_the_user(self):
        self.assertEqual(own_words("ligado em 18 graus desliga o ar", SPOKEN), "desliga o ar")


    def test_a_number_from_its_own_sentence_is_not_a_request(self):
        said = "Um dia tem 1.440 minutos, que é 24 horas vezes 60 minutos."
        self.assertEqual(own_words("440.", said), "")
        self.assertEqual(own_words("60", said), "")
        self.assertEqual(own_words("não", "Eu não sei o que é isso."), "não")   # a short word people do say over it


if __name__ == "__main__":
    unittest.main()
