"""voice/plans.py: a plan file's steps, and which file belongs to a project."""

import tempfile
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401
from voice import plans

SAMPLE = """---
name: Phone plan
overview: Show the steps on the phone.
todos:
  - id: one
    content: "Read the file"
    status: completed
  - id: two
    content: "Draw the steps"
    status: in_progress
  - id: three
    content: A button
    status: pending
isProject: false
---

# Phone plan

```mermaid
flowchart LR
  a --> b
```
"""


class Parsed(unittest.TestCase):
    def test_the_steps_and_how_many_are_done(self):
        got = plans.parse(SAMPLE)
        self.assertEqual(got["name"], "Phone plan")
        self.assertEqual(got["done"], 1)
        self.assertEqual(got["total"], 3)
        self.assertEqual(got["todos"][1]["status"], "in_progress")
        self.assertIn("mermaid", got["body"])

    def test_a_cursor_file_takes_its_title_from_the_heading(self):
        text = """---
todos:
  - id: one
    content: "Read the file"
    status: pending
isProject: false
---
# Email into the list
"""
        got = plans.parse(text)
        self.assertEqual(got["name"], "Email into the list")
        self.assertEqual(got["total"], 1)

    def test_a_comment_before_the_header_is_not_the_document(self):
        text = """<!-- 2ed6eebc-f2ce-43e5-8730-fc126b9f6794 -->
---
todos:
  - id: voices
    content: "A voices sheet"
    status: pending
isProject: false
---
# Voices

See [plan_mode](call/server.py) and the rest.

```mermaid
flowchart LR
  phone[Phone] --> laptop[Laptop]
```
"""
        got = plans.parse(text)
        self.assertEqual(got["name"], "Voices")
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["todos"][0]["content"], "A voices sheet")
        self.assertNotIn("<!--", got["body"])
        self.assertNotIn("id:", got["body"])
        paragraph = next(b for b in got["blocks"] if b["t"] == "p")
        self.assertNotIn("](", paragraph["html"])
        self.assertIn("plan_mode", paragraph["html"])
        diagram = next(b for b in got["blocks"] if b["t"] == "diagram")
        self.assertEqual(diagram["nodes"], ["Phone", "Laptop"])

    def test_the_newest_file_for_this_chat_is_the_plan(self):
        import os
        root = Path(tempfile.mkdtemp())
        folder = root / ".cursor/plans"
        folder.mkdir(parents=True)
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        old = folder / "old.plan.md"
        new = folder / "new.plan.md"
        old.write_text(f"<!-- {chat} -->\n---\nname: Old\ntodos:\n  - id: a\n    content: waiting\n    status: pending\n---\n")
        new.write_text(f"<!-- {chat} -->\n---\nname: New\ntodos:\n  - id: a\n    content: done\n    status: completed\n---\n")
        os.utime(old, (1, 1_000))
        os.utime(new, (1, 2_000))
        got = plans.for_session(f"cursor-{chat}", root)
        self.assertEqual(got, new)

    def test_a_plan_that_names_the_project_is_the_one(self):
        root = Path(tempfile.mkdtemp())
        folder = root / "plans"
        folder.mkdir()
        (folder / "other.plan.md").write_text("---\nname: Other\ntodos:\n  - id: a\n    content: no\n    status: pending\n---\n")
        (folder / "mine.plan.md").write_text(SAMPLE.replace("Phone plan", "henchline work"))
        project = root / "henchline"
        project.mkdir()
        import voice.plans as mod
        original = mod._places
        mod._places = lambda _project: [folder]
        try:
            got = plans.find(project)
        finally:
            mod._places = original
        self.assertEqual(got["name"], "henchline work")
        self.assertTrue(got["file"].endswith("mine.plan.md"))


if __name__ == "__main__":
    unittest.main()
