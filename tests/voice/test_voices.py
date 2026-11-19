"""voice/voices.py: the saved choice, and which voice an agent speaks in."""

import unittest

from tests import sandbox  # noqa: F401
from voice import voices
from voice.agents import AGENT_VOICES


class Choice(unittest.TestCase):
    def setUp(self):
        from voice.state import VOICE_CHOICE
        VOICE_CHOICE.unlink(missing_ok=True)

    def test_each_agent_keeps_its_own_until_told_otherwise(self):
        self.assertEqual(voices.for_agent("cursor"), AGENT_VOICES["cursor"])
        voices.apply({"mode": "own", "agent": "cursor", "voice": "af_heart"})
        self.assertEqual(voices.for_agent("cursor"), "af_heart")
        self.assertEqual(voices.for_agent("claude"), AGENT_VOICES["claude"])

    def test_one_voice_is_used_for_every_agent(self):
        voices.apply({"mode": "one", "voice": "bf_alice"})
        self.assertEqual(voices.for_agent("claude"), "bf_alice")
        self.assertEqual(voices.for_agent("codex"), "bf_alice")

    def test_surprise_picks_once_and_keeps_it(self):
        voices.apply({"mode": "surprise"})
        first = voices.for_agent("claude")
        self.assertIn(first, {voice for voice, _ in voices.CHOICES})
        self.assertEqual(voices.for_agent("claude"), first)
        voices.apply({"mode": "surprise", "again": True})
        # a new pick is allowed to match; it is stored either way
        self.assertIn(voices.for_agent("claude"), {voice for voice, _ in voices.CHOICES})

    def test_an_unknown_voice_is_ignored(self):
        voices.apply({"mode": "one", "voice": "not-a-voice"})
        self.assertEqual(voices.load()["voice"], "af_heart")


class ClaudeModeWord(unittest.TestCase):
    def test_the_latest_phrase_in_the_footer(self):
        from voice import commands
        screen = "older plan mode\n" + "\n".join([""] * 40) + "accept edits on"
        self.assertEqual(commands.claude_mode_word(screen), "accept")
        self.assertEqual(commands.claude_mode_word("bypass permissions"), "bypass")
        self.assertTrue(commands.skips_permissions(b"claude\x00--dangerously-skip-permissions"))
        self.assertFalse(commands.skips_permissions(b"claude\x00--permission-mode\x00auto"))


if __name__ == "__main__":
    unittest.main()
