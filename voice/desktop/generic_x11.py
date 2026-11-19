"""The Desktop for any X11 session without KDE: windows and keys through X11
(voice/desktop/x11.py), the clipboard through xclip, and terminals of any kind.

A terminal tab is its window (WINDOWID, which xterm, kitty, alacritty and
others set for the programs inside) and its tty. Typing into it raises the
window, checks it is in front, pastes from the clipboard and presses Enter.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from voice.desktop import x11
from voice.agents import SHELLS, ancestors, comm, process, terminal_foreground
from voice.desktop import Desktop, Tab, Terminals, Window


def tty_of(pid: int) -> str:
    """The terminal device a process runs on, e.g. /dev/pts/3; "" if none."""
    try:
        return os.readlink(f"/proc/{pid}/fd/0")
    except OSError:
        return ""


def foreground_on(tty: str) -> int:
    """The foreground process group of that terminal, from any process still on it; 0 if none."""
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit() and tty_of(int(proc.name)) == tty:
            try:
                return terminal_foreground(int(proc.name))
            except (OSError, ValueError, IndexError):
                continue
    return 0


class AnyTerminal(Terminals):
    def __init__(self, desktop: Desktop) -> None:
        self.desktop = desktop

    def tab_of_caller(self) -> Tab:
        window = os.environ.get("WINDOWID", "")
        chain = ancestors(os.getpid())
        tty = next((t for t in (tty_of(p.pid) for p in chain) if t.startswith("/dev/pts/")), "")
        return Tab(hex(int(window)) if window.isdigit() else window, tty) if window and tty else Tab()

    def agent_running(self, tab: Tab) -> bool:
        foreground = foreground_on(tab.address)
        return bool(foreground) and any(p.agent for p in [process(foreground)] if p)

    def type_line(self, tab: Tab, text: str, again: bool = False, taken=None) -> bool:
        """Into the terminal's window: paste (Ctrl+Shift+V, as most terminals take it), then Enter. A second
        Enter (the Cursor CLI's steer) needs the window focused, so it is not sent from here."""
        del again, taken
        return self.desktop.paste_into(tab.owner, text, ("ctrl", "shift", "v"), ("return",))

    def only_a_shell(self, window: Window) -> bool:
        try:
            children = Path(f"/proc/{window.pid}/task/{window.pid}/children").read_text().split()
            shells = [c for c in children if comm(c) in SHELLS]
            if len(shells) != 1:
                return False
            return comm(terminal_foreground(int(shells[0]))) in SHELLS
        except (OSError, ValueError, IndexError):
            return False


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
