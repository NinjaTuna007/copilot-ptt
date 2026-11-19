#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""A phone call with the agents: a web page that streams the phone's mic in and talkback out.

    call/server.py [--port 8790] [--host serve|tailscale|ADDRESS] [--cert PEM --key PEM]

It listens on this machine's Tailscale address, so your own devices (and
programs on this machine) reach it and nothing else does. In front of it,
`tailscale serve` gives https://<this machine's Tailscale name>/ with a real
certificate, once Tailscale lets this user configure it (`sudo tailscale set
--operator=$USER`, once); until then the page has its own certificate
(install.sh call makes one; the phone accepts it once). Open the address and tap
Call. While a call is up, two virtual sinks exist: the phone's mic is played
into PHONE_MIC, whose monitor hands-free listens to, and talkback plays into
PHONE_SPEAKER, whose monitor is streamed back to the phone. The phone's
browser cancels its own echo. One call at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "handsfree/core")]  # voice/, and hands-free's routing
import routing  # noqa: E402
from routing import Heard  # noqa: E402
from voice import commands, cursor_cli, files, pickup, plans, reminders, snaps, spool, screenshot, usage, voices, workflows  # noqa: E402
from voice.agents import AGENT_VOICES, DEFAULT_VOICE  # noqa: E402
from voice.desktop import current as desktop, cursor_bridge, tmux  # noqa: E402
from voice.state import (AWAY, RENAMED, BY_HANDSFREE, CALL, CALL_BUTTON_ONLY, CALL_SILENT, CALL_STAY_ON, CALL_HANGUP, CALL_MIRROR, CALL_ORIGIN,  # noqa: E402
                         CALL_PINNED, CALL_RANG_BACK, CALL_TOLD, PHASE,
                         SPAWNED, TEXTS, VOICE_NOTE_STALE, VOICE_NOTES,
                         CALL_PORT, CALL_PUSH, CALL_INTERRUPT, CALL_CUT_IN, CALL_CHECK, CLAUDE_MODE,
                         CALL_RING, CALL_RINGER, CALL_TLS, HANDSFREE_LOG, HANDSFREE_MODE,
                         PHONE_MIC, PHONE_SPEAKER, PHONE_SPEAKER_MONITOR, Phase, Playing, SENT, SPEAKERS,
                         SPOOL, Sent, Speaker, in_call, last_speaker, log, phase_now, read_json,
                         recent_speakers, turn_on_handsfree, write_json)

MIC_RATE = 16000  # what the page sends: what hands-free listens at
SPEAKER_RATE = 24000  # what the page plays: Kokoro's own rate
CHUNK = SPEAKER_RATE // 50 * 2  # bytes of s16 mono sent to the phone at a time: 20 ms


STATUS_EVERY = 0.2  # seconds between looks at what hands-free and talkback are doing
AGENTS_EVERY = 3.0  # seconds between looks at which agents can be switched to
RING_HOSTS = ("ntfy.sh",)  # where Henchline's UnifiedPush addresses may live: the ntfy app's server
RINGER_FRESH = 180  # seconds a ring stays the one a call answers (it rings 30; Answer may wait for an unlock)
MAX_UPLOAD = 16 * 1024 ** 3  # bytes in one shared file: a long video fits
KEEP_FREE = 2 * 1024 ** 3  # bytes an upload leaves free on the laptop's disk: past that it is refused
# A call where it's the user's turn and they say nothing: the agent asks after them, then hangs up and calls back
# (the user's wish, 2026-11-16: "that's the whole point of a call"). Their turn: nothing playing or waiting to be
# said, and the agent not working (an agent that gives no sign of working gets the longer wait).
NUDGE_AFTER, NUDGE_UNSURE = 45.0, 120.0  # seconds of quiet before each nudge
NUDGES = ("Are you still there? I'm waiting for you.",
          "Hello? Say something, or I'll hang up and call you back.")
HANGUP_SAY, HANGUP_QUIET, HANGUP_MAX = 0.5, 1.5, 120.0  # a hang-up waits until talkback has finished:
# nothing queued and nothing playing, and that has stayed true for HANGUP_QUIET (the last of the audio leaving
# the phone). HANGUP_SAY is the least it waits, so a goodbye queued a moment later is not missed. HANGUP_MAX
# only ends a call whose clip never finishes.


class Status:
    """What the phone shows: the talk button's colour, a status line, and the chat.

    Read from the shared state: hands-free's mode, the phase, the clip
    playing, and the record of the last message delivered (to whom, how
    heard, and during a call the words as heard). The agents' own words become chat.
    """

    def __init__(self) -> None:
        self.since = time.time()
        self.waiting_for = ""  # the agent a message went to, until it starts speaking
        self.clip = ""  # the clip last put in the chat

    def events(self) -> list[dict]:
        """New chat lines: what an agent started saying, and that a spoken message went out."""
        lines = []
        sent = Sent.last()
        if sent and sent.at > self.since:
            self.since = sent.at
            self.waiting_for = sent.agent.capitalize()
            if sent.heard != Heard.TYPED.value:  # a typed one is in the chat already, as the page sent it
                # From the phone off the call (a text, a voice message, a share): the phone's chat has it already
                # (a text to Cursor showed twice while on a call with Claude, 2026-11-16)
                if sent.heard in (Heard.SHARED.value, Heard.TEXTED.value, Heard.VOICE_NOTE.value):
                    said = ""
                else:  # what it heard you say, so a mishearing shows
                    said = f"🎙 {sent.words}" if sent.words else f"(spoken, sent to {self.waiting_for})"
                if said:
                    lines.append({"chat": {"from": "you", "text": said, "session": sent.session}})
        playing = Playing.now()
        if playing and playing.clip != self.clip:
            self.clip = playing.clip
            lines.append({"chat": {"from": playing.agent.capitalize(), "text": playing.text,
                                   "session": playing.session}})
        for line in sorted(CALL_MIRROR.glob("*.json")):  # typed in the terminal (call/hooks)
            mirrored = read_json(line)
            line.unlink(missing_ok=True)
            if mirrored.get("text"):
                lines.append({"chat": {"from": mirrored.get("from", "Claude"), "session": mirrored.get("session", ""),
                                       "text": ("⌨ " if mirrored.get("from") == "you" else "") + mirrored["text"],
                                       **({"tid": mirrored["tid"]} if mirrored.get("tid") else {})}})
        for meta in files.undelivered():  # `handsfree send`: a file in the chat, or one that never reached the phone
            files.delivered(meta)
            lines.append({"chat": {"from": meta["agent"].capitalize(), "text": meta["text"], "session": meta["session"],
                                   "file": {"url": files.link(meta), "name": meta["name"], "type": meta["type"],
                                            "size": meta["size"]}}})
        return lines

    def now(self) -> dict:
        mode = HANDSFREE_MODE.read_text().strip() if HANDSFREE_MODE.exists() else "waiting"
        playing = Playing.now()
        if playing:
            self.waiting_for = ""
            # Who: the phone shows another agent chipping in on its call bar (the user's design, 2026-11-17)
            return {"mode": mode, "state": f"{playing.agent.capitalize()} is speaking",
                    "speaker": {"agent": playing.agent, "session": playing.session}}
        phase, by = phase_now()
        if by == BY_HANDSFREE and phase is Phase.RECORDING:
            return {"mode": mode, "state": "Hearing you…"}
        if by == BY_HANDSFREE and phase is Phase.TRANSCRIBING:
            return {"mode": mode, "state": "Writing it down…"}
        if self.waiting_for:
            return {"mode": mode, "state": f"Sent to {self.waiting_for}: waiting for the answer…"}
        if CALL_BUTTON_ONLY.exists():
            return {"mode": mode, "state": "Open line off: hold the button to talk"}
        return {"mode": mode, "state": "Listening"}


def pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True).stdout.strip()


class Devices:
    """The two virtual sinks of a call, there only while it lasts."""

    def __init__(self) -> None:
        self.modules: list[str] = []

    def __enter__(self) -> Devices:
        for name, what in ((PHONE_MIC, "Phone mic (call)"), (PHONE_SPEAKER, "Phone speaker (call)")):
            self.modules.append(pactl("load-module", "module-null-sink", f"sink_name={name}",
                                      f"sink_properties=device.description='{what}'",
                                      "channels=1", f"rate={MIC_RATE if name == PHONE_MIC else SPEAKER_RATE}"))
        return self

    def __exit__(self, *_) -> None:
        for module in self.modules:
            if module.isdigit():
                pactl("unload-module", module)


class Ear:
    """What the phone says it played, every 2 s: the laptop cannot hear the phone, so a clip lost on the
    way (suspended audio, dropped chunks, the page in the background) only shows up here."""

    def __init__(self) -> None:
        self.loud_ms = 0  # played out loud over the call
        self.while_speaking = 0  # 2 s spans with a clip playing on the laptop
        self.silent = 0  # … of which the phone played (almost) nothing
        self.clip = ""  # the clip playing at the last report
        self.said = ""  # the last trouble logged: said once, not every 2 s

    def report(self, stats: dict) -> None:
        self.loud_ms += stats.get("loud_ms", 0)
        playing = Playing.now()
        clip, self.clip = self.clip, playing.clip if playing else ""
        # Judged only once the same clip was playing a report ago: its first seconds are still being
        # rendered, silent on the laptop too (every "silent" span of the first test call, 2026-11-15)
        if not playing or playing.clip != clip:
            return
        self.while_speaking += 1
        trouble = []
        if stats.get("audio") != "running":
            trouble.append(f"the phone's audio is {stats.get('audio')}")
        if stats.get("dropped"):
            trouble.append(f"dropped {stats['dropped']} chunks (fell behind)")
        if stats.get("hidden"):
            trouble.append("the page is in the background (the app: the screen is off)")
        if stats.get("loud_ms", 0) < 200:
            self.silent += 1
            trouble.append(f"played only {stats.get('loud_ms', 0)} ms of 2 s, got {stats.get('got', 0)} chunks")
        said = "; ".join(trouble)
        if said and said != self.said:
            log(HANDSFREE_LOG, f"call: talkback playing, but {said}")
        self.said = said

    def summary(self) -> str:
        return (f"the phone played {self.loud_ms / 1000:.0f} s out loud; silent in {self.silent} of "
                f"{self.while_speaking} 2-s spans while talkback played")


async def keep_unmuted() -> None:
    """The laptop's mute key mutes every output, the call's two sinks too: the phone then hears nothing and
    hands-free hears nothing from the phone (2026-11-15). They are the call's own plumbing, not speakers,
    so while the call lasts they are unmuted again at once; the laptop's speakers stay muted."""
    try:
        watch = await asyncio.create_subprocess_exec("pactl", "subscribe", stdout=asyncio.subprocess.PIPE)
    except OSError as exc:
        log(HANDSFREE_LOG, f"call: can't watch for mutes ({exc}): the laptop's mute key would silence this call")
        return
    try:
        while line := await watch.stdout.readline():
            if b"'change' on sink" not in line:
                continue
            for sink in (PHONE_MIC, PHONE_SPEAKER):
                if pactl("get-sink-mute", sink).endswith("yes"):
                    pactl("set-sink-mute", sink, "0")
                    log(HANDSFREE_LOG, f"call: {sink} was muted (the laptop's mute key?): unmuted, the call goes on")
    finally:
        if watch.returncode is None:
            watch.terminate()
            await watch.wait()


def route_to_ringer(reply: str) -> None:
    """The phone answered a ring (it says which, by the ring's reply name, as the call opens): the call goes
    to the agent that rang, not to whoever spoke last. A Call the user starts themselves names no ring and
    goes where they were. A ringer hands-free can't type to yet is said out loud, never routed around."""
    ringer = read_json(CALL_RINGER)
    if not ringer or ringer.get("reply") != reply or time.time() - ringer.get("at", 0) > RINGER_FRESH:
        return
    CALL_RINGER.unlink(missing_ok=True)
    speaker = Speaker.load(SPEAKERS / f"{ringer.get('session', '')}.json")
    if speaker and routing.reachable(speaker):
        routing.switch_to(speaker)
        log(HANDSFREE_LOG, f"call: answering {speaker.agent}'s ring, so to {speaker.agent}")
        return
    name, now = str(ringer.get("agent", "the agent")).capitalize(), last_speaker()
    where = (f"so your words go to {now.spoken()}. Say switch me to, to change" if now
             else "so pick someone: say switch me to")
    routing.say(f"{name} rang, but I can't type to it yet, {where}.")
    log(HANDSFREE_LOG, f"call: answering {name}'s ring, but it can't be reached yet")


def blank_words() -> None:
    """The record of the last delivery keeps the words only during a call: blanked when one starts and ends.
    Sent.last never returns words outside a call, so a delivery racing the end shows nothing either."""
    try:
        data = read_json(SENT)
        if data.get("words"):
            write_json(SENT, {**data, "words": ""}, private=True)
    except OSError as exc:
        log(HANDSFREE_LOG, f"call: couldn't blank the last message's words ({exc})")


def flag(path: Path, on: bool) -> None:
    if on:
        path.touch()
    else:
        path.unlink(missing_ok=True)


def keep_ring_endpoint(address: str) -> bool:
    """Where texts and rings reach the app (its push address): only the phone's distributor, nobody's URL."""
    url = urllib.parse.urlsplit(address)
    if url.scheme != "https" or url.hostname not in RING_HOSTS:
        log(HANDSFREE_LOG, f"call: refused a ring address on {url.hostname or 'no host'} (not {', '.join(RING_HOSTS)})")
        return False
    if CALL_RING.exists() and CALL_RING.read_text().strip() == url.geturl():
        return True
    fd = os.open(CALL_RING, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # an older file keeps its mode through O_CREAT
    with os.fdopen(fd, "w") as out:
        out.write(url.geturl())
    log(HANDSFREE_LOG, "the app's push address changed: kept")
    return True


async def ring_endpoint(request: web.Request) -> web.Response:
    """The app's push address, sent whenever the app opens or it changes, not only on a call (2026-11-18: after
    reinstalls the laptop kept an old one, and every text went nowhere). {"endpoint"} → {"ok"}."""
    trusted(request)
    try:
        address = str((await request.json()).get("endpoint", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    return web.json_response({"ok": keep_ring_endpoint(address)})


async def from_page(message: dict, ear: Ear) -> str:
    """The talk button held or let go, a switch, or a typed chat message; a note for the page, if any."""
    if "answering" in message:  # this call answers a ring: to the agent that rang
        await asyncio.get_running_loop().run_in_executor(None, route_to_ringer, str(message["answering"]))
    elif "ring_endpoint" in message:  # where `handsfree ring` reaches the app: kept private, never logged
        keep_ring_endpoint(str(message["ring_endpoint"]))
    elif "sound" in message:  # the app's sound mode: turns after this line ran in it, for comparing them
        log(HANDSFREE_LOG, f"call: phone sound {message['sound']}")
    elif "stats" in message:
        ear.report(message["stats"])
    elif "cut_in" in message:  # the call menu: cut in, let it wait, or follow the agent (Cursor cuts in)
        choice = message["cut_in"]
        if choice == "auto":
            CALL_CUT_IN.unlink(missing_ok=True)
        else:
            CALL_CUT_IN.parent.mkdir(parents=True, exist_ok=True)
            CALL_CUT_IN.write_text("1" if choice else "0")
    elif message.get("interrupt"):  # send held on the call, nothing typed: the spoken turn cuts in
        await asyncio.get_running_loop().run_in_executor(None, routing.arm_interrupt)
        return json.dumps({"note": "Cutting in."})
    elif "push" in message:
        flag(CALL_PUSH, message["push"])
    elif "button_only" in message:
        flag(CALL_BUTTON_ONLY, message["button_only"])
    elif "stay_on" in message:
        flag(CALL_STAY_ON, message["stay_on"])
    elif "switch" in message:
        await asyncio.get_running_loop().run_in_executor(None, switch, message["switch"])
    elif words := message.get("chat", "").strip():
        session = message.get("session", "")
        sent, note = await asyncio.get_running_loop().run_in_executor(
            None, typed, words, session, bool(message.get("urgent")))
        if note:
            return json.dumps({"note": note, "unsent": message.get("id", "")})
        if sent and watch_pickup(message.get("id", ""), session, words):  # one tick until the agent takes it in
            return json.dumps({"queued": message.get("id", ""), "session": session})
        if sent:  # typed into the agent: the phone's two ticks, in the chat it was typed in
            return json.dumps({"delivered": message.get("id", ""), "session": session})
        return ""
    return ""


def chat_named(speaker: Speaker) -> str:
    """For one of several Cursor chats in a window: " · " and its first words, to tell them apart. For Claude: the
    name its session was given (/rename, or Claude's own title for it), not one made up from its folder."""
    if speaker.agent == "claude" and speaker.pid:
        record = read_json(Path.home() / ".claude/sessions" / f"{speaker.pid}.json")
        name = str(record.get("name") or "").strip() if isinstance(record, dict) else ""
        return f" · {name}" if name and record.get("nameSource") != "derived" else ""
    if speaker.agent == "codex":
        name = usage.codex_name(speaker.session)
        return f" · {name}" if name else ""
    chat = cursor_bridge.chat_of(speaker.session) if speaker.agent == "cursor" else ""
    title = cursor_bridge.chat_title(chat) if chat else ""
    return f" · {title}" if title else ""


def agents() -> dict:
    """The agents a call can switch to: heard lately and still running, and the one replies go to now."""
    current, locked = last_speaker(), desktop().locked()
    started = spawned()
    listed = [{"session": s.session,
               "name": s.spoken() + chat_named(s) + (" (laptop locked)" if s.in_editor and locked else ""),
               "spawned": s.tab.owner == tmux.OWNER and s.tab.address in started,
               # a workflow running in it: shown in place of the chat's last line on the phone's list
               "workflow": workflows.live(s.session) if s.agent == "claude" else None}
              for s in recent_speakers() if routing.reachable(s)]
    return {"agents": listed,
            "current": current.session if current else "",
            "renamed": read_json(RENAMED)}  # chats the phone moves to an agent's own key


def switch(session: str) -> None:
    """The phone's pick (its agent menu, or the chat a call was started from): replies go to that agent now.
    Not announced: the phone shows who it is, and "Now talking to …" over the call said it again (the user's
    catch, 2026-11-16). A switch said out loud ("switch me to Cursor") is still announced: there it's the only sign."""
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target and routing.reachable(target):
        routing.switch_to(target)


# Messages typed into an agent that logs what it takes in (Claude, Codex), not yet taken in: id → (session, agent,
# words, when). Taken in: id → (session, when the agent took it in). The phone asks with its working poll (two ticks), and keeps one
# tick meanwhile (the agent may be busy: the message waits in its queue, the user's catch, 2026-11-16)
WAITING: dict[str, tuple[str, str, str, float]] = {}
TAKEN: dict[str, tuple[str, float]] = {}
PICKUP_LOCK = threading.Lock()  # both, from the loop and executor threads


def watch_pickup(mid: str, session: str, words: str) -> bool:
    """Watched until the agent takes it in, when its agent can say so: whether it is watched."""
    target = (next((s for s in recent_speakers() if s.session == session), None) if session else last_speaker())
    if not mid or not target or target.agent not in ("claude", "codex") or not pickup.key(words):
        return False
    if routing.is_command(words, target):  # not logged as the words typed: its pickup can't be seen, two ticks now
        return False
    now = time.time()
    with PICKUP_LOCK:
        for old in [k for k, v in WAITING.items() if now - v[3] > 6 * 3600]:  # never taken in: forgotten after 6 h
            WAITING.pop(old, None)
        WAITING[mid] = (target.session, target.agent, words, now)
    return True


def taken_in(session: str) -> dict[str, float]:
    """The phone's messages to that agent it has taken in since, with when it did: checked now, kept an hour."""
    now = time.time()
    with PICKUP_LOCK:
        mine = [(mid, v) for mid, v in WAITING.items() if v[0] == session]
    taken = {mid: pickup.took(agent, s, words, since) for mid, (s, agent, words, since) in mine}  # unlocked: slow
    with PICKUP_LOCK:
        for mid, at in taken.items():
            if at:
                WAITING.pop(mid, None)
                TAKEN[mid] = (session, at)
        for old in [k for k, v in TAKEN.items() if now - v[1] > 3600]:
            TAKEN.pop(old, None)
        return {mid: at for mid, (s, at) in TAKEN.items() if s == session}


def typed(words: str, session: str, urgent: bool = False) -> tuple[bool, str]:
    """A typed line, to the agent whose chat it was typed in; a switch ("switch me to Cursor") works as spoken.
    (typed into an agent, a note why not): a switch is neither."""
    if routing.meaning(words) is routing.Meaning.ROUTING:
        routing.route(words, None)
        return False, ""
    got, note = to_agent(words, session, Heard.TYPED, urgent=urgent)
    return bool(got), note


def to_agent(words: str, session: str, heard: Heard, switch: bool = True, urgent: bool = False) -> tuple[str, str]:
    """Words for the agent whose chat the phone shows: (who got them, "") or ("", a note why nobody did).

    Another agent may have spoken since the phone last heard who is current: the chat shown wins, so the words
    go to exactly that session (typed to it by name, never to whoever is last by then), or nowhere, with a note.
    On a call that agent also becomes the one spoken words go to (switch); a text leaves that alone. No session:
    no agent yet, to the last one (calls only; texts need a session).
    """
    target = None
    if session:
        target = next((s for s in recent_speakers() if s.session == session), None)
        if target is None:
            return "", "That agent isn't reachable, so I didn't send that. Pick another and send again."
        why = routing.place(target)  # the chat id: the live process, or that id started in tmux
        if why or not routing.reachable(target):
            return "", why or "That agent isn't reachable, so I didn't send that. Pick another and send again."
        current = last_speaker()
        if switch and (not current or current.session != session):
            routing.switch_to(target)
    if not routing.deliver(words, heard, to=target, urgent=urgent):  # it says why out loud on the laptop, and keeps it
        return "", "Not sent: " + (routing.why_not() or "the laptop couldn't type it into the agent.")
    got = target or last_speaker()
    return (got.spoken() if got else "your agent"), ""


def trusted(request: web.Request) -> None:
    """Only the call page, or Henchline, which sends the page's own Origin. Browsers let any web page open a
    WebSocket or post a form to any address: without this, a page on some other site, open on the phone, could
    type into the agents' terminals. Not a password: a program on the tailnet can send any Origin. Only your
    own devices reaching the Tailscale address is the real boundary."""
    origin = f"https://{request.host}"
    if request.headers.get("Origin") != origin:
        raise web.HTTPForbidden(text="only the call page may connect")
    if CALL_ORIGIN.exists() and CALL_ORIGIN.read_text() == origin:
        return
    CALL_ORIGIN.parent.mkdir(parents=True, exist_ok=True)  # a fresh install has no state folder yet
    CALL_ORIGIN.write_text(origin)  # where `handsfree send` points a notification's link


async def share_file(request: web.Request) -> web.Response:
    """One file shared from the phone, streamed to INBOX/<today>/: the name it was saved under, for share_tell.
    Nothing is overwritten, and an upload that would leave under KEEP_FREE on the disk is refused."""
    trusted(request)
    folder = files.inbox_today()
    name = files.safe_name(request.query.get("name", "file"))
    part = folder / f".{secrets.token_hex(8)}.part"  # its own: two uploads of one name don't share it
    size = 0
    try:
        with part.open("xb") as out:
            while chunk := await request.content.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise OSError(f"bigger than {files.size_text(MAX_UPLOAD)}")
                if size % (64 << 20) < len(chunk) and shutil.disk_usage(folder).free < KEEP_FREE:
                    raise OSError("the laptop's disk is nearly full")
                out.write(chunk)
        if shutil.disk_usage(folder).free < KEEP_FREE:
            raise OSError("the laptop's disk is nearly full")
        while True:  # a link never replaces a file: another upload of the same name may land meanwhile
            path = files.unused(folder, name)
            try:
                os.link(part, path)
                break
            except FileExistsError:
                continue
        part.unlink()
    except (OSError, ConnectionError) as exc:
        part.unlink(missing_ok=True)
        log(HANDSFREE_LOG, f"call: a shared file didn't arrive whole ({exc})")
        return web.json_response({"note": f"Not saved: {exc}"}, status=507 if "disk" in str(exc) else 400)
    log(HANDSFREE_LOG, f"call: the phone shared a file ({files.size_text(size)})")
    return web.json_response({"saved": path.name})


async def share_tell(request: web.Request) -> web.Response:
    """After the uploads: the agent the user is talking to is told where the files are, with any words shared.
    {"files": [saved names], "text": shared text, "note": what the user added, "session": the chat shown}"""
    trusted(request)
    try:
        shared = await request.json()
        names = [str(n) for n in shared.get("files", [])]
    except (ValueError, AttributeError, TypeError):
        raise web.HTTPBadRequest(text="expected JSON")
    paths = [files.received(n) for n in names]
    if None in paths:
        return web.json_response({"note": "Some of those files never arrived on the laptop."}, status=400)
    text, note = str(shared.get("text", "")), str(shared.get("note", ""))
    if not paths and not (text + note).strip():
        return web.json_response({"note": "Nothing to share."}, status=400)
    words = files.message(paths, text, note)
    session = str(shared.get("session", ""))
    if not session and not in_call():  # off a call a share is a text: for one agent, named
        return web.json_response({"note": "Pick an agent's chat in Henchline first, then share again."}, status=400)
    got, why = await asyncio.get_running_loop().run_in_executor(  # off a call it is a text: to that agent only
        None, to_agent, words, session, Heard.SHARED, in_call())
    return web.json_response({"to": got} if got else {"note": why})


async def share_snap(request: web.Request) -> web.Response:
    """Photos from the call screen's camera button, uploaded with /share/file: they wait to go with the turn being
    spoken (hands-free adds them to the next turn it delivers), or, when no turn comes in snaps.WAIT seconds, are
    sent to the agent on their own. {"files": [saved names], "session": the chat shown}"""
    trusted(request)
    try:
        shared = await request.json()
        paths = [files.received(str(n)) for n in shared.get("files", [])]
        session = str(shared.get("session", ""))
    except (ValueError, AttributeError, TypeError):
        raise web.HTTPBadRequest(text="expected JSON")
    if not paths or None in paths:
        return web.json_response({"note": "That photo never arrived on the laptop."}, status=400)
    if not in_call():
        return web.json_response({"note": "The camera button is for calls: share it from the chat instead."}, status=400)
    snaps.add(paths, session)
    log(HANDSFREE_LOG, f"call: {len(paths)} photo(s) from the phone's camera wait for the next turn")
    arm_snaps(snaps.WAIT + 1)
    return web.json_response({"waiting": snaps.WAIT})


SNAP_TIMER: asyncio.TimerHandle | None = None


def arm_snaps(after: float) -> None:
    """Looks in `after` seconds for photos no turn took (one timer at a time, whichever session they are for)."""
    global SNAP_TIMER
    if SNAP_TIMER is not None:
        SNAP_TIMER.cancel()
    SNAP_TIMER = asyncio.get_running_loop().call_later(after, lambda: asyncio.ensure_future(snaps_alone()))


async def snaps_alone() -> None:
    """No turn took the photos in time: the agent gets them by themselves. One that can't be delivered goes back on
    the list and is tried again (until it is too old), so it isn't lost."""
    loop = asyncio.get_running_loop()
    taken = await loop.run_in_executor(None, lambda: snaps.take(older_than=snaps.WAIT))
    for session in {s["session"] for s in taken}:
        mine = [s for s in taken if s["session"] == session]
        try:
            words = files.message(snaps.paths(mine), "", "A photo taken with the call screen's camera, no words with it.")
            got, why = await loop.run_in_executor(None, to_agent, words, session, Heard.SHARED, in_call())
        except OSError as exc:  # a photo deleted meanwhile
            got, why = "", str(exc)
        log(HANDSFREE_LOG, f"call: photo(s) sent alone to {got}" if got else f"call: photo(s) not delivered ({why})")
        if not got:
            for lost in await loop.run_in_executor(None, snaps.put_back, mine, True):
                log(HANDSFREE_LOG, f"call: gave up on a photo ({Path(lost['path']).name}) after {snaps.TRIES} tries")
    if await loop.run_in_executor(None, snaps.waiting):
        arm_snaps(30 if taken else 10)  # a turn may yet take them; or this was not yet their time


ACCEPTED: dict[str, tuple[float, asyncio.Future]] = {}  # a text's id → its answer: a retry is answered, not typed again
ACCEPTED_FOR = 3600


async def texted(request: web.Request) -> web.Response:
    """A reply typed into an agent's text notification on the phone (`handsfree text`): to that agent.
    {"session": the agent session that texted, "text": the reply}"""
    trusted(request)
    try:
        message = await request.json()
        words, session = str(message.get("text", "")).strip(), str(message.get("session", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    # A text always names its agent, never whoever spoke last; only a call's own chat may go to the last speaker
    if not words or not session:
        return web.json_response({"note": "Nothing to send, or no agent named."}, status=400)
    urgent, mid = bool(message.get("urgent")), str(message.get("id", ""))
    renaming = re.fullmatch(r"/rename\s+(.+)", words.strip(), re.S)
    if renaming and cursor_bridge.chat_of(session):  # a Cursor chat's /rename: done by Cursor's own rename, not typed
        why = await asyncio.get_running_loop().run_in_executor(
            None, cursor_bridge.rename, cursor_bridge.chat_of(session), renaming.group(1))
        return web.json_response({"note": f"Renamed to “{' '.join(renaming.group(1).split())[:120]}”" if not why
                                  else f"Not renamed: {why}"})
    # The phone's outbox sends again what timed out on its side, though it may have been typed in: same id, same answer
    now = time.time()
    for old in [k for k, (at, _) in ACCEPTED.items() if now - at > ACCEPTED_FOR]:
        del ACCEPTED[old]
    if mid and mid in ACCEPTED:
        return web.json_response(await asyncio.shield(ACCEPTED[mid][1]))
    answer = asyncio.get_running_loop().create_future()
    if mid:
        ACCEPTED[mid] = (now, answer)
    typing = asyncio.get_running_loop().run_in_executor(
        None, to_agent, " ".join(words.split()), session, Heard.TEXTED, False, urgent)

    def typed(done: asyncio.Future) -> None:  # settled by the typing itself, even if this request is gone
        if done.exception() is not None:
            ACCEPTED.pop(mid, None)  # not typed: a retry may try again
            answer.set_exception(done.exception())
            return
        got, why = done.result()
        answer.set_result({"to": got, "queued": watch_pickup(mid, session, words)} if got else {"note": why})

    typing.add_done_callback(typed)
    return web.json_response(await asyncio.shield(answer))


async def crew(request: web.Request) -> web.Response:
    """Henchline's pull-to-refresh: every agent running is looked for and registered, then the crew is listed as
    on a call ({"agents": [{session, name}], "current": session})."""
    trusted(request)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, routing.discover_all)
    return web.json_response(await loop.run_in_executor(None, agents))


SPAWNED_LOCK = threading.Lock()  # SPAWNED is read, changed and written from the loop and executor threads


def spawned(change=None) -> list[str]:
    """The tmux panes of agents started from the phone that are still those panes: each kept with its process id,
    since tmux numbers panes from 0 again after a restart, and a reused id may be one the user started. `change`
    edits the list under the same lock (a new pane, an ended one)."""
    with SPAWNED_LOCK:
        kept = [e for e in read_json(SPAWNED).get("panes", []) if isinstance(e, dict) and e.get("pid")]
        panes = tmux.pane_pids()  # one tmux call; None: tmux didn't answer, so nothing is taken as gone
        alive = kept if panes is None else [e for e in kept if panes.get(str(e.get("pane"))) == e["pid"]]
        if change:
            alive = change(alive)
        if alive != kept:  # gone, or now someone else's: forgotten
            write_json(SPAWNED, {"panes": alive}, private=True)
        return [str(e["pane"]) for e in alive]


def trusted_folders() -> list[Path]:
    """Where a new chat can start: folders Claude Code already trusts (its own trust prompt would otherwise be the
    first thing the phone's words answer), that still exist, most recently used first."""
    try:
        projects = json.loads((Path.home() / ".claude.json").read_text()).get("projects", {})
    except (OSError, ValueError, AttributeError):
        return []
    found = [Path(p) for p, v in projects.items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted")]

    def used(folder: Path) -> float:  # Claude keeps each folder's sessions in ~/.claude/projects/<path, dashed>
        try:
            return (Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(folder))).stat().st_mtime
        except OSError:
            return 0.0
    # Not a hidden folder's insides (~/.claude/jobs/…: an agent's own scratch folders, which once showed up here)
    shown = (f for f in found if f.is_dir() and not any(part.startswith(".") for part in f.parts))
    return sorted(shown, key=used, reverse=True)[:30]


def notice(title: str, words: str) -> None:
    """A notification on the user's phone through their own ntfy topic (handsfree's; never printed or logged)."""
    topic_file = Path.home() / ".config/ptt/ntfy-topic"
    topic = topic_file.read_text().strip() if topic_file.exists() else ""
    if not topic:
        return
    # Header values go as latin-1: the title folded to ASCII, or a folder named outside it loses the notice
    title = title.encode("ascii", "replace").decode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{topic}", data=words.encode(),
                                                      headers={"Title": title, "Tags": "lock"}), timeout=10).close()
    except (OSError, ValueError):
        log(HANDSFREE_LOG, f"phone notice not sent: {title}")


def typed_folder(given: str) -> Path | None:
    """A folder typed on the phone for a new chat: an existing folder of the user's (under home, or a drive mounted
    under /media/<user>), never home's credential folders (files.PRIVATE) and never home itself, which holds them."""
    path = Path(given).expanduser()
    if not path.is_absolute():
        return None
    path = path.resolve()
    roots = [Path.home(), Path("/media") / Path.home().name]
    if not path.is_dir() or path in roots or not any(r in path.parents for r in roots) or files.private(path):
        return None
    return path


async def crew_folders(request: web.Request) -> web.Response:
    """A new chat's choices: the folders it can start in and the agents installed."""
    trusted(request)
    folders = await asyncio.get_running_loop().run_in_executor(None, trusted_folders)
    listed = [{"path": str(f), "name": "home" if f == Path.home() else f.name} for f in folders]
    return web.json_response({"folders": listed, "agents": tmux.available()})


def usage_of(target: Speaker) -> dict:
    """That agent's plan limits and context, the numbers the phone draws as bars (voice/usage.py). A Cursor CLI
    chat has no composer record: its account limits still come from the hidden CLI, and context only when a record has it."""
    if target.agent == "claude":
        return usage.claude(target.session)
    if target.agent == "codex":
        return usage.codex(target.session)
    if target.agent == "cursor" and cursor_bridge.chat_of(target.session):
        record = cursor_bridge.composer(cursor_bridge.chat_of(target.session)) or {}
        if record:
            return usage.cursor(record)
        limits, notes = usage.cursor_plan()
        if limits or notes:
            return {"model": "", "limits": limits, "context": None, "notes": notes}
    return {}


async def crew_usage(request: web.Request) -> web.Response:
    """The phone's /usage and /context: that agent's plan limits and context, as numbers for bars (voice/usage.py):
    {"agent", "model", "limits": [{label, percent, resets}], "context": {percent, size, used, parts}, "notes"}."""
    trusted(request)
    session = request.query.get("session", "")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if not target:
        return web.json_response({"note": "That agent isn't on the crew."}, status=404)
    loop = asyncio.get_running_loop()
    found = await loop.run_in_executor(None, usage_of, target)
    if not found:
        return web.json_response({"note": f"{target.agent.capitalize()} hasn't reported its usage here yet."})
    return web.json_response({"agent": target.agent, **found})


async def crew_usage_tell(request: web.Request) -> web.Response:
    """Type the readout into the agent. {session, which: usage|context}. The bars stay on the phone. Not typed as
    the slash command: that would open a panel and wait."""
    trusted(request)
    try:
        body = await request.json()
        session = str(body.get("session", ""))
        which = str(body.get("which", "usage"))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if not target:
        return web.json_response({"note": "That agent isn't on the crew."}, status=404)
    loop = asyncio.get_running_loop()
    found = await loop.run_in_executor(None, usage_of, target)
    if not found:
        return web.json_response({"note": f"{target.agent.capitalize()} hasn't reported its usage here yet."})
    text = usage.words(found, "context" if which == "context" else "usage")
    got, why = await loop.run_in_executor(None, to_agent, text, session, Heard.TEXTED, False)
    return web.json_response({"to": got} if got else {"note": why})


async def away(request: web.Request) -> web.Response:
    """Away on or off (the phone's /away): {"on": bool} sets it, {} toggles it. {"away": bool}."""
    trusted(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    on = body.get("on") if isinstance(body, dict) and isinstance(body.get("on"), bool) else not AWAY.exists()
    if on:
        AWAY.parent.mkdir(parents=True, exist_ok=True)
        AWAY.touch()
    else:
        AWAY.unlink(missing_ok=True)
    log(HANDSFREE_LOG, "away: hands-free not listening, talkback texts the phone" if on else "back: away is off")
    return web.json_response({"away": on})


async def away_state(request: web.Request) -> web.Response:
    """Whether the user is away, for the phone's avatar: {"away": bool}."""
    trusted(request)
    return web.json_response({"away": AWAY.exists()})


async def voices_state(request: web.Request) -> web.Response:
    """How the agents sound: the mode, and the voice each one is using now."""
    trusted(request)
    return web.json_response(voices.public())


async def voices_save(request: web.Request) -> web.Response:
    """A change from the voices sheet: {mode, voice, agent, again}."""
    trusted(request)
    try:
        change = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected JSON")
    if not isinstance(change, dict):
        raise web.HTTPBadRequest(text="expected JSON")
    return web.json_response(voices.apply(change))


async def voices_preview(request: web.Request) -> web.Response:
    """One short line in a voice, as a file the phone can play: {"voice"} -> {"url", "name", "size"}."""
    trusted(request)
    try:
        voice = str((await request.json()).get("voice", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(None, voices.sample, voice)
    except Exception as exc:  # noqa: BLE001 - Kokoro missing or a bad render: said, not a traceback to the phone
        log(HANDSFREE_LOG, f"voice preview: {exc}")
        return web.json_response({"note": "Couldn't make that preview."}, status=500)
    if path is None:
        return web.json_response({"note": "That isn't one of the voices."}, status=400)
    def posted(path):
        meta = files.post(path, "", "handsfree", "")
        files.delivered(meta)  # a preview is for the phone to play, not a file waiting in the call chat
        return meta

    meta = await loop.run_in_executor(None, posted, path)
    return web.json_response({"url": files.link(meta), "name": meta["name"], "size": meta["size"]})


def _push_picture(meta: dict, target: Speaker) -> None:
    """Off a call, the picture is a notification the phone opens in this chat. On a call the status loop sends it."""
    if in_call() or not CALL_RING.exists() or not CALL_ORIGIN.exists():
        return
    origin = CALL_ORIGIN.read_text().strip()
    body = json.dumps({"agent": target.agent, "session": target.session, "why": meta["text"],
                       "file": {"url": origin + files.link(meta), "name": meta["name"], "type": meta["type"],
                                "size": meta["size"]}}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(CALL_RING.read_text().strip(), data=body,
                                                      headers={"Content-Type": "application/json"}), timeout=10).close()
        files.delivered(meta)
    except (OSError, ValueError):
        pass


async def show_windows(request: web.Request) -> web.Response:
    """Windows the phone can ask for a picture of: [{id, app, title}]."""
    trusted(request)
    found = await asyncio.get_running_loop().run_in_executor(None, screenshot.windows)
    return web.json_response({"windows": found})


async def show_send(request: web.Request) -> web.Response:
    """A picture into this chat. {session, what: pane|screen|window|find, id, query, words}."""
    trusted(request)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected JSON")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected JSON")
    session = str(body.get("session", ""))
    what = str(body.get("what", "pane"))
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None:
        return web.json_response({"note": "That agent isn't known to the laptop."}, status=404)
    loop = asyncio.get_running_loop()
    wid = str(body.get("id", ""))
    if what == "find":
        hits = screenshot.matching(await loop.run_in_executor(None, screenshot.windows), str(body.get("query", "")))
        if len(hits) != 1:
            note = "No window matches that." if not hits else "Several windows match. Pick one."
            return web.json_response({"matches": hits, "note": note})
        what, wid = "window", hits[0]["id"]
        label = hits[0].get("title") or hits[0].get("app") or "a window"
    elif what == "screen":
        label = "the whole screen"
    elif what == "window":
        listed = await loop.run_in_executor(None, screenshot.windows)
        match = next((item for item in listed if item["id"] == wid), None)
        label = (match["title"] or match["app"]) if match else "a window"
    else:
        label = ""
    pane = target.tab.address if target.tab and target.tab.owner == "tmux" else ""
    path, why = await loop.run_in_executor(None, lambda: screenshot.take(what, wid, pane, label))
    if path is None:
        return web.json_response({"note": why})
    words = str(body.get("words", "")).strip() or "Here."
    meta = await loop.run_in_executor(None, files.post, path, words, target.agent, target.session)
    await loop.run_in_executor(None, _push_picture, meta, target)
    return web.json_response({"ok": True, "name": meta["name"]})


async def chat_workflows(request: web.Request) -> web.Response:
    """A Claude chat's newest workflow runs, each with its phases and agents (voice/workflows.py): the phone's
    workflow sheet and /workflow."""
    trusted(request)
    session = request.query.get("session", "")
    found = await asyncio.get_running_loop().run_in_executor(None, workflows.runs, session)
    return web.json_response({"runs": found})


async def chat_plan(request: web.Request) -> web.Response:
    """The plan for the agent whose chat the phone shows: its steps and how many are done. {"note"} when it has none."""
    trusted(request)
    session = request.query.get("session", "")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None:
        return web.json_response({"note": "That agent isn't known to the laptop."}, status=404)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, project_of, target)
    plan = await loop.run_in_executor(None, plans.of_session, session, project)
    if plan is None:
        return web.json_response({"note": "This chat has no plan."})
    return web.json_response(plan)


async def chat_plan_build(request: web.Request) -> web.Response:
    """The user approved the plan: the agent is told to build it and to keep the document updated."""
    trusted(request)
    try:
        session = str((await request.json()).get("session", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None:
        return web.json_response({"note": "That agent isn't known to the laptop."}, status=404)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, project_of, target)
    plan = await loop.run_in_executor(None, plans.find, project)
    if plan is None:
        return web.json_response({"note": "This chat has no plan."}, status=404)
    got, why = await loop.run_in_executor(None, to_agent, plans.approval(plan), session, Heard.TEXTED, False)
    return web.json_response({"to": got} if got else {"note": why})


async def usage_overview(request: web.Request) -> web.Response:
    """The phone's Usage tab: every AI's plan limits at once, with when each resets (voice/usage.py)."""
    trusted(request)
    fresh = request.query.get("fresh") == "1"  # pulled down: Cursor's plan read again, not the one kept 10 minutes
    return web.json_response({"agents": await asyncio.get_running_loop().run_in_executor(None, usage.overview, fresh)})


async def cursor_chats(request: web.Request) -> web.Response:
    """Every Cursor editor chat that can be opened again, active or quiet, newest first (the user's wish,
    2026-11-17): {"chats": [{session, name, where, updated, status, open}]}, open when its window is open now."""
    trusted(request)
    loop = asyncio.get_running_loop()
    chats = await loop.run_in_executor(None, cursor_bridge.all_chats)
    open_projects = {cursor_bridge.project_of(c["folder"]) for c in chats
                     if cursor_bridge.windows_of(cursor_bridge.project_of(c["folder"]))}
    return web.json_response({"chats": [
        {"session": f"cursor-{c['chat']}", "name": c["name"], "where": Path(c["folder"]).name,
         "updated": c["updated"], "status": c["status"],
         "open": cursor_bridge.project_of(c["folder"]) in open_projects} for c in chats[:200]]})


async def cursor_chat_open(request: web.Request) -> web.Response:
    """One of those chats, opened in Cursor (its window first, when it's closed) and on the crew from now on:
    {"session"} → {"session", "name"} and the crew, or {"note"} why not."""
    trusted(request)
    try:
        chat = cursor_bridge.chat_of(str((await request.json()).get("session", "")))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    loop = asyncio.get_running_loop()
    found = next((c for c in await loop.run_in_executor(None, cursor_bridge.all_chats) if c["chat"] == chat), None)
    if not found:
        return web.json_response({"note": "Cursor doesn't have that chat any more."}, status=404)
    if not await loop.run_in_executor(None, cursor_bridge.open_anywhere, chat, found["folder"]):
        return web.json_response({"note": "Cursor didn't open it: is the laptop unlocked, with Henchline's bridge "
                                          "in Cursor?"})
    speaker = Speaker(f"cursor-{chat}", "cursor", Path(found["folder"]).name)
    await loop.run_in_executor(None, speaker.remember)
    return web.json_response({"session": speaker.session, "name": found["name"], **await loop.run_in_executor(None, agents)})


async def crew_new(request: web.Request) -> web.Response:
    """A new chat: the agent started in its own tmux window in one of those folders (nothing else: the phone names
    one from the list), registered, and its session returned with the crew."""
    trusted(request)
    try:
        body = await request.json()
        folder, agent, typed = str(body.get("folder", "")), str(body.get("agent", "claude")), bool(body.get("typed"))
        model = str(body.get("model", ""))
        settings = body.get("params") if isinstance(body.get("params"), dict) else {}
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    loop = asyncio.get_running_loop()
    match = next((f for f in await loop.run_in_executor(None, trusted_folders) if str(f) == folder), None)
    if match is None and typed:  # a path the user typed (and confirmed trusting): any folder of theirs that exists
        match = typed_folder(folder)
        if match is None:
            return web.json_response({"note": f"Not a folder of yours on the laptop: {folder}"}, status=400)
    if match is None or agent not in tmux.available():
        return web.json_response({"note": "Not a folder or agent the laptop offers."}, status=400)
    if model and settings and tmux.checked(agent, model, {str(k): str(v) for k, v in settings.items()}) is None:
        return web.json_response({"note": f"{agent} has no such setting for {model}."}, status=400)
    known = ({m["id"] for m in await loop.run_in_executor(None, tmux.models, agent)}
             | {m["id"] for m in tmux.menu(agent)})
    if model and model not in known:
        return web.json_response({"note": f"{agent} has no model {model}."}, status=400)
    tab = await loop.run_in_executor(None, tmux.start, agent, match, model,
                                     {str(k): str(v) for k, v in settings.items()})
    if not tab:
        return web.json_response({"note": f"{agent} didn't start."}, status=500)
    # Claude's question only comes for a typed folder (the listed ones are those it trusts); Codex keeps its own
    # list, so it may ask about any folder: answered once that exact question is on screen (it may not come at all)
    asks = agent == "codex" or (typed and agent == "claude"
                                and match not in await loop.run_in_executor(None, trusted_folders))
    if asks:
        name = agent.capitalize()
        wait = 8.0 if agent == "codex" else 12.0
        accepted = await loop.run_in_executor(None, tmux.accept_trust, tab, match, agent, wait)
        if accepted or typed:
            log(HANDSFREE_LOG, f"{name}'s trust for {match} {'accepted' if accepted else 'left to answer in tmux'}")
        if accepted:  # said on the phone too: a request from anything else on the tailnet can't do this unseen
            loop.run_in_executor(None, notice, f"{name} now trusts a folder",
                                 f"Started from a new chat, {name}'s trust question was answered yes for {match.name}.")
    # Every start is told to the phone: the agent runs commands without asking (auto mode, Run Everything, Codex's
    # reviewer), so one started by anything else on the tailnet can't go unseen
    # Not awaited: a slow ntfy mustn't hold up the chat. Only the folder's name goes to ntfy.sh, never its path
    loop.run_in_executor(None, notice, f"{agent.capitalize()} started in {match.name}",
                         f"From a new chat, in {match.name}. It runs commands without asking; "
                         "`tmux attach -t henchline` to watch.")
    pid = await loop.run_in_executor(None, tmux.pane_pid, tab)
    await loop.run_in_executor(None, spawned, lambda alive: [*alive[-49:], {"pane": tab.address, "pid": pid}])
    log(HANDSFREE_LOG, f"started {agent} in {match} (tmux {tab.address}) from the phone")
    for _ in range(16):  # the pane alone is asked (cheap) until the agent runs in it, then the desktop once
        if await loop.run_in_executor(None, desktop().terminals.agent_running, tab):
            break
        await asyncio.sleep(0.5)
    # The pane is brand new: no record of that tab can be its agent (a pane id is reused after a tmux restart)
    for old in [s for s in recent_speakers() if s.tab == tab]:
        (SPEAKERS / f"{old.session}.json").unlink(missing_ok=True)
    await loop.run_in_executor(None, routing.discover_all)
    found = next((s for s in recent_speakers() if s.tab == tab), None)
    if found:
        return web.json_response({"session": found.session, **await loop.run_in_executor(None, agents)})
    return web.json_response({"note": f"{agent} started, but didn't show up: pull down to refresh."}, status=504)


# Words a Cursor model's listed name ends with that its /model picker doesn't use ("Claude Opus 5 1M Thinking" is
# "Claude Opus 5" there)
CURSOR_TRIM = {"low", "medium", "high", "extra", "fast", "thinking", "max", "1m", "500k", "300k", "(no", "zdr)"}


def project_of(target: Speaker) -> Path | None:
    """The folder an agent works in: Claude's from its session log, else its workspace folder under home."""
    if target.agent == "claude":
        uuid = target.session.removeprefix("claude-")
        for log_file in (Path.home() / ".claude/projects").glob(f"*/{uuid}.jsonl"):
            try:
                for entry in backwards(log_file, most=4 * 1024 * 1024):
                    if entry.get("cwd"):
                        return Path(entry["cwd"])
            except OSError:
                pass
    folder = Path.home() / target.where if target.where else None
    return folder if folder and folder.is_dir() else None


async def crew_commands(request: web.Request) -> web.Response:
    """The "/" list for the agent whose chat the phone shows (?session=): the phone's own actions, the agent's
    built-ins that work typed in, and the user's and its project's commands and skills."""
    trusted(request)
    session = request.query.get("session", "")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None:
        return web.json_response({"note": "That agent isn't known to the laptop."}, status=404)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, project_of, target)
    listed = await loop.run_in_executor(None, commands.listed, target.agent, project)
    if target.agent == "cursor" and not cursor_bridge.chat_of(session):  # the CLI: no /rename known in it
        listed = [c for c in listed if c["name"] != "rename"]
    return web.json_response({"agent": target.agent, "commands": listed})


async def crew_models(request: web.Request) -> web.Response:
    """The models an agent can start with (?agent=) or switch to (?session=), and whether a running one can switch."""
    trusted(request)
    loop = asyncio.get_running_loop()
    agent = request.query.get("agent", "")
    session = request.query.get("session", "")
    switchable = False
    if session:
        target = next((s for s in recent_speakers() if s.session == session), None)
        if target is None:
            return web.json_response({"note": "That agent isn't known to the laptop."}, status=404)
        agent = target.agent
        # Codex's /model only opens a picker; a Cursor editor chat switches through the bridge (Cursor's own
        # "Switch to Model Slug", found 2026-11-16 after its "cycle model" proved out of reach)
        switchable = ((agent in ("claude", "cursor") and bool(target.tab))
                      or bool(cursor_bridge.chat_of(target.session)))
        now = await loop.run_in_executor(None, model_in_use, target)
    else:
        now = ""
    if request.query.get("only") == "now":  # a chat opening: just what it is using, not the whole list
        label = await loop.run_in_executor(None, model_label, target) if session else ""
        mode = await loop.run_in_executor(None, plan_mode, target) if session else ""
        return web.json_response({"agent": agent, "switch": switchable, "now": now, "label": label, "mode": mode})
    agent_menu = await loop.run_in_executor(None, tmux.menu, agent)
    if agent_menu and (not session or agent == "claude"):  # Claude and Codex: the same menu as Cursor's
        current = {"model": claude_model_id(target) if session else "", "params": {}}
        if session and agent == "claude":  # its effort from its log; its context as the label guesses it
            context = claude_context(target)
            plain = {"1M": "1m"}.get(context, "standard" if context else "")
            current["params"] = {k: v for k, v in (("effort", claude_effort(target)), ("context", plain)) if v}
        return web.json_response({"agent": agent, "switch": switchable or not session, "now": now,
                                  "menu": agent_menu, "current": current})
    chat = cursor_bridge.chat_of(session) if session else ""
    if chat and target.in_editor:  # Cursor's own menu: models with their settings, and what the chat is on
        current = {"model": cursor_bridge.chat_model(chat),
                   "params": {p["id"]: str(p.get("value")) for p in cursor_bridge.params_of(chat)}}
        models = await loop.run_in_executor(None, cursor_bridge.catalog)
        shown = [m for m in models if m["on"] or m["id"] == current["model"]]
        return web.json_response({"agent": agent, "switch": True, "now": now, "menu": shown, "current": current})
    if session and agent == "cursor" and not target.in_editor:  # the CLI: the same menu, from its catalog
        slugs = [m["id"] for m in await loop.run_in_executor(None, tmux.models, "cursor")]
        shown = cursor_cli.menu(await loop.run_in_executor(None, cursor_bridge.catalog), slugs)
        if shown:
            current = await loop.run_in_executor(None, cursor_cli.saved, chat) if chat else {"model": "", "params": {}}
            if not current.get("model") and target.tab and target.tab.owner == tmux.OWNER:
                pane = await loop.run_in_executor(
                    None, lambda: tmux.out("capture-pane", "-p", "-t", target.tab.address).splitlines())
                current = cursor_cli.reading(cursor_model(pane), shown)
            return web.json_response({"agent": agent, "switch": switchable, "now": now, "menu": shown,
                                      "current": current})
    return web.json_response({"agent": agent, "switch": switchable, "now": now,
                              "models": await loop.run_in_executor(None, tmux.models, agent)})


def claude_context(target: Speaker) -> str:
    """The context a Claude session runs with, as Claude reported it to its status line ("1M", "200K"); "" when
    it hasn't (no status line set up, or not refreshed yet)."""
    uuid = target.session.removeprefix("claude-")
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return ""
    status = read_json(Path.home() / ".local/state/ptt/claude-status" / f"{uuid}.json")
    model = status.get("model") if isinstance(status, dict) else None
    shown = str((model or {}).get("display_name", "")) if isinstance(model, dict) else ""
    found = re.search(r"\((\d+[KM]) context\)", shown, re.I)
    if found:
        return found.group(1).upper()
    size = (status.get("context_window") or {}).get("context_window_size") if isinstance(status, dict) else None
    if not isinstance(size, int):
        return ""
    return f"{size // 1000}K" if size < 1_000_000 else "1M"


def claude_effort(target: Speaker) -> str:
    """The effort a Claude session runs at ("medium"): as Claude reports it to its status line, else as its last
    answer was asked for in its session log."""
    uuid = target.session.removeprefix("claude-")
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return ""
    reported = read_json(Path.home() / ".local/state/ptt/claude-status" / f"{uuid}.json").get("effort")
    if isinstance(reported, dict):
        reported = reported.get("level") or reported.get("value")
    if isinstance(reported, str) and reported:
        return reported
    for log_file in (Path.home() / ".claude/projects").glob(f"*/{uuid}.jsonl"):
        try:
            with log_file.open("rb") as f:
                f.seek(max(0, log_file.stat().st_size - 300_000))
                found = re.findall(rb'"output_config":\{"effort":"([a-z]+)"', f.read())
        except OSError:
            continue
        if found:
            return found[-1].decode()
    return ""


def plan_mode(target: Speaker) -> str:
    """"plan" when that agent is in plan mode, else "agent", from the agent's own record, never its screen (the
    user's rule, 2026-11-17: the screen's placeholder text read as plan mode): Claude writes its permissionMode on
    every message in its session log; the Cursor CLI keeps "mode" in its chat store. "" when it can't be known."""
    if target.agent == "claude":
        logged = _claude_logged(target)
        seen = _seen_claude(target)
        # The chip's press is remembered until the log, which Claude writes on its next message, says the same thing
        if seen and logged and (seen == "plan") == (logged == "plan"):
            _forget_claude(target)
            seen = ""
        return seen or logged
    meta = cursor_meta(target)
    return ("plan" if meta.get("mode") == "plan" else "agent") if meta.get("mode") else ""


def _claude_uuid(target: Speaker) -> str:
    uuid = target.session.removeprefix("claude-")
    return uuid if re.fullmatch(r"[0-9a-f-]{36}", uuid) else ""


def _claude_logged(target: Speaker) -> str:
    """"plan" or "agent" from the last permissionMode in Claude's session log, or "" when it can't be read."""
    uuid = _claude_uuid(target)
    logs = list((Path.home() / ".claude/projects").glob(f"*/{uuid}.jsonl")) if uuid else []
    if len(logs) != 1:
        return ""
    try:
        with logs[0].open("rb") as f:
            f.seek(max(0, logs[0].stat().st_size - 4 * 1024 * 1024))
            found = re.findall(rb'"permissionMode":"([A-Za-z]+)"', f.read())
    except OSError:
        return ""
    return ("plan" if found[-1] == b"plan" else "agent") if found else ""


def _seen_claude(target: Speaker) -> str:
    uuid = _claude_uuid(target)
    if not uuid:
        return ""
    try:
        word = (CLAUDE_MODE / uuid).read_text().strip()
    except OSError:
        return ""
    return word if word in {"plan", "agent"} else ""


def _remember_claude(target: Speaker, word: str) -> None:
    uuid = _claude_uuid(target)
    if not uuid:
        return
    CLAUDE_MODE.mkdir(parents=True, exist_ok=True)
    (CLAUDE_MODE / uuid).write_text("plan" if word == "plan" else "agent")


def _forget_claude(target: Speaker) -> None:
    uuid = _claude_uuid(target)
    if uuid:
        (CLAUDE_MODE / uuid).unlink(missing_ok=True)


def _presses_stopped(target: Speaker) -> bool:
    """Shift+Tab already landed somewhere the chip must not press again."""
    uuid = _claude_uuid(target)
    return bool(uuid) and (CLAUDE_MODE / f"{uuid}.stopped").is_file()


def _stop_presses(target: Speaker) -> None:
    uuid = _claude_uuid(target)
    if not uuid:
        return
    CLAUDE_MODE.mkdir(parents=True, exist_ok=True)
    (CLAUDE_MODE / f"{uuid}.stopped").write_text("stopped")


def _allow_presses(target: Speaker) -> None:
    uuid = _claude_uuid(target)
    if uuid:
        (CLAUDE_MODE / f"{uuid}.stopped").unlink(missing_ok=True)


def _footer(address: str) -> str:
    return commands.claude_mode_word(tmux.out("capture-pane", "-p", "-t", address))


def _watch_footer(address: str, before: str, seconds: float = 2.4) -> str:
    """The footer after a press, read until it moves, says bypass, or the wait is up. One quick read can miss it."""
    deadline = time.time() + seconds
    after = before
    while True:
        after = _footer(address)
        if after == "bypass" or (after and after != before) or time.time() >= deadline:
            return after
        time.sleep(0.3)


def _may_press_tab(target: Speaker) -> str:
    """"" when Shift+Tab is safe to try, otherwise the note. Refuses a command line or settings that can reach
    bypass permissions, and a project that can't be found (its settings would go unread)."""
    pid = desktop().terminals.agent_pid(target.tab) or target.pid
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes() if pid else b""
    except OSError:
        raw = b""
    project = project_of(target)
    if (not raw or project is None or commands.skips_permissions(raw)
            or commands.settings_skip_permissions(commands.claude_settings(project))):
        return "This Claude can reach bypass permissions, so the chip won't press Shift+Tab. Leave it with Build this."
    return ""


def leave_claude_plan(target: Speaker) -> tuple[str, str]:
    """One Shift+Tab out of plan mode. (mode, note). Refuses while Claude is busy, when its command line or its
    settings can reach bypass permissions, or when an earlier press already landed badly. The press is confirmed
    on the footer. A miss, or bypass, stops further presses: the chip stays on plan and does not send another."""
    if not target.tab or target.tab.owner != tmux.OWNER:
        return "plan", "Claude isn't in a terminal I can switch."
    if claude_working(target) is not False:
        return "plan", "Claude is busy. The chip waits until it is idle."
    address = target.tab.address
    if _presses_stopped(target):
        word = _footer(address)
        if word and word not in {"plan", "bypass"}:  # the user moved it in the terminal
            _allow_presses(target)
            _remember_claude(target, "agent")
            return "agent", ""
        return "plan", "The chip won't press Shift+Tab again. Switch it in Claude's terminal."
    why = _may_press_tab(target)
    if why:
        return "plan", why
    before = _footer(address)
    if before == "bypass":
        _stop_presses(target)
        log(HANDSFREE_LOG, "plan chip: Claude is on bypass permissions: not pressing")
        return "plan", "Claude is on bypass permissions, so the chip won't press Shift+Tab."
    tmux.tmux("send-keys", "-t", address, "BTab")
    verdict = commands.shift_tab_result(before, _watch_footer(address, before))
    if verdict == "stop":
        _stop_presses(target)
        log(HANDSFREE_LOG, "plan chip: Shift+Tab missed or landed on bypass: no further press")
        return "plan", "That press didn't leave plan mode cleanly, so I won't press Shift+Tab again. Switch it in Claude's terminal."
    _allow_presses(target)
    _remember_claude(target, "agent")
    return "agent", ""


def enter_claude_plan(target: Speaker) -> tuple[str, str]:
    """Ask Claude for plan mode with its own /plan. Only while it is idle, and only in a terminal. The chip
    remembers plan only once the footer says so, so the next tap cannot Shift+Tab out of some other mode."""
    mode = plan_mode(target) or "agent"
    if claude_working(target) is not False:
        return mode, "Claude is busy. The chip waits until it is idle."
    if not target.tab or target.tab.owner != tmux.OWNER:
        return mode, "Claude isn't in a terminal I can switch."
    if _presses_stopped(target):
        return mode, "The chip won't press keys in this Claude. Switch it in the terminal."
    address = target.tab.address
    before = _footer(address)
    if before == "bypass":
        _stop_presses(target)
        return mode, "Claude is on bypass permissions, so the chip won't type into it."
    if not desktop().terminals.type_line(target.tab, "/plan"):
        return mode, "Couldn't type /plan into Claude."
    verdict = commands.plan_command_result(_watch_footer(address, before))
    if verdict == "stop":
        _stop_presses(target)
        log(HANDSFREE_LOG, "plan chip: /plan showed bypass permissions: stopped")
        return mode, "That showed bypass permissions, so the chip won't press anything else. Switch it in Claude's terminal."
    if verdict != "plan":
        return mode, "Claude didn't switch to plan mode, so the chip was left as it was."
    _remember_claude(target, "plan")
    return "plan", "Plan mode on. Tap the chip again, once Claude is idle, to leave it."


def cursor_meta(target: Speaker) -> dict:
    """A Cursor CLI chat's own settings from its store, read only: mode (plan, debug, search, default),
    isRunEverything, approvalMode, …; {} when it can't be read."""
    chat = cursor_bridge.chat_of(target.session) if target.agent == "cursor" and not target.in_editor else ""
    stores = list((Path.home() / ".cursor/chats").glob(f"*/{chat}/store.db")) if chat else []
    if len(stores) != 1:
        return {}
    try:
        import sqlite3
        with contextlib.closing(sqlite3.connect(f"file:{stores[0]}?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("select value from meta where key = '0'").fetchone()
        raw = row[0] if row else ""
        meta = json.loads(bytes.fromhex(raw).decode() if isinstance(raw, str) and re.fullmatch(r"[0-9a-f]+", raw) else raw)
    except Exception:  # noqa: BLE001 - unknown, not guessed
        return {}
    return meta if isinstance(meta, dict) else {}


def switch_cursor_mode(target: Speaker, want: str) -> str:
    """The Cursor CLI into plan or default mode, one Shift+Tab at a time, each checked in its own store (tested
    2026-11-17: it cycles plan, debug, search, default and leaves Run Everything and approvals alone). Stops on
    the mode wanted, or at once if anything else in its settings moves. The mode it is in at the end."""
    if not target.tab or target.tab.owner != tmux.OWNER:
        return ""
    before = cursor_meta(target)
    if not before.get("mode"):
        return ""
    keep = {k: before.get(k) for k in ("isRunEverything", "approvalMode")}
    for _ in range(5):
        if before.get("mode") == want:
            break
        tmux.tmux("send-keys", "-t", target.tab.address, "BTab")
        time.sleep(1.0)
        now = cursor_meta(target)
        if not now.get("mode") or {k: now.get(k) for k in keep} != keep:
            log(HANDSFREE_LOG, f"plan chip: Cursor's settings moved or went unreadable ({now.get('mode')}): stopped")
            return str(now.get("mode") or "")
        if now.get("mode") == before.get("mode"):  # the press went elsewhere (a question on its screen): no more
            log(HANDSFREE_LOG, "plan chip: Cursor's mode didn't move with Shift+Tab: stopped")
            return str(now.get("mode"))
        before = now
    return str(before.get("mode") or "")


async def crew_plan(request: web.Request) -> web.Response:
    """The plan chip: {session} enters plan mode, or leaves it if the terminal is already planning. {"mode"}."""
    trusted(request)
    try:
        session = str((await request.json()).get("session", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None or target.agent not in ("claude", "cursor"):
        return web.json_response({"note": "Plan mode is there for Claude and Cursor."}, status=400)
    loop = asyncio.get_running_loop()
    known = await loop.run_in_executor(None, plan_mode, target)
    mode = known or "agent"
    if target.agent == "cursor" and not target.in_editor:  # each press checked in Cursor's own store
        want = "default" if mode == "plan" else "plan"
        got = await loop.run_in_executor(None, switch_cursor_mode, target, want)
        if got:
            note = "" if got == want else (f"Cursor is in {got} mode: it didn't move further, so finish what's on its "
                                           "screen first, or switch it in its terminal.")
            return web.json_response({"mode": "plan" if got == "plan" else "agent", **({"note": note} if note else {})})
    if target.agent == "claude":
        if not known:
            return web.json_response({"note": "I can't tell whether Claude is in plan mode."}, status=409)
        step = leave_claude_plan if mode == "plan" else enter_claude_plan
        got, note = await loop.run_in_executor(None, step, target)
        body = {"mode": got or mode or "agent"}
        if note:
            body["note"] = note
        return web.json_response(body)
    return web.json_response({"mode": mode, "note": "Plan mode can't be switched from the phone for this agent yet. "
                                                    "Type /plan, or press Shift+Tab in its terminal."}, status=409)


def model_label(target: Speaker) -> str:
    """The model pill's words: the model, its effort and its context, short ("Opus 5.5 · medium · 1M")."""
    if target.agent == "claude":
        model = claude_model_id(target)
        if not model:
            return ""
        name = next((m["name"] for m in tmux.menu("claude") if m["id"] == model), model)
        # Its context only as Claude itself reports it (its status line's model name says "(1M context)"; see
        # agents/claude-status.py): never guessed (the user's rule, 2026-11-16)
        return " · ".join(x for x in (name, claude_effort(target), claude_context(target)) if x)
    chat = cursor_bridge.chat_of(target.session) if target.in_editor else ""
    if chat:
        used = cursor_bridge.chat_model(chat)
        entry = next((m for m in cursor_bridge.catalog() if m["id"] == used), None)
        settings = {p["id"]: str(p.get("value")) for p in cursor_bridge.params_of(chat)}
        effort = settings.get("reasoning_effort") or settings.get("reasoning") or ""
        context = settings.get("context", "").upper()
        fast = "fast" if settings.get("fast") == "true" else ""
        return " · ".join(x for x in ((entry or {}).get("name", used), effort, context, fast) if x)
    if target.agent == "cursor" and not target.in_editor:
        got = cursor_cli.saved(cursor_bridge.chat_of(target.session))
        entry = next((m for m in cursor_bridge.catalog() if m["id"] == got.get("model")), None)
        return cursor_cli.described(entry, got.get("params") or {})
    return ""


def claude_model_id(target: Speaker) -> str:
    """The exact model a Claude session last answered with ("claude-opus-5-5"), from its session log."""
    uuid = target.session.removeprefix("claude-")
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return ""
    for log_file in (Path.home() / ".claude/projects").glob(f"*/{uuid}.jsonl"):
        with log_file.open("rb") as f:
            f.seek(max(0, log_file.stat().st_size - 2_000_000))
            tail = f.read().splitlines()
        for raw in reversed(tail):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            model = (entry.get("message") or {}).get("model", "") if isinstance(entry, dict) else ""
            if entry.get("type") == "assistant" and not entry.get("isSidechain") and model.startswith("claude-"):
                return re.sub(r"-\d{8}$", "", model)
    return ""


def cursor_model(lines: list[str]) -> str:
    """The model the Cursor CLI's status line shows ("Claude Opus 5.5 300K Medium   …   Run Everything")."""
    status = next((line.strip() for line in reversed(lines) if "Run Everything" in line or " · MAX" in line), "")
    return re.split(r"\s{2,}| · ", status)[0] if status else ""


def model_in_use(target: Speaker) -> str:
    """The model an agent is using now, when it can be seen: Claude's from its session log (any terminal), the
    Cursor CLI's and Codex's from their status line (tmux panes only: a Konsole tab's screen can't be read)."""
    if target.agent == "claude":
        uuid = target.session.removeprefix("claude-")
        if re.fullmatch(r"[0-9a-f-]{36}", uuid):
            for log_file in (Path.home() / ".claude/projects").glob(f"*/{uuid}.jsonl"):
                with log_file.open("rb") as f:
                    f.seek(max(0, log_file.stat().st_size - 2_000_000))
                    tail = f.read().splitlines()
                for raw in reversed(tail):  # the conversation's own last answer: not a side call (titles, subagents)
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    model = (entry.get("message") or {}).get("model", "") if isinstance(entry, dict) else ""
                    main_answer = entry.get("type") == "assistant" and not entry.get("isSidechain")
                    if main_answer and model.startswith("claude-"):
                        # claude-opus-5-5 → Opus 5.5; a dated id's date (…-20250929) left out
                        family, *version = [p for p in model.removeprefix("claude-").split("-")
                                            if not re.fullmatch(r"\d{8}", p)]
                        return f"{family.capitalize()} {'.'.join(version)}".strip()
        return ""
    if target.in_editor and (chat := cursor_bridge.chat_of(target.session)):  # Cursor's store: its id, named
        # Named as Cursor's own menu names it: the model, then its effort ("Grok 4.7 Extra High"), from its catalog
        used = cursor_bridge.chat_model(chat)
        settings = {p["id"]: str(p.get("value")) for p in cursor_bridge.params_of(chat)}
        entry = next((m for m in cursor_bridge.catalog() if m["id"] == used), None)
        if entry is None:
            return used
        effort = next((v["name"] for p in entry["params"] if p["id"] in ("reasoning_effort", "reasoning")
                       for v in p["values"] if v["value"] == settings.get(p["id"])), "")
        return f"{entry['name']} {effort}".strip()
    if target.agent == "cursor" and not target.in_editor:
        got = cursor_cli.saved(cursor_bridge.chat_of(target.session))
        if got.get("model"):
            entry = next((m for m in cursor_bridge.catalog() if m["id"] == got["model"]), None)
            return cursor_cli.described(entry, got.get("params") or {})
    if not target.tab or target.tab.owner != tmux.OWNER:
        return ""
    lines = tmux.out("capture-pane", "-p", "-t", target.tab.address).splitlines()
    if target.agent == "cursor":
        return cursor_model(lines)
    if target.agent == "codex":  # "gpt-5.6-sol xhigh · ~/pc_diagnostics"
        found = [m.group(1) for line in lines if (m := re.match(r"\s*(\S+(?: \S+)?) · [~/]", line))]
        return found[-1] if found else ""
    return ""


def switch_cursor_plain(target: Speaker, model_id: str) -> str:
    """A Cursor CLI chat onto a model with no settings, Auto included. Typed as `/model <slug>` in whatever
    terminal it is in (Konsole or tmux). "" when its own record then says that model."""
    chat = cursor_bridge.chat_of(target.session)
    if cursor_cli.on_model(chat, model_id):
        return ""
    terminals = desktop().terminals
    if not target.tab or not terminals.agent_running(target.tab):
        return "That agent isn't running."
    if not terminals.type_line(target.tab, f"/model {cursor_cli.cli_slug(model_id)}"):
        return "Couldn't type into it."
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if cursor_cli.on_model(chat, model_id):
            return ""
        time.sleep(0.4)
    return "Cursor didn't switch. If a model list is open in that terminal, press Escape."


def switch_claude(target: Speaker, model: str, settings: dict) -> str:
    """A running Claude to an exact model, its context and effort: its own /model and /effort typed in, then the
    user's defaults put back in ~/.claude/settings.json (both commands also save themselves as the default)."""
    terminals = desktop().terminals
    if not terminals.agent_running(target.tab):
        return "That agent isn't running."
    path = Path.home() / ".claude/settings.json"
    try:
        before = path.read_text() if path.exists() else ""
    except OSError:
        before = ""
    name = model + ("[1m]" if settings.get("context") == "1m" else "")
    if not terminals.type_line(target.tab, f"/model {name}"):
        return "Couldn't type into it."
    if settings.get("effort"):
        time.sleep(1.5)
        if not terminals.type_line(target.tab, f"/effort {settings['effort']}"):
            return "Switched the model, but couldn't set its effort."
    # Each saves itself as the default a moment later: watched until both have (or ten seconds), then put back
    def parsed(text: str) -> dict | None:
        try:
            found = json.loads(text) if text else {}
        except ValueError:
            return None  # empty or half-written right now
        return found if isinstance(found, dict) else None

    old = parsed(before)
    if not before.strip():  # no settings of the user's to put back: never an empty file written over Claude's
        return "Switched. Claude has no settings file of yours, so it may keep this as its default."
    if old is None:
        return "Switched, but Claude's settings couldn't be read before, so its defaults weren't checked."
    expected = {"model"} | ({"effortLevel", "effort", "modelSettings"} if settings.get("effort") else set())
    new: dict | None = old
    for _ in range(40):
        time.sleep(0.25)
        try:
            new = parsed(path.read_text() if path.exists() else "")
        except OSError:
            new = None
        if new is None:
            continue
        changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
        if "model" in changed and (not settings.get("effort") or changed & (expected - {"model"})):
            break
    if new is None:
        return "Switched, but Claude's settings couldn't be read after, so its defaults weren't checked."
    changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
    if not changed:
        return "Switched; Claude hasn't saved it as a default (yet), so there was nothing to put back."
    late = bool(settings.get("effort")) and not changed & {"effortLevel", "effort", "modelSettings"}
    if changed <= {"model", "effortLevel", "effort", "modelSettings"}:  # only what the two commands touch: put back
        aside = path.with_name(f"{path.name}.henchline-{os.getpid()}-{time.monotonic_ns()}")
        aside.write_text(before)
        os.chmod(aside, path.stat().st_mode & 0o777)
        aside.replace(path)
        if late:  # the effort's own save not seen yet: it may still land as the default
            return "Switched. Its model default was put back; its effort may still save itself as your default."
    else:
        return "Switched, but Claude's settings changed in other ways too, so its defaults were left as they are."
    return ""


def switch_model(target: Speaker, model: dict) -> str:
    """Types the agent's own /model into its terminal; "" when done, else why not."""
    if target.in_editor:  # see crew_model: Cursor editor chats switch by Cursor's catalog, not this list
        return "Pick it from Cursor's own model menu."
    terminals = desktop().terminals
    if not terminals.agent_running(target.tab):
        return "That agent isn't running."
    if target.agent == "claude":
        # Claude's /model also makes it the default for new sessions: the user's default is put back after
        settings = Path.home() / ".claude/settings.json"
        before = read_json(settings).get("model")
        if not terminals.type_line(target.tab, f"/model {model['id']}"):
            return "Couldn't type into it."
        written = None
        for _ in range(20):  # it writes the new default within a second or two; up to ten
            time.sleep(0.5)
            if read_json(settings).get("model") != before:
                written = read_json(settings).get("model")
                break
        if written is None:
            return ""
        # Only that one line goes back, read again right now, and only while it still holds what Claude just wrote:
        # anything else in the file (or a later change) is left as it is
        text = settings.read_text()
        line = f'"model": {json.dumps(written)}'
        if before is None or text.count(line) != 1:
            return (f"Switched, but Claude also made {model['name']} your default for new sessions "
                    "(change it with /model).")
        # Written aside (a name of its own), then swapped in: never half a file, never another restore's
        aside = settings.with_name(f"{settings.name}.henchline-{os.getpid()}-{time.monotonic_ns()}")
        aside.write_text(text.replace(line, f'"model": {json.dumps(before)}'))
        os.chmod(aside, settings.stat().st_mode & 0o777)
        aside.replace(settings)
        return ""
    if target.agent == "cursor":  # its picker, filtered by the model's own name, then Enter on the first match
        words = []
        for word in model["name"].split():
            if word.lower() in CURSOR_TRIM:
                break
            words.append(word)
        name = " ".join(words)
        # Its /model with a name switches at once to its best match (no list), or opens its list when none matches
        if not words or not terminals.type_line(target.tab, f"/model {name}"):
            return "Couldn't type into it."
        if target.tab.owner != tmux.OWNER:  # a Konsole tab: its screen can't be read back
            return f"Switched, to Cursor's best match for {name}."
        now = ""
        for _ in range(8):  # its status line can take a moment to show the new model
            time.sleep(0.5)
            lines = tmux.out("capture-pane", "-p", "-t", target.tab.address).splitlines()
            if any(line.strip().startswith("Filter:") for line in lines[-30:]):  # no match: its list is open
                tmux.tmux("send-keys", "-t", target.tab.address, "Escape")
                return f"Cursor has no model called {name}: left as it was."
            # Its status line shows the model in use ("Claude Opus 5.5 300K Medium   …   Run Everything")
            now = cursor_model(lines)
            if now and re.match(re.escape(name) + r"(\s|$)", now, re.I):
                return ""
        return f"Switched, but Cursor picked {now} (its best match for {name})." if now else ""
    return "Codex switches models only in its own picker: start a new chat with the model instead."


async def crew_model(request: web.Request) -> web.Response:
    """Switching a running agent's model mid-chat (the user's wish, 2026-11-16): {session, model}."""
    trusted(request)
    try:
        body = await request.json()
        session, wanted = str(body.get("session", "")), str(body.get("model", ""))
        settings = body.get("params") if isinstance(body.get("params"), list) else []
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None or not (target.tab or cursor_bridge.chat_of(target.session)):
        return web.json_response({"note": "Only an agent in a terminal or a Cursor chat can switch models from here."},
                                 status=400)
    loop = asyncio.get_running_loop()
    if target.in_editor:  # a Cursor editor chat: a model of Cursor's catalog and its settings, read back after
        why = await loop.run_in_executor(None, cursor_bridge.set_model, cursor_bridge.chat_of(target.session),
                                         wanted, settings)
        if why:
            return web.json_response({"note": why}, status=409)
        log(HANDSFREE_LOG, f"{target.spoken()} switched to {wanted} {settings} from the phone")
        return web.json_response({"ok": True, "name": wanted,
                                  "note": "Switched. Cursor also makes it the default for new chats."})
    if target.agent == "cursor" and not target.in_editor:
        entry = next((m for m in await loop.run_in_executor(None, cursor_bridge.catalog) if m["id"] == wanted), None)
        if entry is not None:
            picked = {str(p.get("id")): str(p.get("value")) for p in settings if isinstance(p, dict)}
            if entry.get("params"):
                if not cursor_cli.valid(entry, picked):
                    return web.json_response({"note": "Cursor has no such setting for that model."}, status=400)
                if not target.tab:
                    return web.json_response({"note": "That agent isn't running."}, status=400)
                why = await loop.run_in_executor(
                    None, cursor_cli.apply_tab, target.tab, entry, picked, cursor_bridge.chat_of(target.session))
            else:
                why = await loop.run_in_executor(None, switch_cursor_plain, target, wanted)
            if why and not why.startswith("Switched"):
                return web.json_response({"note": why}, status=409)
            log(HANDSFREE_LOG, f"{target.spoken()} switched to {wanted} {picked} from the phone")
            return web.json_response({"ok": True, "name": entry.get("name") or wanted,
                                      "note": why or "Switched. Cursor also makes it the default for new CLI chats."})
    if target.agent == "claude" and any(m["id"] == wanted for m in tmux.menu("claude")):
        chosen = tmux.checked("claude", wanted, {str(p.get("id")): str(p.get("value")) for p in settings
                                                  if isinstance(p, dict)})
        if chosen is None:
            return web.json_response({"note": "Claude has no such setting for that model."}, status=400)
        why = await loop.run_in_executor(None, switch_claude, target, wanted, chosen)
        if why and not why.startswith("Switched"):
            return web.json_response({"note": why}, status=409)
        log(HANDSFREE_LOG, f"{target.spoken()} switched to {wanted} {chosen} from the phone")
        return web.json_response({"ok": True, "name": wanted, "note": why})
    model = next((m for m in await loop.run_in_executor(None, tmux.models, target.agent) if m["id"] == wanted), None)
    if model is None:
        return web.json_response({"note": f"{target.agent} has no model {wanted}."}, status=400)
    why = await loop.run_in_executor(None, switch_model, target, model)
    if why.startswith("Switched,"):
        return web.json_response({"ok": True, "name": model["name"], "note": why})
    if why:
        return web.json_response({"note": why}, status=409)
    log(HANDSFREE_LOG, f"{target.spoken()} switched to {model['name']} from the phone")
    return web.json_response({"ok": True, "name": model["name"]})


def claude_thoughts(target: Speaker, since: str, most: int = 20) -> list[dict]:
    """A Claude session's thinking that it kept as text (summaries of its reasoning, not every step), from its
    session log, after `since` (an ISO time): [{at, text}], newest last."""
    uuid = target.session.removeprefix("claude-")
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return []
    found = []
    for log_file in (Path.home() / ".claude/projects").glob(f"*/{uuid}.jsonl"):
        try:
            with log_file.open("rb") as f:
                f.seek(max(0, log_file.stat().st_size - 2_000_000))
                tail = f.read().splitlines()
        except OSError:
            continue
        for raw in tail:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            at = str(entry.get("timestamp", "")) if isinstance(entry, dict) else ""
            if not isinstance(message, dict) or entry.get("isSidechain") or not at or at <= since:
                continue
            for part in message.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "thinking" and str(part.get("thinking", "")).strip():
                    found.append({"at": at, "text": str(part["thinking"]).strip()[:2000]})
    return found[-most:]


async def chat_thoughts(request: web.Request) -> web.Response:
    """A Cursor IDE chat's thinking for the phone's thought bubbles (the user's wish, 2026-11-16): ?session=&since=
    (an ISO time); {"thoughts": [{at, text}]}, empty for any other agent."""
    trusted(request)
    session = request.query.get("session", "")
    target = next((s for s in recent_speakers() if s.session == session), None)
    chat = cursor_bridge.chat_of(session) if target and target.in_editor else ""
    since = request.query.get("since", "")  # ISO times, compared as text: both stores write them the same way
    loop = asyncio.get_running_loop()
    if chat:
        found = await loop.run_in_executor(None, cursor_bridge.thoughts, chat, since)
    elif target and target.agent == "claude":  # Claude keeps some of its thinking as text in its session log
        found = await loop.run_in_executor(None, claude_thoughts, target, since)
    else:  # Codex keeps its reasoning encrypted: nothing to show
        found = []
    return web.json_response({"thoughts": found})


STALE = 15 * 60  # seconds without a word in its log: an unfinished turn may be a long quiet command, or killed


def backwards(log_file: Path, most: int = 64 * 1024 * 1024):
    """A log's JSON lines, newest first, read from its end a block at a time (Claude's lines can be megabytes:
    a request blob each), up to `most` bytes."""
    with log_file.open("rb") as f:
        end = f.seek(0, 2)
        tail, read = b"", 0
        while end > 0 and read < most:
            size = min(1024 * 1024, end)
            end -= size
            f.seek(end)
            block = f.read(size) + tail
            read += size
            lines = block.split(b"\n")
            tail = lines[0] if end > 0 else b""  # maybe cut: completed by the next block
            for raw in reversed(lines[1:] if end > 0 else lines):
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry


def claude_working(target: Speaker) -> bool | None:
    return claude_activity(target)[0]


TOOL_WORDS = {"Read": "reading", "Edit": "editing", "Write": "writing", "MultiEdit": "editing",
              "NotebookEdit": "editing", "Grep": "searching", "Glob": "looking for files",
              "WebFetch": "reading the web", "WebSearch": "searching the web", "Agent": "working with a helper",
              "Task": "working with a helper", "Skill": "using a skill", "TodoWrite": "planning"}


def tool_label(call: dict) -> str:
    """A few words for the phone on what a tool call does: a command's own description ("Run the unit tests"),
    else the tool's verb and the file's name ("editing routing.py"). Never the command itself, nor a path's
    folders: the phone shows it as it is."""
    name = str(call.get("name", ""))
    given = call.get("input") if isinstance(call.get("input"), dict) else {}
    if name in ("Bash", "PowerShell"):
        words = str(given.get("description") or "running a command")
    else:
        words = TOOL_WORDS.get(name, f"using {name}" if re.fullmatch(r"[A-Za-z_]{1,30}", name) else "using a tool")
        path = given.get("file_path") or given.get("notebook_path")
        if isinstance(path, str) and path and name in TOOL_WORDS:
            words += " " + Path(path).name
    words = " ".join(words.split())
    return words[:60] + ("…" if len(words) > 60 else "")


def claude_activity(target: Speaker) -> tuple[bool | None, str, str]:
    """Is that Claude session in the middle of a turn? Its session log, newest line first, up to the first that
    tells: a turn's end (Claude writes turn_duration), a reply that ended, an interruption: no; a prompt, a tool
    call or its result: yes. None when its log isn't found, or mid-turn but quiet for STALE (a long command, or
    killed: the phone then guesses). A permission prompt or a question to you reads as working: the log can't
    tell them apart. With it, what it is doing: "tool" (a tool it called is running) or "thinking" (the model is
    thinking or writing: after your prompt or a tool's result), and for a tool, a few words on it."""
    uuid = target.session.removeprefix("claude-")
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return None, "", ""
    logs = list((Path.home() / ".claude/projects").glob(f"*/{uuid}.jsonl"))
    if len(logs) != 1:
        return None, "", ""
    try:
        quiet = time.time() - logs[0].stat().st_mtime > STALE
        for entry in backwards(logs[0]):
            if entry.get("isSidechain"):
                continue
            kind, message = entry.get("type"), entry.get("message")
            if kind == "system" and entry.get("subtype") in ("turn_duration", "stop_hook_summary"):
                return False, "", ""
            if kind == "assistant" and isinstance(message, dict) and message.get("stop_reason"):
                # A tool call waiting on its result: working (or waiting on you, at a permission prompt or a
                # question: the log can't tell); after a long silence, not known
                if message.get("stop_reason") != "tool_use":
                    return False, "", ""
                calls = [p for p in message.get("content") or [] if isinstance(p, dict) and p.get("type") == "tool_use"]
                label = tool_label(calls[0]) if calls else ""
                if len(calls) > 1:
                    label += f" (+{len(calls) - 1} more)"
                return (None, "", "") if quiet else (True, "tool", label)
            if kind == "user" and isinstance(message, dict) and not entry.get("isMeta") \
                    and not entry.get("isCompactSummary"):
                content = message.get("content")
                text = content if isinstance(content, str) else " ".join(
                    str(p.get("text", "")) for p in content or [] if isinstance(p, dict) and p.get("type") == "text")
                if "[Request interrupted by user" in text:
                    return False, "", ""
                # A slash command's or the shell mode's own lines are no turn of the model
                if not text.lstrip().startswith(("<local-command", "<command-name>", "<bash-")):
                    return (None, "", "") if quiet else (True, "thinking", "")
    except OSError:
        return None, "", ""
    return False, "", ""


def codex_working(target: Speaker) -> bool | None:
    """Is that Codex session working? Its newest session log for that folder (~/.codex/sessions), newest line
    first: a task started (yes) or completed or aborted (no). None when no log is found, or a task started but
    the log has been quiet for STALE (a long command, or killed: the phone then guesses). Two sessions in one
    folder: the newest log speaks for both."""
    folder = target.session.removeprefix("codex-")
    found = None

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:  # gone since it was listed
            return 0.0

    for log_file in sorted((Path.home() / ".codex/sessions").glob("*/*/*/rollout-*.jsonl"),
                           key=mtime, reverse=True)[:30]:
        try:
            with log_file.open("rb") as f:
                meta = json.loads(f.readline(1024 * 1024) or b"{}")  # its first line carries all its instructions
        except (OSError, ValueError):
            continue
        cwd = str((meta.get("payload") or {}).get("cwd", "")) if isinstance(meta, dict) else ""
        if cwd and re.sub(r"[^A-Za-z0-9_.-]+", "_", cwd)[-120:] == folder[-120:]:
            found = log_file
            break
    if found is None:
        return None
    try:
        quiet = time.time() - found.stat().st_mtime > STALE
        for entry in backwards(found):
            event = (entry.get("payload") or {}).get("type") if entry.get("type") == "event_msg" else None
            if event == "task_started":
                return None if quiet else True
            if event in ("task_complete", "turn_aborted"):
                return False
    except OSError:
        return None
    return False


def agent_working(target: Speaker) -> bool | None:
    """Is that agent working right now (Cursor's page or store, Claude's or Codex's log); None: it gives no sign."""
    if target.in_editor and cursor_bridge.chat_of(target.session):
        return cursor_bridge.working(cursor_bridge.chat_of(target.session), careful=False)
    if target.agent == "claude":
        return claude_working(target)
    if target.agent == "codex":
        return codex_working(target)
    return None


async def chat_working(request: web.Request) -> web.Response:
    """Is the agent of the chat on the phone working right now, for its "working…" line (the user's wish,
    2026-11-16: the terminal shows it at once, the phone showed nothing): ?session=; {"working": true | false |
    null, "doing": "tool" | "thinking" | "", "label": "editing routing.py" | ""}, null when that agent gives no
    sign (the phone then guesses as before). "tool": a tool it called is running, with a few words on it (Claude
    only; the others say "thinking" throughout)."""
    trusted(request)
    session = request.query.get("session", "")
    target = next((s for s in recent_speakers() if s.session == session), None)
    loop = asyncio.get_running_loop()
    working, doing, label = None, "thinking", ""
    if target and target.in_editor and cursor_bridge.chat_of(session):
        working = await loop.run_in_executor(None, cursor_bridge.working, cursor_bridge.chat_of(session), False)
    elif target and target.agent == "claude":
        working, doing, label = await loop.run_in_executor(None, claude_activity, target)
    elif target and target.agent == "codex":
        working = await loop.run_in_executor(None, codex_working, target)
    with PICKUP_LOCK:
        watched = any(v[0] == session for v in WAITING.values()) or any(v[0] == session for v in TAKEN.values())
    taken = await loop.run_in_executor(None, taken_in, session) if watched else {}
    # "taken_at": when each was taken in, so only the agent's lines after it count as its answer (blue)
    # A workflow running in that Claude session: the strip at the bottom of its chat
    flow = await loop.run_in_executor(None, workflows.live, session) if target and target.agent == "claude" else None
    return web.json_response({"working": working, "doing": doing if working else "", "label": label if working else "",
                              "taken": list(taken), "taken_at": taken, "workflow": flow})


async def crew_stop(request: web.Request) -> web.Response:
    """The phone's stop button: the agent stops what it's doing, as Escape in its terminal (or Cursor's own stop for
    an editor chat, through the bridge). {session}"""
    trusted(request)
    try:
        session = str((await request.json()).get("session", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None:
        return web.json_response({"note": "That agent isn't known to the laptop."}, status=404)
    loop = asyncio.get_running_loop()
    if target.in_editor:
        chat = cursor_bridge.chat_of(target.session)
        # Cursor's stop on an idle chat rejects all its pending edits: only a chat that is working is stopped
        if chat and not await loop.run_in_executor(None, cursor_bridge.working, chat):
            return web.json_response({"note": "That chat isn't working on anything, so nothing was stopped "
                                              "(its pending edits are left as they are)."}, status=409)
        stopped = bool(chat) and await loop.run_in_executor(None, cursor_bridge.ask_selected, chat, {"stop": chat})
        if not stopped and cursor_bridge.last_why == "not working":
            return web.json_response({"note": "That chat had just finished, so nothing was stopped "
                                              "(its pending edits are left as they are)."}, status=409)
        if not stopped and cursor_bridge.last_why == "not a chat id":
            return web.json_response({"note": "Cursor runs an older Henchline bridge: Reload Window in Cursor."},
                                     status=409)
        if not stopped and cursor_bridge.last_why == "another chat is selected":
            return web.json_response({"note": "Another chat is selected in Cursor, so nothing was stopped."},
                                     status=409)
    else:
        terminals = desktop().terminals
        running = await loop.run_in_executor(None, terminals.agent_running, target.tab)
        stopped = running and await loop.run_in_executor(None, terminals.interrupt, target.tab)
    if not stopped:
        return web.json_response({"note": "Couldn't stop it from here."}, status=409)
    log(HANDSFREE_LOG, f"stopped {target.spoken()} from the phone")
    return web.json_response({"ok": True})


async def crew_end(request: web.Request) -> web.Response:
    """Ending an agent from the phone (the long-press menu, after asking): only one Henchline started itself, in
    its tmux pane. Agents the user started never are."""
    trusted(request)
    try:
        session = str((await request.json()).get("session", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target is None or target.tab.owner != tmux.OWNER or target.tab.address not in spawned():
        return web.json_response({"note": "Only agents started from the phone can be ended from it."}, status=403)
    await asyncio.get_running_loop().run_in_executor(None, tmux.end, target.tab)
    await asyncio.get_running_loop().run_in_executor(
        None, spawned, lambda alive: [e for e in alive if e.get("pane") != target.tab.address])
    # Every name it went by: the same agent in the same pane may be known under its older key too (a Cursor CLI
    # before its chat id was used: ending one name left the other in the list, 2026-11-16)
    for other in (Speaker.load(p) for p in SPEAKERS.glob("*.json")):  # every record, however old
        if other and (other.session == session or (other.tab == target.tab and other.agent == target.agent)):
            (SPEAKERS / f"{other.session}.json").unlink(missing_ok=True)
    log(HANDSFREE_LOG, f"ended {target.spoken()} (tmux {target.tab.address}) from the phone")
    return web.json_response({"ok": True})


async def text_status(request: web.Request) -> web.Response:
    """The phone saying how far agents' texts got: {"ids": [...], "status": "delivered" | "read"}. Only ever
    forward (a read text isn't made delivered again), only texts this laptop sent."""
    trusted(request)
    try:
        body = await request.json()
        ids, status = [str(i) for i in body.get("ids", [])], str(body.get("status", ""))
    except (ValueError, AttributeError, TypeError):
        raise web.HTTPBadRequest(text="expected JSON")
    if status not in ("delivered", "read"):
        raise web.HTTPBadRequest(text="status: delivered or read")
    order = ["sent", "delivered", "read"]
    for tid in ids:
        if not re.fullmatch(r"[0-9a-f]{16}", tid):
            continue
        record = read_json(TEXTS / f"{tid}.json")
        if record and order.index(status) > order.index(record.get("status", "sent")):
            write_json(TEXTS / f"{tid}.json", {**record, "status": status, f"{status}_at": time.time()}, private=True)
    return web.json_response({"ok": True})


async def texts_pending(request: web.Request) -> web.Response:
    """Agents' texts the phone hasn't confirmed yet, so the app fetches them itself (2026-11-18: with the phone in
    hand on mobile data, pushes sat on the ntfy server for half an hour while the ntfy app's connection had
    silently stalled). The last two days, oldest first: {"texts": [{id, agent, session, text}]}."""
    trusted(request)
    found = []
    for path in TEXTS.glob("*.json"):
        record = read_json(path)
        if (record.get("status") == "sent" and record.get("words") and time.time() - record.get("at", 0) < 2 * 86400
                and re.fullmatch(r"[0-9a-f]{16}", str(record.get("id", "")))):
            found.append(record)
    found.sort(key=lambda r: r.get("at", 0))
    return web.json_response({"texts": [{"id": r["id"], "agent": r.get("agent", ""), "session": r.get("session", ""),
                                         "text": r["words"]} for r in found[-50:]]})


VOICE_NOTE_MAX = 20 * 1024 * 1024  # bytes of one voice message: about ten minutes at 16 kHz


async def voice_note(request: web.Request) -> web.Response:
    """A voice message recorded in Henchline off a call (a WAV, the chat's agent session in the query): kept for
    hands-free to transcribe and type to that agent. Its id, to ask after it."""
    trusted(request)
    session = request.query.get("session", "")
    # Only an agent hands-free knows (as /text): the session is never made into a path
    if not any(s.session == session for s in recent_speakers()):
        return web.json_response({"note": "That agent isn't known to the laptop."}, status=400)
    VOICE_NOTES.mkdir(parents=True, exist_ok=True)
    if sum(1 for m in VOICE_NOTES.glob("*.json") if read_json(m).get("status") in ("waiting", "transcribing")) >= 5:
        return web.json_response({"note": "Too many voice messages waiting."}, status=429)
    nid = secrets.token_hex(8)
    wav = VOICE_NOTES / f"{nid}.wav"
    size = 0
    with wav.open("wb") as out:
        while chunk := await request.content.read(1 << 16):
            size += len(chunk)
            if size > VOICE_NOTE_MAX:
                out.close()
                wav.unlink(missing_ok=True)
                return web.json_response({"note": "Too long."}, status=413)
            out.write(chunk)
    os.chmod(wav, 0o600)
    write_json(VOICE_NOTES / f"{nid}.json", {"id": nid, "session": session, "status": "waiting", "at": time.time()},
               private=True)
    return web.json_response({"id": nid})


async def voice_note_status(request: web.Request) -> web.Response:
    """How a voice message is getting on: waiting, done (with what was heard) or failed (with why)."""
    trusted(request)
    nid = request.match_info["nid"]
    if not re.fullmatch(r"[0-9a-f]{16}", nid):
        raise web.HTTPNotFound()
    meta = read_json(VOICE_NOTES / f"{nid}.json")
    if not meta:
        raise web.HTTPNotFound()
    if meta.get("status") == "waiting" and time.time() - meta.get("at", 0) > VOICE_NOTE_STALE:
        # Hands-free never got to it (off, or down): dropped now, so it is never typed after the phone gave up
        meta.update(status="failed", note="Not sent: the laptop didn't transcribe it in time.")
        write_json(VOICE_NOTES / f"{nid}.json", meta, private=True)
        (VOICE_NOTES / f"{nid}.wav").unlink(missing_ok=True)
    return web.json_response({k: meta.get(k, "") for k in ("status", "text", "note")})


async def sent_file(request: web.Request) -> web.StreamResponse:
    """A file an agent sent (`handsfree send`): only its secret folder name finds it."""
    path = files.find(request.match_info["secret"], request.match_info["name"])
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers=files.headers(path))


LIVE: list[web.WebSocketResponse] = []  # this server's call connection, while one is up


async def call(request: web.Request) -> web.WebSocketResponse:
    trusted(request)
    ws = web.WebSocketResponse(heartbeat=15, timeout=2)  # closing a dropped one doesn't wait 10 s for a reply
    await ws.prepare(request)
    # The phone back after its connection dropped (walking about, Wi-Fi to mobile data; the user's catch,
    # 2026-11-16): the old connection may not have noticed yet, so this one takes over, with the same agent
    resume = request.query.get("resume") == "1"
    if resume and LIVE and in_call():
        log(HANDSFREE_LOG, "call: the phone reconnected: taking over from the dropped connection")
        await LIVE[-1].close(message=b"replaced by the phone reconnecting")
        for _ in range(50):
            if not in_call():
                break
            await asyncio.sleep(0.1)
    if in_call():
        await ws.close(message=b"busy: another call is up")
        return ws
    with Devices():
        mic = await asyncio.create_subprocess_exec(
            "pacat", "--playback", "--raw", f"--device={PHONE_MIC}", "--format=s16le", f"--rate={MIC_RATE}",
            "--channels=1", "--latency-msec=60", "--client-name=phone-call", stdin=asyncio.subprocess.PIPE)
        speaker = await asyncio.create_subprocess_exec(
            "parec", "--raw", f"--device={PHONE_SPEAKER_MONITOR}", "--format=s16le", f"--rate={SPEAKER_RATE}",
            "--channels=1", "--latency-msec=40", "--client-name=phone-call", stdout=asyncio.subprocess.PIPE)
        CALL_HANGUP.unlink(missing_ok=True)  # a hang-up asked for when no call was up is not for this one
        CALL_INTERRUPT.unlink(missing_ok=True)
        if not resume:
            if CALL_CHECK.exists():  # a wake-up, or a ring that needs an answer: ask after them if they go quiet
                CALL_CHECK.unlink(missing_ok=True)
                CALL_STAY_ON.unlink(missing_ok=True)
            else:
                CALL_STAY_ON.touch()  # the usual call stays up while the agent works
        if not resume:  # a call back after a drop goes on as it was: same agent, etiquette already told
            CALL_PINNED.unlink(missing_ok=True)  # the last call's pick: this call starts with whoever spoke last
            for told in CALL_TOLD.glob("*"):  # this call reminds each agent afresh how to be on a call
                told.unlink(missing_ok=True)
        if not resume:
            snaps.clear()  # photos taken on an earlier call
        blank_words()  # words a killed server never blanked
        for stale in CALL_MIRROR.glob("*.json"):  # terminal lines from before this call
            stale.unlink(missing_ok=True)
        write_json(CALL, {"from": request.remote or "", "pid": os.getpid(),
                          "cmdline": Path("/proc/self/cmdline").read_text()})
        await asyncio.get_running_loop().run_in_executor(None, turn_on_handsfree, False)  # with talkback
        log(HANDSFREE_LOG, "phone call resumed" if resume else "phone call started")
        LIVE.append(ws)

        async def to_phone() -> None:
            while not ws.closed:
                data = await speaker.stdout.read(CHUNK)
                if not data:
                    return
                await ws.send_bytes(data)

        def held(clip: Path) -> bool:  # waiting for the user's answer: it won't play now, so it isn't waited for
            return spool.is_held(str(read_json(clip).get("session", "")))

        async def show_status() -> None:
            status, shown = Status(), None
            listed, shown_agents = 0.0, None
            asked = 0.0  # when a hang-up was asked for
            owed: set[str] = set()  # the clips queued or playing then: only those are waited for
            quiet_since = 0.0  # when those were last all said
            while not ws.closed:
                if CALL_HANGUP.exists():  # the user said "hang up", or an agent ran `handsfree hangup`
                    if not asked:
                        asked = time.monotonic()
                        playing = Playing.now()
                        owed = {p.name for p in SPOOL.glob("*.json") if not held(p)} | ({playing.clip} if playing else set())
                    waited = time.monotonic() - asked
                    # Clips queued after the hang-up was asked (an agent's next update) don't keep the call up: the
                    # user asked to go (the review, 2026-11-17: it could talk on for 2 min). A goodbye queued within
                    # HANGUP_SAY is still said
                    if waited <= HANGUP_SAY:
                        owed |= {p.name for p in SPOOL.glob("*.json") if not held(p)}
                    playing = Playing.now()
                    quiet = not (playing and playing.clip in owed) and not any(p.name in owed for p in SPOOL.glob("*.json"))
                    quiet_since = (quiet_since or time.monotonic()) if quiet else 0.0
                    settled = quiet_since and time.monotonic() - quiet_since >= HANGUP_QUIET
                    if (waited > HANGUP_SAY and settled) or waited > HANGUP_MAX:
                        CALL_HANGUP.unlink(missing_ok=True)
                        hung_up_here.append(True)
                        log(HANDSFREE_LOG, "call: hung up from the laptop")
                        await ws.close(message=b"Hung up from the laptop")
                        return
                if time.monotonic() - listed > AGENTS_EVERY:  # the menu: who is reachable, who is current
                    listed = time.monotonic()
                    now_agents = await asyncio.get_running_loop().run_in_executor(None, agents)
                    if now_agents != shown_agents:
                        await ws.send_json(now_agents)
                        shown_agents = now_agents
                for event in status.events():
                    await ws.send_json(event)
                now = status.now()
                if now != shown:
                    await ws.send_json(now)
                    shown = now
                await asyncio.sleep(STATUS_EVERY)

        call_back: list[Speaker] = []  # set when a quiet call is hung up: rung again once it has ended
        hung_up_here: list[bool] = []  # the laptop closed it (not the user hanging up first)
        try:  # this call answers a ring-back: if it goes quiet too, it just ends (one ring-back per chain)
            answering_ring_back = time.time() - CALL_RANG_BACK.stat().st_mtime < 600
        except OSError:
            answering_ring_back = False

        async def nudge() -> None:
            """The user's turn and silence: asked after, twice, in the agent's own voice; then hung up and rung
            back (once: a ring-back's own call just ends). Anything of theirs (words, typing) starts it all again;
            a clip (the nudge's own too) or the agent at work only restarts the wait."""
            loop = asyncio.get_running_loop()

            def theirs() -> tuple:  # moves with every turn of their speech and every message of theirs delivered
                return read_json(PHASE).get("at", 0), read_json(SENT).get("at", 0)

            quiet_since, nudged, heard = time.monotonic(), 0, theirs()
            while not ws.closed:
                await asyncio.sleep(2)
                target = last_speaker()
                now_heard = theirs()
                if now_heard != heard or phase_now()[0] is not Phase.IDLE:
                    heard, quiet_since, nudged = now_heard, time.monotonic(), 0
                    continue
                # Stay on (the user's wish, 2026-11-17: on the line while the agent works, outdoors, for as long as
                # they like): never asked after, never hung up on
                if CALL_STAY_ON.exists():
                    quiet_since, nudged = time.monotonic(), 0  # switched off later: asked after from the first nudge
                    continue
                if Playing.now() is not None or any(SPOOL.glob("*.json")):
                    quiet_since = time.monotonic()
                    continue
                if target is None or time.monotonic() - quiet_since < NUDGE_AFTER:
                    continue  # not yet: the agent isn't even asked whether it works (Cursor's page costs a visit)
                working = await loop.run_in_executor(None, agent_working, target)
                if working:
                    quiet_since = time.monotonic()
                    continue
                if working is None and time.monotonic() - quiet_since < NUDGE_UNSURE:
                    continue
                if nudged < len(NUDGES):
                    log(HANDSFREE_LOG, f"call: quiet, nudging ({nudged + 1})")
                    # A Henchline notice in the agent's own voice: never "Claude here, from …" and never a change to
                    # the agent's record (said as the agent, it was introduced as a new one, from the call
                    # server's folder, 2026-11-16)
                    words, voice = NUDGES[nudged], AGENT_VOICES.get(target.agent, DEFAULT_VOICE)
                    await loop.run_in_executor(None, lambda: subprocess.run(
                        [str(routing.SAY), "--agent", BY_HANDSFREE, "--session", BY_HANDSFREE, "--voice", voice, words],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10))
                    nudged += 1
                    quiet_since = time.monotonic()
                    continue
                if answering_ring_back:
                    log(HANDSFREE_LOG, "call: quiet again after a ring-back: hanging up, no ring-back")
                else:
                    log(HANDSFREE_LOG, "call: quiet after two nudges: hanging up to call back")
                    call_back.append(target)
                CALL_HANGUP.touch()
                return

        last_audio = [time.monotonic()]

        async def watch_line() -> None:
            """The phone's mic arrives all the time: when it stops for 3 s the line has dropped, and talkback waits
            instead of speaking to nobody (the user's worry, 2026-11-17). It starts again when the phone is back,
            or, if the call ends, the waiting words go to the phone as texts."""
            while not ws.closed:
                await asyncio.sleep(0.5)
                if time.monotonic() - last_audio[0] > 3:  # an earbud switch can pause it 1-3 s
                    CALL_SILENT.touch()
                else:
                    CALL_SILENT.unlink(missing_ok=True)

        sender = asyncio.create_task(to_phone())
        liner = asyncio.create_task(watch_line())
        shower = asyncio.create_task(show_status())
        unmuter = asyncio.create_task(keep_unmuted())
        nudger = asyncio.create_task(nudge())
        heard = 0  # bytes of the phone's mic, for the log
        ear = Ear()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    last_audio[0] = time.monotonic()
                    heard += len(msg.data)
                    mic.stdin.write(msg.data)
                    await mic.stdin.drain()
                elif msg.type == WSMsgType.TEXT and (note := await from_page(msg.json(), ear)):
                    await ws.send_str(note)  # only a refused typed line has one
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if ws in LIVE:
                LIVE.remove(ws)
            CALL_PUSH.unlink(missing_ok=True)
            CALL_BUTTON_ONLY.unlink(missing_ok=True)  # the next call starts with an open line again
            CALL_STAY_ON.unlink(missing_ok=True)  # and is asked after when it goes quiet
            CALL_SILENT.unlink(missing_ok=True)
            liner.cancel()
            CALL_HANGUP.unlink(missing_ok=True)  # the phone went first: the hang-up is moot
            sender.cancel()
            shower.cancel()
            unmuter.cancel()
            nudger.cancel()
            CALL.unlink(missing_ok=True)  # hands-free and talkback go back to the laptop first
            for target in call_back if hung_up_here else []:  # hung up for quiet (by us, not them): once
                CALL_RANG_BACK.touch()
                subprocess.Popen([sys.executable, str(HERE.parent / "handsfree/handsfree.py"), "ring", "--now", "--as",
                                  target.session,
                                  "You went quiet on our call. Calling you back."],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            blank_words()  # the call's words don't outlast it
            for proc in (mic, speaker):
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:  # gone already: the service being stopped kills the whole group
                        pass
                await proc.wait()
            log(HANDSFREE_LOG, f"phone call ended ({heard / 2 / MIC_RATE:.0f} s of the phone's mic arrived; "
                               f"{ear.summary()})")
    return ws


def serve(address: str, port: int) -> bool:
    """Put the page behind `tailscale serve` (https on this machine's Tailscale name, a real certificate).

    The proxy points at the Tailscale address, not 127.0.0.1, so the page's own socket stays where it was:
    reachable from your devices (and, as any address of this machine, from programs on it), and nothing
    more. False when Tailscale won't (`sudo tailscale set --operator=$USER` lets this user configure it,
    once) or its https root already serves something else, which is left alone.
    """
    target = f"http://{address}:{port}"
    status = subprocess.run(["tailscale", "serve", "status"], capture_output=True, text=True).stdout
    root = re.search(r"\|-- / proxy (\S+)", status)
    if root and root.group(1) != target:
        log(HANDSFREE_LOG, f"tailscale serve already proxies {root.group(1)}: left alone, own certificate instead")
        return False
    done = subprocess.run(["tailscale", "serve", "--bg", "--https=443", target], capture_output=True, text=True)
    if done.returncode != 0:
        log(HANDSFREE_LOG, f"tailscale serve refused ({done.stderr.strip()[:120]}): own certificate instead")
        return False
    return True


REMIND_EVERY = 10.0  # seconds between looks for due reminders


async def watch_workflows(app: web.Application) -> None:
    """A Claude chat's workflow that ends (done or failed) is texted to the phone once, from that agent: the phone
    shows no banner when that chat is already on screen (the design review, 2026-11-17)."""
    loop = asyncio.get_running_loop()
    seen: dict[str, str] = {}  # run id → its state when last looked at
    first = True
    while True:
        try:
            claudes = [s for s in recent_speakers() if s.agent == "claude" and time.time() - s.at < 2 * 86400]
            looked: set[str] = set()
            for speaker in claudes:
                for run in await loop.run_in_executor(None, workflows.runs, speaker.session, 2):
                    was, now = seen.get(run["id"]), run["state"]
                    looked.add(run["id"])
                    seen[run["id"]] = now
                    # Only a real end (its end record) is told: a quiet agent can leave the journal unchanged for
                    # half an hour, which reads as "stopped" and isn't one
                    if first or was not in ("running", "stopped") or now not in ("done", "failed"):
                        continue
                    failed = sum(a["status"] == "failed" for p in run["phases"] for a in p["agents"])
                    words = (f"⚙ Workflow {run['name']} {'failed' if now == 'failed' else 'finished'}: "
                             f"{run['done']}/{run['total']} agents done" + (f", {failed} failed" if failed else "")
                             + ". /workflow shows it.")
                    await loop.run_in_executor(None, lambda w=words, s=speaker.session: subprocess.run(
                        [sys.executable, str(HERE.parent / "handsfree/handsfree.py"), "text", "--as", s, w],
                        capture_output=True, timeout=30, env={**os.environ, "TALKBACK_AGENT": "claude"}))
                    log(HANDSFREE_LOG, f"workflow {run['id']} ended ({now}): told the phone")
            for gone in [k for k in seen if k not in looked]:  # no longer among the newest: forgotten
                del seen[gone]
            first = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the loop must outlive any one run's files
            log(HANDSFREE_LOG, f"workflows: {exc}")
        await asyncio.sleep(10)


async def deliver_reminders(app: web.Application) -> None:
    """Due reminders typed into their agents, as a message from the phone is (paste, a pause, then Enter): tried
    until delivered, for an hour; meanwhile the laptop kept awake for those that asked."""
    loop = asyncio.get_running_loop()
    awake: list = [None]  # the sleep inhibitor, while one is wanted
    while True:
        try:
            for item in reminders.due():
                late = time.time() - item["at"]
                got, note = await loop.run_in_executor(
                    None, to_agent, item["words"], item["session"], Heard.SCHEDULED, False)
                if got:
                    reminders.settle(item["id"], "delivered", f"to {got}" + (f", {late / 60:.0f} min late" if late > 120 else ""))
                    log(HANDSFREE_LOG, f"reminder {item['id']} delivered to {got}" + (f" ({late:.0f} s late)" if late > 30 else ""))
                elif late > reminders.RETRY_FOR:
                    reminders.settle(item["id"], "failed", note)
                    log(HANDSFREE_LOG, f"reminder {item['id']} given up: {note}")
                else:
                    reminders.later(item["id"])
                    log(HANDSFREE_LOG, f"reminder {item['id']} not delivered yet ({note}): trying again later")
            until = reminders.keep_awake_until()
            if until > time.time() and awake[0] is None:
                awake[0] = await asyncio.create_subprocess_exec(
                    "systemd-inhibit", "--what=sleep:idle", "--who=Henchline", "--why=A scheduled message is due",
                    "--mode=block", "sleep", "infinity", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                log(HANDSFREE_LOG, "keeping the laptop awake for a scheduled message")
            elif until <= time.time() and awake[0] is not None:
                awake[0].terminate()
                awake[0] = None
                log(HANDSFREE_LOG, "no scheduled message needs the laptop awake any more")
        except asyncio.CancelledError:  # the server stopping: the laptop may sleep again
            if awake[0] is not None:
                awake[0].terminate()
            raise
        except Exception as exc:  # the loop must outlive any one reminder
            log(HANDSFREE_LOG, f"reminders: {exc}")
        try:
            await asyncio.sleep(REMIND_EVERY)
        except asyncio.CancelledError:
            if awake[0] is not None:
                awake[0].terminate()
            raise


async def start_reminders(app: web.Application) -> None:
    app["reminders"] = asyncio.create_task(deliver_reminders(app))
    app["workflows"] = asyncio.create_task(watch_workflows(app))


async def stop_reminders(app: web.Application) -> None:
    app["reminders"].cancel()
    app["workflows"].cancel()


async def reminders_list(request: web.Request) -> web.Response:
    """The reminders for an agent's chat (?session=), soonest first: pending, delivered, failed, cancelled."""
    trusted(request)
    return web.json_response({"reminders": reminders.listed(request.query.get("session", ""))})


async def reminders_add(request: web.Request) -> web.Response:
    """The phone's /later: {session, words, at: seconds since the epoch, or "08:30" / "in 20m", keep_awake}."""
    trusted(request)
    try:
        body = await request.json()
        session, words = str(body.get("session", "")), str(body.get("words", "")).strip()
        at = body.get("at")
        at = float(at) if isinstance(at, (int, float)) else reminders.when(str(at or ""))
    except (ValueError, AttributeError) as exc:
        return web.json_response({"note": f"Not scheduled: {exc}"}, status=400)
    if not session or not words:
        return web.json_response({"note": "Nothing to schedule, or no agent named."}, status=400)
    item = reminders.add(at, session, words, "phone", bool(body.get("keep_awake")))
    log(HANDSFREE_LOG, f"reminder {item['id']} set from the phone for {time.strftime('%Y-%m-%d %H:%M', time.localtime(at))}")
    return web.json_response({"reminder": item})


async def reminders_cancel(request: web.Request) -> web.Response:
    trusted(request)
    try:
        rid = str((await request.json()).get("id", ""))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="expected JSON")
    return web.json_response({"ok": reminders.cancel(rid)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=CALL_PORT)
    parser.add_argument("--cert", type=Path, help="serve https with this certificate (and --key): a self-signed one\n"
                        "works when `tailscale serve` is not available; the phone accepts it once")
    parser.add_argument("--key", type=Path)
    parser.add_argument("--host", default="serve",
                        help="serve (default): behind `tailscale serve`, at https://<this machine's Tailscale name>/ "
                             "with a real certificate, falling back to `tailscale` when this user may not configure "
                             "Tailscale; tailscale: this machine's Tailscale address, with its own certificate. "
                             "Either way the socket is on the Tailscale address: your devices reach it")
    args = parser.parse_args()
    if in_call():  # another server has a call up: starting would repoint the proxy away from it
        print("a call is up on another call server; not starting", file=sys.stderr)
        return 1
    CALL.unlink(missing_ok=True)  # leftovers from a crash: the marker, a hang-up never done, the call's sinks
    CALL_HANGUP.unlink(missing_ok=True)
    for line in pactl("list", "short", "modules").splitlines():
        found = re.search(r"sink_name=(\S+)", line)
        if "module-null-sink" in line and found and found.group(1) in (PHONE_MIC, PHONE_SPEAKER):
            pactl("unload-module", line.split()[0])
    app = web.Application()
    app.router.add_get("/", lambda _: web.FileResponse(HERE / "page.html"))
    app.router.add_get("/call", call)
    app.router.add_post("/share/file", share_file)
    app.router.add_post("/share/tell", share_tell)
    app.router.add_post("/share/snap", share_snap)
    app.router.add_post("/text", texted)
    app.router.add_post("/text-status", text_status)
    app.router.add_get("/texts/pending", texts_pending)
    app.router.add_post("/voice-note", voice_note)
    app.router.add_get("/voice-note/{nid}", voice_note_status)
    app.router.add_post("/crew", crew)  # it registers what it finds: a POST
    app.router.add_get("/crew/folders", crew_folders)
    app.router.add_get("/crew/cursor-chats", cursor_chats)
    app.router.add_get("/crew/usage", crew_usage)
    app.router.add_post("/crew/usage/tell", crew_usage_tell)
    app.router.add_get("/usage", usage_overview)
    app.router.add_get("/chat/workflows", chat_workflows)
    app.router.add_post("/away", away)
    app.router.add_get("/away", away_state)
    app.router.add_post("/ring-endpoint", ring_endpoint)
    app.router.add_get("/voices", voices_state)
    app.router.add_post("/voices", voices_save)
    app.router.add_post("/voices/preview", voices_preview)
    app.router.add_get("/show/windows", show_windows)
    app.router.add_post("/show", show_send)
    app.router.add_get("/chat/plan", chat_plan)
    app.router.add_post("/chat/plan/build", chat_plan_build)
    app.router.add_post("/crew/cursor-chats/open", cursor_chat_open)
    app.router.add_post("/crew/new", crew_new)
    app.router.add_post("/crew/end", crew_end)
    app.router.add_post("/crew/stop", crew_stop)
    app.router.add_get("/chat/thoughts", chat_thoughts)
    app.router.add_get("/chat/working", chat_working)
    app.router.add_get("/crew/models", crew_models)
    app.router.add_post("/crew/plan", crew_plan)
    app.router.add_get("/crew/commands", crew_commands)
    app.router.add_get("/reminders", reminders_list)
    app.router.add_post("/reminders", reminders_add)
    app.router.add_post("/reminders/cancel", reminders_cancel)
    app.on_startup.append(start_reminders)
    app.on_cleanup.append(stop_reminders)
    app.router.add_post("/crew/model", crew_model)
    app.router.add_get("/files/{secret}/{name}", sent_file)
    proxied = False
    if args.host in ("serve", "tailscale"):
        try:
            found = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.split()
        except OSError:
            found = []
        if not found:  # the page is only for your own devices: no tailnet, no page
            print("Tailscale has no address (or is not installed): sudo tailscale up", file=sys.stderr)
            return 1
        mode, args.host = args.host, found[0]
        proxied = mode == "serve" and args.cert is None and serve(args.host, args.port)
    if args.cert is None and not proxied:
        if not (CALL_TLS / "cert.pem").exists():  # plain http: the phone's browser would refuse the mic
            print(f"no certificate in {CALL_TLS} and no tailscale serve: ./install.sh call", file=sys.stderr)
            return 1
        args.cert, args.key = CALL_TLS / "cert.pem", CALL_TLS / "key.pem"
    tls = None
    if args.cert:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(args.cert, args.key)
    web.run_app(app, host=args.host, port=args.port, ssl_context=tls, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
