"""The Desktop under Wayland: the compositor's own interface for windows and focus, wl-clipboard for the
clipboard, and ydotool (or wtype) for keys.

Wayland gives no program X11's `_NET_ACTIVE_WINDOW`, so the windows are asked of each compositor in its own
way: KWin through its scripting interface (voice/desktop/kwin.py), sway and Hyprland through the IPC tools they
ship. Anywhere else the windows are unknown, and the words still reach tmux panes, Konsole's tabs and Cursor's
debugging port; push-to-talk leaves them on the clipboard rather than pressing keys into a window it cannot name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from voice.desktop import Desktop, Tab, Window
from voice.desktop.generic import AnyTerminal

# Linux input event codes, as ydotool's key command takes them (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, …)
KEYCODES = {"ctrl": 29, "shift": 42, "alt": 56, "return": 28, "enter": 28, "f12": 88, "v": 47, "slash": 53}
YDO_NAMES = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "return": "enter", "enter": "enter", "f12": "f12",
             "v": "v", "slash": "slash"}
WTYPE_NAMES = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "return": "Return", "enter": "Return", "f12": "F12",
               "v": "v", "slash": "slash"}


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess | None:
    """A command whose failure is an answer, never an exception."""
    kwargs.setdefault("capture_output", "stdout" not in kwargs and "stderr" not in kwargs)
    try:
        return subprocess.run(command, text=True, timeout=5, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None


class WaylandTerminal(AnyTerminal):
    """A terminal on a compositor that names no tab: nothing beyond tmux (WithTmux) is known.

    KDE finds Konsole's own tabs through Konsole's D-Bus interface (voice/desktop/kde_x11.py), which is what
    KdeWayland uses. Elsewhere an agent can still be reached in tmux or in Cursor's chat.
    """

    def tab_of_caller(self) -> Tab:
        return Tab()


class Wayland(Desktop):
    """Wayland with no compositor-specific window interface: the clipboard and keys work, the windows don't."""

    def __init__(self) -> None:
        self.terminals = WaylandTerminal(self)

    def focused(self) -> Window:
        return Window("", "")

    def windows(self, app: str) -> list[Window]:
        return []

    def set_clipboard(self, text: str) -> bool:
        """Through wl-clipboard, and checked: wl-copy forks to keep the selection, so wl-paste may need a moment."""
        if not shutil.which("wl-copy") or not shutil.which("wl-paste"):
            return False
        copied = run(["wl-copy", "--type", "text/plain;charset=utf-8"], input=text,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if copied is None or copied.returncode != 0:
            return False
        for _ in range(10):
            got = run(["wl-paste", "--no-newline", "--type", "text/plain"])
            if got is not None and got.returncode == 0 and got.stdout == text:
                return True
            time.sleep(0.02)
        return False

    def press(self, *keys: str) -> None:
        """Hold the keys down in order and let go in reverse, like a person."""
        names = [key.lower() for key in keys]
        unknown = next((name for name in names if name not in KEYCODES), "")
        if unknown:
            raise OSError(f"unknown key: {unknown}")
        if shutil.which("ydotool") and self._ydotool(names):
            return
        if shutil.which("wtype") and self._wtype(names):
            return
        raise OSError("no way to press keys on Wayland: install ydotool (or wtype)")

    def _ydotool(self, names: list[str]) -> bool:
        """The keycodes ydotool 1.x takes; its 0.x took names, tried next."""
        codes = [f"{KEYCODES[name]}:1" for name in names] + [f"{KEYCODES[name]}:0" for name in reversed(names)]
        done = run(["ydotool", "key", *codes])
        if done is not None and done.returncode == 0:
            return True
        done = run(["ydotool", "key", "+".join(YDO_NAMES[name] for name in names)])
        return done is not None and done.returncode == 0

    def _wtype(self, names: list[str]) -> bool:
        held, key = names[:-1], names[-1]
        command = ["wtype"]
        for name in held:
            command += ["-M", WTYPE_NAMES[name]]
        command += ["-k", WTYPE_NAMES[key]]
        for name in reversed(held):
            command += ["-m", WTYPE_NAMES[name]]
        done = run(command)
        return done is not None and done.returncode == 0

    def raise_window(self, window_id: str) -> None:
        """No compositor interface: the window cannot be brought forward."""

    def is_front(self, window_id: str) -> bool:
        return False


class KdeWayland(Wayland):
    """KDE Plasma on Wayland: KWin's scripting interface for windows, Konsole's D-Bus for its tabs."""

    def __init__(self) -> None:
        super().__init__()
        from voice.desktop.kde_x11 import Konsole

        self.terminals = Konsole(self)

    def focused(self) -> Window:
        from voice.desktop import kwin

        data = kwin.run(
            "var w = workspace.activeWindow;\n"
            "if (!w) return null;\n"
            "return {id: String(w.internalId), app: String(w.resourceClass), name: String(w.resourceName), "
            "role: String(w.windowRole), pid: w.pid, title: String(w.caption)};"
        )
        return window_of(data) if isinstance(data, dict) else Window("", "")

    def windows(self, app: str) -> list[Window]:
        from voice.desktop import kwin

        data = kwin.run(
            "return workspace.windowList().map(function (w) {\n"
            "  return {id: String(w.internalId), app: String(w.resourceClass), name: String(w.resourceName), "
            "role: String(w.windowRole), pid: w.pid, title: String(w.caption)};\n"
            "});"
        )
        return [found for item in (data if isinstance(data, list) else [])
                if isinstance(item, dict) and f'"{app}"' in (found := window_of(item)).app]

    def raise_window(self, window_id: str) -> None:
        if not window_id:
            return
        from voice.desktop import kwin

        kwin.run(
            f"var wanted = {json.dumps(window_id)};\n"
            "var list = workspace.windowList();\n"
            "for (var i = 0; i < list.length; i++) {\n"
            "  if (String(list[i].internalId) === wanted) { workspace.activeWindow = list[i]; return true; }\n"
            "}\n"
            "return false;"
        )

    def is_front(self, window_id: str) -> bool:
        return bool(window_id) and self.focused().id == window_id

    def set_clipboard(self, text: str) -> bool:
        """wl-clipboard first; Klipper, KDE's own clipboard, when wl-clipboard is not installed."""
        if super().set_clipboard(text):
            return True
        from voice.desktop.kde_x11 import KLIPPER, qdbus, qdbus_ok

        if not qdbus_ok(*KLIPPER, "setClipboardContents", text):
            return False
        return qdbus(*KLIPPER, "getClipboardContents") == text.strip()


def window_of(data: dict) -> Window:
    """A compositor's window description as our Window; `app` keeps the X11 class spelling is_app() looks for."""
    app = f'"{data.get("app", "")}", "{data.get("name", "")}"'.lower()
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return Window(str(data.get("id", "")), app, str(data.get("role", "")).lower(), pid, str(data.get("title", "")))


class Sway(Wayland):
    """sway (and i3-Wayland), which answers over swaymsg: a JSON tree, and a focus command."""

    def _tree(self) -> dict | None:
        done = run(["swaymsg", "-t", "get_tree", "-r"])
        if done is None or done.returncode != 0:
            return None
        try:
            return json.loads(done.stdout)
        except ValueError:
            return None

    @staticmethod
    def _walk(node: dict):
        yield node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            yield from Sway._walk(child)

    @staticmethod
    def _window(node: dict) -> Window | None:
        properties = node.get("window_properties") or {}
        app = node.get("app_id") or properties.get("class") or ""
        if not app:
            return None
        role = properties.get("window_role", "")
        return window_of({"id": node.get("id", ""), "app": app, "name": app, "role": role,
                          "pid": node.get("pid"), "title": node.get("name", "")})

    def focused(self) -> Window:
        tree = self._tree()
        for node in self._walk(tree) if tree else []:
            if node.get("focused") and (found := self._window(node)):
                return found
        return Window("", "")

    def windows(self, app: str) -> list[Window]:
        tree = self._tree()
        return [found for node in (self._walk(tree) if tree else [])
                if (found := self._window(node)) and f'"{app}"' in found.app]

    def raise_window(self, window_id: str) -> None:
        if window_id.isdigit():  # never a sway command from anything but a container id
            run(["swaymsg", f"[con_id={window_id}]", "focus"])

    def is_front(self, window_id: str) -> bool:
        return bool(window_id) and self.focused().id == window_id


class Hyprland(Wayland):
    """Hyprland, which answers over hyprctl: JSON for the active window and the clients, and an address to focus."""

    def _json(self, *args: str):
        done = run(["hyprctl", "-j", *args])
        if done is None or done.returncode != 0:
            return None
        try:
            return json.loads(done.stdout)
        except ValueError:
            return None

    @staticmethod
    def _window(data: dict) -> Window:
        return window_of({"id": data.get("address", ""), "app": data.get("class", ""), "name": data.get("class", ""),
                          "role": "", "pid": data.get("pid"), "title": data.get("title", "")})

    def focused(self) -> Window:
        data = self._json("activewindow")
        return self._window(data) if isinstance(data, dict) and data.get("address") else Window("", "")

    def windows(self, app: str) -> list[Window]:
        found = self._json("clients")
        return [window for item in (found if isinstance(found, list) else []) if isinstance(item, dict)
                and f'"{app}"' in (window := self._window(item)).app]

    def raise_window(self, window_id: str) -> None:
        if re.fullmatch(r"0x[0-9a-fA-F]+", window_id):  # never a hyprctl argument from anything but an address
            run(["hyprctl", "dispatch", "focuswindow", f"address:{window_id}"])

    def is_front(self, window_id: str) -> bool:
        return bool(window_id) and self.focused().id == window_id


def pick() -> Desktop:
    """The compositor's own implementation, by what the session says it is; generic Wayland otherwise."""
    from voice.desktop import kde_session

    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    if kde_session():
        return KdeWayland()
    if os.environ.get("SWAYSOCK") or {"SWAY", "I3"} & set(desktops):
        return Sway()
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or "HYPRLAND" in desktops:
        return Hyprland()
    return Wayland()
