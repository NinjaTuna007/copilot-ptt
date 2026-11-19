"""voice/commands.py: the phone's "/" list for an agent."""

import tempfile
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)
from voice import commands


class Described(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def write(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_front_matter_line_and_block(self):
        self.assertEqual(commands.described(self.write("a.md", "---\ndescription: Say hi\n---\nbody")), "Say hi")
        folded = "---\nname: x\ndescription: >-\n  Speak updates\n  out loud.\nother: y\n---\n"
        self.assertEqual(commands.described(self.write("b.md", folded)), "Speak updates out loud.")

    def test_first_prose_line_without_front_matter(self):
        self.assertEqual(commands.described(self.write("c.md", "\n# Deploy the site\nsteps")), "Deploy the site")


class Listed(unittest.TestCase):
    def test_phone_actions_first_then_built_ins_then_custom(self):
        home = Path(tempfile.mkdtemp())
        project = Path(tempfile.mkdtemp())
        (project / ".claude/commands/git").mkdir(parents=True)
        (project / ".claude/commands/git/ship.md").write_text("---\ndescription: Ship it\n---\n")
        (home / ".claude/skills/talk").mkdir(parents=True)
        (home / ".claude/skills/talk/SKILL.md").write_text("---\ndescription: Talk\n---\n")
        names = [c["name"] for c in commands.listed("claude", project, home)]
        self.assertEqual(names[:5], ["stop", "model", "flush", "call", "later"])
        self.assertIn("compact", names)
        self.assertIn("plan", names)
        self.assertIn("git:ship", names)
        self.assertIn("talk", names)
        self.assertNotIn("resume", names)  # a picker in the terminal: nobody could press its keys

    def test_a_name_is_listed_once_project_first(self):
        home = Path(tempfile.mkdtemp())
        project = Path(tempfile.mkdtemp())
        for root, about in ((project, "Project's"), (home, "Mine")):
            (root / ".cursor/commands").mkdir(parents=True)
            (root / ".cursor/commands/fleet.md").write_text(f"---\ndescription: {about}\n---\n")
        fleet = [c for c in commands.listed("cursor", project, home) if c["name"] == "fleet"]
        self.assertEqual(fleet, [{"name": "fleet", "about": "Project's", "kind": "custom"}])

    def test_codex_prompts_are_prefixed(self):
        home = Path(tempfile.mkdtemp())
        (home / ".codex/prompts").mkdir(parents=True)
        (home / ".codex/prompts/plan.md").write_text("Make a plan")
        self.assertIn({"name": "prompts:plan", "about": "Make a plan", "kind": "custom"},
                      commands.listed("codex", None, home))

class SkipPermissions(unittest.TestCase):
    def test_a_settings_file_that_bypasses_is_caught(self):
        home = Path(tempfile.mkdtemp())
        project = Path(tempfile.mkdtemp())
        paths = commands.claude_settings(project, home)
        self.assertEqual(len(paths), 4)
        self.assertFalse(commands.settings_skip_permissions(paths))
        (home / ".claude").mkdir()
        (home / ".claude/settings.json").write_text('{"permissions": {"defaultMode": "acceptEdits"}}')
        self.assertFalse(commands.settings_skip_permissions(paths))
        (project / ".claude").mkdir()
        (project / ".claude/settings.local.json").write_text(
            '{"permissions": {"defaultMode": "bypassPermissions"}}')
        self.assertTrue(commands.settings_skip_permissions(paths))

    def test_a_settings_file_that_cannot_be_read_stops_the_press(self):
        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_text("{")
        self.assertTrue(commands.settings_skip_permissions([path]))
        path.write_bytes(b"\xff")
        self.assertTrue(commands.settings_skip_permissions([path]))


class PlanPress(unittest.TestCase):
    def test_a_miss_or_bypass_does_not_ask_for_another_press(self):
        self.assertEqual(commands.shift_tab_result("plan", "bypass"), "stop")
        self.assertEqual(commands.shift_tab_result("plan", "plan"), "stop")
        self.assertEqual(commands.shift_tab_result("plan", ""), "stop")
        self.assertEqual(commands.shift_tab_result("plan", "accept"), "agent")

    def test_plan_is_remembered_only_when_the_footer_says_so(self):
        self.assertEqual(commands.plan_command_result("plan"), "plan")
        self.assertEqual(commands.plan_command_result("bypass"), "stop")
        self.assertEqual(commands.plan_command_result("accept"), "unchanged")

    def test_a_mode_word_above_the_footer_does_not_count(self):
        screen = "we discussed plan mode\n" + "\n".join(["transcript"] * 12) + "\naccept edits"
        self.assertEqual(commands.claude_mode_word(screen), "accept")


if __name__ == "__main__":
    unittest.main()
