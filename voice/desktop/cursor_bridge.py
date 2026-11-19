"""Henchline's bridge inside Cursor (cursor-bridge/): each Cursor window listens on a private socket and, asked,
brings one agent chat to the front by its id, so words typed into the window land in that chat.

A Cursor editor chat's commands run with CURSOR_CONVERSATION_ID (its chat id) and CURSOR_WORKSPACE_LABEL (its
window's workspace); voice/agents.py keys the chat by the first and names it by the second.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.parse
from pathlib import Path

from voice.desktop import cursor_cdp
from voice.state import STATE

DIR = STATE / "cursor-bridge"
CHAT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def chat_of(session: str) -> str:
    """The Cursor chat id in a session key ("cursor-1e6e96a3-…"), or "" for a key that isn't one chat's."""
    found = CHAT_ID.fullmatch(session.removeprefix("cursor-"))
    return found.group(0) if found else ""


def project_of(folder: str) -> str:
    """Cursor's name for a workspace folder's project under ~/.cursor/projects: its path, dashed."""
    return re.sub(r"[^A-Za-z0-9]+", "-", folder).strip("-")


def owner_of(chat: str) -> str:
    """The project (~/.cursor/projects/<project>) whose transcripts hold that chat; "" when none does."""
    found = list((Path.home() / ".cursor/projects").glob(f"*/agent-transcripts/{chat}"))
    return found[0].parent.parent.name if len(found) == 1 else ""


last_why = ""  # what the bridge said when it said no ("not a chat id" from an older bridge, before a reload)


def pid_alive(pid: object) -> bool:
    """The bridge note's process is still that window. A killed Cursor leaves the note behind."""
    return isinstance(pid, int) and pid > 0 and Path(f"/proc/{pid}").exists()


def windows_of(project: str) -> list[tuple[Path, dict]]:
    """The Cursor windows (their bridge note, and what it says) whose workspace folder is that project.
    A note whose process is gone is removed: after a reboot it otherwise looks like a second window, and a chat
    then opens in neither."""
    found = []
    for note in DIR.glob("*.json"):
        try:
            window = json.loads(note.read_text())
        except (OSError, ValueError):
            continue
        if not pid_alive(window.get("pid")):
            note.unlink(missing_ok=True)
            note.with_suffix(".sock").unlink(missing_ok=True)
            continue
        if project in {project_of(f) for f in window.get("folders", []) if isinstance(f, str)}:
            found.append((note, window))
    return found


def ask_window(chat: str, request: dict) -> bool:
    """Sends one request to the one Cursor window that has the chat (see open_chat); True when it said yes."""
    global last_why
    last_why = ""
    project = owner_of(chat)
    if not project:
        return False
    owners = [note for note, _ in windows_of(project)]
    if len(owners) != 1:  # none, or two windows on that project: which has the chat can't be told, so neither
        last_why = "no Cursor window has that project" if not owners else "more than one Cursor window has that project"
        return False
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(5)
            conn.connect(str(owners[0].with_suffix(".sock")))
            conn.sendall(json.dumps(request).encode() + b"\n")
            answer = b""
            while b"\n" not in answer and len(answer) < 4096:  # the whole answer line, however it arrives
                part = conn.recv(4096)
                if not part:
                    break
                answer += part
        said = json.loads(answer.decode() or "{}")
        last_why = str(said.get("why", ""))
        return bool(said.get("ok"))
    except (OSError, ValueError):
        return False


def ask_selected(chat: str, request: dict) -> bool:
    """A request for Cursor's commands that act on its selected chat (stop, model): the chat opened, then its box
    focused through the debugging port and seen to hold focus, so it is the selected one wherever its pane is
    ({"focused"}); without the port, the bridge's old rule (only the last chat listed) refuses rather than act on
    another."""
    global last_why
    where = workspace_of(chat)
    if where and cursor_cdp.available(where):
        with cursor_cdp.FOCUS:  # nothing else moves focus in Cursor between proving it and acting on it
            if ask_window(chat, {"open": chat}) and cursor_cdp.focus_chat(where, chat):
                # A stop again checked at the last moment: the chat may have finished meanwhile, and Cursor's stop
                # on an idle chat rejects all its pending edits
                if "stop" in request and cursor_cdp.chat_status(where, chat) != "generating":
                    last_why = "not working"
                    return False
                return ask_window(chat, {**request, "focused": True})
    return ask_window(chat, request)


def workspace_of(chat: str) -> str:
    """The workspace label of the one Cursor window that has that chat, "" when none or two do."""
    project = owner_of(chat)
    windows = windows_of(project) if project else []
    return str(windows[0][1].get("workspace") or "") if len(windows) == 1 else ""


def working(chat: str, careful: bool = True) -> bool:
    """Is that chat generating now? By its part of Cursor's page when the port is open (live), else by Cursor's
    store (written a little late). Careful (before a stop, whose stop on an idle chat would reject all its pending
    edits): the store must say so twice, a second apart; the phone's "working…" line asks without care."""
    where = workspace_of(chat)
    status = cursor_cdp.chat_status(where, chat) if where else ""
    if status:
        return status == "generating"
    # Cursor's store, written late: a chat that just finished can still read "generating" there
    if str(composer(chat).get("status") or "") != "generating":
        return False
    if not careful:
        return True
    time.sleep(1)
    return str(composer(chat).get("status") or "") == "generating"


EFFORTS = ("xhigh", "high", "medium", "low")


_catalog: tuple[float, list[dict]] = (0.0, [])


def catalog() -> list[dict]:
    """Cursor's own model list, as its model menu shows it: [{id, name, on, params: [{id, name, kind ("enum" |
    "bool"), values: [{value, name}]}]}], from its settings store (availableDefaultModels2), kept a minute."""
    global _catalog
    at, models = _catalog
    if time.monotonic() - at < 60 and models:
        return models
    models = []
    try:
        with contextlib.closing(sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("select value from ItemTable where key like ?",
                             ("%reactiveStorageServiceImpl.persistentStorage.applicationUser",)).fetchone()
        listed = json.loads(row[0]).get("availableDefaultModels2") or [] if row else []
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        listed = []
    for m in listed:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        params = []
        for p in m.get("parameterDefinitions") or []:
            kind = p.get("parameterType") or {}
            values = (kind.get("enumParameter") or kind.get("booleanParameter") or {}).get("values") or []
            params.append({"id": p.get("id", ""), "name": p.get("name", p.get("id", "")),
                           "kind": "bool" if "booleanParameter" in kind else "enum",
                           "values": [{"value": str(v.get("value")), "name": re.sub(r"[\u200b]", "", str(
                               v.get("displayName") or v.get("value")))} for v in values]})
        models.append({"id": m["name"], "name": m.get("clientDisplayName") or m["name"], "on": bool(m.get("defaultOn")),
                       "params": params})
    _catalog = (time.monotonic(), models)
    return models


def set_model(chat: str, model: str, params: list[dict]) -> str:
    """Sets a Cursor editor chat's model and its settings (each checked against Cursor's catalog for that model)
    through Cursor's own "Switch to Model Slug", then reads them back from Cursor's store: "" when it is on them,
    else what it is on. As when changed in Cursor itself, it also becomes the default for new chats."""
    entry = next((m for m in catalog() if m["id"] == model), None)
    if entry is None:
        return f"Cursor doesn't list a model {model}."
    allowed = {p["id"]: {v["value"] for v in p["values"]} for p in entry["params"]}
    picked = [{"id": str(p.get("id")), "value": str(p.get("value"))} for p in params if isinstance(p, dict)]
    wrong = [p["id"] for p in picked if p["value"] not in allowed.get(p["id"], set())]
    if wrong:
        return f"{entry['name']} has no such {', '.join(wrong)}."
    before = chat_model(chat)
    if not before:
        return "Cursor's record of that chat can't be read, so a switch couldn't be checked: not switched."
    if not ask_selected(chat, {"model": chat, "config": {"modelId": model, "params": picked}}):
        if last_why == "not a chat id":
            return "Cursor runs an older Henchline bridge: Reload Window in Cursor, then try again."
        return f"Cursor didn't take it ({last_why or 'no answer'}): still on {before}."
    want = {p["id"]: p["value"] for p in picked}
    got: dict = {}
    for _ in range(40):  # Cursor writes its store a while later: the model, and its settings a few seconds after
        time.sleep(0.25)
        got = {p["id"]: str(p.get("value")) for p in params_of(chat)}
        if chat_model(chat) == model and all(got.get(k) == v for k, v in want.items()):
            return ""
    if chat_model(chat) != model:
        return f"Cursor still shows {chat_model(chat) or before} for that chat."
    off = [k for k, v in want.items() if got.get(k) != v]
    return f"Switched to {entry['name']}, but Cursor shows {', '.join(off)} differently so far."


def thoughts(chat: str, since: str = "", most: int = 20) -> list[dict]:
    """A Cursor editor chat's thinking, as Cursor keeps it with each step (what its IDE lets you expand), newest
    last: [{at (ISO time), text}], only those after `since`. Read only from Cursor's store."""
    if not CHAT_ID.fullmatch(chat):
        return []
    found = []
    try:
        with contextlib.closing(sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute("select value from cursorDiskKV where key like ? order by rowid desc limit 300",
                              (f"bubbleId:{chat}:%",)).fetchall()
    except sqlite3.Error:
        return []
    for (raw,) in rows:
        try:
            bubble = json.loads(raw)
        except ValueError:
            continue
        thought = bubble.get("thinking") if isinstance(bubble, dict) else None
        text = str(thought.get("text") or "").strip() if isinstance(thought, dict) else ""
        at = str(bubble.get("createdAt", "")) if isinstance(bubble, dict) else ""
        if text and at and at > since:
            found.append({"at": at, "text": text[:2000]})
    return sorted(found, key=lambda t: t["at"])[-most:]


def params_of(chat: str) -> list[dict]:
    """The settings Cursor keeps with a chat's model: [{id, value}] (effort, fast, context)."""
    config = composer(chat).get("modelConfig") or {}
    picked = (config.get("selectedModels") or [{}])[0] if isinstance(config, dict) else {}
    return [p for p in (picked.get("parameters") or []) if isinstance(p, dict) and "id" in p]


def open_chat(workspace: str, chat: str, strict: bool = False) -> bool:
    """Brings that chat to the front in the one Cursor window that has it: the window whose workspace folder is
    the project holding the chat's transcript (Cursor's own commands say yes to any id, 2026-11-16). False when no
    window has it: then nothing may be typed, or the words would land in whatever chat is on screen. (workspace:
    the chat's label, kept for the callers; the transcript decides.) strict: about to type with keys, so that chat
    must be the focused one (the last of those open side by side), not only open."""
    if not CHAT_ID.fullmatch(chat) or not ask_window(chat, {"open": chat, "strict": strict}):
        return False
    time.sleep(0.4)  # Cursor moving its focus to that chat, before anything is typed
    return True


def rename(chat: str, name: str) -> str:
    """A Cursor editor chat renamed as from Cursor's own menu (the phone's /rename, the user's wish, 2026-11-17):
    the bridge in its window opens Cursor's "Chat name" box for it, and the box is filled in through the debugging
    port. "" when done, else why not."""
    name = " ".join(name.split())[:120]
    if not CHAT_ID.fullmatch(chat) or not name:
        return "no name given"
    found = next((c for c in all_chats() if c["chat"] == chat), None)
    if not found:
        return "Cursor doesn't have that chat"
    why = cursor_cdp.rename(Path(found["folder"]).name, name, lambda: ask_window(chat, {"rename": chat, "name": name}))
    if not why:
        _titles.pop(chat, None)  # shown under its new name at once
    return why


STATE_DB = Path.home() / ".config/Cursor/User/globalStorage/state.vscdb"
_titles: dict[str, tuple[float, str]] = {}  # chat id: (when read, title); the list is rebuilt every few seconds


def composer(chat: str) -> dict:
    """Cursor's own record of a chat (title, model), from its store, read only; {} when it can't be read."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("select value from cursorDiskKV where key = ?", (f"composerData:{chat}",)).fetchone()
        found = json.loads(row[0]) if row else {}
        return found if isinstance(found, dict) else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}


def cursor_title(chat: str) -> str:
    """The chat's title as Cursor shows it ("Ozer's PR review rules"), kept a minute."""
    at, title = _titles.get(chat, (0.0, ""))
    if time.monotonic() - at > 60:
        title = str(composer(chat).get("name") or "").strip()
        _titles[chat] = (time.monotonic(), title)
    return title


def chat_model(chat: str) -> str:
    """The model a Cursor editor chat uses ("grok-4.7"), from Cursor's own store, read only."""
    if not CHAT_ID.fullmatch(chat):
        return ""
    config = composer(chat).get("modelConfig")
    return str(config.get("modelName") or "") if isinstance(config, dict) else ""


def chat_effort(chat: str) -> str:
    """The reasoning effort a Cursor editor chat's model runs at ("xhigh"), kept apart from the model in Cursor's
    store; "" when it has none."""
    config = composer(chat).get("modelConfig") if CHAT_ID.fullmatch(chat) else None
    for picked in (config or {}).get("selectedModels") or []:
        for parameter in (picked.get("parameters") or []) if isinstance(picked, dict) else []:
            # "reasoning_effort" for most models, "reasoning" for Codex ones (gpt-5.3-codex, 2026-11-16)
            if isinstance(parameter, dict) and parameter.get("id") in ("reasoning_effort", "reasoning"):
                return str(parameter.get("value") or "")
    return ""


def chat_title(chat: str) -> str:
    """A chat's name for the phone: Cursor's own title for it (the user's catch, 2026-11-16: the phone's didn't
    match the IDE's), else a few words of the first thing asked in it (its transcript)."""
    if not CHAT_ID.fullmatch(chat):
        return ""
    if title := cursor_title(chat):
        return title
    for transcript in (Path.home() / ".cursor/projects").glob(f"*/agent-transcripts/{chat}/{chat}.jsonl"):
        try:
            with transcript.open("rb") as f:
                first = json.loads(f.readline(65536) or b"{}")
            text = " ".join(part.get("text", "") for part in first["message"]["content"] if isinstance(part, dict))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        asked = re.search(r"<user_query>\s*(.*?)\s*(?:</user_query>|$)", text, re.S)
        asked_words = asked.group(1) if asked else text
        # Not Henchline's own tag and one-time introduction, which can be the first thing a chat was sent
        asked_words = re.sub(r"^\s*\[[^\]]*\]\s*(\(Henchline, said once:.*?How it all works: \S+\)\s*)?", "",
                             asked_words, flags=re.S)
        words = asked_words.split()
        return " ".join(words[:6]) + ("…" if len(words) > 6 else "")
    return ""


WORKSPACES = Path.home() / ".config/Cursor/User/workspaceStorage"


def folders_by_project() -> dict[str, str]:
    """Every folder Cursor has opened, by its project name under ~/.cursor/projects."""
    found = {}
    for record in WORKSPACES.glob("*/workspace.json"):
        try:
            data = json.loads(record.read_text())
        except (OSError, ValueError):
            continue
        # Folders only: a .code-workspace window is titled "<name> (Workspace)", which its chats' place can't match
        uri = str(data.get("folder") or "") if isinstance(data, dict) else ""
        if uri.startswith("file://"):
            path = urllib.parse.unquote(uri.removeprefix("file://"))
            if Path(path).exists():
                found[project_of(path)] = path
    return found


def all_chats() -> list[dict]:
    """Every Cursor editor chat that can be opened again, active or not (the user's wish, 2026-11-17: a quiet chat
    in Cursor's list couldn't be reached from the phone), newest first: {chat, name, folder, updated (ms),
    status}. A chat counts when Cursor still has its record and its transcript names a project folder that exists."""
    owners: dict[str, str] = {}
    for transcripts in (Path.home() / ".cursor/projects").glob("*/agent-transcripts"):
        for chat_dir in transcripts.iterdir():
            if CHAT_ID.fullmatch(chat_dir.name):
                owners.setdefault(chat_dir.name, transcripts.parent.name)
    folders = folders_by_project()
    found = []
    try:
        with contextlib.closing(sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=2)) as db:
            rows = db.execute("select key, value from cursorDiskKV where key like 'composerData:%'").fetchall()
    except sqlite3.Error:
        return []
    for key, value in rows:
        chat = str(key).removeprefix("composerData:")
        folder = folders.get(owners.get(chat, ""))
        if not folder:
            continue
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        found.append({"chat": chat, "name": str(data.get("name") or "").strip() or chat_title(chat) or "Untitled chat",
                      "folder": folder, "updated": int(data.get("lastUpdatedAt") or data.get("createdAt") or 0),
                      "status": str(data.get("status") or "")})
    return sorted(found, key=lambda c: c["updated"], reverse=True)


def open_anywhere(chat: str, folder: str, wait: float = 30.0) -> bool:
    """That chat in front in its Cursor window, the window opened first when it isn't (Cursor's own launcher, the
    folder as argument): True once the bridge in that window has it open."""
    if not CHAT_ID.fullmatch(chat):
        return False
    if not windows_of(project_of(folder)):
        launcher = shutil.which("cursor")
        if not launcher:
            return False
        # The port the bridge reaches chats through (the desktop launcher's own flag), in case Cursor isn't running
        subprocess.Popen([launcher, f"--remote-debugging-port={cursor_cdp.PORT}", folder], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        until = time.monotonic() + wait
        while not windows_of(project_of(folder)) and time.monotonic() < until:
            time.sleep(1)
        time.sleep(2)  # the bridge listens a moment before Cursor's chats are loaded
    return open_chat(Path(folder).name, chat)


def installed() -> bool:
    return any(DIR.glob("*.sock"))

