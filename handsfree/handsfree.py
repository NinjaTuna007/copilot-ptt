#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""Hands-free conversation with the talkback agents: no keys.

    handsfree on | off | status        (handsfree run is the service itself)
    handsfree switch AGENT [WORKSPACE] hand the user over to that agent, announced
    handsfree listen [SECONDS]         keep listening that long (default 180)
    handsfree close                    stop listening until the wake word
    handsfree hangup                   end the phone call (call/), once talkback has finished speaking
    handsfree ring [--done] [--now] [--no-wait] [--check] [--as SESSION] [WHY]
                                       ring the user's phone (Henchline full-screen, else an ntfy notification
                                       whose Answer opens Henchline) and wait up to a minute for what happened:
                                       exit 0 answered, 2 rang but not answered, 3 declined, 4 answered but the
                                       call couldn't reach the laptop, 5 never reached the phone, 1 not rung.
                                       Unless answered, a missed call with WHY is left in ntfy (the topic is
                                       read from ~/.config/ptt/ntfy-topic). --now: past the 2-minute limit,
                                       for a ring the user asked for. --no-wait: ring and return. --done: the
                                       ring reports finished work. --check: the call asks after them if they
                                       go quiet (a wake-up, or an answer is needed). The usual call stays on.
                                       --as SESSION: ring as
                                       that agent (the call server calling back after a quiet call)
    handsfree text [--until-read MIN | --until-replied MIN] WORDS
                                       a message to the user's phone, not a call: a notification they can
                                       reply to from the notification itself (the reply comes back tagged
                                       [phone · text]); on a call it goes to the chat.
                                       Prints its id. --until-read MIN: wait that long for the user to read
                                       it, then ring if they haven't; --until-replied MIN: the same, until
                                       they answer it (any message of theirs to this agent after it). Exit 0
                                       sent (or read, or answered), 1 not
    handsfree text-status ID            sent, delivered (on the phone) or read (opened, or replied to)
    handsfree remind [--keep-awake] [--to "AGENT [in WORKSPACE]"] TIME WORDS
                                       type WORDS into an agent at TIME (08:30, tomorrow 08:30,
                                       2026-11-18 08:30, in 20m), tagged [henchline · scheduled]: yourself by
                                       default, or the agent named. The call server delivers it, through the
                                       same path as a phone message, even to Cursor. --keep-awake: the laptop
                                       doesn't sleep by itself until it is delivered (a wake-up call); a
                                       closed lid still suspends it. Prints its id
    handsfree remind list | cancel ID   the reminders set, and taking one back
    handsfree send FILE [FILE ...] [WORDS]
                                       show the user files on their phone: in the call's chat while a call
                                       is up, else a Henchline notification (an ntfy one if Henchline can't
                                       be pushed) that saves and opens it. Any type; kept 7 days. Exit 0 sent,
                                       1 not
    handsfree show [--screen | --window 0xID] [WORDS]
                                       "show me": a picture of this terminal, one window, or the whole screen
                                       tmux's own text, works with the screen locked), or with --screen the whole
                                       screen (also when it isn't in tmux), sent as `handsfree send` does

The built-in mic runs through the echo canceller (sensing.py). The listener
(listener.py) records a turn when the user talks after a clip, over a clip,
after the wake word or an earbud tap, and routing.py types it, tagged with
how it was heard, into the agent that spoke last.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE / "core"), str(HERE.parent)]  # hands-free's modules (core/), and the repository for voice/

import routing  # noqa: E402
import tray  # noqa: E402
from listener import Listener  # noqa: E402
from sensing import EC_UNIT, start_canceller  # noqa: E402
from voice import files  # noqa: E402
from voice.speech import whisper  # noqa: E402
from voice.state import (ANNOUNCE, AWAY, BY_HANDSFREE, CALL_CHECK, CALL_HANGUP, CALL_MIRROR, CALL_ORIGIN, CALL_RING, CALL_RINGER,  # noqa: E402
                         CHAT_KEY, CLOSE, HANDSFREE_LOG, HANDSFREE_ON, TEXTS, VOICE_NOTE_STALE, VOICE_NOTES, read_json,
                         recent_speakers,
                         LISTEN, SPEAKERS, Speaker, in_call, log,
                         release_phase, turn_on_handsfree, write_json)
from voice.agents import identity  # noqa: E402
from voice.state import HANDSFREE_UNIT as UNIT  # noqa: E402


RING_TOPIC = Path.home() / ".config/ptt/ntfy-topic"  # the user's own ntfy topic: never printed or logged
RING_SERVER = "https://ntfy.sh"
RING_EVERY = 120  # seconds: an agent can't keep ringing
RING_WAIT = 60  # seconds a ring waits for the answer: it rings for 30, and on a locked phone Answer connects
               # only once it is unlocked
RANG = Path.home() / ".local/state/ptt/call.rang"  # when the user was last rung


def ring(why: str, now: bool = False, wait: bool = True, done: bool = False, as_session: str = "",
         check: bool = False, urgent: bool = False) -> int:
    """An agent wants to talk: the phone rings (Henchline full-screen, or an ntfy notification whose Answer
    opens Henchline's call), then this waits for what happened. Henchline reports back on a one-time ntfy
    address carried in the ring (it works off Tailscale too):

        0 answered            2 rang, not answered       3 declined
        4 answered, but the call couldn't reach the laptop (the phone off Tailscale?)
        5 never reached the phone (offline?)             1 not rung (refused, or no way to ring)

    Without Henchline's push address (the ntfy notification), nothing reports back: no answer is 2. Unless
    answered, a "missed call" with the reason is left in ntfy, which the phone shows when it is online."""
    if in_call():
        print("a phone call is up already")
        return 1
    if AWAY.exists() and not now and not urgent:  # the user can't take a call: the agent learns it here
        print("not rung: the user is away and can't take a call. Text them instead (handsfree text \"…\"). "
              "If it truly can't wait, ring with --urgent.")
        return 6
    if check:  # this ring needs them: the call asks after them if they go quiet. The usual call stays on.
        CALL_CHECK.parent.mkdir(parents=True, exist_ok=True)
        CALL_CHECK.touch()
    if not now and RANG.exists() and time.time() - RANG.stat().st_mtime < RING_EVERY:
        print(f"rang less than {RING_EVERY} s ago; not again yet (--now if the user asked for it)")
        return 1
    try:  # only for the ntfy fallback and the missed call: Henchline's push address works without it
        topic = RING_TOPIC.read_text().strip()
    except OSError:
        topic = ""
    speaker = Speaker.load(SPEAKERS / f"{as_session}.json") if as_session else caller(register=True)
    if as_session and speaker is None:
        print(f"no agent {as_session} known to ring as")
        return 1
    who = (speaker.agent.capitalize() if speaker else "Your crew").encode("ascii", "ignore").decode()  # headers
    reply = f"henchline-{secrets.token_urlsafe(16)}"  # this ring's own, used once
    since = int(time.time()) - 1  # before the push: the phone may say "rang" before the push call returns
    pushed = ring_henchline(speaker.agent if speaker else "handsfree", why, f"{RING_SERVER}/{reply}", done)
    if not (pushed or topic and ntfy(topic, f"{who} is calling", why or "Tap Answer to talk.",
                                     {"Priority": "high", "Tags": "telephone", "Click": "henchline://call",
                                      "Actions": "view, Answer, henchline://call, clear=true"})):
        print("couldn't ring" + ("" if topic else f": no Henchline push address, and no ntfy topic in {RING_TOPIC}"))
        return 1
    RANG.parent.mkdir(parents=True, exist_ok=True)
    RANG.touch()
    if speaker:  # whenever the user answers, even after this stops waiting: the call server routes it there
        write_json(CALL_RINGER, {"session": speaker.session, "agent": speaker.agent, "reply": reply,
                                 "at": time.time()}, private=True)  # the reply name is the ring's secret
    log(HANDSFREE_LOG, f"rang the user ({who})")
    print("rang", flush=True)
    if not wait:
        return 0
    heard: set[str] = set()
    deadline = time.time() + RING_WAIT
    while time.time() < deadline:
        if in_call():  # the call server has routed the user's words to the caller (CALL_RINGER)
            return ring_ended(who, "answered", 0)
        if pushed:
            heard |= replies(reply, since)
            if "declined" in heard:  # they saw it and said no: no missed call to remind them
                return ring_ended(who, "declined", 3)
            if "unreachable" in heard:
                return ring_ended(who, "answered, but the call couldn't reach the laptop (the phone off Tailscale?)",
                                  4, topic, why)
            if "missed" in heard:
                break
        time.sleep(2)
    if pushed and "rang" not in heard and "missed" not in heard:
        return ring_ended(who, "never reached the phone (offline?)", 5, topic, why)
    return ring_ended(who, "rang, not answered", 2, topic, why)


def caller(register: bool = False) -> Speaker | None:
    """The agent running `handsfree ring`: its henchman, its name, and where the call goes. Not whoever
    spoke last (a Cursor ring was announced and answered as Claude, 2026-11-15)."""
    agent, session = identity()
    known = Speaker.load(SPEAKERS / f"{session}.json")
    if known and known.tab:  # this very agent is calling: from its own tab now (resumed in another, say), its process
        from voice.desktop import current as desktop
        here = desktop().terminals.tab_of_caller()
        moved = bool(here) and here != known.tab
        if moved:
            known.tab, known.pid, known.born = here, 0, 0
        if known.pin() or moved:
            known.save(SPEAKERS / f"{session}.json")
    if known or agent == "agent":
        return known
    # Only a ring makes its caller known; a text or a file doesn't put it in the crew. Except a Cursor editor chat:
    # its own key comes only from its chat id, and the phone's reply must find it (and its window) again
    if not register and not CHAT_KEY.search(session):
        return Speaker(session, agent)
    # Never heard through talkback: found now, as talkback would (its workspace, its terminal tab), and kept, so an
    # answered ring can be put through to it (a Cursor ring was routed to Claude, 2026-11-15). Not made the one
    # spoken words go to: that is still whoever spoke last, until the ring is answered
    from voice.agents import agent_where
    from voice.desktop import current as desktop
    speaker = Speaker(session, agent, agent_where(), desktop().terminals.tab_of_caller())
    speaker.pin()
    speaker.save(SPEAKERS / f"{session}.json")
    speaker.replaces_found()  # found running from a new chat before it texted: that chat follows it
    return speaker


def ring_ended(who: str, what: str, code: int, topic: str = "", why: str = "") -> int:
    """Says how the ring went (and logs it); unless answered, leaves a missed call in ntfy."""
    if code and topic:
        ntfy(topic, f"Missed call from {who}", why or "They wanted to talk.", {"Tags": "telephone_receiver"})
    log(HANDSFREE_LOG, f"{who}'s call: {what}")
    print(what + (" (a missed call was left in ntfy)" if code and topic else ""))
    return code


def send(args: list[str]) -> int:
    """Files for the user's phone, from the agent running this: `handsfree send FILE … [WORDS]` (the leading
    arguments that are files are the files; the rest are the words that come with them)."""
    paths = []
    while args and Path(args[0]).expanduser().is_file():
        paths.append(Path(args.pop(0)).expanduser())
    if refused := [p for p in paths if files.private(p)]:
        print(f"not sent: {', '.join(map(str, refused))} looks like keys or credentials, which stay on the laptop")
        return 1
    if not paths:
        print("usage: handsfree send FILE [FILE ...] [WORDS]  (no such file: " + (args[0] if args else "none given") + ")")
        return 1
    words = " ".join(args)
    speaker = caller()
    agent, session = (speaker.agent, speaker.session) if speaker else identity()
    metas = [files.post(p, words if i == len(paths) - 1 else "", agent, session) for i, p in enumerate(paths)]
    if in_call():  # the call server shows them in the chat within a moment
        print(f"sent to the call's chat: {', '.join(m['name'] for m in metas)}")
        log(HANDSFREE_LOG, f"{agent} sent {len(metas)} file(s) to the call's chat")
        return 0
    origin = CALL_ORIGIN.read_text().strip() if CALL_ORIGIN.exists() else ""
    if not origin:
        print("no call has connected yet, so the phone's address for files isn't known: call once, then send")
        return 1
    pushed = 0
    for meta in metas:
        url = origin + files.link(meta)
        body = {"agent": agent, "session": session, "why": meta["text"], "file": {"url": url, "name": meta["name"],
                                                             "type": meta["type"], "size": meta["size"]}}
        if CALL_RING.exists() and push(json.dumps(body).encode()):
            files.delivered(meta)
            pushed += 1
    if pushed < len(metas):  # kept for the next call's chat; ntfy only says so, the link (a secret) stays here
        topic = RING_TOPIC.read_text().strip() if RING_TOPIC.exists() else ""
        if topic:
            ntfy(topic, f"{agent.capitalize()} sent you {len(metas) - pushed} file(s)",
                 "They'll be in the chat when you next call from Henchline (kept a week).", {"Tags": "paperclip"})
    log(HANDSFREE_LOG, f"{agent} sent {len(metas)} file(s), {pushed} pushed to the phone")
    print(f"pushed {pushed} of {len(metas)} to the phone as notifications"
          + ("; the rest show in the chat on the next call" if pushed < len(metas) else ""))
    return 0 if pushed == len(metas) else 2


def show(args: list[str]) -> int:
    """A picture of what the agent running this sees, into the call's chat (or the phone): its own terminal pane,
    one window (`--window 0xID`, its own pixels, so a locked screen still shows that window), or `--screen` for
    the whole screen. The rest of the arguments are the words that go with it."""
    from voice import screenshot
    whole = "--screen" in args
    wid = ""
    if "--window" in args:
        at = args.index("--window")
        if at + 1 >= len(args):
            print("usage: handsfree show [--screen | --window 0xID] [WORDS]")
            return 1
        wid = args[at + 1]
        args = args[:at] + args[at + 2:]
    words = [a for a in args if a != "--screen"]
    if any(a.startswith("--") for a in words):
        print("usage: handsfree show [--screen | --window 0xID] [WORDS]")
        return 1
    if wid:
        match = next((item for item in screenshot.windows() if item["id"] == wid), None)
        label = (match["title"] or match["app"]) if match else "a window"
        path, why = screenshot.take("window", wid, of=label)
    elif whole:
        path, why = screenshot.take("screen", of="the whole screen")
    else:
        speaker = caller()
        tab = speaker.tab if speaker and speaker.tab and speaker.tab.owner == "tmux" else None
        pane = tab.address if tab else os.environ.get("TMUX_PANE", "")
        path, why = screenshot.take("pane", pane=pane)
        if path is None and not pane:  # not in tmux: the screen, as before
            path, why = screenshot.take("screen")
    if path is None:
        print(why)
        return 1
    return send([str(path), *words])


def taken_as(args: list[str]) -> tuple[list[str], str | None]:
    """`--as SESSION` out of the arguments: on that agent's behalf (talkback's own worker, for one). None: bad."""
    if "--as" not in args:
        return args, ""
    at = args.index("--as")
    if at + 1 >= len(args):
        print("--as needs an agent's session")
        return args, None
    session = args[at + 1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", session) or ".." in session:
        print("--as needs an agent's session (letters, digits, - _ .)")
        return args, None
    return args[:at] + args[at + 2:], session


def text(words: str, until_read: float = 0, until: str = "read", as_session: str = "") -> int:
    """A message for the user's phone from the agent running this: a notification with a reply box, or a line
    in the call's chat when a call is up. Its id is printed: `handsfree text-status ID` says how far it got,
    the way messaging apps do it (sent, delivered: on the phone, read: the user opened that chat or replied). until_read:
    minutes to wait for it to be read (until="read"), or answered (until="replied": any message of the user's to
    this agent after it), and then ring if it wasn't: a text first, a call when it goes unanswered (the user's
    idea, 2026-11-16)."""
    if not words.strip():
        print("usage: handsfree text [--until-read MINUTES | --until-replied MINUTES] WORDS")
        return 1
    if as_session:
        agent, session = as_session.split("-", 1)[0], as_session
    else:
        speaker = caller()
        agent, session = (speaker.agent, speaker.session) if speaker else identity()
    tid = secrets.token_hex(8)
    # The words kept too (private): the app fetches any text whose push never reached it (2026-11-18). Records
    # older than a week go, so the folder (read on every catch-up) stays small
    for old in TEXTS.glob("*.json"):
        try:
            if time.time() - old.stat().st_mtime > 7 * 86400:
                old.unlink()
        except OSError:
            pass
    write_json(TEXTS / f"{tid}.json", {"id": tid, "agent": agent, "session": session, "status": "sent",
                                       "at": time.time(), "words": words}, private=True)
    sent = False
    if in_call():  # the chat shows it within a moment
        write_json(CALL_MIRROR / f"{time.time_ns()}.json", {"session": session, "from": agent.capitalize(),
                                                            "text": words, "tid": tid}, private=True)
        print(f"sent to the call's chat (id {tid})")
        sent = True
    elif CALL_RING.exists() and push(json.dumps({"agent": agent, "session": session, "text": words,
                                                  "id": tid}).encode(),
                                      {"Priority": "high", "Tags": "speech_balloon"}):
        log(HANDSFREE_LOG, f"{agent} texted the phone")
        print(f"sent to the phone (id {tid})")
        sent = True
        subprocess.Popen(  # not a thread: this command must return now, and the backup still runs after 20 s
            [sys.executable, str(Path(__file__).resolve()), "text-backup", tid],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sent:
        if until_read <= 0:
            return 0
        deadline = time.time() + until_read * 60
        while time.time() < deadline:
            if until == "read" and text_status(tid) == "read" or until == "replied" and text_replied(tid):
                print(until)
                return 0
            time.sleep(5)
        print(f"not {until} in {until_read:g} min: ringing")
        return ring(words, now=False, wait=True)
    topic = RING_TOPIC.read_text().strip() if RING_TOPIC.exists() else ""
    if topic and ntfy(topic, f"{agent.capitalize()}", words, {"Tags": "speech_balloon"}):
        print("sent as an ntfy notification (no reply box: Henchline couldn't be pushed)")
        return 0
    print("couldn't reach the phone")
    return 1


def text_replied(tid: str) -> bool:
    """Has the user answered that text: any message of theirs to its agent since (the call server marks it)."""
    return bool(read_json(TEXTS / f"{tid}.json").get("replied")) if tid else False


def text_status(tid: str) -> str:
    """sent, delivered (it reached the phone) or read (the user opened that chat, or replied); "" if unknown."""
    return str(read_json(TEXTS / f"{tid}.json").get("status", "")) if tid else ""


def _backup_text(tid: str) -> None:
    """The app push can be accepted and still never shown (the phone asleep, or its address stale). If the phone
    hasn't confirmed the text, say it as a normal notification too. Run as its own process: the text command
    itself must return at once."""
    time.sleep(20)  # the app fetches unconfirmed texts every 15 s; sooner, a text about to be confirmed is also pushed
    record = read_json(TEXTS / f"{tid}.json")
    if record.get("status") != "sent":
        return
    topic = RING_TOPIC.read_text().strip() if RING_TOPIC.exists() else ""
    agent = str(record.get("agent") or "Henchline")
    if topic and ntfy(topic, agent.capitalize(), str(record.get("words") or ""),
                      {"Tags": "speech_balloon", "Priority": "high"}):
        log(HANDSFREE_LOG, f"{agent} text not confirmed by the phone: sent the backup notification")


def push(body: bytes, headers: dict | None = None) -> bool:
    """Henchline's own push address (the ntfy app wakes it): never printed."""
    try:
        urllib.request.urlopen(urllib.request.Request(CALL_RING.read_text().strip(), data=body,
                                                      headers={"Content-Type": "application/json", **(headers or {})}),
                               timeout=10).close()
        return True
    except (OSError, ValueError) as exc:
        print(f"Henchline's push address failed ({exc.__class__.__name__})")
        return False


def replies(reply: str, since: int) -> set[str]:
    """What Henchline has posted to this ring's reply address so far: rang, declined, missed, unreachable."""
    try:
        with urllib.request.urlopen(f"{RING_SERVER}/{reply}/json?poll=1&since={since}", timeout=10) as answer:
            events = [json.loads(line) for line in answer.read().decode().splitlines() if line.strip()]
    except (OSError, ValueError):
        return set()
    return {e.get("message", "").strip() for e in events if e.get("event") == "message"}


def ring_henchline(agent: str, why: str, reply: str, done: bool = False) -> bool:
    """Henchline's own push address, if it gave one: a full-screen call with the agent's henchman and tone,
    which posts what happens to it (rang, declined, missed, unreachable) to `reply`."""
    if not CALL_RING.exists():
        return False
    if push(json.dumps({"agent": agent, "why": why, "reply": reply, "kind": "done" if done else "need"}).encode()):
        return True
    print("an ntfy notification instead")
    return False


def ntfy(topic: str, title: str, message: str, headers: dict[str, str]) -> bool:
    """A notification on the user's phone; the topic is never printed."""
    try:
        urllib.request.urlopen(urllib.request.Request(f"{RING_SERVER}/{topic}", data=message.encode(),
                                                      headers={"Title": title, **headers}), timeout=10).close()
        return True
    except (OSError, ValueError) as exc:
        print(f"ntfy failed: {exc.__class__.__name__}")
        return False


def run() -> int:
    # A desktop-restored copy, on top of the service. It would be a second listener and a second headset.
    if any(a == "-session" or a.startswith("-session=") for a in sys.argv[1:]):
        return 0
    cfg = whisper.Config.load(HANDSFREE_LOG)
    canceller_since = start_canceller()
    transcriber = whisper.Transcriber(cfg, HANDSFREE_LOG)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    listener = Listener(cfg, transcriber, stop, canceller_since)
    threading.Thread(target=listener.run, daemon=True).start()
    threading.Thread(target=voice_notes, args=(transcriber, stop), daemon=True).start()
    log(HANDSFREE_LOG, "on")
    if ANNOUNCE.exists():  # only when the user turned it on, not on every restart
        ANNOUNCE.unlink(missing_ok=True)
        routing.say("Hands-free is on.")
    try:
        tray.show(listener, stop)
    finally:
        stop.set()
        release_phase(BY_HANDSFREE)
        HANDSFREE_ON.unlink(missing_ok=True)
        log(HANDSFREE_LOG, "off")
    return 1 if listener.crashed else 0  # non-zero: systemd starts it again


def voice_notes(transcriber: whisper.Transcriber, stop: threading.Event) -> None:
    """Voice messages recorded in Henchline off a call (the call server keeps them in VOICE_NOTES): each one
    transcribed with hands-free's own Whisper and typed into the agent whose chat it was recorded in, tagged
    [phone · voice message]; what was heard, or why not, goes back for the phone to show."""
    while not stop.wait(1.0):
        for meta_path in sorted(VOICE_NOTES.glob("*.json")):
            try:  # one bad note never stops the rest
                voice_note(transcriber, meta_path)
            except Exception as exc:
                log(HANDSFREE_LOG, f"voice message {meta_path.stem} failed: {exc!r}")
                write_json(meta_path, {**read_json(meta_path), "status": "failed", "note": "The laptop failed on it."},
                           private=True)
                meta_path.with_suffix(".wav").unlink(missing_ok=True)


def voice_note(transcriber: whisper.Transcriber, meta_path: Path) -> None:
    meta = read_json(meta_path)
    age = time.time() - meta.get("at", 0)
    wav = meta_path.with_suffix(".wav")
    if meta.get("status") != "waiting":
        if age > 86400:  # the phone has long since asked: gone after a day
            meta_path.unlink(missing_ok=True)
        return
    if age > VOICE_NOTE_STALE:  # the phone has given up on it: never typed late
        late = "Not sent: the laptop didn't transcribe it in time."
        write_json(meta_path, {**meta, "status": "failed", "note": late}, private=True)
        wav.unlink(missing_ok=True)
        return
    # Taken: the call server's status check no longer drops it as stale while it is transcribed
    write_json(meta_path, {**meta, "status": "transcribing"}, private=True)
    try:
        plain, rich = transcriber.file(wav)
    except Exception as exc:  # a bad file, or Whisper failing: said, not left waiting
        plain, rich = "", ""
        log(HANDSFREE_LOG, f"voice message not transcribed: {exc}")
    # By equality with an agent hands-free knows, as the call server checks: the session never becomes a path
    target = next((s for s in recent_speakers() if s.session == meta.get("session")), None)
    if not plain.strip():
        result = {"status": "failed", "note": "Nothing heard in it."}
    elif time.time() - meta.get("at", 0) > VOICE_NOTE_STALE + 25:  # transcribed after the phone gave up (180 s)
        result = {"status": "failed", "text": plain, "note": "Not sent: transcribed too late."}
    elif not target or (why := routing.place(target)) or not routing.reachable(target):
        result = {"status": "failed", "text": plain, "note": why if target and why else "That agent isn't reachable."}
    elif routing.deliver(rich or plain, routing.Heard.VOICE_NOTE, to=target):
        result = {"status": "done", "text": plain}
    else:
        result = {"status": "failed", "text": plain, "note": "It couldn't be typed to the agent."}
    write_json(meta_path, {**meta, **result}, private=True)
    wav.unlink(missing_ok=True)
    log(HANDSFREE_LOG, f"voice message: {result['status']}")


def switch(words: list[str]) -> int:
    if not words:
        sys.exit("usage: handsfree switch claude|cursor|codex [workspace words]")
    target = routing.find(words[0], " ".join(words[1:]) or None)
    if target is None:
        print(f"no {words[0]} has spoken through talkback in the last day")
        return 1
    routing.switch_to(target)
    routing.say(f"Now talking to {target.spoken()}.")
    print(f"now talking to {target.spoken()}")
    return 0


def remind(args: list[str]) -> int:
    """`handsfree remind …`: a scheduled message (voice/reminders.py), delivered by the call server."""
    from voice import reminders

    if args[:1] == ["list"]:
        for r in reminders.listed():
            at = time.strftime("%a %d %b %H:%M", time.localtime(r["at"]))
            print(f"{r['id']}  {at}  {r['state']:9}  {r['session']}  {r['words'][:60]}"
                  + (f"  ({r['note']})" if r.get("note") else ""))
        return 0
    if args[:1] == ["cancel"]:
        if len(args) < 2 or not reminders.cancel(args[1]):
            print("no pending reminder with that id (handsfree remind list)")
            return 1
        print("cancelled")
        return 0
    keep_awake = "--keep-awake" in args
    args = [a for a in args if a != "--keep-awake"]
    to = ""
    if "--to" in args:
        at = args.index("--to")
        if at + 1 >= len(args):
            print('--to needs an agent, e.g. --to "cursor in physical health"')
            return 1
        to, args = args[at + 1], args[:at] + args[at + 2:]
    # The time is the first one or two words ("tomorrow 08:30", "in 20m"), the rest the message
    for n in (3, 2, 1):
        try:
            due = reminders.when(" ".join(args[:n]))
            words = " ".join(args[n:]).strip()
            break
        except ValueError:
            continue
    else:
        print("usage: handsfree remind [--keep-awake] [--to AGENT] TIME WORDS  (TIME: 08:30, tomorrow 08:30, in 20m)")
        return 1
    if not words:
        print("nothing to say: handsfree remind TIME WORDS")
        return 1
    if to:
        parts = re.split(r"\s+in\s+", to, maxsplit=1)
        target = routing.find(parts[0].strip(), parts[1].strip() if len(parts) > 1 else None)
        if target is None:
            print(f"no agent heard lately matches {to!r}")
            return 1
        session = target.session
    else:
        speaker = caller(register=True)
        if speaker is None:
            print("couldn't tell which agent is asking: use --to")
            return 1
        session = speaker.session
    item = reminders.add(due, session, words, "agent" if not to else "user", keep_awake)
    print(f"reminder {item['id']} for {time.strftime('%a %d %b %H:%M', time.localtime(due))} to {session}"
          + (" (the laptop stays awake until then)" if keep_awake else ""))
    if subprocess.run(["systemctl", "--user", "is-active", "--quiet", "call.service"]).returncode != 0:
        print("note: the call server is down, and it is what delivers reminders (./install.sh call)")
    return 0


def main() -> int:
    if {"-h", "--help"} & set(sys.argv[1:]):  # anywhere: never a ring or a text with "--help" as its words
        print(__doc__)
        return 0
    command, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("status", [])
    if command == "type-into-cursor":  # internal: routing.deliver runs it in a child process
        return 0 if routing.type_into_cursor(*args) else 1
    if command == "run":
        return run()
    if command == "on":
        return turn_on_handsfree()
    if command == "off":
        HANDSFREE_ON.unlink(missing_ok=True)
        return subprocess.call(["systemctl", "--user", "stop", UNIT, EC_UNIT])
    if command == "switch":
        return switch(args)
    if command == "listen":
        LISTEN.write_text(str(time.time() + float(args[0] if args else 180)))
        return 0
    if command == "ring":
        now = "--now" in args  # the user asked for this ring: past the limit meant for agents
        wait = "--no-wait" not in args
        done = "--done" in args
        check = "--check" in args
        urgent = "--urgent" in args  # past Away: something that truly can't wait
        args, as_session = taken_as(args)
        if as_session is None:
            return 1
        return ring(" ".join(a for a in args if a not in ("--now", "--no-wait", "--done", "--check", "--urgent")), now,
                    wait, done, as_session, check, urgent)
    if command == "remind":
        return remind(args)
    if command == "send":
        return send(args)
    if command == "show":
        return show(args)
    if command == "text":
        args, as_session = taken_as(args)
        if as_session is None:
            return 1
        minutes, until = 0.0, "read"
        for flag in ("--until-read", "--until-replied"):
            if flag in args:
                at = args.index(flag)
                try:
                    minutes = float(args[at + 1])
                except (IndexError, ValueError):
                    print(f"{flag} needs minutes")
                    return 1
                args = args[:at] + args[at + 2:]
                until = flag.removeprefix("--until-")
        return text(" ".join(args), minutes, until, as_session)
    if command == "text-backup" and args:  # internal: the backup, detached so `handsfree text` returns now
        _backup_text(args[0])
        return 0
    if command == "text-status":
        status = text_status(args[0] if args else "")
        down = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "call.service"]).returncode != 0
        print((status or "unknown id") + (" (the call server is down: the phone can't report back)"
                                         if status == "sent" and down else ""))
        return 0 if status else 1
    if command == "hangup":
        if not in_call():
            print("no phone call is up")
            return 1
        CALL_HANGUP.touch()
        return 0
    if command == "close":
        CLOSE.touch()
        return 0
    active = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True).stdout
    print(f"hands-free {'on' if active.strip() == 'active' else 'off'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
