"""The wake-word spelling check, including a word heard in two pieces."""

import unittest

from tests import sandbox  # noqa: F401
from sensing import said


class Spelling(unittest.TestCase):
    def test_a_split_wake_word_still_counts(self):
        self.assertTrue(said("henchline", "hench line"))
        self.assertTrue(said("henchline", "hey henchline"))
        self.assertFalse(said("henchline", "the line"))

    def test_a_close_spelling_still_counts(self):
        self.assertTrue(said("jarvis", "jervis"))
        self.assertFalse(said("jarvis", "harvest"))


if __name__ == "__main__":
    unittest.main()
