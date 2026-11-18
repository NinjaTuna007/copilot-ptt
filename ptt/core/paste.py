"""Where the words go: what kind of window has focus, and typing into it.

Terminals and editors may have an agent reading, so they get tone marks;
Okular and a Thunderbird mail window are left alone; a terminal tab that
only runs a shell gets the words without Enter, so they are not run.
"""

from __future__ import annotations

import time

from voice.desktop import Window
from voice.desktop import current as desktop
from voice.state import ptt_log as log
from voice.state import set_target

TERMINALS = ("konsole", "yakuake", "kitty", "alacritty", "xterm", "gnome-terminal", "wezterm", "terminator",
             "tilix", "ghostty")
EDITORS = ("cursor", "code")
SETTLE = 0.12  # seconds between the paste and Enter, for the window to take the text


def can_type(window: Window) -> bool:
    if window.is_app("okular"):
        return False
    if "thunderbird" in window.app or window.is_app("mail"):
        return "msgcompose" in window.role or "msgcompose" in window.app
    return True


def agent_window(window: Window) -> bool:
    """A terminal or editor, where an agent may be reading what is pasted."""
    return window.is_app(*TERMINALS, *EDITORS) and can_type(window)


def deliver(text: str, rich: str) -> bool:
    """Clipboard, then paste and Enter where that is safe; rich (tone marks) for agent windows.

    True once the words are on the clipboard: even where nothing is typed, they can be pasted by hand.
    """
    if not desktop().set_clipboard(rich if agent_window(desktop().focused()) else text):
        log("clipboard set failed")
        return False
    paste()
    return True


def paste() -> None:
    screen = desktop()
    window = screen.focused()
    set_target(agent_window(window))
    if not window.id:
        log("no focused window known: clipboard only, no keys")
        return
    if screen.locked():
        log("screen locked: clipboard only, no keys")
        return
    if not can_type(window):
        log(f"clipboard only, focus {window.app.strip() or 'unknown'} role {window.role.strip() or 'none'}")
        return
    terminal = window.is_app(*TERMINALS)
    log(f"focus {window.app.strip() or 'unknown'} chord={'ctrl+shift+v' if terminal else 'ctrl+v'}")
    try:
        screen.press(*(("ctrl", "shift", "v") if terminal else ("ctrl", "v")))
        time.sleep(SETTLE)
        if terminal and screen.terminals.only_a_shell(window):
            log("only a shell in that terminal: pasted without Enter")
        else:
            screen.press("return")
    except OSError as exc:
        log(str(exc))
