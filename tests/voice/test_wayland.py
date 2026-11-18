"""voice/desktop/wayland.py and kwin.py: which desktop Wayland picks, the clipboard, keys, and each
compositor's window answers. No compositor, display or sound device is needed: the commands are mocked."""

import json
import os
import subprocess
import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)
from voice.desktop import current, kwin, wayland
from voice.desktop import Window

assert sandbox.HOME


def done(command, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, code, stdout, stderr)


class Selection(unittest.TestCase):
    """current() picks the session's own implementation instead of refusing Wayland."""

    def setUp(self):
        current.cache_clear()

    def tearDown(self):
        current.cache_clear()

    def chosen(self, **environment):
        with mock.patch.dict(os.environ, environment, clear=True):
            current.cache_clear()
            return current()

    def test_kde_wayland(self):
        self.assertIsInstance(self.chosen(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="KDE"),
                              wayland.KdeWayland)

    def test_sway(self):
        self.assertIsInstance(self.chosen(XDG_SESSION_TYPE="wayland", SWAYSOCK="/run/user/1/sway-ipc.sock"),
                              wayland.Sway)

    def test_hyprland(self):
        self.assertIsInstance(self.chosen(XDG_SESSION_TYPE="wayland",
                                          HYPRLAND_INSTANCE_SIGNATURE="instance"), wayland.Hyprland)

    def test_wayland_with_only_its_socket(self):
        self.assertIsInstance(self.chosen(WAYLAND_DISPLAY="wayland-0"), wayland.Wayland)

    def test_another_wayland_compositor_is_still_a_desktop(self):
        found = self.chosen(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="GNOME")
        self.assertIsInstance(found, wayland.Wayland)
        self.assertEqual(found.focused(), Window("", ""))

    def test_x11_is_unchanged(self):
        self.assertEqual(type(self.chosen(XDG_SESSION_TYPE="x11", XDG_CURRENT_DESKTOP="KDE")).__name__, "KdeX11")
        self.assertEqual(type(self.chosen(XDG_SESSION_TYPE="x11", XDG_CURRENT_DESKTOP="XFCE")).__name__, "GenericX11")


class Clipboard(unittest.TestCase):
    def test_wl_clipboard_is_set_and_read_back(self):
        calls = []

        def fake_run(command, **kwargs):
            # subprocess.run refuses capture_output together with stdout/stderr; wl-copy needs them at DEVNULL
            self.assertFalse(kwargs.get("capture_output") and ("stdout" in kwargs or "stderr" in kwargs))
            calls.append(command)
            return done(command, stdout="hello" if command[0] == "wl-paste" else "")

        with mock.patch.object(wayland.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(wayland.subprocess, "run", fake_run):
            self.assertTrue(wayland.Wayland().set_clipboard("hello"))
        self.assertEqual(calls[0][:2], ["wl-copy", "--type"])
        self.assertEqual(calls[-1][0], "wl-paste")

    def test_a_wrong_clipboard_is_a_failure(self):
        with mock.patch.object(wayland.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(wayland, "run", return_value=done(["wl-paste"], stdout="other")), \
             mock.patch.object(wayland.time, "sleep"):
            self.assertFalse(wayland.Wayland().set_clipboard("hello"))

    def test_without_wl_clipboard_there_is_no_clipboard(self):
        with mock.patch.object(wayland.shutil, "which", return_value=None):
            self.assertFalse(wayland.Wayland().set_clipboard("hello"))


class Keys(unittest.TestCase):
    def press(self, keys, which, runs):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return runs(command)

        with mock.patch.object(wayland.shutil, "which", which), \
             mock.patch.object(wayland.subprocess, "run", fake_run):
            wayland.Wayland().press(*keys)
        return calls

    def test_ydotool_gets_the_chord_as_keycodes(self):
        calls = self.press(("ctrl", "shift", "v"), lambda name: f"/usr/bin/{name}" if name == "ydotool" else None,
                           lambda command: done(command))
        self.assertEqual(calls, [["ydotool", "key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]])

    def test_an_older_ydotool_gets_the_chord_by_name(self):
        calls = self.press(("ctrl", "v"), lambda name: f"/usr/bin/{name}" if name == "ydotool" else None,
                           lambda command: done(command, code=0 if "+" in command[-1] else 1))
        self.assertEqual(calls[-1], ["ydotool", "key", "ctrl+v"])

    def test_wtype_when_ydotool_is_missing(self):
        calls = self.press(("ctrl", "shift", "v"), lambda name: f"/usr/bin/{name}" if name == "wtype" else None,
                           lambda command: done(command))
        self.assertEqual(calls, [["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]])

    def test_no_tool_is_an_error(self):
        with mock.patch.object(wayland.shutil, "which", return_value=None):
            with self.assertRaises(OSError):
                wayland.Wayland().press("return")

    def test_an_unknown_key_is_an_error(self):
        with self.assertRaises(OSError):
            wayland.Wayland().press("hyper")


SWAY_TREE = {"nodes": [{"type": "output", "nodes": [{"type": "workspace", "nodes": [
    {"type": "con", "id": 1, "name": "shell", "app_id": "Alacritty", "pid": 10, "focused": False,
     "nodes": [], "floating_nodes": [], "window_properties": {}},
    {"type": "con", "id": 2, "name": "agent", "app_id": "kitty", "pid": 11, "focused": True,
     "nodes": [], "floating_nodes": [], "window_properties": {"window_role": "main"}},
    {"type": "con", "id": 3, "name": "project - Cursor", "focused": False, "nodes": [], "floating_nodes": [],
     "window_properties": {"class": "Cursor"}},
]}]}]}


class Sway(unittest.TestCase):
    def sway(self, command):
        if command[:3] == ["swaymsg", "-t", "get_tree"]:
            return done(command, stdout=json.dumps(SWAY_TREE))
        return done(command)

    def test_finds_the_focused_window(self):
        with mock.patch.object(wayland, "run", side_effect=self.sway):
            found = wayland.Sway().focused()
        self.assertEqual(found.id, "2")
        self.assertTrue(found.is_app("kitty"))
        self.assertEqual(found.pid, 11)

    def test_lists_a_cursor_window_by_its_xwayland_class(self):
        with mock.patch.object(wayland, "run", side_effect=self.sway):
            found = wayland.Sway().windows("cursor")
        self.assertEqual([window.id for window in found], ["3"])
        self.assertEqual(found[0].title, "project - Cursor")

    def test_raises_and_checks_the_front(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return self.sway(command)

        with mock.patch.object(wayland, "run", fake_run):
            sway = wayland.Sway()
            sway.raise_window("2")
            self.assertTrue(sway.is_front("2"))
            self.assertFalse(sway.is_front("1"))
        self.assertIn(["swaymsg", "[con_id=2]", "focus"], calls)

    def test_only_a_container_id_becomes_a_command(self):
        calls = []
        with mock.patch.object(wayland, "run", side_effect=lambda command, **kw: calls.append(command)):
            wayland.Sway().raise_window("2; exec rm -rf /")
        self.assertEqual(calls, [])


HYPR_ACTIVE = {"address": "0xabc", "class": "kitty", "title": "agent", "pid": 11}
HYPR_CLIENTS = [HYPR_ACTIVE, {"address": "0xdef", "class": "Cursor", "title": "project - Cursor", "pid": 12}]


class Hyprland(unittest.TestCase):
    def hypr(self, command):
        if command[-1] == "activewindow":
            return done(command, stdout=json.dumps(HYPR_ACTIVE))
        if command[-1] == "clients":
            return done(command, stdout=json.dumps(HYPR_CLIENTS))
        return done(command)

    def test_finds_the_focused_window(self):
        with mock.patch.object(wayland, "run", side_effect=self.hypr):
            found = wayland.Hyprland().focused()
        self.assertEqual(found.id, "0xabc")
        self.assertTrue(found.is_app("kitty"))

    def test_lists_a_cursor_window(self):
        with mock.patch.object(wayland, "run", side_effect=self.hypr):
            self.assertEqual([w.id for w in wayland.Hyprland().windows("cursor")], ["0xdef"])

    def test_raises_and_checks_the_front(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return self.hypr(command)

        with mock.patch.object(wayland, "run", fake_run):
            screen = wayland.Hyprland()
            screen.raise_window("0xabc")
            self.assertTrue(screen.is_front("0xabc"))
            self.assertFalse(screen.is_front("0xdef"))
        self.assertIn(["hyprctl", "dispatch", "focuswindow", "address:0xabc"], calls)

    def test_only_an_address_becomes_a_command(self):
        calls = []
        with mock.patch.object(wayland, "run", side_effect=lambda command, **kw: calls.append(command)):
            wayland.Hyprland().raise_window("0xabc; exec rm -rf /")
        self.assertEqual(calls, [])


KDE_FOCUS = {"id": "{uuid}", "app": "konsole", "name": "konsole", "role": "", "pid": 7, "title": "term"}
KDE_WINDOWS = [KDE_FOCUS, {"id": "{other}", "app": "cursor", "name": "cursor", "role": "", "pid": 8,
                           "title": "project - Cursor"}]


class KdeWayland(unittest.TestCase):
    def kde(self, expression):
        if "workspace.activeWindow" in expression:
            return KDE_FOCUS
        if "windowList().map" in expression:
            return KDE_WINDOWS
        return True

    def test_a_kwin_window_keeps_the_class_spelling_is_app_looks_for(self):
        with mock.patch.object(kwin, "run", side_effect=self.kde):
            found = wayland.KdeWayland().focused()
        self.assertEqual(found.id, "{uuid}")
        self.assertTrue(found.is_app("konsole"))
        self.assertEqual(found.pid, 7)

    def test_lists_cursor_windows(self):
        with mock.patch.object(kwin, "run", side_effect=self.kde):
            self.assertEqual([w.id for w in wayland.KdeWayland().windows("cursor")], ["{other}"])

    def test_raises_and_checks_the_front(self):
        seen = []

        def fake_run(expression):
            seen.append(expression)
            return self.kde(expression)

        with mock.patch.object(kwin, "run", fake_run):
            screen = wayland.KdeWayland()
            screen.raise_window("{uuid}")
            self.assertTrue(screen.is_front("{uuid}"))
            self.assertFalse(screen.is_front("{other}"))
        self.assertTrue(any('workspace.activeWindow = list[i]' in expression for expression in seen))
        self.assertTrue(any('"{uuid}"' in expression for expression in seen))

    def test_no_active_window_is_an_empty_window(self):
        with mock.patch.object(kwin, "run", return_value=None):
            self.assertEqual(wayland.KdeWayland().focused(), Window("", ""))


class KWinScript(unittest.TestCase):
    def test_nothing_is_asked_without_qdbus_and_journalctl(self):
        with mock.patch.object(kwin.shutil, "which", return_value=None):
            self.assertIsNone(kwin.run("return 1;"))

    def test_the_script_is_loaded_run_and_unloaded(self):
        calls = []

        def fake_call(*args, **kwargs):
            calls.append(args)
            return "3" if args[2] == "org.kde.kwin.Scripting.loadScript" else "true"

        with mock.patch.object(kwin.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(kwin, "call", fake_call), \
             mock.patch.object(kwin, "_journal_since", return_value={"id": "x"}):
            self.assertEqual(kwin.run("return {id: 'x'};"), {"id": "x"})
        self.assertTrue(any(call[2].endswith("loadScript") for call in calls))
        self.assertTrue(any(call[2].endswith("Script.run") for call in calls))
        self.assertTrue(any(call[2].endswith("unloadScript") for call in calls))

    def test_a_script_error_is_no_answer(self):
        with mock.patch.object(kwin.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(kwin, "call", return_value="3"), \
             mock.patch.object(kwin, "_journal_since", return_value={"error": "ReferenceError"}):
            self.assertIsNone(kwin.run("return nope;"))

    def test_a_refused_load_returns_none(self):
        with mock.patch.object(kwin.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(kwin, "call", return_value="-1"), \
             mock.patch.object(kwin, "_journal_since") as journal:
            self.assertIsNone(kwin.run("return 1;"))
        journal.assert_not_called()


class Paste(unittest.TestCase):
    def test_an_unknown_window_never_gets_keys(self):
        from ptt.core import paste

        screen = mock.Mock()
        screen.focused.return_value = Window("", "")
        screen.set_clipboard.return_value = True
        with mock.patch.object(paste, "desktop", return_value=screen), \
             mock.patch.object(paste, "set_target"):
            self.assertTrue(paste.deliver("hello", "hello"))
            paste.paste()
        screen.press.assert_not_called()


if __name__ == "__main__":
    unittest.main()
