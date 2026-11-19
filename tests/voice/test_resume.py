"""A chat is recognised by its id."""

import unittest

from tests import sandbox  # noqa: F401
from voice import resume
from voice.desktop import Tab


class Identity(unittest.TestCase):
    def test_a_session_key_is_the_chat_id(self):
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        self.assertEqual(resume.chat_id("cursor-" + chat), chat)
        self.assertEqual(resume.chat_id("claude-" + chat), chat)
        self.assertEqual(resume.chat_id("cursor-_henchline"), "")

    def test_the_environment_names_the_chat_and_its_terminal(self):
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        raw = b"\0".join([
            b"CURSOR_CONVERSATION_ID=" + chat.encode(),
            b"KONSOLE_DBUS_SERVICE=:1.110",
            b"KONSOLE_DBUS_SESSION=/Sessions/5",
            b"OTHER=1",
        ])
        self.assertTrue(resume.carries("cursor", chat, raw))
        self.assertFalse(resume.carries("claude", chat, raw))
        self.assertEqual(resume.tab_of(raw), Tab(":1.110", "/Sessions/5"))
        pane = b"TMUX_PANE=%12\0CURSOR_CONVERSATION_ID=" + chat.encode()
        self.assertEqual(resume.tab_of(pane), Tab("tmux", "%12"))

    def test_a_resume_on_the_command_line_is_that_chat(self):
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        command = b"\0".join([b"cursor-agent", b"--force", b"--resume", chat.encode()])
        self.assertTrue(resume.resumed(command, chat))
        self.assertTrue(resume.resumed(b"cursor-agent\0--resume=" + chat.encode(), chat))
        self.assertFalse(resume.resumed(command, "521b6ab2-0692-4903-aef2-de675cd6e972"))
        self.assertFalse(resume.resumed(b"cursor-agent\0--resume\0", chat))
        self.assertFalse(resume.resumed(b"--resume=" + chat.encode() + b"-extra", chat))


if __name__ == "__main__":
    unittest.main()
