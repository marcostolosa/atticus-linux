"""Talking to the browser: the message framing and which pages count."""
import struct
import unittest

from atticus_shell import browser


class Frames(unittest.TestCase):
    def unmask(self, frame):
        n = frame[1] & 0x7F
        at = 2
        if n == 126:
            n, at = struct.unpack(">H", frame[2:4])[0], 4
        elif n == 127:
            n, at = struct.unpack(">Q", frame[2:10])[0], 10
        mask, body = frame[at:at + 4], frame[at + 4:]
        self.assertEqual(len(body), n)
        return bytes(b ^ mask[i % 4] for i, b in enumerate(body))

    def test_a_message_survives_the_trip_at_every_size(self):
        for size in (0, 5, 125, 126, 70000):
            payload = ("é" * size).encode()[:size] if size else b""
            frame = browser._frame(payload)
            self.assertEqual(frame[0], 0x81)
            self.assertTrue(frame[1] & 0x80, "a client must mask what it sends")
            self.assertEqual(self.unmask(frame), payload)


class Pages(unittest.TestCase):
    def test_only_real_pages_are_listed(self):
        rows = [{"type": "page", "id": "a", "webSocketDebuggerUrl": "ws://x/a"},
                {"type": "service_worker", "id": "b", "webSocketDebuggerUrl": "ws://x/b"},
                {"type": "page", "id": "c"}]
        old, browser._http = browser._http, lambda *a, **k: rows
        try:
            self.assertEqual([t["id"] for t in browser.tabs()], ["a"])
            self.assertEqual(browser.tab("a")["id"], "a")
            self.assertIsNone(browser.tab("b"))
        finally:
            browser._http = old

    def test_its_data_is_kept_apart_from_the_everyday_browser(self):
        self.assertTrue(str(browser.profile()).endswith("atticus/browser") or "atticus" in str(browser.profile()))
