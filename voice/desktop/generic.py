"""Terminal tabs that are not a particular desktop's: found from our own process tree, typed into through the
desktop's paste, and asked whether the tab runs nothing but a shell. Shared by the X11 and Wayland desktops.

A terminal tab is its window and its tty. Most terminals set WINDOWID for the programs inside, which names the
window on X11; a Wayland terminal has no such window id, so a desktop that has none overrides tab_of_caller().
"""

from __future__ import annotations

import os
from pathlib import Path

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

    def type_line(self, tab: Tab, text: str) -> bool:
        """Into the terminal's window: paste (Ctrl+Shift+V, as most terminals take it), then Enter."""
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
