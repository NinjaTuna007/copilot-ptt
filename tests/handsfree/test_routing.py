"""handsfree/core/routing.py: what a transcribed turn asks for, before any agent sees it."""

import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # first: a throwaway HOME
import routing
from routing import Meaning
from voice.desktop import Tab

assert sandbox.HOME  # imported for its effect


class Switching(unittest.TestCase):
    def test_the_fixed_phrases(self):
        self.assertEqual(routing.switch_request("Switch me to Cursor"), ("cursor", None, False))
        self.assertEqual(routing.switch_request("just switch me to Cursor in pc diagnostics"),
                         ("cursor", "pc diagnostics", False))
        self.assertEqual(routing.switch_request("send that to Codex"), ("codex", None, True))
        self.assertEqual(routing.switch_request("Scratch that, hand this to Claude")[0], "claude")
        self.assertEqual(routing.switch_request("Yeah, I'm here now. Please switch me to Cursor.")[0], "cursor")

    def test_messages_that_merely_mention_an_agent_are_messages(self):
        self.assertIsNone(routing.switch_request("don't switch me to Claude"))
        self.assertIsNone(routing.switch_request("I wonder whether Cursor would handle this better than Claude"))
        long_lead = "This is a long message about the parser and the tests and the build. Switch me to Cursor."
        self.assertIsNone(routing.switch_request(long_lead))


class HangingUp(unittest.TestCase):
    def test_short_requests_only(self):
        self.assertTrue(routing.hangup_request("hang up"))
        self.assertTrue(routing.hangup_request("Okay, hang up now"))
        self.assertTrue(routing.hangup_request("I'm in, hang up"))
        self.assertFalse(routing.hangup_request("how do I hang up?"))
        self.assertFalse(routing.hangup_request("don't hang up"))

    def test_only_on_a_call(self):
        with mock.patch.object(routing, "in_call", return_value=True):
            self.assertEqual(routing.meaning("hang up"), Meaning.HANGUP)
        with mock.patch.object(routing, "in_call", return_value=False):
            self.assertEqual(routing.meaning("hang up"), Meaning.MESSAGE)


class Meanings(unittest.TestCase):
    def test_each_kind(self):
        self.assertEqual(routing.meaning("give me a minute"), Meaning.TIME)
        self.assertEqual(routing.meaning("who am I talking to?"), Meaning.ROUTING)
        self.assertEqual(routing.meaning("hands-free off"), Meaning.OFF)
        self.assertEqual(routing.meaning("never mind"), Meaning.CANCEL)
        self.assertEqual(routing.meaning("um"), Meaning.NOTHING)
        self.assertEqual(routing.meaning("   "), Meaning.NOTHING)
        self.assertEqual(routing.meaning("run the tests again please"), Meaning.MESSAGE)
        self.assertEqual(routing.meaning("yes"), Meaning.MESSAGE)


class OnACall(unittest.TestCase):
    """The first words an agent gets in a call carry how to be on a call, once per call."""

    def test_once_per_call_and_only_on_a_call(self):
        speaker = routing.Speaker("claude-1234", "claude", "x")
        with mock.patch.object(routing, "in_call", return_value=True):
            self.assertIn("stay responsive", routing.on_a_call(speaker, routing.Heard.CALL))
            self.assertEqual(routing.on_a_call(speaker, routing.Heard.TEXTED), "")  # a text isn't the call
            routing.told_on_call(speaker, routing.Heard.CALL)
            self.assertEqual(routing.on_a_call(speaker, routing.Heard.CALL), "")
        with mock.patch.object(routing, "in_call", return_value=False):
            self.assertEqual(routing.on_a_call(routing.Speaker("claude-5678", "claude", "x"), routing.Heard.CALL), "")


class Placing(unittest.TestCase):
    """A dead chat is resumed once. Two live copies are left alone, and a resume that never pins is not started again."""

    CHAT = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"

    def setUp(self):
        routing._RESUMING.mkdir(parents=True, exist_ok=True)
        for path in routing._RESUMING.glob("*"):
            path.unlink()
        self.speaker = routing.Speaker("claude-" + self.CHAT, "claude", "henchline")

    def tearDown(self):
        for path in routing._RESUMING.glob("*"):
            path.unlink()

    def test_two_copies_are_refused(self):
        hits = [(11, Tab("tmux", "%1")), (12, Tab("tmux", "%2"))]
        with mock.patch("voice.resume.find_running", return_value=hits) as found, \
                mock.patch("voice.desktop.tmux.resume") as resume, \
                mock.patch.object(routing, "say") as say:
            note = routing.place(self.speaker)
        self.assertEqual(note, "That chat is open in more than one terminal, so I didn't start another.")
        found.assert_called_once_with("claude", self.CHAT)
        resume.assert_not_called()
        say.assert_not_called()
        self.assertEqual(self.speaker.pid, 0)

    def test_a_resume_that_pins_is_announced(self):
        with mock.patch("voice.resume.find_running", return_value=[]), \
                mock.patch("voice.resume.folder_of", return_value=Path("/tmp/henchline-place")), \
                mock.patch("voice.desktop.tmux.resume", return_value=Tab("tmux", "%9")) as resume, \
                mock.patch.object(routing, "say") as say, \
                mock.patch.object(routing.Speaker, "pin", return_value=True), \
                mock.patch.object(routing.Speaker, "save") as save:
            note = routing.place(self.speaker)
        self.assertEqual(note, "")
        resume.assert_called_once()
        save.assert_called_once()
        say.assert_called_once()
        self.assertIn("I started that chat.", say.call_args.args[0])

    def test_a_resume_that_never_pins_is_not_started_again(self):
        clock = {"t": 100_000.0}

        def monotonic():
            clock["t"] += 0.35
            return clock["t"]

        with mock.patch("voice.resume.find_running", return_value=[]), \
                mock.patch("voice.resume.folder_of", return_value=Path("/tmp/henchline-place")), \
                mock.patch("voice.desktop.tmux.resume", return_value=Tab("tmux", "%9")) as resume, \
                mock.patch.object(routing, "say") as say, \
                mock.patch.object(routing.time, "monotonic", monotonic), \
                mock.patch.object(routing.time, "sleep"), \
                mock.patch.object(routing.Speaker, "pin", return_value=False), \
                mock.patch.object(routing.Speaker, "save") as save, \
                mock.patch("voice.desktop.tmux.pane_there", return_value=True):
            note = routing.place(self.speaker)
            self.assertEqual(note, "That chat didn't come up in tmux.")
            say.assert_not_called()
            save.assert_not_called()
            self.assertFalse(self.speaker.tab)  # the new pane is not remembered until it is pinned
            again = routing.place(self.speaker)
        self.assertEqual(again, "")  # the pane is still there: the words go to it
        self.assertEqual(self.speaker.tab, Tab("tmux", "%9"))
        self.assertEqual(resume.call_count, 1)

    def test_a_live_pane_is_not_started_again_after_the_wait(self):
        clock = {"t": 100_000.0}

        def monotonic():
            clock["t"] += 0.35
            return clock["t"]

        desk = mock.Mock()
        desk.terminals.agent_running.return_value = True
        with mock.patch("voice.resume.find_running", return_value=[]), \
                mock.patch("voice.resume.folder_of", return_value=Path("/tmp/henchline-place")), \
                mock.patch("voice.desktop.tmux.resume", return_value=Tab("tmux", "%9")) as resume, \
                mock.patch.object(routing, "say"), \
                mock.patch.object(routing.time, "monotonic", monotonic), \
                mock.patch.object(routing.time, "sleep"), \
                mock.patch.object(routing.Speaker, "pin", return_value=False), \
                mock.patch.object(routing.Speaker, "save"), \
                mock.patch("voice.desktop.tmux.pane_there", return_value=True):
            self.assertEqual(routing.place(self.speaker), "That chat didn't come up in tmux.")
            clock["t"] += 40
            self.assertEqual(routing.place(self.speaker), "")
        self.assertEqual(resume.call_count, 1)

    def test_a_dead_pane_can_be_started_again_after_the_wait(self):
        clock = {"t": 100_000.0}

        def monotonic():
            clock["t"] += 0.35
            return clock["t"]

        desk = mock.Mock()
        with mock.patch("voice.resume.find_running", return_value=[]), \
                mock.patch("voice.resume.folder_of", return_value=Path("/tmp/henchline-place")), \
                mock.patch("voice.desktop.tmux.resume", return_value=Tab("tmux", "%9")) as resume, \
                mock.patch.object(routing, "say"), \
                mock.patch.object(routing.time, "monotonic", monotonic), \
                mock.patch.object(routing.time, "sleep"), \
                mock.patch.object(routing.Speaker, "pin", return_value=False), \
                mock.patch.object(routing.Speaker, "save"), \
                mock.patch("voice.desktop.tmux.pane_there", return_value=False):
            routing.place(self.speaker)
            clock["t"] += 40
            routing.place(self.speaker)
        self.assertEqual(resume.call_count, 2)

    def test_a_second_call_while_resume_is_running_does_not_start_another(self):
        gate = threading.Event()
        entered = threading.Event()

        def resume(*_args, **_kwargs):
            entered.set()
            gate.wait(2)
            return Tab("tmux", "%9")

        other = routing.Speaker("claude-" + self.CHAT, "claude", "henchline")
        with mock.patch("voice.resume.find_running", return_value=[]), \
                mock.patch("voice.resume.folder_of", return_value=Path("/tmp/henchline-place")), \
                mock.patch("voice.desktop.tmux.resume", side_effect=resume) as resumed, \
                mock.patch.object(routing, "say"), \
                mock.patch.object(routing.time, "sleep"), \
                mock.patch.object(routing.Speaker, "pin", return_value=False), \
                mock.patch.object(routing.Speaker, "save"), \
                mock.patch.object(routing, "desktop") as desk:
            desk.return_value.terminals.agent_running.return_value = False
            worker = threading.Thread(target=lambda: routing.place(other))
            worker.start()
            self.assertTrue(entered.wait(2))
            note = routing.place(self.speaker)
            gate.set()
            worker.join(3)
        self.assertEqual(note, "That chat is still starting.")
        self.assertEqual(resumed.call_count, 1)

    def test_a_second_call_during_the_pin_wait_does_not_start_another(self):
        """The pane is recorded and the agent is not up yet: that is the whole wait, and it must hold."""
        hold = threading.Event()
        waiting = threading.Event()

        calls = {"n": 0}
        ticks = {"n": 0}

        def monotonic():
            ticks["n"] += 1
            return 0 if ticks["n"] < 3 else 100

        def pin(self):
            calls["n"] += 1
            if calls["n"] == 1:
                waiting.set()
                hold.wait(2)
            return False

        other = routing.Speaker("claude-" + self.CHAT, "claude", "henchline")
        with mock.patch("voice.resume.find_running", return_value=[]), \
                mock.patch("voice.resume.folder_of", return_value=Path("/tmp/henchline-place")), \
                mock.patch("voice.desktop.tmux.resume", return_value=Tab("tmux", "%9")) as resume, \
                mock.patch("voice.desktop.tmux.pane_there", return_value=True), \
                mock.patch.object(routing, "say"), \
                mock.patch.object(routing.time, "monotonic", monotonic), \
                mock.patch.object(routing.time, "sleep"), \
                mock.patch.object(routing.Speaker, "pin", pin), \
                mock.patch.object(routing.Speaker, "save"):
            worker = threading.Thread(target=lambda: routing.place(other))
            worker.start()
            self.assertTrue(waiting.wait(2))
            note = routing.place(self.speaker)
            hold.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(note, "")
        self.assertEqual(self.speaker.tab, Tab("tmux", "%9"))
        self.assertEqual(resume.call_count, 1)

    def test_a_tmux_query_that_fails_does_not_start_another(self):
        routing._RESUMING.mkdir(parents=True, exist_ok=True)
        (routing._RESUMING / self.CHAT).write_text(
            '{"pid": 1, "token": "held", "phase": "up", "owner": "tmux", "address": "%9"}')
        with mock.patch("voice.resume.find_running", return_value=[]), \
                mock.patch("voice.resume.folder_of", return_value=Path("/tmp/henchline-place")), \
                mock.patch("voice.desktop.tmux.resume") as resume, \
                mock.patch("voice.desktop.tmux.pane_there", return_value=None), \
                mock.patch.object(routing, "say"):
            note = routing.place(self.speaker)
        self.assertEqual(note, "That chat is still starting.")
        resume.assert_not_called()

    def test_an_earlier_clear_does_not_erase_a_later_claim(self):
        routing._RESUMING.mkdir(parents=True, exist_ok=True)
        path = routing._RESUMING / self.CHAT
        path.write_text('{"pid": 1, "token": "first", "phase": "wait", "owner": "", "address": ""}')
        routing._clear_reservation(self.CHAT, "first")
        path.write_text('{"pid": 1, "token": "second", "phase": "wait", "owner": "tmux", "address": "%2"}')
        routing._clear_reservation(self.CHAT, "first")
        self.assertIn("second", path.read_text())


if __name__ == "__main__":
    unittest.main()
