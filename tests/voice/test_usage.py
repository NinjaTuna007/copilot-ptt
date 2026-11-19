"""The Cursor CLI's /usage panel, read as text; limits whose window has ended read as reset."""

import time
import unittest
from datetime import datetime

from tests import sandbox  # first: a throwaway HOME
from voice import usage

assert sandbox.HOME  # imported for its effect

PANEL = """
 Usage • Pro+                                                     Resets Nov 30
 Monthly plan and on-demand usage
 Category        Current             Usage
 Included        44% used            ██████░░░░
   Auto          40% used            █████░░░░░
   API           94% used            █████████░
 On-Demand       Disabled            ——————————
 On-demand usage is off
"""


class CursorPanel(unittest.TestCase):
    def test_rows_reset_and_plan(self):
        limits, notes = usage.parse_cursor_usage(PANEL, datetime(2026, 11, 17))
        self.assertEqual([(l["label"], l["percent"]) for l in limits],
                         [("Month: included", 44.0), ("Month: Auto model", 40.0), ("Month: other models", 94.0)])
        self.assertEqual(datetime.fromtimestamp(limits[0]["resets"]).date(), datetime(2026, 11, 30).date())
        self.assertEqual(notes, ["Plan: Pro+", "On-demand usage is off"])

    def test_reset_date_after_new_year(self):
        limits, _ = usage.parse_cursor_usage(PANEL.replace("Nov 30", "Jan 2"), datetime(2026, 12, 20))
        self.assertEqual(datetime.fromtimestamp(limits[0]["resets"]).year, 2027)

    def test_nothing_shown(self):
        self.assertEqual(usage.parse_cursor_usage(""), ([], []))


class Limits(unittest.TestCase):
    def test_ended_window_reads_as_reset(self):
        self.assertEqual(usage._limit("Week", 80, time.time() - 60), {"label": "Week", "percent": 0.0, "resets": 0})
        self.assertEqual(usage._limit("Week", 80, time.time() + 60)["percent"], 80.0)


class CodexName(unittest.TestCase):
    def test_newest_name_of_the_folders_thread(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            day = root / "2026/11/17"
            day.mkdir(parents=True)
            (day / "rollout-x.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {
                "id": "t-1", "cwd": "/home/me/proj"}}) + "\n")
            (Path(tmp) / "session_index.jsonl").write_text(
                json.dumps({"id": "t-1", "thread_name": "First"}) + "\n" +
                json.dumps({"id": "t-2", "thread_name": "Other"}) + "\n" +
                json.dumps({"id": "t-1", "thread_name": "Renamed"}) + "\n")
            self.assertEqual(usage.codex_name("codex-_home_me_proj", root), "Renamed")
            self.assertEqual(usage.codex_name("codex-_elsewhere", root), "")


class Readout(unittest.TestCase):
    def test_usage_and_context_are_different_sentences(self):
        found = {"limits": [{"label": "Week", "percent": 12}], "notes": ["Plan: Pro"],
                 "context": {"percent": 21, "size": 500000, "used": 100000,
                             "parts": [{"label": "Chat", "tokens": 80000}]}}
        self.assertIn("Week 12%", usage.words(found, "usage"))
        self.assertNotIn("Context", usage.words(found, "usage"))
        self.assertIn("21% full", usage.words(found, "context"))
        self.assertIn("Chat 80K", usage.words(found, "context"))
        self.assertEqual(usage.words({}, "usage"), "Usage hasn't been reported here.")
        self.assertEqual(usage.words({}, "context"), "Context hasn't been reported for this chat.")


if __name__ == "__main__":
    unittest.main()
