"""Home Assistant client against a local stand-in server (no real home needed)."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

STATES = [
    {"entity_id": "light.sala", "state": "off", "attributes": {"friendly_name": "Luz da sala"}},
    {"entity_id": "light.quarto", "state": "on", "attributes": {"friendly_name": "Luz do quarto"}},
    {"entity_id": "light.quarto_abajur", "state": "off", "attributes": {"friendly_name": "Abajur do quarto"}},
    {"entity_id": "light.sala_tela_do_painel", "state": "on", "attributes": {"friendly_name": "Painel Screen"}},
    {"entity_id": "switch.cafeteira", "state": "off", "attributes": {"friendly_name": "Cafeteira"}},
    {"entity_id": "climate.sala", "state": "cool", "attributes": {"friendly_name": "Ar da sala"}},
    {"entity_id": "light.varanda", "state": "unavailable", "attributes": {"friendly_name": "Luz da varanda"}},
    {"entity_id": "switch.travada", "state": "off", "attributes": {"friendly_name": "Tomada travada"}},
    {"entity_id": "sensor.temp_sala", "state": "24.5", "attributes": {"friendly_name": "Temperatura da sala", "unit_of_measurement": "°C"}},
]
CALLS = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        if self.headers.get("Authorization") != "Bearer good":
            self.send_response(401); self.end_headers()
            return False
        return True

    def do_GET(self):
        if self._auth():
            one = self.path.rsplit("/", 1)[-1]
            found = [s for s in STATES if s["entity_id"] == one]
            body = json.dumps(found[0] if found else STATES).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if self._auth():
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            CALLS.append((self.path, data))
            service = self.path.rsplit("/", 1)[-1]
            for s in STATES:   # obey, except the stuck device, which accepts the call and stays as it was
                if s["entity_id"] == data.get("entity_id") and s["entity_id"] != "switch.travada":
                    if service == "turn_on": s["state"] = "on"
                    if service == "turn_off": s["state"] = "off"
                    if "brightness_pct" in data: s["attributes"]["brightness"] = round(data["brightness_pct"] * 2.55)
                    if "temperature" in data: s["attributes"]["temperature"] = data["temperature"]
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"[]")


class HomeAssistant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.dir = tempfile.mkdtemp(prefix="atticus-test-")
        os.environ["ATTICUS_DIR"] = cls.dir
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.environ.pop("ATTICUS_DIR", None)

    def config(self, token="good"):
        with open(os.path.join(self.dir, "settings.json"), "w") as f:
            json.dump({"haUrl": self.url, "haToken": token}, f)
        CALLS.clear()

    def test_find(self):
        from atticus_shell import home
        self.config()
        self.assertEqual([e["id"] for e in home.find("a luz da sala")], ["light.sala"])
        self.assertEqual([e["id"] for e in home.find("cafeteira")], ["switch.cafeteira"])
        self.assertEqual([e["id"] for e in home.find("ar da sala")], ["climate.sala"])
        self.assertEqual(len(home.find("quarto")), 2)            # ambiguous: the caller asks which
        self.assertEqual(home.find("luz da garagem"), [])
        self.assertEqual(home.find("temperatura da sala"), [])    # a sensor is not switchable
        self.assertEqual(len(home.find("temperatura da sala", switchable=False)), 1)

    def test_switch_and_set(self):
        from atticus_shell import home
        self.config()
        self.assertEqual(home.main(["on", "luz", "da", "sala"]), 0)
        self.assertEqual(CALLS[-1], ("/api/services/light/turn_on", {"entity_id": "light.sala"}))
        self.assertEqual(home.main(["off", "cafeteira"]), 0)
        self.assertEqual(CALLS[-1][0], "/api/services/switch/turn_off")
        self.assertEqual(home.main(["set", "luz da sala", "brightness", "40"]), 0)
        self.assertEqual(CALLS[-1][1], {"entity_id": "light.sala", "brightness_pct": 40})
        self.assertEqual(home.main(["set", "ar da sala", "temperature", "22"]), 0)
        self.assertEqual(CALLS[-1][1]["temperature"], 22.0)
        self.assertEqual(home.main(["on", "tomada travada"]), 1)   # accepted by the server, never changed: reported, not claimed
        n = len(CALLS)
        self.assertEqual(home.main(["on", "quarto"]), 1)          # ambiguous: nothing is switched
        self.assertEqual(home.main(["on", "garagem"]), 1)
        self.assertEqual(len(CALLS), n)

    def test_errors_are_said_not_raised(self):
        from atticus_shell import home
        self.config(token="bad")
        self.assertEqual(home.main(["on", "luz da sala"]), 1)
        with open(os.path.join(self.dir, "settings.json"), "w") as f:
            json.dump({}, f)
        self.assertFalse(home.configured())
        self.assertEqual(home.main(["list"]), 1)

    def test_instant_intent_runs(self):
        from atticus_shell import intents
        self.config()
        reply = intents.run(intents.parse("liga a luz da sala", home=True))
        self.assertEqual(reply, "Luz da sala: ligado.")
        self.assertEqual(CALLS[-1][0], "/api/services/light/turn_on")
        n = len(CALLS)
        self.assertEqual(intents.run(intents.parse("liga a luz da varanda", home=True)), "Luz da varanda está fora do ar no Home Assistant.")
        self.assertEqual(len(CALLS), n)   # an offline device is reported, never "switched"
        self.assertEqual(intents.run(intents.parse("liga a tomada travada", home=True)), "Tomada travada não respondeu.")
        self.assertIsNone(intents.run(intents.parse("apaga a luz do corredor", home=True)))   # unknown: the model takes it


if __name__ == "__main__":
    unittest.main()
