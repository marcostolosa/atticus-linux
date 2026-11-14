"""Reading, writing and the one-time wake-word key migration."""
import os
import tempfile
import unittest

from atticus_shell import settings as cfg


class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="atticus-test-")
        self.old = os.environ.get("ATTICUS_DIR")
        os.environ["ATTICUS_DIR"] = self.dir

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ATTICUS_DIR", None)
        else:
            os.environ["ATTICUS_DIR"] = self.old

    def test_set_and_get_roundtrip_and_keep_other_keys(self):
        cfg.set("safe", False)
        cfg.set("model", "opus")
        self.assertEqual(cfg.get("safe"), False)
        self.assertEqual(cfg.get("model"), "opus")

    def test_get_falls_back_to_the_given_default(self):
        self.assertEqual(cfg.get("neverSet", "fallback"), "fallback")


class WakeWordMigration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="atticus-test-")
        self.old = os.environ.get("ATTICUS_DIR")
        os.environ["ATTICUS_DIR"] = self.dir

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ATTICUS_DIR", None)
        else:
            os.environ["ATTICUS_DIR"] = self.old

    def test_a_real_configured_word_moves_to_its_own_key_not_a_bool(self):
        cfg.set("wake", True)
        cfg.set("wakeWord", "siri")   # the pre-Settings-window shape: wake=on/off, wakeWord=the word
        cfg.migrate_wake_word_keys()
        self.assertEqual(cfg.get("wakeWordName"), "siri")
        self.assertIs(cfg.get("wakeWord"), True)

    def test_wake_off_is_preserved_through_the_migration(self):
        cfg.set("wake", False)
        cfg.set("wakeWord", "atticus")
        cfg.migrate_wake_word_keys()
        self.assertEqual(cfg.get("wakeWordName"), "atticus")
        self.assertIs(cfg.get("wakeWord"), False)

    def test_nothing_to_migrate_is_a_no_op(self):
        cfg.migrate_wake_word_keys()
        self.assertIsNone(cfg.get("wakeWordName"))
        self.assertIsNone(cfg.get("wakeWord"))

    def test_already_migrated_is_left_alone(self):
        cfg.set("wakeWord", True)
        cfg.set("wakeWordName", "siri")
        cfg.migrate_wake_word_keys()
        self.assertEqual(cfg.get("wakeWordName"), "siri")
        self.assertIs(cfg.get("wakeWord"), True)

    def test_runs_safely_twice_in_a_row(self):
        cfg.set("wake", True)
        cfg.set("wakeWord", "siri")
        cfg.migrate_wake_word_keys()
        cfg.migrate_wake_word_keys()
        self.assertEqual(cfg.get("wakeWordName"), "siri")
        self.assertIs(cfg.get("wakeWord"), True)
