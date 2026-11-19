"""voice/cursor_cli.py: the CLI's flat model list grouped like the editor's menu, and the keys that set it."""

import json
import sqlite3
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401
from voice import cursor_cli

GROK = {"id": "grok-4.7", "name": "Grok 4.7", "on": True, "params": [
    {"id": "context", "name": "Context", "kind": "enum",
     "values": [{"value": "256k", "name": "256K"}, {"value": "500k", "name": "500K"}]},
    {"id": "reasoning_effort", "name": "Effort", "kind": "enum",
     "values": [{"value": "low", "name": "Low"}, {"value": "medium", "name": "Medium"},
                {"value": "high", "name": "High"}, {"value": "xhigh", "name": "Extra High"}]},
    {"id": "fast", "name": "Fast", "kind": "bool", "values": [{"value": "false", "name": "false"},
                                                             {"value": "true", "name": "Fast"}]},
]}
COMPOSER = {"id": "composer-2.5", "name": "Composer 2.5", "on": True, "params": []}
COMPOSER_FAST = {"id": "composer-2.5-fast", "name": "Composer 2.5 Fast", "on": True, "params": []}

EDITOR = """
 Grok 4.7 — Edit Parameters
  Context
  → ○ 256K
    ● 500K ✓
  Effort
    ● Low ✓
    ○ Medium
    ○ High
    ○ Extra High
    ◯ Fast
 ↑/↓ to navigate • Enter to select • Esc to go back
""".splitlines()


class Menu(unittest.TestCase):
    def test_a_variant_slug_belongs_to_its_model(self):
        shown = cursor_cli.menu([GROK, COMPOSER, COMPOSER_FAST], ["grok-4.7-low", "grok-4.7-xhigh-fast", "auto"])
        self.assertEqual([m["id"] for m in shown], ["grok-4.7"])

    def test_auto_stays_on_the_menu(self):
        auto = {"id": "default", "name": "Auto", "on": True, "params": []}
        shown = cursor_cli.menu([auto, GROK], ["auto", "grok-4.7-xhigh"])
        self.assertEqual([m["id"] for m in shown], ["default", "grok-4.7"])
        self.assertEqual(cursor_cli.cli_slug("default"), "auto")
        self.assertEqual(cursor_cli.cli_slug("grok-4.7"), "grok-4.7")

    def test_a_longer_model_id_blocks_a_blind_switch(self):
        self.assertEqual(cursor_cli.longer_model(
            {"id": "composer-2.5"},
            [{"id": "composer-2.5", "name": "Composer 2.5"},
             {"id": "composer-2.5-fast", "name": "Composer 2.5 Fast"}]), "Composer 2.5 Fast")
        self.assertEqual(cursor_cli.longer_model({"id": "grok-4.7"}, [{"id": "grok-4.7", "name": "Grok 4.7"}]), "")

    def test_the_longer_id_wins(self):
        shown = cursor_cli.menu([COMPOSER, COMPOSER_FAST], ["composer-2.5", "composer-2.5-fast"])
        self.assertEqual([m["id"] for m in shown], ["composer-2.5", "composer-2.5-fast"])

    def test_the_status_line_is_the_current_pick(self):
        got = cursor_cli.reading("Grok 4.7 500K Extra High Fast", [GROK])
        self.assertEqual(got, {"model": "grok-4.7",
                               "params": {"context": "500k", "reasoning_effort": "xhigh", "fast": "true"}})

    def test_a_slash_command_in_the_words_is_broken(self):
        got = cursor_cli.disarm("fix the /model filter please")
        self.assertIn("/\u200bmodel", got)
        self.assertFalse(cursor_cli.SLASH.match(got))

    def test_a_path_stays_whole(self):
        path = "look at /home/shekharu/henchline/voice"
        self.assertEqual(cursor_cli.disarm(path), path)

    def test_high_is_not_read_out_of_extra_high(self):
        self.assertEqual(cursor_cli.reading("Grok 4.7 500K Extra High", [GROK])["params"]["reasoning_effort"], "xhigh")


class Editor(unittest.TestCase):
    def test_only_what_is_wrong_is_pressed(self):
        wanted = {"context": "500k", "reasoning_effort": "medium", "fast": "false"}
        keys, why = cursor_cli.plan(EDITOR, GROK, wanted)
        self.assertEqual(why, "")
        self.assertEqual(keys, ["Down", "Down", "Down", "Enter"])
        # Konsole cannot read the screen, so it rebuilds this same editor from the catalog
        screen = cursor_cli.editor_screen(GROK, {"context": "500k", "reasoning_effort": "low", "fast": "false"})
        self.assertEqual(cursor_cli.plan(screen, GROK, wanted), (keys, ""))

    def test_a_choice_already_on_is_left(self):
        wanted = {"context": "500k", "reasoning_effort": "low", "fast": "false"}
        self.assertEqual(cursor_cli.plan(EDITOR, GROK, wanted), ([], ""))

    def test_fast_is_toggled_only_when_it_is_off(self):
        wanted = {"context": "500k", "reasoning_effort": "low", "fast": "true"}
        keys, why = cursor_cli.plan(EDITOR, GROK, wanted)
        self.assertEqual(why, "")
        self.assertEqual(keys, ["Down"] * 6 + ["Enter"])

    def test_fast_already_on_is_seen(self):
        lines = [line.replace("◯ Fast", "◉ Fast") for line in EDITOR]
        wanted = {"context": "500k", "reasoning_effort": "low", "fast": "true"}
        self.assertEqual(cursor_cli.plan(lines, GROK, wanted), ([], ""))

    def test_a_longer_name_is_not_the_same_model(self):
        row = " →  Composer 2.5 Fast          1M (Tab to modify)"
        self.assertEqual(cursor_cli.row_title(row), "Composer 2.5 Fast")
        self.assertNotEqual(cursor_cli.highlighted_title([row]).lower(), "composer 2.5")

    def test_a_missing_row_is_refused(self):
        keys, why = cursor_cli.plan(["  → ○ 256K"], GROK, {"context": "256k", "reasoning_effort": "low", "fast": "false"})
        self.assertEqual(keys, [])
        self.assertIn("Effort", why)


class Saved(unittest.TestCase):
    CHAT = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"

    def setUp(self):
        store = Path.home() / ".cursor/chats/proj" / self.CHAT
        store.mkdir(parents=True, exist_ok=True)
        db_path = store / "store.db"
        db_path.unlink(missing_ok=True)
        with sqlite3.connect(db_path) as db:
            db.execute("create table meta (key text primary key, value text)")
            db.execute("insert into meta values ('0', ?)", (json.dumps({"lastUsedModel": "grok-4.7"}),))
        cfg = Path.home() / ".cursor/cli-config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "selectedModel": {"modelId": "grok-4.7", "parameters": [
                {"id": "context", "value": "500k"},
                {"id": "reasoning_effort", "value": "xhigh"},
                {"id": "fast", "value": "false"}]},
            "modelParameters": {"composer-2.5": [{"id": "fast", "value": "true"}]},
        }))

    def test_the_selected_models_parameters_are_the_chats(self):
        self.assertEqual(cursor_cli.saved(self.CHAT), {
            "model": "grok-4.7",
            "params": {"context": "500k", "reasoning_effort": "xhigh", "fast": "false"}})
        self.assertEqual(cursor_cli.described(GROK, cursor_cli.saved(self.CHAT)["params"]),
                         "Grok 4.7 · Extra High · 500K")

    def test_another_models_saved_parameters_are_used(self):
        store = next((Path.home() / ".cursor/chats").glob(f"*/{self.CHAT}/store.db"))
        with sqlite3.connect(store) as db:
            db.execute("update meta set value = ? where key = '0'", (json.dumps({"lastUsedModel": "composer-2.5"}),))
        self.assertEqual(cursor_cli.saved(self.CHAT)["params"], {"fast": "true"})

    def test_auto_counts_when_the_store_says_default(self):
        store = next((Path.home() / ".cursor/chats").glob(f"*/{self.CHAT}/store.db"))
        with sqlite3.connect(store) as db:
            db.execute("update meta set value = ? where key = '0'", (json.dumps({"lastUsedModel": "default"}),))
        self.assertTrue(cursor_cli.on_model(self.CHAT, "default"))
        self.assertEqual(cursor_cli.saved(self.CHAT)["model"], "default")
        self.assertFalse(cursor_cli.on_model(self.CHAT, "grok-4.7"))

    def test_auto_in_the_store_is_the_menus_default(self):
        store = next((Path.home() / ".cursor/chats").glob(f"*/{self.CHAT}/store.db"))
        with sqlite3.connect(store) as db:
            db.execute("update meta set value = ? where key = '0'", (json.dumps({"lastUsedModel": "auto"}),))
        self.assertEqual(cursor_cli.saved(self.CHAT)["model"], "default")
        self.assertTrue(cursor_cli.on_model(self.CHAT, "default"))


if __name__ == "__main__":
    unittest.main()
