# The desktop layer

Everything the tools need from the desktop (the focused window, raising a window, keys, the clipboard, typing into a terminal tab) goes through one interface, `voice/desktop/__init__.py`. `current()` picks an implementation from the session:

- **KDE Plasma on X11** (`voice/desktop/kde_x11.py`): Klipper for the clipboard, Konsole's D-Bus interface for terminal tabs.
- **Any other X11 desktop** (`voice/desktop/generic_x11.py`): `xclip` for the clipboard; any terminal that sets `WINDOWID` and pastes with Ctrl+Shift+V (most do; plain xterm needs `XTerm*VT100.translations: #override Shift Ctrl <Key>V: insert-selection(CLIPBOARD)`, as the test sets). Hands-free raises the terminal, checks it really is in front, pastes and presses Enter.
- **KDE Plasma on Wayland** (`voice/desktop/wayland.py`): KWin's scripting interface for the focused window, the window list and activation (`voice/desktop/kwin.py`); `wl-clipboard` for the clipboard; `ydotool` (or `wtype`) for keys; Konsole's D-Bus for its tabs.
- **sway and Hyprland** (`voice/desktop/wayland.py`): `swaymsg`'s window tree and `hyprctl`'s JSON for windows, focus and activation; the same clipboard and keys.
- **Any other Wayland compositor**: the clipboard and keys work, and tmux panes, Konsole's tabs and Cursor's debugging port still reach an agent. The focused window cannot be named there, so push-to-talk leaves the words on the clipboard and presses nothing rather than typing into the wrong window.

KWin has no D-Bus method that returns the active window (`queryWindowInfo` asks the user to click one), so `kwin.py` loads a tiny JavaScript into KWin, runs it, and reads its `print()` back from the user journal. `Script.run` replies only after KWin has evaluated the script, and the script is unloaded again in every case. sway and Hyprland need no such bridge: their own tools answer directly.

`tests/generic_x11/run.sh` proves the plain X11 one without touching your system: a throwaway Fedora container with a virtual screen, the Openbox window manager and two xterms, one running a pretend agent; it checks window lookup, focus, the clipboard, finding an agent's tab and typing a line into it (10 checks). It found two bugs that this laptop's KDE setup hid. The Wayland implementations are unit-tested in `tests/voice/test_wayland.py` with the compositor commands mocked; there is no headless-compositor integration test yet.
