"""The Desktop for any X11 session without KDE: windows and keys through X11
(voice/desktop/x11.py), the clipboard through xclip, and terminals of any kind.

A terminal tab is its window (WINDOWID, which xterm, kitty, alacritty and
others set for the programs inside) and its tty (voice/desktop/generic.py).
Typing into it raises the window, checks it is in front, pastes from the
clipboard and presses Enter.
"""

from __future__ import annotations

import os
import subprocess

from voice.desktop import x11
from voice.desktop import Desktop, Window
from voice.desktop.generic import AnyTerminal, foreground_on, tty_of  # noqa: F401  (re-exported)


class GenericX11(Desktop):
    """Plain X11; KDE (voice/desktop/kde_x11.py) is this with Klipper and Konsole."""

    def __init__(self) -> None:
        os.environ.setdefault("DISPLAY", ":0")
        self.terminals = AnyTerminal(self)

    def focused(self) -> Window:
        wid = x11.active_window()
        return Window(wid, x11.window_class(wid), x11.window_role(wid), x11.window_pid(wid) if wid else 0)

    def windows(self, app: str) -> list[Window]:
        return [Window(wid, cls, title=x11.window_title(wid)) for wid in x11.windows()
                if f'"{app}"' in (cls := x11.window_class(wid))]

    def set_clipboard(self, text: str) -> bool:
        """Through xclip, and checked."""
        try:
            subprocess.run(["xclip", "-selection", "clipboard", "-in"], input=text, text=True, check=True, timeout=5)
            got = subprocess.run(["xclip", "-selection", "clipboard", "-out"], capture_output=True, text=True,
                                 timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return got.stdout == text

    def press(self, *keys: str) -> None:
        with x11.Display() as display:
            display.press(*(x11.KEY_NAMES[key.lower()] for key in keys))

    def raise_window(self, window_id: str) -> None:
        with x11.Display() as display:
            display.raise_window(window_id)

    def is_front(self, window_id: str) -> bool:
        focused = x11.active_window()
        return bool(focused) and int(focused, 16) == int(window_id, 16)

    def prepare_threads(self) -> None:
        x11.init_threads()
