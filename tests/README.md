# Tests

    ~/.local/share/henchline/venv/bin/python3 -m unittest discover -s tests -t .

Unit tests per piece, run with the private venv's Python (they need numpy and aiohttp from it). Each starts from
`tests/sandbox.py`, which swaps HOME for a throwaway folder first, so nothing touches your real
`~/.local/state`, settings or Downloads, and nothing needs a desktop, a microphone or a GPU:

- `voice/`: file names, where shared files land, what the call server serves and how, what is never sent to the
  phone (`test_files.py`); whole-or-nothing and private writes, and the words a call may keep (`test_state.py`);
  which desktop Wayland picks, the clipboard, keys and each compositor's window answers (`test_wayland.py`).
- `handsfree/`: what a transcribed turn asks for: switching agents, hanging up, asking for time (`test_routing.py`).
  `turn_offline.py` is not a unit test: it runs one real turn from a recording through hands-free and checks that
  it is delivered (needs the venv, Whisper and a recording).
- `call/`: who may post to the call server, and that a text or a share always names its agent (`test_server.py`);
  secrets masked and the reply found in the terminal mirror (`test_mirror.py`).
- `generic_x11/`: the desktop layer on a plain X11 desktop, in a throwaway Fedora container (`run.sh`, needs Docker).
