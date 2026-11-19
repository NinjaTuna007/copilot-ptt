"""Which window a query names."""

import unittest

from tests import sandbox  # noqa: F401
from voice import screenshot

WINDOWS = [
    {"id": "0x1", "app": "thunderbird", "title": "Write: Hello"},
    {"id": "0x2", "app": "konsole", "title": "Henchline"},
]


class Matching(unittest.TestCase):
    def test_a_query_matches_the_app_or_the_title(self):
        self.assertEqual(screenshot.matching(WINDOWS, "thunder"), [WINDOWS[0]])
        self.assertEqual(screenshot.matching(WINDOWS, "hench"), [WINDOWS[1]])
        self.assertEqual(screenshot.matching(WINDOWS, ""), [])

    def test_a_window_id_has_to_be_one(self):
        self.assertIsNone(screenshot.window("thunderbird", screenshot.folder() / "nope.png"))
        self.assertIsNone(screenshot.window("0xZZ", screenshot.folder() / "nope.png"))


if __name__ == "__main__":
    unittest.main()
