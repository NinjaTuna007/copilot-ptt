"""One append-only log per job a boss starts.

The boss is depth 1 and is the only session that may deploy. A child is depth 2,
stored with may_deploy false, and cannot raise the chain's knobs. The default
knobs are max_depth 2 and slots 8 (live children, so an ended one frees a slot).

The directory is voice.state.CHAINS, one ``<id>.jsonl`` per job. The first line
is the chain (boss and knobs). After that the lines are launch, bound,
correction, report, ended and replaced. A launch may only know the pane and its
pid; a bound line attaches the session once it exists. No topic, token or folder
path is written. Corrections and reports are the words themselves.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from voice.state import CHAINS

KINDS = ("launch", "bound", "correction", "report", "ended", "replaced")
DEFAULT_DEPTH = 2  # boss is 1, so a child is as deep as a chain allows
DEFAULT_SLOTS = 8
REPORT = "[henchline · report]"  # an internal report, not a correction from the user
_LOCK = CHAINS.parent / "chains.lock"
_DROP = {"topic", "token", "secret", "password", "folder", "path"}


@dataclass
class _Chain:
    id: str
    boss: str
    max_depth: int
    slots: int
    events: list[dict] = field(default_factory=list)
    closed: bool = False


@contextmanager
def _locked():
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load_all() -> list[_Chain]:
    if not CHAINS.is_dir():
        return []
    found = []
    for path in sorted(CHAINS.glob("*.jsonl")):
        chain = _load(path)
        if chain:
            found.append(chain)
    return found


def _load(path) -> _Chain | None:
    header = None
    events = []
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "chain" and header is None:
            header = item
        elif item.get("kind") in KINDS:
            events.append(item)
    if not header or not header.get("id") or not header.get("boss"):
        return None
    boss = str(header["boss"])
    closed = any(e.get("kind") == "ended" and str(e.get("session") or "") == boss for e in events)
    try:
        depth, slots = int(header.get("max_depth")), int(header.get("slots"))
    except (TypeError, ValueError):
        depth, slots = DEFAULT_DEPTH, DEFAULT_SLOTS
    return _Chain(str(header["id"]), boss, depth, slots, events, closed)


def _one(chain_id: str) -> _Chain | None:
    if not chain_id:
        return None
    path = CHAINS / f"{chain_id}.jsonl"
    return _load(path) if path.is_file() else None


def _parents(chain: _Chain) -> dict[str, str]:
    """Session → the boss that started it. An ended child still counts: ending one pane doesn't drop a sibling."""
    parents: dict[str, str] = {}
    pane_parent: dict[str, str] = {}
    for event in chain.events:
        kind = event.get("kind")
        pane = str(event.get("pane") or "")
        session = str(event.get("session") or "")
        if kind == "launch":
            parent = str(event.get("parent") or chain.boss)
            if pane:
                pane_parent[pane] = parent
            if session and session != chain.boss:
                parents[session] = parent
        elif kind == "bound":
            parent = pane_parent.get(pane, str(event.get("parent") or chain.boss))
            if session and session != chain.boss:
                parents[session] = parent
                if pane:
                    pane_parent[pane] = parent
        elif kind == "replaced":
            parents.pop(session, None)
            new = str(event.get("by") or "")
            if new and new != chain.boss:
                parents[new] = pane_parent.get(pane, chain.boss)
    return parents


def _renamed_as(session: str) -> set[str]:
    """This session and the keys it was renamed to or from. Not another session that shares a pane."""
    from voice.state import RENAMED, read_json
    try:
        data = read_json(RENAMED)
    except OSError:
        data = {}
    names = {session}
    changed = True
    while changed and isinstance(data, dict):
        changed = False
        for old, new in data.items():
            old, new = str(old), str(new)
            if new in names and old not in names:
                names.add(old)
                changed = True
            if old in names and new not in names:
                names.add(new)
                changed = True
    return names


def _known_as(session: str) -> set[str]:
    """This session, the keys it was renamed to or from, and any key already written on the same live pane.

    A child is often filed first under a folder placeholder, then under its chat id. A correction on one and a
    report on the other are the same child. A pane id is only shared when the pid matches, because tmux reuses
    pane ids.
    """
    names = _renamed_as(session)
    panes: set[tuple[str, int]] = set()
    for chain in _load_all():
        for event in chain.events:
            if str(event.get("session") or "") in names and event.get("pane"):
                panes.add((str(event["pane"]), int(event.get("pid") or 0)))
    if not panes:
        return names
    for chain in _load_all():
        for event in chain.events:
            who = str(event.get("session") or "")
            pane = str(event.get("pane") or "")
            if not who or not pane:
                continue
            recorded = int(event.get("pid") or 0)
            for known_pane, known_pid in panes:
                if pane == known_pane and (not known_pid or not recorded or known_pid == recorded):
                    names.add(who)
    return names


def _parent_of(session: str) -> str:
    if not session:
        return ""
    names = _known_as(session)
    for chain in _load_all():
        parents = _parents(chain)
        for name in names:
            parent = parents.get(name, "")
            if parent and parent != session:
                return parent
    return ""


def _chain_of(session: str) -> _Chain | None:
    """The open job this session belongs to: as a child first, else as its boss."""
    names = _known_as(session) if session else set()
    as_boss = None
    for chain in _load_all():
        parents = _parents(chain)
        if any(name in parents for name in names):
            return chain
        if chain.boss == session and not chain.closed:
            as_boss = chain
    return as_boss


def _same_pid(left: int, right: int) -> bool:
    """Both name one process, or both are unknown. A missing pid is not a known one."""
    if left and right:
        return left == right
    return not left and not right


def _closes(later: dict, launch: dict) -> bool:
    """An ended or replaced line closes this launch only.

    The same session closes it. A shared pane id closes it only when the pids
    are the same number, or both lines have no pid. Ending pid 7 does not close
    pid 9, and a line with no pid does not close a launch that has one.
    """
    if later.get("kind") not in ("ended", "replaced"):
        return False
    if str(launch.get("session") or "") and str(later.get("session") or "") == str(launch.get("session") or ""):
        return True
    if not launch.get("pane") or str(later.get("pane") or "") != str(launch.get("pane") or ""):
        return False
    return _same_pid(int(launch.get("pid") or 0), int(later.get("pid") or 0))


def _active(events: list[dict]) -> int:
    """Launches with no later ended or replaced line for that same process."""
    live: dict[str, dict] = {}
    for index, event in enumerate(events):
        if event.get("kind") == "launch":
            pane = str(event.get("pane") or "")
            pid = int(event.get("pid") or 0)
            key = f"{pane}#{pid}" if pane else f"at:{event.get('at')}:{index}"
            live[key] = event
        else:
            for key, launch in list(live.items()):
                if _closes(event, launch):
                    del live[key]
    return len(live)


def _write(chain_id: str, item: dict) -> None:
    CHAINS.mkdir(parents=True, exist_ok=True)
    os.chmod(CHAINS, 0o700)
    path = CHAINS / f"{chain_id}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(item) + "\n")


def _knob_pair(max_depth: int, slots: int) -> tuple[int, int]:
    try:
        max_depth = int(max_depth)
    except (TypeError, ValueError):
        max_depth = DEFAULT_DEPTH
    try:
        slots = int(slots)
    except (TypeError, ValueError):
        slots = DEFAULT_SLOTS
    if max_depth < 1:
        max_depth = DEFAULT_DEPTH
    if slots < 1:
        slots = DEFAULT_SLOTS
    return max_depth, slots


def knobs(chain: str) -> dict:
    """``{"max_depth", "slots"}`` for that job, or empty when it isn't one."""
    with _locked():
        found = _one(chain)
    if not found:
        return {}
    return {"max_depth": found.max_depth, "slots": found.slots}


def history(chain: str) -> list[dict]:
    """The job's lines, oldest first, without the opening knob line."""
    with _locked():
        found = _one(chain)
    return list(found.events) if found else []


def open(boss_session: str, *, max_depth: int = DEFAULT_DEPTH, slots: int = DEFAULT_SLOTS) -> str:
    """The boss's open job, reused when one exists and has not ended.

    A session that is already a child gets that same job back. Higher knobs on
    the call are ignored: a child cannot raise them, and reopening doesn't either.
    """
    if not boss_session:
        raise ValueError("a chain needs a boss")
    max_depth, slots = _knob_pair(max_depth, slots)
    with _locked():
        parent = _parent_of(boss_session)
        existing = _chain_of(boss_session)
        if parent and existing:
            return existing.id
        if existing and existing.boss == boss_session:
            return existing.id
        chain_id = secrets.token_hex(4)
        while (CHAINS / f"{chain_id}.jsonl").exists():
            chain_id = secrets.token_hex(4)
        _write(chain_id, {"at": time.time(), "kind": "chain", "id": chain_id, "boss": boss_session,
                          "max_depth": max_depth, "slots": slots})
        return chain_id


def append(chain: str, kind: str, **fields) -> None:
    """Append one line. A launch is stored with may_deploy false; knob fields are dropped."""
    if kind not in KINDS:
        raise ValueError(f"not a chain line: {kind}")
    clean = {}
    for key, value in fields.items():
        if key in _DROP or key == "kind":
            continue
        if isinstance(value, str) and key == "where" and "/" in value:
            value = value.rstrip("/").rsplit("/", 1)[-1]
        clean[key] = value
    with _locked():
        found = _one(chain)
        if found is None:
            raise ValueError(f"no such chain: {chain}")
        if kind == "launch":
            clean["may_deploy"] = False  # every child: only the boss deploys, and a child cannot raise this
            try:
                depth = int(clean.get("depth") or DEFAULT_DEPTH)
            except (TypeError, ValueError):
                depth = DEFAULT_DEPTH
            clean["depth"] = min(max(depth, 2), found.max_depth)
            clean.pop("max_depth", None)
            clean.pop("slots", None)
        else:
            clean.pop("max_depth", None)
            clean.pop("slots", None)
            clean.pop("may_deploy", None)
        if "pid" in clean:
            try:
                clean["pid"] = int(clean["pid"])
            except (TypeError, ValueError):
                clean["pid"] = 0
        _write(chain, {"at": time.time(), "kind": kind, **clean})


def allow(chain: str, caller_session: str, depth: int) -> str:
    """``""`` when ``caller_session``, working at ``depth``, may start a child. Otherwise a short reason.

    The boss is depth 1. A child is depth 2. With max_depth 2 there is no room
    under a child, and a child is not the boss, so it may not deploy.
    """
    with _locked():
        found = _one(chain)
        if found is None:
            return "no such chain"
        if found.closed:
            return "that chain has ended"
        if caller_session != found.boss:
            return "only the boss may deploy"
        try:
            here = int(depth)
        except (TypeError, ValueError):
            here = found.max_depth
        if here >= found.max_depth:
            return "too deep"
        if _active(found.events) >= found.slots:
            return "no slots left"
        return ""


def parent_of(session: str) -> str:
    """The boss that started this session, or ``""`` when it isn't a child."""
    with _locked():
        return _parent_of(session)


def chain_of(session: str) -> str:
    """The job id this session belongs to, or ``""``."""
    with _locked():
        found = _chain_of(session)
    return found.id if found else ""


def pane_of(session: str) -> str:
    """The tmux pane last recorded for this session, or ``""``."""
    found = ""
    with _locked():
        for chain in _load_all():
            for event in chain.events:
                if str(event.get("session") or "") == session and event.get("pane") and event.get("kind") in (
                        "launch", "bound", "ended"):
                    found = str(event["pane"])
    return found


def _launch_open(events: list[dict], index: int) -> bool:
    launch = events[index]
    return not any(_closes(later, launch) for later in events[index + 1:])


def bind(session: str, pane: str = "", pid: int = 0) -> str:
    """Attach ``session`` to the live launch of that pane (or that pane pid). The boss, or ``""``.

    The child often has no session id at launch. The pane and pid are enough until this line.
    """
    if not session:
        return ""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        pid = 0
    pane = str(pane or "")
    with _locked():
        already = _parent_of(session)
        if already:
            return already
        if not pane and not pid:
            return ""
        match: tuple[_Chain, dict] | None = None
        for chain in _load_all():
            if chain.closed:
                continue
            for index, event in enumerate(chain.events):
                if event.get("kind") != "launch" or not _launch_open(chain.events, index):
                    continue
                recorded = int(event.get("pid") or 0)
                recorded_session = str(event.get("session") or "")
                same_pane = bool(pane) and str(event.get("pane") or "") == pane
                # The same pid, or both unknown. A missing pid may still name an unnamed launch
                # (a report arrives before the pid is known). It must not take a launch that already has a pid.
                if _same_pid(pid, recorded) and (same_pane or (not pane and bool(pid))):
                    match = (chain, event)  # the latest open launch wins
                elif same_pane and not recorded_session and bool(pid) != bool(recorded):
                    match = (chain, event)
        if match is None:
            return ""
        chain, launch = match
        _write(chain.id, {"at": time.time(), "kind": "bound", "session": session,
                          "pane": str(launch.get("pane") or pane), "pid": int(launch.get("pid") or pid or 0)})
        return chain.boss


def corrections_since(session: str) -> list[str]:
    """Correction texts for this session that no report has included yet, oldest first."""
    if not session:
        return []
    texts: list[str] = []
    with _locked():
        for chain in _load_all():
            names = _known_as(session)
            last = -1
            for index, event in enumerate(chain.events):
                if event.get("kind") == "report" and str(event.get("session") or "") in names:
                    last = index
            for index, event in enumerate(chain.events):
                if index <= last or event.get("kind") != "correction" or str(event.get("session") or "") not in names:
                    continue
                text = str(event.get("text") or "").strip()
                if text:
                    texts.append(text)
    return texts


def mark_reported(session: str) -> None:
    """Those corrections are in a report now. A later correction waits for the next one."""
    if not session:
        return
    with _locked():
        found = _chain_of(session)
    if found:
        append(found.id, "report", session=session, mark=True)


def note_correction(session: str, text: str, pane: str = "", pid: int = 0) -> None:
    """Remember the user's words to a child. A line that is itself a report is not one, and nothing is typed on."""
    if not session or not str(text or "").strip() or str(text).lstrip().startswith(REPORT):
        return
    if not CHAINS.is_dir():  # no job has been opened: nothing to attach, and ordinary delivery stays a directory check
        return
    if pane or pid:
        bind(session, pane, pid)
    if not parent_of(session):
        return
    chain = chain_of(session)
    if chain:
        append(chain, "correction", session=session, text=str(text).strip(), pane=str(pane or ""), pid=int(pid or 0))


def close_child(session: str, pane: str = "", pid: int = 0) -> None:
    """Record that this child ended, even when the launch line never stored its session.

    The pane and pid find the launch. A session that was only bound later, or only known to the phone, still
    frees the slot when the pid is the same one. A missing pid does not end a launch that has a pid, and it
    does not bind onto a launch that already has another name.
    """
    pane = str(pane or "")
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        pid = 0
    # Binding first with no pid would claim whatever launch is on that pane and then look like its session.
    if session and pid:
        bind(session, pane, pid)
    with _locked():
        match: tuple[_Chain, dict] | None = None
        for chain in _load_all():
            for index, event in enumerate(chain.events):
                if event.get("kind") != "launch" or not _launch_open(chain.events, index):
                    continue
                recorded = int(event.get("pid") or 0)
                if not _same_pid(pid, recorded):
                    continue
                same_pane = bool(pane) and str(event.get("pane") or "") == pane
                recorded_session = str(event.get("session") or "")
                same_session = bool(session) and recorded_session in _renamed_as(session)
                if same_pane or same_session or (not pane and pid and recorded == pid):
                    match = (chain, event)
        if match is None:
            return
        chain, launch = match
        _write(chain.id, {"at": time.time(), "kind": "ended", "session": session,
                          "pane": str(launch.get("pane") or pane), "pid": int(launch.get("pid") or pid or 0)})
