import unittest

from voice.desktop import cursor_bridge


class ProjectNames(unittest.TestCase):
    """Cursor's project folder names, as seen under ~/.cursor/projects (2026-11-16): a change in its scheme must
    show up as a refused delivery, so these pin the assumption."""

    def test_dashed_path(self):
        self.assertEqual(cursor_bridge.project_of("/home/shekharu/colcon_ws/src/smarc2"),
                         "home-shekharu-colcon-ws-src-smarc2")

    def test_dots_and_doubled_separators(self):
        self.assertEqual(cursor_bridge.project_of("//home/u/my.project__x/"), "home-u-my-project-x")

    def test_chat_of(self):
        chat = "1e6e96a3-90f8-476a-9fe9-d2be71fc2053"
        self.assertEqual(cursor_bridge.chat_of("cursor-" + chat), chat)
        self.assertEqual(cursor_bridge.chat_of("cursor-_smarc2"), "")


class DeadWindow(unittest.TestCase):
    def test_a_dead_note_is_not_a_second_window(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        folder = Path(tempfile.mkdtemp())
        (folder / "1.json").write_text('{"pid": 5, "folders": ["/home/shekharu/colcon_ws/src/smarc2"]}')
        (folder / "2.json").write_text('{"pid": 9, "folders": ["/home/shekharu/colcon_ws/src/smarc2"]}')
        (folder / "2.sock").write_bytes(b"")
        with patch.object(cursor_bridge, "DIR", folder), patch.object(cursor_bridge, "pid_alive", lambda pid: pid == 5):
            found = cursor_bridge.windows_of("home-shekharu-colcon-ws-src-smarc2")
        self.assertEqual([note.name for note, _ in found], ["1.json"])
        self.assertFalse((folder / "2.json").exists())
        self.assertFalse((folder / "2.sock").exists())


if __name__ == "__main__":
    unittest.main()
