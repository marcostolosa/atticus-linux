"""The Settings window's backend: what each {do: "..."} message actually
does, with the brain faked out so these run without a model or a service.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from atticus_shell import settings_backend as backend
from atticus_shell import settings as cfg


class SettingsBackend(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="atticus-test-")
        self.old = os.environ.get("ATTICUS_DIR")
        os.environ["ATTICUS_DIR"] = self.dir

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ATTICUS_DIR", None)
        else:
            os.environ["ATTICUS_DIR"] = self.old

    def test_a_plain_setting_is_written_and_nothing_is_echoed_back(self):
        self.assertIsNone(backend.dispatch({"do": "set", "k": "safe", "v": False}))
        self.assertEqual(cfg.get("safe"), False)

    def test_an_unknown_message_does_nothing(self):
        self.assertIsNone(backend.dispatch({"do": "something-nobody-sends"}))
        self.assertIsNone(backend.dispatch({}))

    def test_quit_is_a_special_sentinel_not_a_page_call(self):
        self.assertEqual(backend.dispatch({"do": "quit"}), ("__quit__", None))

    def test_dictionary_words_are_added_edited_and_removed(self):
        name, payload = backend.dispatch({"do": "dictationWord", "word": "Omarchy"})
        self.assertEqual((name, payload["words"]), ("dictation", ["Omarchy"]))
        name, payload = backend.dispatch({"do": "dictationWord", "old": "Omarchy", "word": "Hyprland"})
        self.assertEqual(payload["words"], ["Hyprland"])
        name, payload = backend.dispatch({"do": "dictationWordDelete", "word": "Hyprland"})
        self.assertEqual(payload["words"], [])

    def test_editing_a_word_to_empty_removes_it(self):
        backend.dispatch({"do": "dictationWord", "word": "um"})
        backend.dispatch({"do": "dictationWord", "word": "dois"})
        _, payload = backend.dispatch({"do": "dictationWord", "old": "um", "word": ""})
        self.assertEqual(payload["words"], ["dois"])

    def test_dictation_history_delete_and_clear_go_through_the_page(self):
        from atticus_shell import dictation_history as dh
        row = dh.add("Chego às oito.")
        name, payload = backend.dispatch({"do": "dictationDelete", "id": row["id"]})
        self.assertEqual((name, payload["history"]), ("dictation", []))
        dh.add("outra frase")
        name, payload = backend.dispatch({"do": "dictationClear"})
        self.assertEqual((name, payload["history"]), ("dictation", []))

    def test_a_permission_that_is_really_checkable_reports_honestly(self):
        with patch.object(backend, "PERM_CHECKS", {"ax": lambda: False}), \
             patch.object(backend, "PERM_FIX", {"ax": "set up ydotool"}):
            name, payload = backend.dispatch({"do": "grant", "id": "ax"})
            self.assertEqual((name, payload["text"]), ("notice", "set up ydotool"))
        with patch.object(backend, "PERM_CHECKS", {"ax": lambda: True}):
            name, payload = backend.dispatch({"do": "grant", "id": "ax"})
            self.assertEqual((name, payload), ("perms", {"ax": True}))

    def test_a_grant_with_no_linux_equivalent_says_so_instead_of_pretending(self):
        name, payload = backend.dispatch({"do": "grant", "id": "auto"})
        self.assertEqual(name, "notice")
        self.assertIn("no per-app permission", payload["text"])

    def test_deferred_subsystems_say_so_rather_than_silently_doing_nothing(self):
        for do in ("whisperInstall", "record", "automation", "resetHotkey"):
            name, payload = backend.dispatch({"do": do})
            self.assertEqual(name, "notice")
            self.assertTrue(payload["text"])

    def test_background_agents_are_honestly_reported_as_zero(self):
        self.assertEqual(backend.dispatch({"do": "agents"}), ("agents", {"count": 0}))

    @patch.object(backend, "brain_json")
    def test_remember_a_rule_asks_the_brain_then_refreshes_state(self, brain_json):
        brain_json.return_value = {"rules": ["when I say focus mode, open Notes"]}
        with patch.object(backend, "brain", return_value="") as brain_call:
            name, payload = backend.dispatch({"do": "remember", "line": "when I say focus mode, open Notes"})
        brain_call.assert_called_once_with("--remember", "when I say focus mode, open Notes")
        self.assertEqual((name, payload["rules"]), ("state", ["when I say focus mode, open Notes"]))

    def test_the_brain_helper_never_raises_when_the_subprocess_cannot_run(self):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            self.assertEqual(backend.brain("--state"), "")
            self.assertIsNone(backend.brain_json("--state"))

    def test_mic_list_skips_monitors_and_atticus_own_devices(self):
        rows = [
            {"name": "alsa_input.real", "properties": {"device.class": "sound", "device.description": "Built-in Mic"}},
            {"name": "alsa_output.x.monitor", "properties": {"device.class": "monitor"}},
            {"name": "atticus_aec_source", "properties": {"device.class": "sound"}},
        ]
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = __import__("json").dumps(rows)
            found = backend.mic_list()
        self.assertEqual(found, [{"name": "Built-in Mic", "uid": "alsa_input.real"}])

    def test_an_untouched_setting_shows_its_real_runtime_default(self):
        self.assertEqual(cfg.all().get("safe"), None)   # never written: this is the point of the test
        prefs = backend.prefs_with_defaults()
        self.assertEqual(prefs["safe"], True)
        self.assertEqual(prefs["dictateMode"], "verbatim")
        self.assertEqual(prefs["model"], "sonnet")

    def test_a_saved_value_still_wins_over_the_default(self):
        cfg.set("safe", False)
        self.assertEqual(backend.prefs_with_defaults()["safe"], False)

    def test_initial_state_always_gives_the_page_a_hotkey_label_string(self):
        # settings.html's own paint() calls label.match(...) on these two keys
        # unconditionally; undefined (no key at all) crashes st.load() entirely.
        with patch.object(backend, "hyprland_binds", return_value={}):
            prefs = backend.initial_state()["prefs"]
        self.assertEqual(prefs["hotkeyLabel"], "")
        self.assertEqual(prefs["dictateHotkeyLabel"], "")
        with patch.object(backend, "hyprland_binds", return_value={"talk": "SUPER+SPACE"}):
            prefs = backend.initial_state()["prefs"]
        self.assertEqual(prefs["hotkeyLabel"], "SUPER+SPACE")
        self.assertEqual(prefs["dictateHotkeyLabel"], "")

    def test_hyprland_binds_finds_the_talk_and_dictate_keys(self):
        rows = [{"mods": "SUPER", "key": "SPACE", "arg": "atticus-ctl press"},
                {"mods": "SUPER", "key": "D", "arg": "atticus-ctl dictate"},
                {"mods": "SUPER", "key": "Q", "arg": "killactive"}]
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = __import__("json").dumps(rows)
            found = backend.hyprland_binds()
        self.assertEqual(found, {"talk": "SUPER+SPACE", "dictate": "SUPER+D"})
