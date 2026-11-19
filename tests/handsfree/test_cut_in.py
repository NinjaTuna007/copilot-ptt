"""Hands-free to the Cursor CLI cuts in, or the words sit in its follow-up queue."""

import unittest

from tests import sandbox  # noqa: F401
import turn
from routing import Heard
from voice.desktop import Tab
from voice.state import Speaker

CLI = Speaker("cursor-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cursor", "henchline", Tab("tmux", "%12"))


class CutsIn(unittest.TestCase):
    def test_a_handsfree_turn_to_the_cli_cuts_in(self):
        self.assertTrue(turn.cut_in(Heard.REPLY, CLI))
        self.assertTrue(turn.cut_in(Heard.FOLLOW_ON, CLI))
        self.assertTrue(turn.cut_in(Heard.WOKE, CLI))
        self.assertTrue(turn.cut_in(Heard.THINK, CLI))

    def test_not_another_agent(self):
        claude = Speaker("claude-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "claude", "henchline", Tab("tmux", "%9"))
        self.assertFalse(turn.cut_in(Heard.REPLY, claude))
        editor = Speaker("cursor-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cursor", "henchline")
        self.assertFalse(turn.cut_in(Heard.REPLY, editor))


if __name__ == "__main__":
    unittest.main()
