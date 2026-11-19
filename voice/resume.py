"""A chat is its id, not the terminal it was last seen in.

Claude, the Cursor CLI and Codex can be started again with that id. The folder comes from the agent's own
record of the chat. A running process is recognised by the id in its environment, not by a tab number.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MARK = {"claude": "CLAUDE_CODE_SESSION_ID", "cursor": "CURSOR_CONVERSATION_ID", "codex": "CODEX_THREAD_ID"}


def chat_id(session: str) -> str:
    """The uuid in a session key (`cursor-<uuid>`), or "" when the key is a folder placeholder."""
    for prefix in ("cursor-", "claude-", "codex-"):
        if session.startswith(prefix) and UUID.fullmatch(session.removeprefix(prefix)):
            return session.removeprefix(prefix)
    return ""


def marked(environ: bytes) -> dict[str, str]:
    """The chat id and the terminal address carried in a process's environment."""
    found = {}
    for part in environ.split(b"\0"):
        if b"=" not in part:
            continue
        key, value = part.split(b"=", 1)
        name = key.decode(errors="replace")
        if name in (*MARK.values(), "TMUX_PANE", "KONSOLE_DBUS_SERVICE", "KONSOLE_DBUS_SESSION"):
            found[name] = value.decode(errors="replace")
    return found


def carries(agent: str, chat: str, environ: bytes) -> bool:
    """That process's environment is this chat, not one it inherited for a tool."""
    return marked(environ).get(MARK.get(agent, ""), "") == chat


def folder_of(agent: str, chat: str) -> Path | None:
    """The folder that chat was working in, from the agent's own record. None when it isn't there."""
    if agent == "claude":
        logs = list((Path.home() / ".claude/projects").glob(f"*/{chat}.jsonl"))
        if len(logs) != 1:
            return None
        try:
            tail = logs[0].read_bytes()[-256_000:]
        except OSError:
            return None
        found = re.findall(rb'"cwd":"([^"\\]+)"', tail)
        path = Path(found[-1].decode()) if found else None
        return path if path and path.is_dir() else None
    if agent == "cursor":
        from voice.desktop.cursor_bridge import folders_by_project, owner_of
        path = folders_by_project().get(owner_of(chat), "")
        return Path(path) if path and Path(path).is_dir() else None
    if agent == "codex":
        rolls = [p for p in (Path.home() / ".codex/sessions").rglob(f"*{chat}.jsonl") if p.name.endswith(f"{chat}.jsonl")]
        if len(rolls) != 1:
            return None
        try:
            head = rolls[0].read_text(errors="replace")[:4000]
        except OSError:
            return None
        found = re.search(r'"cwd":"([^"\\]+)"', head)
        path = Path(found.group(1)) if found else None
        return path if path and path.is_dir() else None
    return None


def tab_of(environ: bytes):
    """The terminal that process is in, from its own environment. Empty when it names neither tmux nor Konsole."""
    from voice.desktop import Tab
    env = marked(environ)
    pane = env.get("TMUX_PANE", "")
    if pane.startswith("%"):
        return Tab("tmux", pane)
    service, session = env.get("KONSOLE_DBUS_SERVICE", ""), env.get("KONSOLE_DBUS_SESSION", "")
    if service and session.startswith("/Sessions/"):
        return Tab(service, session)
    return Tab()


def holds_store(pid: int, chat: str) -> bool:
    """The Cursor CLI keeps its chat's store open. The id is in that path when the environment doesn't say it."""
    try:
        links = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False
    for link in links:
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if chat in target and target.endswith("store.db"):
            return True
    return False


def find_running(agent: str, chat: str) -> list:
    """Every agent process that is this chat: (pid, terminal). Empty when none. More than one is the caller's
    to refuse. A terminal-less match is included, so a second copy is not started just because the tab is unknown."""
    from voice.agents import process
    hits = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        if not carries(agent, chat, raw) and not (agent == "cursor" and holds_store(pid, chat)):
            continue
        running = process(pid)
        if not running or running.agent != agent or running.is_editor:
            continue
        hits.append((pid, tab_of(raw)))
    return hits
