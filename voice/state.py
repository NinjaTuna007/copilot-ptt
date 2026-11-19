"""The files push-to-talk, talkback and hands-free share, and what is in them.

Every name under ~/.local/state/ptt, and every shared data path, lives here
and is read from here; no tool spells a path of its own. The state files
carry state, never the user's words. Standard library only: doctor.py,
running on the system Python, reads these names too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from voice.desktop import Tab

STATE = Path.home() / ".local/state/ptt"

# Push-to-talk (and hands-free, which records the same way)
PHASE = STATE / "phase"  # recording / transcribing / idle, and who wrote it
TARGET = STATE / "target"  # whether the last paste went to an agent window
PTT_LOG = STATE / "ptt.log"
PTT_WAV = STATE / "utterance.wav"  # the recording, deleted once transcribed
VOICE_BASELINE = STATE / "voice.json"  # the user's usual loudness and pitch, for tone marks

# Talkback
TALKBACK_ON = STATE / "talkback"  # agents speak while this exists
SPOOL = STATE / "talkback.queue"  # one JSON clip per file, oldest first
HOLDS = STATE / "talkback.holds"  # one file per session whose clips wait
LOCK = STATE / "talkback.lock"  # the one worker holds it
WORKER_PID = STATE / "talkback.pid"
PLAYING = STATE / "talkback.playing"
LAST = STATE / "talkback.last"  # the agent a hands-free reply goes to
SPEAKERS = STATE / "talkback.speakers"  # every agent heard lately, one file each
RENAMED = STATE / "talkback.renamed.json"  # {old key: new key}: an agent found running, then heard under its own
TALKBACK_LOG = STATE / "talkback.log"

# Hands-free
HANDSFREE_ON = STATE / "handsfree"
ANNOUNCE = STATE / "handsfree.announce"  # set by `handsfree on`: say so once
PAUSE = STATE / "handsfree.pause"  # e.g. while wake-word samples are recorded
LISTEN = STATE / "handsfree.listen"  # keep the reply window open until this time
CLOSE = STATE / "handsfree.close"  # end the reply window now
HANDSFREE_LOG = STATE / "handsfree.log"
HANDSFREE_WAV = STATE / "handsfree.wav"  # a turn being transcribed, deleted after

# A phone call (call/): while CALL exists, hands-free hears PHONE_MIC and talkback plays into PHONE_SPEAKER
CALL = STATE / "call"
CALL_PUSH = STATE / "call.push"  # exists while the call page's talk button is held
CALL_INTERRUPT = STATE / "call.interrupt"  # the call's send button was held: the next spoken turn cuts in
CALL_CUT_IN = STATE / "call.cut-in"  # "1" or "0" when chosen on the call; absent: Cursor cuts in, others wait
CALL_BUTTON_ONLY = STATE / "call.button-only"  # in a noisy place: only the talk button gets in, nothing else
CALL_STAY_ON = STATE / "call.stay-on"  # an open line kept as long as the user likes: no "are you still there?"
CALL_CHECK = STATE / "call.check"  # the next call starts on check-on-me (a wake-up, or an answer is needed)
# Away (the user's wish, 2026-11-17): on another call, or with other people. Hands-free stops listening and talkback
# texts the phone instead of speaking at the laptop; push-to-talk works as ever. A call ends it
AWAY = STATE / "away"
VOICE_CHOICE = STATE / "voices.json"  # how the agents sound: each its own, one voice, or a surprise pick
CLAUDE_MODE = STATE / "claude-mode"  # a plan-chip press, until Claude's own log catches up
CALL_SILENT = STATE / "call.no-audio"  # the phone's mic stopped arriving mid-call (a dropped line): talkback waits
CALL_RING = STATE / "call.ring-endpoint"  # where Henchline can be rung (its UnifiedPush address): private, never logged
CALL_RINGER = STATE / "call.ringer"  # who rang the user last (session, when): the call server routes the answer there
CALL_RANG_BACK = STATE / "call.rang_back"  # a quiet call last rung back then: one per chain
CALL_SNAPS = STATE / "call.snaps.json"  # photos from the call screen's camera, waiting to go with the next turn
CALL_HANGUP = STATE / "call.hangup"  # end the phone call once talkback is done: the user said so, or an agent
VOICE_NOTES = STATE / "voicenotes"
CALL_TOLD = STATE / "call.told"  # a file per agent session already reminded, in this call, how to be on a call
INTRODUCED = STATE / "introduced"  # a file per agent session already told, once, how phone messages work
SPAWNED = STATE / "spawned.json"  # tmux panes of agents started from the phone: only these can be ended from it
CHAT_KEY = re.compile(r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # a Cursor chat's own key
CALL_PINNED = STATE / "call.pinned"  # the agent picked on this call: another speaking doesn't take the call over
# A voice message not yet typed after this long is dropped, never typed late: the phone stops waiting at 180 s
VOICE_NOTE_STALE = 150  # voice messages from the phone, waiting for hands-free to transcribe them
TEXTS = STATE / "texts"  # each text an agent sent the phone (`handsfree text`): its id, who, and how far it got
CALL_MIRROR = STATE / "call.mirror"  # a Claude Code session's terminal side, for the call's chat (call/hooks/mirror.py)
CALL_ORIGIN = STATE / "call.origin"  # the call page's https address, as the phone reached it: links to files
CALL_PORT = 8790
HANDSFREE_MODE = STATE / "handsfree.mode"  # waiting / listening / recording / transcribing: the tray's colours
PHONE_MIC = "phone_mic"  # a virtual sink the call page writes the phone's mic into; listened to as its monitor
PHONE_SPEAKER = "phone_speaker"  # a virtual sink talkback plays into during a call; streamed to the phone
PHONE_MIC_MONITOR, PHONE_SPEAKER_MONITOR = f"{PHONE_MIC}.monitor", f"{PHONE_SPEAKER}.monitor"  # PipeWire's names
DEFAULT_SINK, DEFAULT_MONITOR = "@DEFAULT_SINK@", "@DEFAULT_MONITOR@"


SENT = STATE / "handsfree.sent"  # the last message hands-free delivered: to whom and how heard; the words only during a call


@dataclass
class Sent:
    """A message hands-free delivered (the call page's chat and status read it)."""

    agent: str
    heard: str  # routing.Heard's value: "reply", "call, typed", …
    at: float = field(default_factory=time.time)
    session: str = ""  # which of the agent's sessions: two Claudes are both "claude"
    # The words as transcribed, only while a call is up, for the phone's chat to show what was heard (the
    # user's wish, 2026-11-15); the file is then private, and the call server blanks them when the call ends
    words: str = ""

    @classmethod
    def last(cls) -> Sent | None:
        data = read_json(SENT)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}  # newer fields: left out
        if not in_call():  # words left by a delivery racing the call's end are never read as said on a call
            known.pop("words", None)
        return cls(**known) if known.keys() >= {"agent", "heard", "at"} else None

    def save(self) -> None:
        write_json(SENT, asdict(self), private=True)
        if self.session and self.heard != "scheduled":  # a reminder isn't the user answering
            mark_replied(self.session)  # whatever way it came (desk, phone, call): an answer to its texts


def mark_replied(session: str) -> None:
    """The user wrote to that agent: its texts so far are answered (`handsfree text --until-replied`)."""
    for path in TEXTS.glob("*.json"):
        text = read_json(path)
        if text.get("session") == session and not text.get("replied"):
            write_json(path, {**text, "replied": True, "replied_at": time.time()}, private=True)


HANDSFREE_UNIT = "handsfree.service"


def turn_on_handsfree(announce: bool = True) -> int:
    """Hands-free on, and talkback with it (hands-free needs the agents to talk); `handsfree on` and a call."""
    STATE.mkdir(parents=True, exist_ok=True)
    HANDSFREE_ON.touch()
    TALKBACK_ON.touch()
    if announce:
        ANNOUNCE.touch()  # "Hands-free is on", once
    return subprocess.call(["systemctl", "--user", "start", HANDSFREE_UNIT])


def in_call() -> bool:
    """A call is up: its marker exists and the server that wrote it still runs (a killed one leaves it).

    The pid alone could be reused by another process: its command line must match the one written down too.
    """
    call = read_json(CALL)
    try:
        return Path(f"/proc/{call['pid']}/cmdline").read_text() == call["cmdline"]
    except (KeyError, OSError):
        return False


def mic_source() -> str:
    """Where hands-free listens: the phone during a call, the echo-cancelled laptop mic otherwise."""
    return PHONE_MIC_MONITOR if in_call() else EC_SOURCE


def speaker_sink() -> str:
    """Where talkback plays: the phone during a call, the default output otherwise."""
    return PHONE_SPEAKER if in_call() else DEFAULT_SINK


def speaker_monitor() -> str:
    """Where our voice can be heard: the phone's sink during a call, the default output's monitor otherwise."""
    return PHONE_SPEAKER_MONITOR if in_call() else DEFAULT_MONITOR

# Data, not state: models, settings and recordings
SHARE = Path.home() / ".local/share"
VENV = SHARE / "henchline/venv"  # the private Python every tool runs on (its path is also in each shebang)
KOKORO_MODELS = SHARE / "talkback"  # kokoro-v1.0.onnx and voices-v1.0.bin
HANDSFREE_DATA = SHARE / "handsfree"
WAKE_MODEL = HANDSFREE_DATA / "hey_claude_personal.pkl"  # the user's own "hey Claude"
JARVIS_MODEL = HANDSFREE_DATA / "hey_jarvis_v0.1.onnx"  # openWakeWord's published "hey Jarvis": a second wake word
JARVIS_PERSONAL = HANDSFREE_DATA / "hey_jarvis_personal.pkl"  # the user's own "hey Jarvis", preferred when there
WAKE_SAMPLES = HANDSFREE_DATA / "samples"  # "hey Claude" takes; other words' in a folder each (samples/jarvis)
ECHO_SET = HANDSFREE_DATA / "echo-set"  # talk-over recordings, one folder per session
EC_SOURCE = "talkback_ec_source"  # the echo-cancelled mic hands-free listens to
EC_UNIT = "talkback-echo-cancel.service"
EC_CONF = Path.home() / ".config/pipewire/talkback-echo-cancel.conf"
OUTBOX = HANDSFREE_DATA / "outbox"  # files agents sent the phone (`handsfree send`): a folder each, named by its secret
INBOX = Path.home() / "Downloads/Henchline"  # files the user shared from the phone, a folder per day
CALL_TLS = SHARE / "henchline/call"  # cert.pem and key.pem: the call page's own certificate (install.sh call)

# Who wrote the phase, and the agent name hands-free's own notices speak as
BY_PTT = "ptt"
BY_HANDSFREE = "handsfree"

STALE_PHASE = 300  # seconds after which a "recording" phase is a leftover from a crash
# How long an agent is remembered after it last spoke. Whether it is still there is asked of the desktop (its
# terminal tab, its Cursor window: routing.reachable), not of a clock: a quiet chat a day old fell off the crew list
# while its window was still open (the user's catch, 2026-11-17). This only bounds the list
SPEAKER_MEMORY = 30 * 86400


class Phase(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"


# The tray icons of push-to-talk and hands-free, in the same colours
PHASE_COLOURS = {Phase.RECORDING: "#e24a4a", Phase.TRANSCRIBING: "#e0a100"}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def write_json(path: Path, data: dict, private: bool = False) -> None:
    """Write whole or not at all: readers never see half a file. `private`: readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Its own name: the call server and hands-free both write some files, and must not share one temp file
    tmp = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644), "w") as out:
            out.write(json.dumps(data))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)  # only left when the write or the replace failed


def log(path: Path, line: str, echo: bool = False) -> None:
    """Append a timestamped line; echo also prints it (to the journal, for a service)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = f"{time.strftime('%F %T')} {line}"
    with open(path, "a") as out:
        out.write(stamped + "\n")
    if echo:
        print(stamped, flush=True)


def ptt_log(line: str) -> None:
    """Push-to-talk's log, also printed to its service journal."""
    log(PTT_LOG, line, echo=True)


def set_phase(phase: Phase, by: str = "") -> None:
    """What the mic is doing; talkback goes quiet and holds while it records."""
    write_json(PHASE, {"phase": phase.value, "by": by, "at": time.time()})


def phase_now() -> tuple[Phase, str]:
    """The current phase and who set it; a stale recording counts as idle."""
    data = read_json(PHASE)
    try:
        phase = Phase(data.get("phase", Phase.IDLE.value))
    except ValueError:
        phase = Phase.IDLE
    if phase is not Phase.IDLE and time.time() - data.get("at", 0) > STALE_PHASE:
        phase = Phase.IDLE
    return phase, data.get("by", "")


def release_phase(by: str) -> None:
    """Back to idle, unless someone else has the mic now."""
    if phase_now()[1] == by:
        set_phase(Phase.IDLE, by)


def ptt_busy() -> bool:
    """Push-to-talk itself has the mic (hands-free leaves it alone then)."""
    phase, by = phase_now()
    return phase is not Phase.IDLE and by != BY_HANDSFREE


def mic_busy() -> bool:
    return phase_now()[0] is not Phase.IDLE


def set_target(to_agent: bool, session: str = "") -> None:
    """Whether the words just went into an agent's window (never the words), and which agent's when known."""
    write_json(TARGET, {"agent": to_agent, "session": session, "at": time.time()})


def target_session_since(since: float) -> str:
    """The agent session the words noted after `since` went to, when the one who sent them knew it (hands-free)."""
    data = read_json(TARGET)
    return str(data.get("session") or "") if data.get("at", 0) >= since else ""


def target_since(since: float) -> bool | None:
    """True/False if a paste was noted after `since` (to an agent or not), else None."""
    data = read_json(TARGET)
    return bool(data.get("agent")) if data.get("at", 0) >= since else None


@dataclass
class Speaker:
    """An agent that spoke through talkback, and where its replies go."""

    session: str
    agent: str
    where: str = ""  # its workspace folder name
    tab: Tab = field(default_factory=Tab)  # its terminal tab, where hands-free types replies
    at: float = field(default_factory=time.time)
    # The agent's own process in that tab, and when it started: it is there exactly while that process runs in that
    # tab (the user's idea, 2026-11-17: know an agent by its identity, not by how long it's been quiet). 0: not known
    pid: int = 0
    born: int = 0

    @classmethod
    def load(cls, path: Path) -> Speaker | None:
        data = read_json(path)
        if not data.get("session") or not data.get("agent"):
            return None
        return cls(data["session"], data["agent"], data.get("where", ""), tab_from(data), data.get("at", 0.0),
                   int(data.get("pid") or 0), int(data.get("born") or 0))

    def pin(self) -> bool:
        """Notes the agent's process in its tab (it is running: it just spoke or called), again when it changed (the
        same session resumed in a new process). Only its own tab is asked: this runs before every talkback clip.
        Whether the record changed."""
        if not self.tab:
            return False
        from voice.agents import process, started
        from voice.desktop import current as desktop  # here: the desktop imports this module

        try:
            pid = desktop().terminals.agent_pid(self.tab)
        except Exception:  # noqa: BLE001 - only a better check later; the record is kept either way
            return False
        running = process(pid) if pid else None
        if not running or running.agent != self.agent:
            return False
        born = started(pid)
        if (pid, born) == (self.pid, self.born):
            return False
        self.pid, self.born = pid, born
        return True

    def save(self, path: Path) -> None:
        write_json(path, {**asdict(self), "tab": [self.tab.owner, self.tab.address]})
        if path.parent == SPEAKERS:  # known under this key again (a found key comes back for the next agent there)
            forget_renamed(self.session)

    @property
    def in_editor(self) -> bool:
        """An agent in the Cursor editor: no tab, reached through its window instead."""
        return not self.tab and self.agent == "cursor" and bool(self.where)

    def remember(self) -> None:
        """This agent spoke: replies go to it, and it can be switched to by name. A record of the same agent in the
        same tab (or, in the Cursor editor, the same workspace) under another key goes: hands-free may have found
        it running before it spoke (routing.discover), and it must not show up twice."""
        self.at = time.time()
        self.pin()
        # On a call where the user picked an agent, another one speaking doesn't take the call from it (Claude's
        # own clip moved a call opened from a Cursor chat back to Claude, 2026-11-16)
        pinned = CALL_PINNED.read_text().strip() if CALL_PINNED.exists() else ""
        if not (pinned and pinned != self.session and in_call()):
            self.save(LAST)
        self.save(SPEAKERS / f"{self.session}.json")
        self.replaces_found()

    def replaces_found(self) -> None:
        """A record of the same agent in the same tab (or, in the Cursor editor, the same workspace) under another
        key goes: found running before it spoke or texted (routing.discover)."""
        for path in SPEAKERS.glob("*.json"):
            other = Speaker.load(path)
            if not other or other.session == self.session or other.agent != self.agent:
                continue
            in_tab = bool(self.tab) and other.tab == self.tab
            # In the Cursor editor, a window's record found before any of its chats spoke goes; another chat in that
            # window, with its own chat id, is another agent and stays
            in_editor = (not self.tab and not other.tab and other.where == self.where
                         and not CHAT_KEY.search(other.session))
            if in_tab or in_editor:
                path.unlink(missing_ok=True)
                # The phone's chat with it follows it to its own key: only from a key it was found under (its
                # folder, never another session's own id: a new session in that tab is another conversation), in
                # the same folder (a tmux pane id reused after a restart is someone else)
                if in_tab and not CHAT_KEY.search(other.session) and other.where == self.where:
                    renamed(other.session, self.session)

    def spoken(self) -> str:
        """How the agent is named out loud: "Claude, in pc diagnostics"."""
        where = self.where.replace("_", " ").replace("-", " ")
        return f"{self.agent.capitalize()}, in {where}" if where else self.agent.capitalize()


def canceller_mic() -> str:
    """The mic install.sh chose for the echo canceller (on the speakers' sound card); "" if none."""
    try:
        found = re.search(r'target\.object\s*=\s*"([^"@]+)"', EC_CONF.read_text())
    except OSError:
        return ""
    return found.group(1) if found else ""


def tab_from(data: dict) -> Tab:
    """A stored tab, as ["service", "session"] ("konsole" in files written before 2026-11-15)."""
    return Tab(*(data.get("tab") or data.get("konsole") or ("", "")))


@dataclass
class Playing:
    """The clip talkback is playing: hands-free checks what it hears over it against the words."""

    clip: str
    session: str
    agent: str
    text: str

    @classmethod
    def now(cls) -> Playing | None:
        data = read_json(PLAYING)
        return cls(**data) if data.keys() >= {"clip", "session", "agent", "text"} else None

    def save(self) -> None:
        write_json(PLAYING, asdict(self))


def last_speaker() -> Speaker | None:
    return Speaker.load(LAST)


def renamed(old: str, new: str) -> None:
    """An agent known as `old` (found running, before it spoke) is `new` now: kept for the phone, which moves its
    chat across (the user's catch, 2026-11-16: a new chat's replies came in another chat). The newest 100."""
    data = {k: (new if v == old else v) for k, v in read_json(RENAMED).items() if k != new}
    data[old] = new
    write_json(RENAMED, dict(list(data.items())[-100:]))


def forget_renamed(key: str) -> None:
    """A key in use again: no longer a rename to follow (the next agent found in that folder gets the same key)."""
    data = read_json(RENAMED)
    if key in data:
        del data[key]
        write_json(RENAMED, data)


def recent_speakers() -> list[Speaker]:
    found = (Speaker.load(path) for path in SPEAKERS.glob("*.json"))
    return [s for s in found if s and time.time() - s.at < SPEAKER_MEMORY]
