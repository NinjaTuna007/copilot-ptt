"""Who hands-free's words go to, and getting them there.

Replies go to the agent that spoke last (talkback records it). Fixed phrases
switch to another agent; there is no language model in the loop, by choice.
Delivery types into the agent's Konsole tab, or into the Cursor editor's
chat for an agent that lives there.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

from voice import snaps
from voice.agents import SPOKEN_TO, identity, process, started
from voice.desktop import Tab, current as desktop, cursor_bridge, cursor_cdp
from voice.state import (AWAY, BY_HANDSFREE, CALL_CUT_IN, CALL_INTERRUPT, CALL_PINNED, CALL_TOLD, CHAT_KEY, HANDSFREE_LOG, INTRODUCED, LAST, LISTEN, SPEAKERS, STATE, Sent,
                         Speaker, in_call, last_speaker, log, recent_speakers, renamed)

SAY = Path(__file__).resolve().parents[2] / "talkback/scripts/say.py"
AFTER_SWITCH = 30.0  # seconds to start talking after "Now talking to …"


class Heard(str, Enum):
    """How a message was heard, in its tag, so the agent can judge whether it was meant for it."""

    WOKE = "woke"  # "hey Claude" or an earbud tap
    REPLY = "reply"  # in the window right after a clip
    THINK = "think"  # after the user asked for time
    FOLLOW_ON = "follow-on"  # just after their own last message
    OVER_YOU = "over you"  # talked over a clip
    CALL = "call"  # on a phone call, where the user talks whenever they like
    PUSHED = "call, held the talk button"  # on a phone call, while holding the page's talk button
    TYPED = "call, typed"  # typed into the call page's chat, not spoken
    # From the phone off a call: tagged [phone · …], not hands-free (the user's call, 2026-11-16: a text isn't spoken
    # across the room), and answered with `handsfree text`
    SHARED = "shared"  # files or text shared to Henchline from another app
    TEXTED = "text"  # typed in Henchline's chat, or in an agent's text notification
    VOICE_NOTE = "voice message"  # recorded in Henchline off a call, transcribed here: may be misheard
    SCHEDULED = "scheduled"  # a reminder the user (or an agent) set for this time: `handsfree remind`, the phone's /later

    @property
    def tag(self) -> str:
        if self is Heard.SCHEDULED:
            return "[henchline · scheduled]"
        source = "phone" if self in (Heard.SHARED, Heard.TEXTED, Heard.VOICE_NOTE) else "hands-free"
        # Away (the user's design, 2026-11-17): the agent is told, and chooses a text over talking
        # (not on a call: there the user hears and talks)
        return f"[{source} · {self.value}{' · away' if AWAY.exists() and not in_call() else ''}]"


NAMES = "|".join(SPOKEN_TO)
AGENT = rf"({NAMES})"
WHERE = r"(?:\s+(?:in|from|on)\s+(?:the\s+)?(.+?))?"
SWITCH = re.compile(
    rf"^\W*(?:(?:okay|ok|alright|right|so|no|actually|hey)[\s,]+)?(?:(?:hey\s+)?(?:{NAMES})[\s,]+)?"
    rf"(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:please\s+)?(?:just\s+)?"
    rf"(?:switch(?:\s+me)?(?:\s+back)?|(?:hand|pass)(?:\s+(?:me|it|this))?(?:\s+(?:over|back))?|"
    rf"(?:give|send)(?:\s+(?:me|it|this))?\s+(?:over|back)|go\s+back|(?:let\s+me\s+)?talk|connect\s+me|"
    rf"put\s+me\s+through|i\s+want\s+to\s+talk|back)\s+(?:over\s+)?to\s+{AGENT}{WHERE}(?:\s+please)?\W*$", re.I)
SEND_THAT = re.compile(rf"^\W*(?:please\s+)?(?:send|forward|give)\s+(?:that|this|it)(?:\s+message)?"
                       rf"\s+to\s+{AGENT}{WHERE}\W*$", re.I)
# "…actually, scratch that, hand this to Cursor": drop what came before, then switch
DISCARD = re.compile(r".*\b(?:never\s*mind|scratch\s+that|forget\s+(?:it|that|all\s+that|what\s+i\s+(?:just\s+)?said))"
                     r"\W+(.+)$", re.I | re.S)
# A short lead-in sentence before the request: a long message that merely ends that way is a message
LAST_SENTENCE = re.compile(r"^(.*[.!?])\s+([^.!?]+[.!?]?)\s*$", re.S)
LEAD_IN_WORDS = 8
CLAUSE = re.compile(r"^(.*[.,;!?])\s+([^.,;!?]+[.!?]?)\s*$", re.S)  # a lead-in, then the last clause
WHO = re.compile(r"^\W*who\s+(?:am\s+i|are\s+you)\s+(?:talking\s+to|speaking\s+to)?\W*$"
                 r"|^\W*who\s+is\s+(?:this|listening)\W*$", re.I)
THINK = re.compile(r"\b(give me a (minute|moment|second|sec)|let me think|hold on|hang on"
                   r"|one (sec|second|moment)|let me (get|come) back)\b", re.I)
JUST_TIME = 12  # words: "hold on, let me think about that" asks for time and says nothing else
CANCEL = re.compile(r"^\W*(never ?mind|cancel( that)?|scratch that)\W*$", re.I)
OFF = re.compile(r"\b(hands[- ]?free (mode )?off|stop hands[- ]?free|turn off hands[- ]?free)\b", re.I)
# On a phone call only: the whole turn is the request, so "how do I hang up?" is still a message
HANGUP = re.compile(r"^\W*(okay\W+|ok\W+|alright\W+)?(please\W+)?(hang up|end (the|this) call)(\s+(now|please))?\W*$",
                    re.I)
# One word is a real answer ("yes", "go", "stop"); only bare fillers are noise
FILLER = re.compile(r"\W*(um+|uh+|hmm+|mm+|ah+|oh|eh)\W*", re.I)


class Meaning(str, Enum):
    """What a transcribed turn asks for, before any agent sees it."""

    TIME = "time"  # only "give me a minute"
    ROUTING = "routing"  # who am I talking to, switch, send that
    OFF = "off"
    HANGUP = "hang up"  # end the phone call
    CANCEL = "cancel"
    NOTHING = "nothing"  # empty or a bare filler
    MESSAGE = "message"


def meaning(text: str) -> Meaning:
    if THINK.search(text) and len(text.split()) <= JUST_TIME:
        return Meaning.TIME
    if WHO.match(text) or switch_request(text):
        return Meaning.ROUTING
    if OFF.search(text):
        return Meaning.OFF
    if hangup_request(text) and in_call():
        return Meaning.HANGUP
    if CANCEL.match(text):
        return Meaning.CANCEL
    if not text.strip() or FILLER.fullmatch(text):
        return Meaning.NOTHING
    return Meaning.MESSAGE


def say(text: str) -> subprocess.Popen:
    """A hands-free notice, in its own voice; it never takes the user's replies. The process ends once the
    notice is in talkback's queue (the worker renders it), so waiting for it means it will be said."""
    return subprocess.Popen([str(SAY), "--agent", BY_HANDSFREE, "--session", BY_HANDSFREE, text],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def squash(words: str) -> str:
    return re.sub(r"[^a-z0-9]", "", words.lower())


def find(agent: str, where: str | None = None) -> Speaker | None:
    """The most recent agent of that kind to speak, in that workspace if named; one still running first (agents are
    remembered for weeks, and an old one long gone mustn't win over the one open now)."""
    found = [s for s in recent_speakers() if s.agent == agent.lower()
             and (not where or squash(where) in squash(s.where) or squash(s.where) in squash(where))]
    running = [s for s in found if reachable(s)]
    return max(running or found, key=lambda s: s.at) if found else None


def discover(agent: str, where: str | None, tabs: list | None = None, windows: list | None = None) -> list[Speaker]:
    """An agent the user asks for that hasn't spoken through talkback yet, found running (the user's idea,
    2026-11-16): in a terminal tab whose foreground program is that agent, or for Cursor its editor window, by
    its workspace's name. route() registers it when it is the only one (several are not guessed between)."""
    agent = agent.lower()
    found = []
    for tab, pid in (desktop().terminals.agent_tabs() if tabs is None else tabs):
        p = process(pid)
        if not p or p.agent != agent:
            continue
        try:
            folder = p.folder
        except OSError:
            continue
        if where and not (squash(where) in squash(folder.name) or squash(folder.name) in squash(where)):
            continue
        session = ""
        if agent == "claude":
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                key = b"CLAUDE_CODE_SESSION_ID="
                session = next((e[len(key):].decode() for e in env if e.startswith(key)), "")
            except OSError:
                pass
        _, key = identity(agent, session or str(folder))
        found.append(Speaker(key, agent, "home" if folder == Path.home() else folder.name, tab, pid=pid,
                             born=started(pid)))
    if agent == "cursor":  # the editor: its window title ends "<workspace> - Cursor"
        for window in (desktop().windows("cursor") if windows is None else windows):
            name = window.title.removesuffix(" - Cursor").rsplit(" - ", 1)[-1]
            if name and (not where or squash(where) in squash(name) or squash(name) in squash(where)):
                found.append(Speaker(identity("cursor", f"/{name}")[1], "cursor", name))
                if where:
                    break
    return found


def discover_all() -> int:
    """Every agent running that the crew doesn't know yet, found and registered (Henchline's pull-to-refresh):
    how many were new. One already known in that tab (or that Cursor window) isn't added again."""
    known = [s for s in (Speaker.load(p) for p in SPEAKERS.glob("*.json")) if s]  # every record, not just recent
    tabs, windows = desktop().terminals.agent_tabs(), desktop().windows("cursor")  # the desktop looked at once
    new = 0
    for agent in SPOKEN_TO:
        for found in discover(agent, None, tabs, windows):
            def same(k: Speaker) -> bool:
                if k.agent != found.agent:
                    return False
                if found.tab and k.tab:
                    return k.tab == found.tab
                return k.where == found.where  # a record without its tab: the same folder is the same agent
            if any(same(k) for k in known):
                continue
            found.save(SPEAKERS / f"{found.session}.json")
            known.append(found)
            new += 1
    folded = fold_placeholders(known)
    if new or folded:
        log(HANDSFREE_LOG, f"found {new} agent(s) running, not yet heard: registered"
            + (f"; {folded} folder placeholder(s) folded into the real chat" if folded else ""))
    return new


def fold_placeholders(known: list | None = None) -> int:
    """A folder placeholder and the real chat in the same tab are one agent. The placeholder goes, and the phone
    follows the real chat. Discover often cannot read the session id (the process hides its environment), so it
    records the folder, and the real id is saved later without removing that record."""
    if known is None:
        known = [s for s in (Speaker.load(p) for p in SPEAKERS.glob("*.json")) if s]
    folded = 0
    for real in known:
        if not real.tab or not CHAT_KEY.search(real.session):
            continue
        for other in list(known):
            if other.agent != real.agent or other.session == real.session or not other.tab:
                continue
            if other.tab == real.tab and other.where == real.where and not CHAT_KEY.search(other.session):
                (SPEAKERS / f"{other.session}.json").unlink(missing_ok=True)
                renamed(other.session, real.session)
                known.remove(other)
                folded += 1
    return folded


def switch_to(speaker: Speaker) -> None:
    """Replies go to this agent now, with time to start talking."""
    speaker.save(LAST)
    if in_call():  # the user's pick, kept for the call (Speaker.remember)
        CALL_PINNED.write_text(speaker.session)
    LISTEN.write_text(str(time.time() + AFTER_SWITCH))
    log(HANDSFREE_LOG, f"switched to {speaker.agent} in {speaker.where or '?'}")


def hangup_request(text: str) -> bool:
    """The turn asks to end the call: all of it, or its last clause after a short lead-in ("I'm in, hang up").
    The lead-in is everything before that clause, at most LEAD_IN_WORDS words, an agent's name included."""
    if HANGUP.match(text):
        return True
    last = CLAUSE.match(text)
    return bool(last) and len(last.group(1).split()) <= LEAD_IN_WORDS and bool(HANGUP.match(last.group(2)))


def switch_request(text: str) -> tuple[str, str | None, bool] | None:
    """(agent, workspace, forward the last message?) if the words ask for a switch."""
    match = SWITCH.match(text) or SEND_THAT.match(text)
    if not match and (tail := DISCARD.match(text)):
        match = SWITCH.match(tail.group(1))
    if not match and (last := LAST_SENTENCE.match(text)) and len(last.group(1).split()) <= LEAD_IN_WORDS:
        match = SWITCH.match(last.group(2))  # "Yeah, I'm talking to you now. Please switch me to Cursor."
    if not match:
        return None
    return match.group(1).lower(), match.group(2), bool(SEND_THAT.match(text))


def route(text: str, last_message: tuple[str, Heard] | None) -> bool:
    """Who am I talking to, switch, send that; True when something was sent along."""
    if WHO.match(text):
        current = last_speaker()
        say(f"You're talking to {current.spoken()}." if current else "Nobody yet: no agent has spoken.")
        return False
    agent, where, forward = switch_request(text)
    target = find(agent, where)
    if target is None:  # not heard yet: running, though? Found, registered, switched to (2026-11-16)
        running = discover(agent, where)
        if len(running) > 1:
            say(f"I see {len(running)} of {agent.capitalize()}{' in ' + where if where else ''}, so I won't guess. "
                "Say the folder, or talk to the one you want from its tab once.")
            return False
        if running and forward and not where:
            # Found by itself, no folder named: say which first, before your words go there
            say(f"I found {running[0].spoken()}. Say send that to {running[0].agent} in {running[0].where} to send it.")
            return False
        if running:
            target = running[0]
            target.save(SPEAKERS / f"{target.session}.json")
            log(HANDSFREE_LOG, f"found {target.agent} in {target.where or '?'} running, not yet heard: registered")
    if target is None:
        log(HANDSFREE_LOG, f"heard: route to {agent}, none found")
        say(f"I can't find {agent.capitalize()}{' in ' + where if where else ''} running.")
        return False
    if not reachable(target):
        current = last_speaker()
        log(HANDSFREE_LOG, f"heard: route to {target.agent} in {target.where or '?'}, not running any more")
        say(f"{target.spoken()} isn't running any more, so I can't switch you there. "
            + (f"You're still talking to {current.spoken()}." if current else ""))
        return False
    switch_to(target)
    if not forward:
        say(f"Now talking to {target.spoken()}.")
    elif not last_message:
        say(f"Now talking to {target.spoken()}, but there's nothing to send yet.")
    elif deliver(*last_message):
        say(f"Sent to {target.spoken()}.")
        return True
    return False


_tabs: tuple[float, list] = (0.0, [])  # every agent tab with its program's pid, asked again after a moment


def open_editor(speaker: Speaker) -> bool:
    """A Cursor editor chat whose window isn't open: open that project, then the chat. False when it still isn't."""
    if not speaker.in_editor or cursor_window(speaker.where):
        return bool(speaker.in_editor and cursor_window(speaker.where))
    chat = cursor_bridge.chat_of(speaker.session)
    found = next((c for c in cursor_bridge.all_chats() if c["chat"] == chat), None) if chat else None
    if not found:
        return False
    log(HANDSFREE_LOG, f"opening Cursor for {speaker.where} so {chat[:8]} can be reached")
    return cursor_bridge.open_anywhere(chat, found["folder"]) and bool(cursor_window(speaker.where))


_RESUMING = STATE / "resuming"  # one file per chat id, shared by the call server and hands-free


def _load(handle) -> dict:
    handle.seek(0)
    try:
        data = json.loads(handle.read() or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _pane_open(tab: Tab) -> bool | None:
    """True when the pane exists, False when it is gone, None when tmux could not be asked. None stays blocked."""
    from voice.desktop import tmux
    if not tab or tab.owner != tmux.OWNER:
        return False
    return tmux.pane_there(tab)


def _reserved_tab(chat: str) -> Tab | None:
    """The pane a resume already recorded, if it named one."""
    path = _RESUMING / chat
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    owner, address = str(data.get("owner") or ""), str(data.get("address") or "")
    return Tab(owner, address) if owner and address else None


def _claim(chat: str) -> tuple[str, str]:
    """Take the shared resume, or ("still starting", ""). The token is this attempt. A live holder blocks, including
    this process, until that attempt records the pane as up or clears its own token. A pane that still exists blocks
    too, even while the agent is not yet the foreground."""
    path = _RESUMING / chat
    _RESUMING.mkdir(parents=True, exist_ok=True)
    path.touch()
    with path.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        data = _load(handle)
        owner, address = str(data.get("owner") or ""), str(data.get("address") or "")
        tab = Tab(owner, address) if owner and address else Tab()
        try:
            holder = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            holder = 0
        holder_alive = bool(holder) and Path(f"/proc/{holder}").exists()
        waiting = data.get("phase") != "up"
        pane = _pane_open(tab) if tab else False
        if pane is not False or (waiting and holder_alive):
            return "That chat is still starting.", ""
        token = f"{os.getpid()}-{time.time_ns()}"
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "token": token, "phase": "wait", "owner": "", "address": ""}, handle)
        handle.flush()
    return "", token


def _write_reservation(chat: str, tab: Tab, token: str, phase: str = "wait") -> None:
    path = _RESUMING / chat
    if not path.exists():
        return
    with path.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        data = _load(handle)
        if data.get("token") != token:
            return
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "token": token, "phase": phase,
                   "owner": tab.owner, "address": tab.address}, handle)
        handle.flush()


def _clear_reservation(chat: str, token: str = "", *, force: bool = False) -> None:
    """Drop the resume. A token only drops its own attempt: a later claim must not be erased by the earlier call.
    force: the chat is running, so whatever attempt is recorded is done."""
    path = _RESUMING / chat
    if not path.exists():
        return
    with path.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not force and _load(handle).get("token") != token:
            return
        handle.seek(0)
        handle.truncate()
        handle.flush()


def place(speaker: Speaker) -> str:
    """Point the speaker at the process that is its chat id. "" when that process is there (started in tmux if it
    wasn't). A note when the id is known but can't be reached. A folder placeholder, which has no id, is left on
    its saved tab."""
    from voice import resume
    from voice.agents import started
    from voice.desktop import tmux
    if (speaker.pid and speaker.tab and Path(f"/proc/{speaker.pid}").exists()
            and started(speaker.pid) == speaker.born and desktop().terminals.agent_running(speaker.tab)):
        return ""  # already the process we pinned for this id
    chat = resume.chat_id(speaker.session)
    if speaker.in_editor:
        if cursor_window(speaker.where) or open_editor(speaker):
            return ""
        return "That Cursor chat isn't open, and its window didn't start."
    if not chat:
        return ""
    found = resume.find_running(speaker.agent, chat)
    if len(found) > 1:
        return "That chat is open in more than one terminal, so I didn't start another."
    if len(found) == 1:
        pid, tab = found[0]
        if not tab:
            return "That chat is running, but not in a terminal I can type into."
        speaker.tab, speaker.pid, speaker.born = tab, pid, started(pid)
        speaker.save(SPEAKERS / f"{speaker.session}.json")
        _clear_reservation(chat, force=True)  # the chat is running: a leftover resume must not block the next one
        return ""
    folder = resume.folder_of(speaker.agent, chat)
    if folder is None:
        return "That chat isn't running, and I don't know which folder to start it in."
    note, token = _claim(chat)
    if note:
        held = _reserved_tab(chat)
        if held and _pane_open(held) is True:
            speaker.tab = held
            if not speaker.pin():
                speaker.pid, speaker.born = 0, 0
            speaker.save(SPEAKERS / f"{speaker.session}.json")
            return ""  # type into the pane already reserved, instead of starting another
        return note
    published = False
    try:
        tab = tmux.resume(speaker.agent, folder, chat)
        if not tab:
            return "That chat isn't running, and it didn't start in tmux."
        _write_reservation(chat, tab, token)
        published = True
        previous = (speaker.tab, speaker.pid, speaker.born)
        speaker.tab = tab  # pin() looks at this tab; put the old one back if the process never appears
        pinned = False
        until = time.monotonic() + 12
        while time.monotonic() < until:
            if speaker.pin():  # pin() is true only the time the process is first noted
                pinned = True
                break
            time.sleep(0.3)
        if not pinned:
            speaker.tab, speaker.pid, speaker.born = previous
            if _pane_open(tab) is False:
                _clear_reservation(chat, token)
            else:
                _write_reservation(chat, tab, token, "up")  # still there, or tmux could not be asked
            log(HANDSFREE_LOG, f"{speaker.agent} {chat[:8]} did not come up in tmux {tab.address}")
            return "That chat didn't come up in tmux."
        _write_reservation(chat, tab, token, "up")  # saved below: the other service adopts this pid instead of starting one
        speaker.save(SPEAKERS / f"{speaker.session}.json")
        log(HANDSFREE_LOG, f"started {speaker.agent} {chat[:8]} in tmux {tab.address}")
        say(f"{speaker.spoken()} wasn't running. I started that chat.")
        return ""
    finally:
        if token and not published:
            _clear_reservation(chat, token)


def reachable(speaker: Speaker) -> bool:
    """Is that agent still running where its words would be typed: its terminal tab, or its Cursor window? In a tab,
    the same kind of agent in the same folder: agents are remembered for weeks, and a tab id can be reused (tmux
    numbers panes from 0 again after a restart) by another agent, which must not get this one's messages."""
    global _tabs
    if speaker.tab:
        if not desktop().terminals.agent_running(speaker.tab):
            return False
        if time.monotonic() - _tabs[0] > 2:
            _tabs = (time.monotonic(), desktop().terminals.agent_tabs())
        pid = next((pid for tab, pid in _tabs[1] if tab == speaker.tab), 0)
        if speaker.pid:  # its own process, known: there exactly while that one runs in its tab
            return pid == speaker.pid and started(pid) == speaker.born
        running = process(pid) if pid else None
        if not running or running.agent != speaker.agent:
            return False
        try:
            folder = running.folder
        except OSError:
            return False
        return not speaker.where or speaker.where == ("home" if folder == Path.home() else folder.name)
    return speaker.in_editor and bool(cursor_window(speaker.where))


def cursor_window(where: str) -> str:
    """The Cursor editor window for a workspace, by its title "… - <where> - Cursor"."""
    for window in desktop().windows("cursor"):
        if window.title == f"{where} - Cursor" or window.title.endswith(f" - {where} - Cursor"):
            return window.id
    return ""


def type_into_cursor(wid: str, message: str) -> bool:
    """Bring the window forward, focus its agent chat, paste, Enter.

    ctrl+alt+shift+F12 runs composer.focusComposer (a user keybinding that
    install.sh agents adds). Run in a child process: Xlib from the listening
    thread, beside the Qt tray thread, crashed hands-free (SIGSEGV).
    """
    if desktop().paste_into(wid, message, ("ctrl", "alt", "shift", "f12"), ("ctrl", "v"), ("return",)):
        return True
    log(HANDSFREE_LOG, f"Cursor's window did not stay in front (focus: {desktop().focused().app.strip()})")
    return False


SNIPPET = Path(__file__).resolve().parents[2] / "agents/AGENTS-snippet.md"
PHONE = (Heard.TEXTED, Heard.VOICE_NOTE, Heard.SHARED)
INTRO = ("(Henchline, said once: I'm writing from my phone and can't see this screen. Answer [phone …] messages with "
         '`handsfree text "…"` (short; detail can stay here). `handsfree ring "why"` calls me when you need me, '
         f"`handsfree send FILE` sends me a file. How it all works: {SNIPPET}) ")


def introduce(speaker: Speaker | None, heard: Heard) -> str:
    """The first phone message an agent session gets says how to answer it (the user's idea, 2026-11-16: a Cursor
    CLI started from the phone answered only in its terminal, not knowing the phone can't see it)."""
    if speaker is None or heard not in PHONE or not speaker.session or "/" in speaker.session \
            or (INTRODUCED / speaker.session).exists():
        return ""
    return INTRO


CALL_NOTE = ("(Henchline, once per call: we're on a phone call, so stay responsive. Say out loud at once that you've "
             "got it and what you'll do, then do it, and while you work tell me in a sentence what you're up to "
             "every minute or so, until it's done. Run anything longer than half a minute in the background, so my next words "
             "reach you at once. If I say not to talk, stay quiet; if I hang up, carry on and text "
             "me or ring me back when it's done.) ")
CALLED = (Heard.CALL, Heard.PUSHED, Heard.TYPED)


def on_a_call(speaker: Speaker | None, heard: Heard) -> str:
    """The first words an agent gets in a call carry how to be on a call (the user's wish, 2026-11-16: acknowledge,
    say what you'll do, then keep them posted), once per call: the call server empties CALL_TOLD as a call starts."""
    if speaker is None or heard not in CALLED or not speaker.session or "/" in speaker.session \
            or not in_call() or (CALL_TOLD / speaker.session).exists():
        return ""
    return CALL_NOTE


def told_on_call(speaker: Speaker, heard: Heard) -> None:
    if heard in CALLED and speaker.session and "/" not in speaker.session and in_call():
        CALL_TOLD.mkdir(parents=True, exist_ok=True)
        (CALL_TOLD / speaker.session).touch()


def introduced(speaker: Speaker, heard: Heard) -> None:
    """Marked once delivered: a file per session, so the call server and hands-free never write over each other."""
    if heard in PHONE and speaker.session and "/" not in speaker.session:
        INTRODUCED.mkdir(parents=True, exist_ok=True)
        (INTRODUCED / speaker.session).touch()


_why = threading.local()  # per thread: the call server delivers on several at once


def why_not() -> str:
    """Why this thread's last delivery didn't happen, for a text's sender (the phone) as well as the room."""
    return getattr(_why, "words", "")


def refuse(words: str) -> None:
    """A delivery that can't happen: said out loud, and kept for the phone (it said only "the laptop says why",
    out of the user's earshot, 2026-11-16)."""
    _why.words = words
    # A scheduled message failing (logged and retried) or the user away (the phone shows why): never said aloud
    if not getattr(_why, "quiet", False) and not (AWAY.exists() and not in_call()):
        say(words)


_interrupted: dict[str, float] = {}  # tab → when "send now" last interrupted its agent
INTERRUPT_FOR = 20.0  # seconds a held send on the call covers the turn being spoken


def arm_interrupt() -> None:
    """The call's send button was held with nothing typed: stop the agent now, and the next spoken turn cuts in
    instead of waiting in its queue."""
    CALL_INTERRUPT.parent.mkdir(parents=True, exist_ok=True)
    CALL_INTERRUPT.write_text(str(time.time()))
    speaker = last_speaker()
    tab = speaker.tab if speaker and not speaker.in_editor else None
    if tab and time.time() - _interrupted.get(tab.address, 0.0) > 2 and desktop().terminals.interrupt(tab):
        _interrupted[tab.address] = time.time()
        log(HANDSFREE_LOG, f"send now: interrupted {speaker.agent} {tab.address} from the call")


def interrupt_waiting() -> bool:
    """A held send on this call is still waiting for the turn it belongs to."""
    try:
        return time.time() - float(CALL_INTERRUPT.read_text()) < INTERRUPT_FOR
    except (OSError, ValueError):
        return False


def clear_interrupt() -> None:
    CALL_INTERRUPT.unlink(missing_ok=True)


def cuts_in(speaker: Speaker | None) -> bool:
    """A spoken turn on a call cuts into the agent's work. Chosen on the phone when the file says so; otherwise
    Cursor does (it parks words until it finishes) and Claude and Codex wait."""
    try:
        choice = CALL_CUT_IN.read_text().strip()
    except OSError:
        choice = ""
    if choice == "1":
        return True
    if choice == "0":
        return False
    who = speaker or last_speaker()
    return bool(who and who.agent == "cursor")


def is_command(text: str, speaker: Speaker | None) -> bool:
    """A slash command that agent has (a built-in, or its user's or project's command or skill): typed exactly,
    since an agent runs a line as a command only when it starts with "/". Not any line that starts with a path
    ("/tmp is full" is words: typed bare, Claude would refuse it as an unknown command and drop it)."""
    found = re.fullmatch(r"/([A-Za-z0-9_.:-]+)(?:\s.*)?", text.strip(), re.S)
    if not found or speaker is None:
        return False
    project = None
    if speaker.pid:
        try:
            project = Path(f"/proc/{speaker.pid}/cwd").resolve()
        except OSError:
            project = None
    from voice import commands
    return found.group(1) in {c["name"] for c in commands.listed(speaker.agent, project) if c["kind"] != "phone"}


def deliver(text: str, heard: Heard, to: Speaker | None = None, urgent: bool = False) -> bool:
    """`deliver_words`, with the photos the user took on the call screen since the last turn going along with these
    words (a spoken or typed turn on a call; a command, a text or a share carries none). Put back if not delivered."""
    from voice import chains
    # A child's report is not the user's turn: no call photos, and it does not move the pinned call.
    if text.lstrip().startswith(chains.REPORT):
        return deliver_words(text, heard, to, urgent)
    speaker = to or last_speaker()
    if not in_call() or heard in PHONE or heard is Heard.SCHEDULED or speaker is None or is_command(text, speaker):
        return deliver_words(text, heard, to, urgent)
    photos = snaps.take(speaker.session)
    delivered = False
    try:
        delivered = deliver_words(text, heard, to, urgent, snaps.words(photos))
    finally:
        if photos and not delivered:
            snaps.put_back(photos)
    return delivered


def deliver_words(text: str, heard: Heard, to: Speaker | None = None, urgent: bool = False, extra: str = "") -> bool:
    """Type the words, tagged with how they were heard, to `to`, or else to the agent that spoke last. When it
    can't, says why out loud and leaves the reason in why_not(), for the phone to show too. urgent: the phone's
    "send now" (held send button): the agent's work is interrupted first, so the words reach it at once instead of
    waiting in its queue (Escape in a terminal; in a Cursor chat, Enter again, which sends a queued message now)."""
    from voice import chains
    _why.words = ""
    # A report is typed as itself (it already carries its marker). A failure isn't spoken: nobody in the room asked.
    report = bool(text) and str(text).lstrip().startswith(chains.REPORT)
    _why.quiet = heard is Heard.SCHEDULED or report  # a scheduled message is retried later by the call server
    speaker = to or last_speaker()
    # A slash command ("/compact", "/rename …") is typed exactly as it is: an agent runs a line as a command only
    # when it starts with "/", so no tag, introduction or call note in front of it (none of those is then given)
    command = report or is_command(text, speaker)
    message = text.strip() if command \
        else f"{heard.tag} {introduce(speaker, heard)}{on_a_call(speaker, heard)}{text}{extra}"
    if speaker is None:
        log(HANDSFREE_LOG, "nobody to send to")
        refuse("I heard you, but no agent has spoken yet.")
        return False
    missing = place(speaker)
    if missing:
        log(HANDSFREE_LOG, f"place {speaker.session[:24]}: {missing}")
        refuse(missing)
        return False
    tab = speaker.tab
    if speaker.in_editor:
        # That very chat brought to the front first, by Henchline's bridge in Cursor; without it, the window's
        # chat on screen gets the words, as before
        chat = cursor_bridge.chat_of(speaker.session)
        if chat and cursor_cdp.available(speaker.where):
            # Through Cursor's debugging port: that chat's box by its id, a real Enter, no keys on the desktop and
            # no window raised, so it works with the screen locked. Never then typed with keys: no second copy.
            why = "" if cursor_bridge.open_chat(speaker.where, chat) else (
                cursor_bridge.last_why or "the bridge couldn't open that chat")
            why = why or cursor_cdp.send(speaker.where, chat, message, urgent=urgent)
            log(HANDSFREE_LOG, f"sent to cursor chat {chat[:8]} in {speaker.where} (debugging port)" if not why
                else f"Cursor chat {chat[:8]} in {speaker.where}: {why}; nothing typed with keys")
            if why:
                refuse(f"Cursor didn't take that: {why}.")
                return False
            if not report:  # a child's report is not the user answering, and not a line in the call chat
                Sent(speaker.agent, heard.value, session=speaker.session, words=text if in_call() else "").save()
            if not command:
                introduced(speaker, heard)
                told_on_call(speaker, heard)
            return True
        # Without the port: that chat to the front, then keys into the raised window (needs the screen unlocked)
        if chat and not cursor_bridge.open_chat(speaker.where, chat, strict=True):
            # Never into whatever chat is open instead: that may be another agent's
            log(HANDSFREE_LOG, f"Cursor bridge couldn't open chat {chat[:8]} in {speaker.where}: nothing typed")
            refuse("I couldn't open that Cursor chat, so nothing was typed. Is its window still open?")
            return False
        wid = cursor_window(speaker.where)
        if not wid:
            log(HANDSFREE_LOG, f"no Cursor window for {speaker.where}")
            refuse("I heard you, but I can't find Cursor's window for that project.")
            return False
        ok = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "handsfree.py"),
                             "type-into-cursor", wid, message], timeout=20).returncode == 0
        log(HANDSFREE_LOG, f"sent to cursor window {speaker.where}" if ok else "Cursor delivery failed")
        if ok and not report:
            Sent(speaker.agent, heard.value, session=speaker.session, words=text if in_call() else "").save()
        if ok and not command:
            introduced(speaker, heard)
            told_on_call(speaker, heard)
        if not ok and desktop().locked():
            refuse("The laptop's screen is locked, so I can't type into Cursor. "
                   "Agents in a terminal still get your words.")
        elif not ok:
            refuse("I couldn't bring Cursor's window to the front, so I didn't type that anywhere. "
                   "Click Cursor's window and say it again.")
        return ok
    if not tab:
        log(HANDSFREE_LOG, f"no terminal tab for {speaker.agent}")
        refuse("I heard you, but I don't know which terminal to type that into.")
        return False
    if not desktop().terminals.agent_running(tab):
        log(HANDSFREE_LOG, f"no agent running in {tab.address}")
        refuse("I heard you, but the agent I was talking to isn't running any more, so I didn't type it anywhere.")
        return False
    # Not twice within 2 s: a second Escape opens the agent's history picker (Claude) or edits the last message
    # (Codex), and the words would land there. Cursor is steered after the words are typed, not stopped.
    if urgent and not (speaker.agent == "cursor" and not speaker.in_editor) and time.time() - _interrupted.get(tab.address, 0.0) > 2 and desktop().terminals.interrupt(tab):
        _interrupted[tab.address] = time.time()
        log(HANDSFREE_LOG, f"send now: interrupted {speaker.agent} {tab.address} first")
        time.sleep(0.5)  # its prompt back before the words
    again = urgent and speaker.agent == "cursor" and not speaker.in_editor  # Cut in: Enter again. Let it wait: one Enter, so it queues
    if speaker.agent == "cursor" and not speaker.in_editor:
        from voice import cursor_cli
        cursor_cli.clear_picker(tab.address)  # a leftover `/model` would swallow this message into its filter
        if not command:
            message = cursor_cli.disarm(message)
    taken = None
    if again:  # cut in: steered until Cursor's own transcript shows these words taken in, not by reading the screen
        from voice import pickup
        chat = cursor_bridge.chat_of(speaker.session)
        record = pickup.cursor_log(chat) if chat else None
        wanted = pickup.key(text)
        if record and wanted:
            try:
                start = record.stat().st_size
            except OSError:
                start = 0
            taken = lambda: pickup.cursor_took(record, wanted, start)  # noqa: E731
    terminals = desktop().terminals
    # Only tmux's typing takes "again" (other terminals' type_line has just the words)
    typed = terminals.type_line(tab, message, again=again, taken=taken) if again else terminals.type_line(tab, message)
    if not typed:
        log(HANDSFREE_LOG, f"typing into {tab.address} failed")
        refuse("I heard you, but that terminal is gone.")
        return False
    log(HANDSFREE_LOG, f"sent to {speaker.agent} {tab.address}" + (" (enter until the queue is clear)" if again else ""))
    # The user's words to a child are kept for its next report. Not typed to the boss, and a report is not one of them.
    chains.note_correction(speaker.session, text, tab.address, speaker.pid)
    if not report:
        Sent(speaker.agent, heard.value, session=speaker.session, words=text if in_call() else "").save()
    if not command:
        introduced(speaker, heard)
        told_on_call(speaker, heard)
    return True

