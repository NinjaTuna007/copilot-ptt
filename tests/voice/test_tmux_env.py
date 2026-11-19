"""voice/desktop/tmux.py: a new agent starts without another agent's inherited marks."""

import os
import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)
from voice.desktop import tmux


class CleanEnv(unittest.TestCase):
    def test_marks_from_tmux_and_from_us_are_unset(self):
        shown = "CURSOR_WORKSPACE_LABEL=smarc2\nHOME=/home/me\n-CURSOR_AGENT\nAGENT_TRANSCRIPTS=/x\n"
        with mock.patch.object(tmux, "out", return_value=shown), \
             mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t", "PATH": "/usr/bin"}, clear=True):
            words = tmux.clean_env()
        self.assertTrue(words[0].endswith("env"))
        self.assertEqual(sorted(words[1:]), ["-uAGENT_TRANSCRIPTS", "-uCODEX_THREAD_ID", "-uCURSOR_AGENT",
                                             "-uCURSOR_WORKSPACE_LABEL"])

    def test_only_an_off_color_setting_is_unset(self):
        shown = "HOME=/home/me\nNO_COLOR=1\nFORCE_COLOR=0\n"
        with mock.patch.object(tmux, "out", return_value=shown), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            words = tmux.clean_env()
        self.assertEqual(sorted(words[1:]), ["-uFORCE_COLOR", "-uNO_COLOR"])

    def test_force_color_left_on_is_kept(self):
        with mock.patch.object(tmux, "out", return_value="HOME=/home/me\nFORCE_COLOR=1\nNO_COLOR=\n"), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            self.assertEqual(sorted(tmux.clean_env()[1:]), ["-uNO_COLOR"])
        with mock.patch.object(tmux, "out", return_value="HOME=/home/me\nFORCE_COLOR=\n"), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            self.assertEqual(tmux.clean_env(), [])
        with mock.patch.object(tmux, "out", return_value="HOME=/home/me\nFORCE_COLOR=false\n"), \
             mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            self.assertEqual(sorted(tmux.clean_env()[1:]), ["-uFORCE_COLOR"])


if __name__ == "__main__":
    unittest.main()
