"""The slash commands an agent takes, for the phone's "/" list: its own built-ins that work typed in from afar,
the user's and the project's custom commands and skills, and the phone's own actions.

Built-ins that only open a picker, a panel or an editor in the terminal (Claude's /model, /resume, /config,
/usage) are left out: typed from the phone they would leave the terminal waiting on a key nobody can press. The model is
switched by the phone's own /model instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# The phone's own: done on the phone (and the laptop), never typed into the agent
PHONE = [
    ("stop", "Stop what the agent is doing"),
    ("model", "Switch the agent's model"),
    ("flush", "Clear this chat on the phone, with its saved files if you like"),
    ("call", "Call your crew"),
    ("later", "Send a message later: /later your words, then pick a time"),
    ("usage", "Plan limits and context, as bars"),
    ("context", "How full the context is, and what fills it"),
    ("workflow", "This chat's workflows: phases and agents, live"),
    ("away", "Away on or off: no listening at the laptop, agents text you instead of speaking"),
    ("show", "A picture of this terminal, the screen, or one window"),
]

BUILT_IN = {
    "claude": [
        ("compact", "Shrink the conversation into a summary (add what to keep after it)"),
        ("clear", "Start the conversation over"),
        ("rename", "Name this session (add the name after it)"),
        ("context", "How full the context window is"),
        ("cost", "What this session has cost so far"),
        ("review", "Review a pull request"),
        ("security-review", "Review the pending changes for security"),
        ("init", "Write a CLAUDE.md for this project"),
        ("todos", "The current to-do list"),
        ("plan", "Plan mode: look and propose, don't edit"),
    ],
    "codex": [
        ("compact", "Shrink the conversation into a summary"),
        ("new", "Start a new conversation"),
        ("rename", "Name this conversation (add the name after it)"),
        ("diff", "Show the changes made, untracked files too"),
        ("status", "Session settings and token use"),
        ("review", "Review the current changes"),
        ("init", "Write an AGENTS.md for this project"),
    ],
    "cursor": [
        ("rename", "Name this chat (add the name after it)"),
        ("plan", "Plan mode: look and propose, don't edit"),
    ],
}


def described(path: Path) -> str:
    """A command or skill file's description: its front matter's, else its first line of prose."""
    try:
        text = path.read_text(errors="replace")[:8000]
    except OSError:
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines))
        front = lines[1:end]
        for i, line in enumerate(front):
            found = re.match(r"description:\s*(.*)$", line)
            if not found:
                continue
            value = found.group(1).strip()
            if value in ("", ">", ">-", "|", "|-"):  # a block: its indented lines, joined
                block = []
                for more in front[i + 1:]:
                    if more.strip() and not more.startswith((" ", "\t")):
                        break
                    block.append(more.strip())
                value = " ".join(part for part in block if part)
            return value.strip("'\"")
        lines = lines[end + 1:]
    for line in lines:
        words = line.strip().lstrip("#").strip()
        if words:
            return words
    return ""


def custom(folder: Path, prefix: str = "") -> list[tuple[str, str]]:
    """Markdown commands in a folder (subfolders named with ":", as Claude names them)."""
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.rglob("*.md")):
        parts = path.relative_to(folder).with_suffix("").parts
        found.append((prefix + ":".join(parts), described(path)))
    return found


def skills(folder: Path) -> list[tuple[str, str]]:
    """Skills in a folder: each a folder with a SKILL.md, called by its folder's name."""
    if not folder.is_dir():
        return []
    return [(skill.parent.name, described(skill)) for skill in sorted(folder.glob("*/SKILL.md"))]


def listed(agent: str, project: Path | None, home: Path | None = None) -> list[dict]:
    """The "/" list for that agent, the phone's own first: [{name, about, kind}], kind "phone", "agent" or
    "custom". A name defined twice is listed once, the first found (project before the user's own)."""
    home = home or Path.home()
    entries: list[tuple[str, str, str]] = [(name, about, "phone") for name, about in PHONE]
    entries += [(name, about, "agent") for name, about in BUILT_IN.get(agent, [])]
    mine: list[tuple[str, str]] = []
    if agent == "claude":
        for root in ([project] if project else []) + [home]:
            mine += custom(root / ".claude/commands") + skills(root / ".claude/skills")
    elif agent == "codex":
        mine += custom(home / ".codex/prompts", prefix="prompts:")
    elif agent == "cursor":
        for root in ([project] if project else []) + [home]:
            mine += custom(root / ".cursor/commands")
    entries += [(name, about, "custom") for name, about in mine]
    seen: set[str] = set()
    result = []
    for name, about, kind in entries:
        if name in seen or not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
            continue
        seen.add(name)
        result.append({"name": name, "about": about[:160], "kind": kind})
    return result


def skips_permissions(cmdline: bytes) -> bool:
    """True when that Claude was started so Shift+Tab can land on bypass permissions. The chip then won't press it."""
    return (b"dangerously-skip-permissions" in cmdline or b"bypassPermissions" in cmdline
            or b"--permission-mode\x00bypass" in cmdline)


def claude_settings(project: Path | None, home: Path | None = None) -> list[Path]:
    """The user and project settings files that can set permissions.defaultMode."""
    home = home or Path.home()
    paths = [home / ".claude/settings.json", home / ".claude/settings.local.json"]
    if project is not None:
        paths += [project / ".claude/settings.json", project / ".claude/settings.local.json"]
    return paths


def settings_skip_permissions(paths: list[Path]) -> bool:
    """True when one of those files sets permissions.defaultMode to bypassPermissions. A file that exists but
    can't be read counts too: the chip won't press Shift+Tab without knowing. A missing file does not."""
    for path in paths:
        try:
            raw = path.read_text()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            return True
        try:
            data = json.loads(raw)
        except ValueError:
            return True
        permissions = data.get("permissions") if isinstance(data, dict) else None
        if isinstance(permissions, dict) and permissions.get("defaultMode") == "bypassPermissions":
            return True
    return False


def shift_tab_result(before: str, after: str) -> str:
    """After one Shift+Tab: "agent" when the footer left a known mode for another safe one, "stop" when it says
    bypass, never moved, or the mode before the press was blank. A blank then "plan" is not leaving plan. Stop
    means the chip must not press Shift+Tab again."""
    if not before or after == "bypass" or not after or after == before:
        return "stop"
    return "agent"


def plan_command_result(after: str) -> str:
    """After /plan: "plan" only when the footer says so, "stop" on bypass, otherwise it did not switch."""
    if after == "bypass":
        return "stop"
    return "plan" if after == "plan" else "unchanged"


def claude_mode_word(screen: str) -> str:
    """The mode Claude is showing at the bottom of its terminal: plan, bypass, accept, auto, manual, dontask.
    Empty when none of those words are in the last lines. The latest phrase wins. Only the footer is read:
    the same words further up the transcript must not count."""
    tail = "\n".join(screen.splitlines()[-8:]).lower()
    found, at = "", -1
    for phrase, name in (("bypass permissions", "bypass"), ("plan mode", "plan"), ("accept edits", "accept"),
                         ("auto mode", "auto"), ("manual mode", "manual"), ("don't ask", "dontask")):
        where = tail.rfind(phrase)
        if where > at:
            found, at = name, where
    return found


