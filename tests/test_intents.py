"""Instant intents: what must be recognized, and what must be left to the model."""
import time
import unittest

from atticus_shell import intents as I

# Saturday 14 Nov 2026, 10:00 local
NOW = time.mktime((2026, 11, 14, 10, 0, 0, 0, 0, -1))


def at(day, h, m=0):
    return time.mktime((2026, 11, day, h, m, 0, 0, 0, -1))


def p(text, now=NOW, home=False):
    return I.parse(text, now, home)


class Timers(unittest.TestCase):
    def test_forms(self):
        for text, secs in [
            ("Coloca um timer de 10 minutos.", 600), ("timer de 1 minuto", 60), ("Põe um timer de dez minutos", 600),
            ("Cria um temporizador de 1 hora e meia", 5400), ("timer de 2 minutos e 30 segundos", 150),
            ("Inicia um timer de meia hora", 1800), ("set a timer for 5 minutes", 300), ("Timer for twenty five minutes", 1500),
            ("Siri, coloca um timer de 3 minutos por favor", 180), ("me avisa daqui a 15 minutos", 900),
            ("timer de 90 segundos", 90), ("10 minute timer", 600),
        ]:
            i = p(text)
            self.assertIsNotNone(i, text)
            self.assertEqual((i.kind, i.args["seconds"]), ("timer", secs), text)

    def test_label(self):
        i = p("Coloca um timer de 12 minutos para o macarrão")
        self.assertEqual((i.kind, i.args["seconds"], i.args["label"]), ("timer", 720, "macarrão"))

    def test_not_a_timer(self):
        for text in ("quanto tempo dura um timer de cozinha", "timer", "coloca um timer", "timer de muitos minutos"):
            i = p(text)
            self.assertFalse(i and i.kind == "timer", text)


class Alarms(unittest.TestCase):
    def test_forms(self):
        for text, when, repeat in [
            ("Me acorda às 7", at(15, 7), ""),                  # a wake-up at 7 is 07:00, never 19:00
            ("Me acorda amanhã às 7", at(15, 7), ""),
            ("Coloca um alarme para as 7 e meia da manhã", at(15, 7, 30), ""),
            ("alarme às 18:45", at(14, 18, 45), ""),
            ("Cria um alarme para 6h30 todo dia", at(15, 6, 30), "daily"),
            ("Alarme às 8 da manhã nos dias de semana", at(16, 8), "weekdays"),   # Monday
            ("wake me up at 6:30 am", at(15, 6, 30), ""),
            ("set an alarm for 9 pm", at(14, 21), ""),
            ("me acorda ao meio-dia".replace("ao ", ""), at(14, 12), ""),
        ]:
            i = p(text)
            self.assertIsNotNone(i, text)
            self.assertEqual(i.kind, "alarm", text)
            self.assertEqual((i.args["at"], i.args["repeat"]), (when, repeat), text)

    def test_evening_rolls_to_morning(self):
        late = time.mktime((2026, 11, 14, 22, 0, 0, 0, 0, -1))
        self.assertEqual(p("me acorda às 7", late).args["at"], at(15, 7))


class Reminders(unittest.TestCase):
    def test_forms(self):
        for text, when, what in [
            ("Me lembra amanhã às 9 horas de ligar pro dentista.", at(15, 9), "ligar pro dentista"),
            ("Me lembra de tirar o bolo do forno daqui a 20 minutos", NOW + 1200, "tirar o bolo do forno"),
            ("me lembre às 15h de tomar o remédio", at(14, 15), "tomar o remédio"),
            ("Me lembra de pagar a conta de luz na segunda às 10", at(16, 10), "pagar a conta de luz"),
            ("remind me to call mom tomorrow at 6 pm", at(15, 18), "call mom"),
            ("remind me in 2 hours to stretch", NOW + 7200, "stretch"),
            ("Me lembra todo dia às 8 da manhã de tomar água", at(15, 8), "tomar água"),
        ]:
            i = p(text)
            self.assertIsNotNone(i, text)
            self.assertEqual(i.kind, "reminder", text)
            self.assertEqual((i.args["at"], i.args["label"]), (when, what), text)

    def test_no_time_goes_to_the_model(self):
        self.assertIsNone(p("me lembra de comprar pão"))

    def test_a_number_alone_is_not_a_time(self):
        self.assertIsNone(p("me lembra de comprar 3 pães"))


class ListCancel(unittest.TestCase):
    def test_list(self):
        for text in ("quais timers eu tenho", "quanto tempo falta no timer", "quais são meus alarmes", "lista os lembretes",
                     "what timers do i have".replace("do i have", ""), "how much time is left"):
            i = p(text)
            self.assertTrue(i and i.kind == "alarms", text)

    def test_cancel(self):
        self.assertEqual(p("cancela o timer").args, {"spec": "", "what": "timer"})
        self.assertEqual(p("cancela todos os alarmes").args["spec"], "alarm")
        self.assertEqual(p("cancel the timer").args["what"], "timer")
        self.assertEqual(p("apaga o lembrete do dentista").args["spec"], "dentista")
        self.assertEqual(p("cancela tudo").args["spec"], "all")


class Controls(unittest.TestCase):
    def test_volume(self):
        self.assertEqual(p("Aumenta o volume").args, {"delta": 10})
        self.assertEqual(p("abaixa o volume um pouco").args, {"delta": -10})
        self.assertEqual(p("aumenta o volume em 20%").args, {"delta": 20})
        self.assertEqual(p("Volume em 50%").args, {"percent": 50})
        self.assertEqual(p("coloca o volume em trinta por cento").args, {"percent": 30})
        self.assertEqual(p("set the volume to 40").args, {"percent": 40})
        self.assertEqual(p("volume up").args, {"delta": 10})
        self.assertEqual(p("muta o som").args, {"mute": True})
        self.assertEqual(p("unmute").args, {"mute": False})
        self.assertEqual(p("qual é o volume atual").args, {})
        self.assertEqual(p("mais alto").args, {"delta": 10})

    def test_volume_not_hijacked(self):
        for text in ("aumenta a fonte", "qual o volume de uma esfera", "aumenta", "abaixa a temperatura"):
            i = p(text)
            self.assertFalse(i and i.kind == "volume", text)

    def test_brightness(self):
        self.assertEqual(p("aumenta o brilho").args, {"delta": 10})
        self.assertEqual(p("diminui o brilho da tela").args, {"delta": -10})
        self.assertEqual(p("brilho em 70%").args, {"percent": 70})

    def test_media(self):
        for text, action in [("pausa", "pause"), ("Pausa a música.", "pause"), ("para a música", "pause"), ("continua", "play"),
                             ("próxima", "next"), ("próxima música", "next"), ("pula essa", "next"), ("música anterior", "previous"),
                             ("next song", "next"), ("pause the music", "pause"), ("resume", "play")]:
            i = p(text)
            self.assertTrue(i and i.kind == "media" and i.args["action"] == action, text)
        self.assertEqual(p("que música é essa").kind, "playing")

    def test_play_something_goes_to_the_model(self):
        for text in ("toca Legião Urbana", "play some jazz", "toca aquela do Skank"):
            self.assertIsNone(p(text), text)

    def test_misc(self):
        self.assertEqual(p("Que horas são?").kind, "time")
        self.assertEqual(p("que dia é hoje").kind, "date")
        self.assertEqual(p("para").kind, "hush")
        self.assertEqual(p("bloqueia a tela").kind, "lock")
        self.assertEqual(p("desliga o bluetooth").args, {"which": "bluetooth", "on": False})
        self.assertEqual(p("turn wifi on").args, {"which": "wifi", "on": True})

    def test_open(self):
        self.assertEqual(p("Abre o Firefox.").args, {"name": "firefox"})
        self.assertEqual(p("abrir youtube.com").args, {"name": "youtube.com"})
        self.assertEqual(p("open the calculator").args, {"name": "calculator"})
        # a compound request is not a plain "open": the model does it
        for text in ("abre o canal do Thiago Ventura no YouTube", "abre o firefox e pesquisa por gatos", "abre o arquivo que eu baixei"):
            self.assertIsNone(p(text), text)

    def test_questions_go_to_the_model(self):
        for text in ("Qual a previsão do tempo para hoje?", "quanto é 15 por cento de 240", "o que tem na minha tela",
                     "manda um email pro João", "por que o céu é azul", "desliga o computador", "apaga a pasta Documentos"):
            self.assertIsNone(p(text), text)


class Lists(unittest.TestCase):
    def test_add(self):
        i = p("Adiciona leite e pão integral na lista de compras")
        self.assertEqual((i.kind, i.args), ("list_add", {"name": "compras", "items": ["leite", "pão integral"]}))
        self.assertEqual(p("coloca detergente na lista").args, {"name": "compras", "items": ["detergente"]})
        self.assertEqual(p("add eggs and milk to the shopping list").args, {"name": "shopping", "items": ["eggs", "milk"]})
        self.assertEqual(p("anota ligar pro banco na lista de tarefas").args["name"], "tarefas")

    def test_show_remove_clear(self):
        self.assertEqual(p("o que tem na lista de compras").kind, "list_show")
        self.assertEqual(p("lê a lista de compras").kind, "list_show")
        self.assertEqual(p("tira o leite da lista de compras").args, {"name": "compras", "item": "leite"})
        self.assertEqual(p("limpa a lista de compras").kind, "list_clear")


class Home(unittest.TestCase):
    def test_only_with_a_server(self):
        self.assertIsNone(p("liga a luz da sala"))
        self.assertEqual(p("liga a luz da sala", home=True).args, {"name": "a luz da sala", "on": True})
        self.assertEqual(p("apaga a luz do quarto", home=True).args, {"name": "a luz do quarto", "on": False})
        self.assertEqual(p("turn off the kitchen lights", home=True).args["on"], False)

    def test_set(self):
        self.assertEqual(p("Coloca o ar-condicionado em 24 graus", home=True).args, {"name": "o ar-condicionado", "what": "temperature", "value": 24.0})
        self.assertEqual(p("luz da sala em 50%", home=True).args, {"name": "luz da sala", "what": "brightness", "value": 50.0})
        self.assertEqual(p("volume em 50%", home=True).kind, "volume")          # still the computer's own volume
        self.assertEqual(p("brilho em 70%", home=True).kind, "brightness")

    def test_not_devices(self):
        for text in ("desliga o computador", "apaga a pasta Documentos", "liga o wifi", "abre o chromium"):
            i = p(text, home=True)
            self.assertFalse(i and i.kind == "home" and text != "abre o chromium", text)


class Misheard(unittest.TestCase):
    """Real transcripts from a noisy room and from whispering: the action must still be right."""

    def test_last_vowel(self):
        self.assertEqual(p("Coloco um timer de 10 minutos").args["seconds"], 600)
        self.assertEqual(p("Abaixo volume").args, {"delta": -10})
        self.assertEqual(p("Coloco o ar condicionado em 23 graus", home=True).args["value"], 23.0)
        self.assertEqual(p("Apago a luz do quarto", home=True).args, {"name": "a luz do quarto", "on": False})

    def test_dropped_small_words(self):
        self.assertEqual(p("Apaga luz do quarto", home=True).args, {"name": "luz do quarto", "on": False})
        self.assertEqual(p("pausa musica").args, {"action": "pause"})
        self.assertEqual(p("Me acorda 6 e meia").args["at"], at(15, 6, 30))
        self.assertEqual(p("me acorda seis e meia").args["at"], at(15, 6, 30))
        self.assertEqual(p("luz do dom e 30%", home=True).args, {"name": "luz do dom", "what": "brightness", "value": 30.0})

    def test_first_person_does_not_hijack_questions(self):
        self.assertIsNone(p("abaixo de zero a água congela"))
        self.assertIsNone(p("ligo para quem em caso de emergência"))


class Phrasing(unittest.TestCase):
    def test_when_text(self):
        self.assertEqual(I.when_text(at(14, 15), "pt", NOW), "hoje às 15:00")
        self.assertEqual(I.when_text(at(15, 9), "pt", NOW), "amanhã às 09:00")
        self.assertEqual(I.when_text(at(16, 10), "pt", NOW), "segunda às 10:00")
        self.assertEqual(I.when_text(at(15, 8), "pt", NOW, "daily"), "todo dia às 08:00")
        self.assertEqual(I.when_text(at(15, 18), "en", NOW), "tomorrow at 18:00")

    def test_language(self):
        self.assertEqual(p("set a timer for 5 minutes").lang, "en")
        self.assertEqual(p("coloca um timer de 5 minutos").lang, "pt")


if __name__ == "__main__":
    unittest.main()


class HomeState(unittest.TestCase):
    """Asking about a device is answered on the spot — and a transcript that
    lost its question mark is still the question."""

    def test_statement_and_question_forms(self):
        for said, name in (("O ar está ligado.", "ar"), ("o ar tá ligado?", "ar"),
                           ("A luz do quarto está acesa?", "luz do quarto"),
                           ("tá ligada a luz da cozinha", "luz da cozinha"),
                           ("vê se o ar está ligado", "ar"), ("como está o ar condicionado", "ar condicionado"),
                           ("Is the bedroom light on?", "bedroom light")):
            i = I.parse(said, home=True)
            self.assertIsNotNone(i, said)
            self.assertEqual((i.kind, i.args["name"]), ("home_state", name), said)

    def test_not_about_the_house(self):
        for said in ("o computador está ligado", "Você está funcionando?", "o wifi tá ligado"):
            self.assertIsNone(I.parse(said, home=True), said)
        self.assertIsNone(I.parse("o ar está ligado", home=False))

    def test_bare_verb_needs_a_recent_device(self):
        I._LAST.clear()
        self.assertIsNone(I.parse("liga", home=True))
        I._remember_device({"id": "climate.ar", "name": "Ar", "domain": "climate", "state": "off"})
        for said, on in (("liga", True), ("pode ligar", True), ("então desliga ele", False)):
            i = I.parse(said, home=True)
            self.assertEqual((i.kind, i.args["name"], i.args["on"]), ("home", "", on), said)
        self.assertIsNone(I.parse("liga", home=True, now=time.time() + 600))   # long after: not a reply
        I._LAST.clear()

    def test_state_sentence(self):
        ac = {"name": "Ar-condicionado", "domain": "climate"}
        self.assertEqual(I._state_text(ac, "off", {"current_temperature": 31.5}, True),
                         "Ar-condicionado está desligado. O ambiente está em 31,5 graus.")
        self.assertEqual(I._state_text(ac, "cool", {"temperature": 18.0}, True),
                         "Ar-condicionado está ligado, em 18 graus.")
        lamp = {"name": "Luz do Quarto", "domain": "light"}
        self.assertEqual(I._state_text(lamp, "on", {"brightness": 128}, True), "Luz do Quarto está acesa, em 50%.")
        self.assertIn("fora do ar", I._state_text(lamp, "unavailable", {}, True))
