"""``handsfree deploy``, ``report`` and ``end``: a boss starts a child, and the child reports back.

Laptop only. The boss is whoever is running the command (``handsfree.caller``).
A child is started with ``tmux.start`` (never resume), in a folder ``call.server``
already accepts, and registered in the same spawned list as a new chat. The ask
is typed in with a note to report back. The chain log stores the pane and pid;
the session id is bound later, when the child is known.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from voice import chains
from voice.desktop import Tab
from voice.state import HANDSFREE_LOG, SPEAKERS, Speaker, log, recent_speakers

AGENTS = ("claude", "cursor", "codex")
ASK_PREFIX = '[henchline · deploy] You were started by another agent. Report back with `handsfree report "…"`.\n\n'


def _server():
    """The call server's folder check, spawned list and phone notice. Imported here so a test can stand in."""
    import call.server as server
    return server


def _routing():
    core = str(Path(__file__).resolve().parents[1] / "handsfree" / "core")
    if core not in sys.path:
        sys.path.insert(0, core)
    import routing
    return routing


def _deploy_args(args: list[str]) -> tuple[str, str, str]:
    agent, folder = "", ""
    ask: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == "--agent" and index + 1 < len(args):
            agent = args[index + 1]
            index += 2
        elif args[index] == "--folder" and index + 1 < len(args):
            folder = args[index + 1]
            index += 2
        elif args[index] == "--ask":
            ask = args[index + 1:]
            break
        else:
            index += 1
    return agent, folder, " ".join(ask).strip()


def _folder(given: str, known: list[Path], typed):
    """A folder the call server already allows: one it trusts, or one ``typed_folder`` accepts."""
    raw = Path(given).expanduser()
    try:
        resolved = raw.resolve()
    except OSError:
        resolved = raw
    for folder in known:
        try:
            same = Path(folder).resolve() == resolved
        except OSError:
            same = False
        if same or str(folder) == given:
            return Path(folder)
    return typed(given)


def _pane(speaker: Speaker) -> str:
    """The caller's pane. Its own tab wins: the environment is this process, which may not be the caller under test."""
    if speaker.tab:
        return speaker.tab.address
    return os.environ.get("TMUX_PANE", "")


def deploy(args: list[str], caller) -> int:
    """Start a child in a new tmux pane and type the ask. The caller is the boss."""
    agent, folder, ask = _deploy_args(args)
    if agent not in AGENTS or not folder or not ask:
        print('usage: handsfree deploy --agent claude|cursor|codex --folder PATH --ask "words"')
        return 1
    boss = caller() if caller else None
    if boss is None or not boss.session:
        print("couldn't tell which agent is deploying")
        return 1
    chains.bind(boss.session, _pane(boss), int(boss.pid or 0))
    depth = 2 if chains.parent_of(boss.session) else 1
    chain = chains.open(boss.session)
    refused = chains.allow(chain, boss.session, depth)
    if refused:
        print(refused)
        return 1
    server = _server()
    known = [Path(p) for p in server.trusted_folders()]
    match = _folder(folder, known, server.typed_folder)
    if match is None:
        print(f"not a folder this laptop can start an agent in: {folder}")
        return 1
    from voice.desktop import current as desktop
    from voice.desktop import tmux
    tab = tmux.start(agent, match)
    if not tab:
        print(f"{agent} didn't start")
        return 1
    # Claude's trust question defaults to exiting, and the ask would be typed into it. Answered first, the same
    # way a new chat is, and only then is the ask typed. Codex may ask about any folder.
    needs_trust = agent == "codex" or (agent == "claude" and match not in known)
    accepted = tmux.accept_trust(tab, match, agent, 8.0 if agent == "codex" else 12.0) if needs_trust else True
    if accepted and needs_trust:
        server.notice(f"{agent.capitalize()} now trusts a folder",
                      f"Started from a deploy, {agent.capitalize()}'s trust question was answered yes for {match.name}.")
    # Not counted, and not registered, until the ask is actually typed. A trust question that is still
    # on screen would swallow it, and a launch we then abandon must not hold a slot.
    if needs_trust and not accepted and (agent == "claude" or _still_asking(tab, agent)):
        tmux.end(tab)
        print(f"{agent} started in {match.name} (tmux {tab.address}), but it is still asking about that folder, "
              "so the ask was not typed")
        return 1
    typed = desktop().terminals.type_line(tab, ASK_PREFIX + ask)
    if not typed:
        tmux.end(tab)
        print(f"{agent} started in {match.name} (tmux {tab.address}), but the ask was not typed")
        return 1
    pid = tmux.pane_pid(tab)
    server.spawned(lambda alive: [*alive[-49:], {"pane": tab.address, "pid": pid}])
    # Folder name only: the notice goes to the phone, and the chain file keeps the pane, not the path
    server.notice(f"{agent.capitalize()} started in {match.name}",
                  "From a deploy, in " + match.name + ". It runs commands without asking; "
                  "`tmux attach -t henchline` to watch.")
    chains.append(chain, "launch", parent=boss.session, pane=tab.address, pid=pid, depth=depth + 1,
                  agent=agent, where=match.name)
    log(HANDSFREE_LOG, f"deployed {agent} in {match.name} (tmux {tab.address})")
    print(f"deployed {agent} in {match.name} (tmux {tab.address})")
    return 0


def _still_asking(tab: Tab, agent: str) -> bool:
    """The trust question is still the thing on screen, so the ask must not be typed into it."""
    from voice.desktop import tmux
    words = {"claude": "trust this folder", "codex": "do you trust the contents of this directory"}.get(agent, "")
    if not words:
        return False
    shown = tmux.out("capture-pane", "-p", "-t", tab.address).lower()
    return words in shown


def _boss(session: str) -> Speaker | None:
    found = Speaker.load(SPEAKERS / f"{session}.json")
    if found:
        return found
    return next((s for s in recent_speakers() if s.session == session), None)


def report(args: list[str], caller) -> int:
    """The caller is the child. Corrections since its last report are delivered to the boss first.

    Whose call is pinned is left as it is: this types to the boss and does not switch.
    """
    words = " ".join(args).strip()
    if not words:
        print("usage: handsfree report WORDS")
        return 1
    child = caller() if caller else None
    if child is None or not child.session:
        print("couldn't tell which agent is reporting")
        return 1
    chains.bind(child.session, _pane(child), int(child.pid or 0))
    boss = chains.parent_of(child.session)
    if not boss:
        print("this agent has no boss to report to")
        return 1
    target = _boss(boss)
    if target is None:
        print("the boss isn't a known agent yet, so the report was not typed")
        return 1
    prior = chains.corrections_since(child.session)
    body = words if not prior else "Corrections since the last report:\n" + "\n".join(prior) + "\n\n" + words
    routing = _routing()
    # SCHEDULED so a failure isn't spoken into the room, and so this isn't the user answering the boss.
    # The text itself starts with the report marker, so it is typed as a report and not kept as a correction.
    if not routing.deliver(f"{chains.REPORT} {body}", routing.Heard.SCHEDULED, to=target):
        print(routing.why_not() or "couldn't deliver the report")
        return 1
    chain = chains.chain_of(child.session)
    if chain:
        chains.append(chain, "report", session=child.session, text=words)
    chains.mark_reported(child.session)
    print("reported")
    return 0


def _hand_up(session: str) -> bool:
    """Deliver unreported corrections to the boss before the pane is closed. False when they could not be delivered."""
    prior = chains.corrections_since(session)
    if not prior:
        return True
    target = _boss(chains.parent_of(session))
    if target is None:
        return False
    body = "Corrections since the last report:\n" + "\n".join(prior)
    routing = _routing()
    if not routing.deliver(f"{chains.REPORT} {body}", routing.Heard.SCHEDULED, to=target):
        return False
    chains.mark_reported(session)
    return True


def end(args: list[str]) -> int:
    """End that one session's pane, when it is one started here (the same rule as ending a chat from the phone).

    Siblings stay. The old id is not resumed.
    """
    if len(args) != 1 or not args[0].strip():
        print("usage: handsfree end SESSION")
        return 1
    session = args[0].strip()
    from voice.desktop import tmux
    target = next((s for s in recent_speakers() if s.session == session), None)
    if target and target.tab:
        tab = target.tab
    else:
        pane = chains.pane_of(session)
        tab = Tab(tmux.OWNER, pane) if pane else Tab()
    if not tab or tab.owner != tmux.OWNER:
        print("only an agent started from here can be ended, and only that one")
        return 1
    server = _server()
    if not server.owns(tab.address, int(target.pid or 0) if target else 0):
        print("only an agent started from here can be ended, and only that one")
        return 1
    from voice.state import LAST, last_speaker
    # The user is in this chat: the boss does not close it out from under them.
    current = last_speaker()
    if current and current.session == session:
        print("the user is in that chat, so it was not ended")
        return 1
    pid = int(target.pid or 0) if target else 0
    if not _hand_up(session):
        print("the user's words were not delivered to the boss, so it was not ended")
        return 1
    if not tmux.end(tab):
        print("couldn't end that pane")
        return 1
    server.spawned(lambda alive: [e for e in alive if e.get("pane") != tab.address])
    chains.close_child(session, tab.address, pid)
    # The next hands-free turn must not resume this id. Follow the boss when we know them.
    followed = last_speaker()
    if followed and followed.session == session:
        boss = _boss(chains.parent_of(session))
        if boss:
            boss.save(LAST)
        else:
            LAST.unlink(missing_ok=True)
    agent = target.agent if target else ""
    for other in (Speaker.load(path) for path in SPEAKERS.glob("*.json")):
        if not other:
            continue
        same = other.session == session or (
            other.tab.address == tab.address and other.tab.owner == tmux.OWNER and (not agent or other.agent == agent))
        if same:
            (SPEAKERS / f"{other.session}.json").unlink(missing_ok=True)
    log(HANDSFREE_LOG, f"ended {session} (tmux {tab.address})")
    print(f"ended {session}")
    return 0
