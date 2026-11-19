"""Agents in tmux: the ones Henchline starts from the phone (a new chat), and any the user runs in tmux.

A tmux pane is a tab whose owner is "tmux" and whose address is the pane id (%12). Typing into it needs no
window, no focus and no desktop: `tmux send-keys`. So an agent started from the phone works on any Linux
(the user's point, 2026-11-16: not tied to KDE), and the user can watch it with `tmux attach -t henchline`.
WithTmux puts this in front of the desktop's own terminals: tmux panes here, every other tab there.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from voice.agents import process
from voice.desktop import Tab, Terminals, Window

OWNER = "tmux"
SESSION = "henchline"  # where the phone's new chats run: `tmux attach -t henchline`
TYPE_PAUSE = 0.15  # seconds between the text and its Enter, as for Konsole
# What a new chat starts, per agent, never stopping to ask for a keypress the phone can't give (the user's point,
# 2026-11-16): Claude in auto mode (a classifier decides; what it refuses is refused, not asked), the Cursor CLI on
# Run Everything; Codex sends its approvals to its own reviewer (approvals_reviewer = "auto_review" in its config)
COMMANDS = {"claude": ["claude", "--permission-mode", "auto"], "codex": ["codex"],
            "cursor": ["cursor-agent", "--force"]}


def tmux(*args: str, own_scope: bool = False, given: str | None = None) -> subprocess.CompletedProcess | None:
    """own_scope: the tmux server started in a systemd scope of its own, so restarting the service that asked
    (the call server) doesn't take every agent started from the phone down with its cgroup."""
    scope = ["systemd-run", "--user", "--scope", "--quiet", "--collect"] \
        if own_scope and shutil.which("systemd-run") else []
    try:
        return subprocess.run([*scope, "tmux", *args], input=given, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def out(*args: str) -> str:
    done = tmux(*args)
    return done.stdout.strip() if done and done.returncode == 0 else ""


def agent_in(pane_pid: int) -> int:
    """The agent the pane runs: the pane's own process, or one of its children (a shell that started it)."""
    if (p := process(pane_pid)) and p.agent:
        return pane_pid
    try:
        children = Path(f"/proc/{pane_pid}/task/{pane_pid}/children").read_text().split()
    except OSError:
        return 0
    return next((int(c) for c in children if (p := process(int(c))) and p.agent), 0)


# Claude Code's aliases for the newest of each model (`claude --model`, `/model`)
CLAUDE_MODELS = [("opus", "Opus"), ("sonnet", "Sonnet"), ("fable", "Fable"), ("haiku", "Haiku")]
MODEL_FLAG = {"claude": "--model", "codex": "-m", "cursor": "--model"}
_cursor_models: tuple[float, list[dict]] = (0.0, [])


def models(agent: str) -> list[dict]:
    """The models a new chat (or a switch) can pick for that agent: [{id, name}], as each CLI lists them."""
    global _cursor_models
    if agent == "claude":
        return [{"id": i, "name": n} for i, n in CLAUDE_MODELS]
    if agent == "codex":  # Codex's own cache of the account's models; the hidden ones (its reviewer) left out
        try:
            listed = json.loads((Path.home() / ".codex/models_cache.json").read_text()).get("models", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [{"id": m["slug"], "name": m.get("display_name", m["slug"])} for m in listed
                if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
    if agent == "cursor":  # `cursor-agent models`: "slug - Name" lines; asked at most every ten minutes
        at, found = _cursor_models
        if time.monotonic() - at > 600 or not found:
            try:
                listing = subprocess.run([shutil.which("cursor-agent") or "cursor-agent", "models"],
                                         capture_output=True, text=True, timeout=20).stdout
            except (OSError, subprocess.TimeoutExpired):
                listing = ""
            lines = (line.partition(" - ") for line in listing.splitlines())
            found = [{"id": slug.strip(), "name": " ".join(name.split())}
                     for slug, _, name in lines if name and " " not in slug.strip()]
            _cursor_models = (time.monotonic(), found)
        return found
    return []


# Claude's models by their exact names (not only the family alias), each with whether it has the 1M context
CLAUDE_MENU = [("claude-opus-5-5", "Opus 5.5", True), ("claude-sonnet-5-5", "Sonnet 5.5", True),
               ("claude-fable-5-1", "Fable 5.1", True), ("claude-haiku-5-5", "Haiku 5.5", False)]
CLAUDE_EFFORTS = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("xhigh", "Extra High"), ("max", "Max")]


def menu(agent: str) -> list[dict]:
    """The model menu for an agent, as Cursor's has it (the user's wish, 2026-11-16: the same across the board):
    [{id, name, params: [{id, name, kind, values: [{value, name}]}]}]. Claude: exact model, effort, context; Codex:
    model, effort. [] for an agent with only a flat list (the Cursor CLI)."""
    def enum(pid: str, name: str, values: list[tuple[str, str]]) -> dict:
        return {"id": pid, "name": name, "kind": "enum", "values": [{"value": v, "name": n} for v, n in values]}
    if agent == "claude":
        return [{"id": mid, "name": name,
                 "params": [enum("effort", "Effort", CLAUDE_EFFORTS)]
                 + ([enum("context", "Context", [("standard", "Standard"), ("1m", "1M")])] if long else [])}
                for mid, name, long in CLAUDE_MENU]
    if agent == "codex":
        try:
            listed = json.loads((Path.home() / ".codex/models_cache.json").read_text()).get("models", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [{"id": m["slug"], "name": m.get("display_name", m["slug"]),
                 "params": [enum("effort", "Effort", [(lv["effort"], lv["effort"].capitalize())
                                                      for lv in m.get("supported_reasoning_levels") or []
                                                      if isinstance(lv, dict) and lv.get("effort")])]}
                for m in listed if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
    return []


def checked(agent: str, model: str, params: dict) -> dict | None:
    """The settings, if that model in the agent's menu has each of them; None when one isn't offered."""
    entry = next((m for m in menu(agent) if m["id"] == model), None)
    if entry is None:
        return None
    allowed = {p["id"]: {v["value"] for v in p["values"]} for p in entry["params"]}
    return dict(params) if all(v in allowed.get(k, set()) for k, v in params.items()) else None


def model_args(agent: str, model: str, params: dict) -> list[str]:
    """The command-line words that start an agent on a model with its settings (all checked by the caller)."""
    if agent == "claude":
        name = model + ("[1m]" if params.get("context") == "1m" else "")
        return ["--model", name] + (["--effort", params["effort"]] if params.get("effort") else [])
    if agent == "codex":
        return ["-m", model] + (["-c", f'model_reasoning_effort="{params["effort"]}"'] if params.get("effort") else [])
    return [MODEL_FLAG[agent], model] if model and agent in MODEL_FLAG else []


def available() -> list[str]:
    """The agents a new chat can start: those installed, with tmux."""
    if not shutil.which("tmux"):
        return []
    return [agent for agent, command in COMMANDS.items() if shutil.which(command[0])]


# Another agent's marks: a tmux server first started by an agent (a Cursor chat's terminal) hands them to every new
# window, and a Claude started there introduced itself as that Cursor chat's workspace (smarc2 for sonar_rig, the
# user's catch, 2026-11-16)
INHERITED = re.compile(r"^(CURSOR_(?!API_KEY$)|__CURSOR|AGENT_TRANSCRIPTS$|CLAUDECODE$|CLAUDE_CODE_ENTRYPOINT$|"
                       r"CLAUDE_CODE_SESSION_ID$|CODEX_THREAD_ID$|CODEX_SANDBOX|TALKBACK_AGENT$)")


def clean_env() -> list[str]:
    """`env -u NAME …` for each agent's mark in tmux's own environment or ours: the new agent starts as itself."""
    names = {name for name in os.environ if INHERITED.match(name)}
    for line in out("show-environment", "-g").splitlines():
        name = line.split("=", 1)[0].lstrip("-")
        if INHERITED.match(name):
            names.add(name)
    env = shutil.which("env")
    return [env, *(f"-u{name}" for name in sorted(names))] if env and names else []


def _window(folder: Path, name: str, argv: list[str]) -> Tab:
    """A tmux window running `argv` in that folder. The pane, or empty when it couldn't start."""
    program = shutil.which(argv[0]) if argv else ""
    if not program:
        return Tab()
    where = ["-c", str(folder), "-n", name, "-P", "-F", "#{pane_id}", *clean_env(), program, *argv[1:]]
    running = tmux("has-session", "-t", SESSION)
    if running and running.returncode == 0:
        pane = out("new-window", "-d", "-t", f"{SESSION}:", *where)
    else:
        started = tmux("new-session", "-d", "-s", SESSION, *where, own_scope=True)
        pane = started.stdout.strip() if started and started.returncode == 0 else ""
    return Tab(OWNER, pane) if pane.startswith("%") else Tab()


def start(agent: str, folder: Path, model: str = "", params: dict | None = None) -> Tab:
    """A new agent in its own tmux window in that folder; its tab, or an empty one when it couldn't start."""
    command = COMMANDS.get(agent, [""])
    chosen = model_args(agent, model, params or {}) if model else []  # checked by the caller
    return _window(folder, f"{agent}:{folder.name}", [*command, *chosen])


def resume(agent: str, folder: Path, chat: str) -> Tab:
    """That chat id, continued, in its own tmux window. Empty when this agent has no resume, or it didn't start."""
    argv = {"claude": ["claude", "--permission-mode", "auto", "--resume", chat],
            "cursor": ["cursor-agent", "--force", "--resume", chat],
            "codex": ["codex", "resume", chat]}.get(agent)
    return _window(folder, f"{agent}:{chat[:8]}", argv) if argv else Tab()


def pane_pid(tab: Tab) -> int:
    """The pane's first process: with the pane id, says it is still the same pane (tmux reuses ids after a
    restart). 0 when it is gone, or when tmux could not be asked: callers that must not treat those the same
    use pane_there."""
    there = pane_there(tab)
    if there is not True:
        return 0
    pid = out("display-message", "-p", "-t", tab.address, "#{pane_pid}")
    return int(pid) if pid.isdigit() else 0


def pane_there(tab: Tab) -> bool | None:
    """True when that tmux pane exists, False when tmux says it does not, None when tmux could not be asked.
    None is not a dead pane: a failed query must not start a second resume."""
    if not tab or tab.owner != OWNER:
        return False
    done = tmux("display-message", "-p", "-t", tab.address, "#{pane_id}")
    if done is None:
        return None
    if done.returncode == 0 and done.stdout.strip() == tab.address:
        return True
    err = f"{done.stderr}\n{done.stdout}".lower()
    if "can't find pane" in err or "no server running" in err or "error connecting" in err:
        return False
    return None


def pane_pids() -> dict[str, int] | None:
    """Every pane with its first process, in one call; {} when no tmux server runs (no panes), None when tmux
    couldn't be asked (slow, missing): then nothing is known gone."""
    done = tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}")
    if done is None:
        return None
    if done.returncode != 0:
        return {} if "no server running" in done.stderr or "error connecting" in done.stderr else None
    pairs = (line.split(" ", 1) for line in done.stdout.splitlines() if " " in line)
    return {pane: int(pid) for pane, pid in pairs if pid.isdigit()}


# Each agent's own question about a folder it hasn't trusted, and the answer that says yes: (words of the question,
# the cursor mark, the chosen line that is yes). Claude Code's default answer is to exit; Codex's is yes.
TRUST_QUESTIONS = {
    "claude": (("trust this folder", "quick safety check"), "❯", "❯ yes, i trust this folder"),
    "codex": (("do you trust the contents of this directory",), "›", "› 1. yes, continue"),
}


def accept_trust(tab: Tab, folder: Path, agent: str = "claude", wait: float = 12.0) -> bool:
    """An agent started in a folder it hasn't trusted yet asks first. Answered yes only when that very question is
    on screen: its wording and the folder's own path, in the pane's last lines, and the highlighted answer (moved
    down to it if need be) being the yes. Anything else is left for the user (`tmux attach -t henchline`). Only for
    a folder the user picked or typed on the phone: their first message must not be the answer (it was, in a
    test, 2026-11-16, and the agent exited)."""
    if agent not in TRUST_QUESTIONS:
        return False
    words, mark, yes = TRUST_QUESTIONS[agent]
    shown = str(folder).replace(str(Path.home()), "~", 1)

    def screen() -> list[str]:  # the bottom of the pane: where the question and its answers are
        return out("capture-pane", "-p", "-t", tab.address).splitlines()[-30:]

    def asking(lines: list[str]) -> bool:
        text = " ".join(lines).lower()
        return any(w in text for w in words) and any(shown in line or str(folder) in line for line in lines)

    def chosen(lines: list[str]) -> str:
        return next((line.strip().lower() for line in lines if line.lstrip().startswith(mark)), "")

    until = time.monotonic() + wait
    while time.monotonic() < until:
        lines = screen()
        if asking(lines) and chosen(lines):
            for _ in range(3):  # down the answers until the yes is the chosen one
                lines = screen()
                if not asking(lines):
                    return False
                if chosen(lines).startswith(yes):
                    done = tmux("send-keys", "-t", tab.address, "Enter")
                    return bool(done and done.returncode == 0)
                tmux("send-keys", "-t", tab.address, "Down")
                time.sleep(0.3)
            return False
        time.sleep(0.5)
    return False


def steer_after(press, taken=None, watch=None, limit: int = 8, pause: float = 0.45) -> None:
    """More Enters after the line is in. On a side thread: the "I got it" chime plays when the first Enter
    lands, and must not wait while these catch the line up."""
    def run() -> None:
        for _ in range(limit):
            time.sleep(pause)
            if taken is not None and taken():
                return
            if watch is not None and watch():
                return
            if not press():
                return
    threading.Thread(target=run, name="steer", daemon=True).start()


def end(tab: Tab) -> bool:
    return tab.owner == OWNER and (done := tmux("kill-pane", "-t", tab.address)) is not None and done.returncode == 0


class WithTmux(Terminals):
    def __init__(self, inner: Terminals) -> None:
        self.inner = inner

    def tab_of_caller(self) -> Tab:
        pane = os.environ.get("TMUX_PANE", "")
        if pane and os.environ.get("TMUX"):  # inside tmux, even in a Konsole tab: the pane is the precise place
            return Tab(OWNER, pane)
        return self.inner.tab_of_caller()

    def agent_running(self, tab: Tab) -> bool:
        if tab.owner != OWNER:
            return self.inner.agent_running(tab)
        pid = pane_pid(tab)
        return pid > 0 and agent_in(pid) > 0

    def type_line(self, tab: Tab, text: str, again: bool = False, taken=None) -> bool:
        if tab.owner != OWNER:
            return self.inner.type_line(tab, text, again=again, taken=taken)
        # Through a paste buffer from stdin: the words are never tmux arguments, so a leading "-" or a trailing ";"
        # (tmux's command separator) can't be read as tmux's own, and a newline doesn't send early
        buffer = f"henchline-{os.getpid()}-{time.monotonic_ns()}"
        loaded = tmux("load-buffer", "-b", buffer, "-", given=text)
        pasted = loaded and loaded.returncode == 0 and tmux("paste-buffer", "-d", "-p", "-b", buffer, "-t", tab.address)
        if not pasted or pasted.returncode != 0:
            tmux("delete-buffer", "-b", buffer)
            return False
        time.sleep(TYPE_PAUSE)
        entered = tmux("send-keys", "-t", tab.address, "Enter")
        if not (entered and entered.returncode == 0):
            return False
        if again:
            def press() -> bool:
                done = tmux("send-keys", "-t", tab.address, "Enter")
                return bool(done and done.returncode == 0)

            def clear() -> bool:
                shown = out("capture-pane", "-p", "-t", tab.address).lower()
                return "follow-ups" not in shown and "enter steer" not in shown

            if taken is not None:
                steer_after(press, taken=taken, limit=10, pause=1.0)
            else:
                steer_after(press, watch=clear)
        return True

    def only_a_shell(self, window: Window) -> bool:
        return self.inner.only_a_shell(window)

    def interrupt(self, tab: Tab) -> bool:
        if tab.owner != OWNER:
            return self.inner.interrupt(tab)
        done = tmux("send-keys", "-t", tab.address, "Escape")
        return bool(done and done.returncode == 0)

    def agent_pid(self, tab: Tab) -> int:
        if tab.owner != OWNER:
            return self.inner.agent_pid(tab)
        pid = pane_pid(tab)
        return agent_in(pid) if pid else 0

    def agent_tabs(self) -> list[tuple[Tab, int]]:
        found = list(self.inner.agent_tabs())
        for line in out("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}").splitlines():
            pane, _, pid = line.partition(" ")
            if pid.isdigit() and (agent := agent_in(int(pid))):
                found.append((Tab(OWNER, pane), agent))
        return found
