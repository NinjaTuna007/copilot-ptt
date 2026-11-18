"""KWin's scripting interface: the focused window, the window list and activating a window on KDE Wayland.

KWin has no D-Bus method that hands a program the active window (`queryWindowInfo` asks the user to click
one), so a small JavaScript is loaded into KWin, run, and its `print()` read back from the user journal.
`Script.run` replies only after KWin has evaluated the script, so the line is written by the time it returns;
a short retry covers journald's own delay. Scripts are unloaded again in every case.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SERVICE = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
MARKER = "henchline-kwin"
WAIT = 0.5  # seconds to look for the script's line in the journal (run() replies only after it is printed)


def qdbus_tool() -> str:
    """Plasma 6 ships qdbus6; Plasma 5 (and some distributions) call it qdbus."""
    return next((tool for name in ("qdbus6", "qdbus") if (tool := shutil.which(name))), "")


def call(*args: str, timeout: float = 5.0) -> str:
    """A D-Bus call to KWin, or "" when it can't be made (no qdbus, or the bus wedged)."""
    tool = qdbus_tool()
    if not tool:
        return ""
    try:
        done = subprocess.run([tool, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _journal_since(since: str, token: str) -> object | None:
    """The value after `token` in the newest journal line carrying it, or None."""
    journal = shutil.which("journalctl")
    if not journal:
        return None
    deadline = time.monotonic() + WAIT
    while True:
        try:
            done = subprocess.run([journal, "--user", "--since", since, "-o", "cat", "--no-pager"],
                                  capture_output=True, text=True, timeout=5)
            found = done.stdout
        except (OSError, subprocess.TimeoutExpired):
            found = ""
        for line in found.splitlines():
            at = line.find(token)
            if at < 0:
                continue
            try:
                return json.loads(line[at + len(token):].strip())
            except ValueError:
                continue
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def run(expression: str) -> object | None:
    """Evaluate JavaScript in KWin and return what it returns (JSON-safe), or None when KWin can't be asked.

    `expression` is the body of a function: it ends with `return`. Nothing but JSON-safe values crosses back.
    """
    if not qdbus_tool() or not shutil.which("journalctl"):
        return None
    token = f"{MARKER}-{os.getpid()}-{random.randrange(1 << 32):08x}"
    script = (
        "try {\n"
        "  var henchline_value = (function () {\n"
        f"    {expression}\n"
        "  })();\n"
        f"  print({json.dumps(token)} + \" \" + JSON.stringify(henchline_value));\n"
        "} catch (error) {\n"
        f"  print({json.dumps(token)} + \" \" + JSON.stringify({{error: String(error)}}));\n"
        "}\n"
    )
    name = token
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    handle = tempfile.NamedTemporaryFile("w", suffix=".js", prefix="henchline-", delete=False)
    try:
        handle.write(script)
        handle.close()
        loaded = call(SERVICE, SCRIPTING_PATH, "org.kde.kwin.Scripting.loadScript", handle.name, name)
        if not loaded.isdigit():
            return None  # a negative id: a script with that name is already loaded
        call(SERVICE, f"/Scripting/Script{loaded}", "org.kde.kwin.Script.run")
        value = _journal_since(since, token)
        return None if isinstance(value, dict) and "error" in value else value
    finally:
        call(SERVICE, SCRIPTING_PATH, "org.kde.kwin.Scripting.unloadScript", name)
        Path(handle.name).unlink(missing_ok=True)
