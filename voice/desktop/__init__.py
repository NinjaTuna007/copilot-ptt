"""What push-to-talk, talkback and hands-free need from the desktop, and nothing else.

Every window, key, clipboard and terminal call goes through a Desktop. Under
X11 there are two: any X11 desktop (voice/desktop/generic_x11.py) and KDE on X11,
which adds Klipper and Konsole (voice/desktop/kde_x11.py). Under Wayland there are
KDE (KWin's scripting interface), sway and Hyprland (their IPC tools), and a generic
one that keeps the clipboard, keys, tmux, Konsole's tabs and Cursor's port working
where the windows cannot be named (voice/desktop/wayland.py). current() picks one.
"""

from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cache


RAISE_TRIES = 3  # attempts to bring a window to the front before giving up
RAISE_SETTLE = 0.4  # seconds for the window manager to bring a window forward
KEY_SETTLE = 0.25  # seconds after each chord, for the window to act on it (open a chat box, take a paste)


@dataclass(frozen=True)
class Window:
    id: str
    app: str  # the window class, lower case, e.g. '"konsole", "konsole"'
    role: str = ""
    pid: int = 0
    title: str = ""

    def is_app(self, *names: str) -> bool:
        return any(f'"{name}"' in self.app for name in names)


@dataclass(frozen=True)
class Tab:
    """Where an agent's terminal tab lives; empty when it has none (e.g. an editor's agent).

    owner: the program that shows it (Konsole: its D-Bus service; plain X11: the window id);
    address: the tab itself (Konsole: its session path; plain X11: its tty).
    """

    owner: str = ""
    address: str = ""

    def __bool__(self) -> bool:
        return bool(self.owner and self.address)


class Terminals(ABC):
    @abstractmethod
    def tab_of_caller(self) -> Tab:
        """The tab the calling agent is shown in, found from our own process tree."""

    @abstractmethod
    def agent_running(self, tab: Tab) -> bool:
        """Is an agent the tab's foreground program (not a bare shell that would run typed words)?"""

    @abstractmethod
    def type_line(self, tab: Tab, text: str) -> bool:
        """Type the text into the tab, then Enter."""

    @abstractmethod
    def only_a_shell(self, window: Window) -> bool:
        """Does that focused terminal window show a tab running nothing but a shell? Unsure: False."""

    def interrupt(self, tab: Tab) -> bool:
        """Stop what the tab's agent is doing, as Escape typed in it does (the phone's stop button). A terminal that
        can't be sent a key without its window in front says no."""
        return False

    def agent_pid(self, tab: Tab) -> int:
        """The pid of the agent that tab runs now (0: none). Terminals that can ask one tab override this."""
        return next((pid for found, pid in self.agent_tabs() if found == tab), 0)

    def agent_tabs(self) -> list[tuple[Tab, int]]:
        """Every tab whose foreground program is an agent, with that program's pid: for finding an agent the user
        asks for that hasn't spoken yet. A terminal that can't be asked lists none."""
        return []


class Desktop(ABC):
    terminals: Terminals

    @abstractmethod
    def focused(self) -> Window:
        """The focused window (id "" when there is none)."""

    @abstractmethod
    def windows(self, app: str) -> list[Window]:
        """Every window of that application, with its title."""

    @abstractmethod
    def set_clipboard(self, text: str) -> bool:
        """Put text on the clipboard and check it arrived."""

    @abstractmethod
    def press(self, *keys: str) -> None:
        """Press keys together, like a person: "ctrl", "shift", "alt", "return", "f12", or a letter."""

    @abstractmethod
    def raise_window(self, window_id: str) -> None:
        """Ask for the window to come to the front and take focus (the window manager may refuse)."""

    @abstractmethod
    def is_front(self, window_id: str) -> bool:
        """Is that window the focused one now?"""

    def prepare_threads(self) -> None:
        """Called once before threads use the desktop (Xlib needs it); nothing by default."""

    def locked(self) -> bool:
        """Is the screen locked? Then no key may be pressed: it would land in the lock screen.

        From systemd-logind, which any desktop on a systemd system reports to.
        """
        session = os.environ.get("XDG_SESSION_ID", "")
        command = ["loginctl", "show-session", session, "-p", "LockedHint", "--value"] if session else \
                  ["loginctl", "show-user", str(os.getuid()), "-p", "Display", "--value"]
        try:
            if not session:  # a service has no session of its own: use the user's graphical one
                display = subprocess.run(command, capture_output=True, text=True, timeout=5).stdout.strip()
                command = ["loginctl", "show-session", display, "-p", "LockedHint", "--value"]
            return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout.strip() == "yes"
        except (OSError, subprocess.SubprocessError):
            return True  # unsure: press nothing

    def paste_into(self, window_id: str, text: str, *chords: tuple[str, ...]) -> bool:
        """Put text on the clipboard, bring the window forward, then press the chords in turn.

        Nothing is pressed unless that window really is in front, checked before
        every chord: a window manager can refuse a background program's raise,
        and keys then land in whatever has focus (a message once went into
        Telegram, 2026-11-15).
        """
        if self.locked():  # keys would go into the lock screen's password box (they did, 2026-11-15)
            return False
        if not self.set_clipboard(text):
            return False
        for _ in range(RAISE_TRIES):
            self.raise_window(window_id)
            time.sleep(RAISE_SETTLE)
            if self.is_front(window_id):
                break
        else:
            return False
        for chord in chords:
            if not self.is_front(window_id):  # the user switched away meanwhile
                return False
            self.press(*chord)
            time.sleep(KEY_SETTLE)
        return True


def kde_session() -> bool:
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    return "KDE" in desktops or bool(os.environ.get("KDE_FULL_SESSION"))


def wayland_session() -> bool:
    """Wayland, named by the login session or by the compositor's own socket (a user service may have only that)."""
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


@cache
def current() -> Desktop:
    """The compositor's own desktop: KDE or any other X11 desktop, or Wayland's KDE, sway or Hyprland."""
    from voice.desktop.tmux import WithTmux

    if wayland_session():
        from voice.desktop.wayland import pick

        found: Desktop = pick()
    elif kde_session():
        from voice.desktop.kde_x11 import KdeX11

        found = KdeX11()
    else:
        from voice.desktop.generic_x11 import GenericX11

        found = GenericX11()
    found.terminals = WithTmux(found.terminals)  # tmux panes on any desktop (voice/desktop/tmux.py)
    return found
