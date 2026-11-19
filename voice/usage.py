"""How much each agent has used, for the phone's /usage (the user's wish, 2026-11-17: what Claude's /usage and
/context show in its terminal, as bars on the phone): its plan's limits and its context, from what each agent
already writes down, never from its screen.

- Claude: its status line's report (agents/claude-status.py): the 5-hour and weekly limits, the context, the cost.
- Codex: the last token count in its session log: the 5-hour and weekly limits, the context.
- Cursor: its own record of the chat: the context, and what fills it. Its plan's limits aren't kept on the laptop,
  so the Cursor CLI is asked: started out of sight in a folder of its own, sent /usage, read, closed (cursor_plan).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from voice.state import STATE

CLAUDE_STATUS = Path.home() / ".local/state/ptt/claude-status"
CODEX_SESSIONS = Path.home() / ".codex/sessions"


def _limit(label: str, percent, resets) -> dict | None:
    if not isinstance(percent, (int, float)):
        return None
    resets = int(resets) if isinstance(resets, (int, float)) else 0
    if resets and resets < time.time():  # read before the window ended: it has started over since
        percent, resets = 0, 0
    return {"label": label, "percent": round(float(percent), 1), "resets": resets}


def _window_name(minutes) -> str:
    if minutes == 300:
        return "5-hour session"
    if minutes == 10080:
        return "Week"
    if isinstance(minutes, (int, float)) and minutes:
        return f"{int(minutes) // 60} hours" if minutes < 2880 else f"{int(minutes) // 1440} days"
    return "Limit"


def claude(session: str, status_dir: Path = CLAUDE_STATUS) -> dict:
    uuid = session.removeprefix("claude-")
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return {}
    try:
        status = json.loads((status_dir / f"{uuid}.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(status, dict):
        return {}
    limits = status.get("rate_limits") or {}
    window = status.get("context_window") or {}
    size, used = window.get("context_window_size"), window.get("used_percentage")
    current = window.get("current_usage") or {}
    tokens = sum(v for k, v in current.items() if k.endswith("input_tokens") and isinstance(v, int)) if isinstance(current, dict) else 0
    cost = (status.get("cost") or {}).get("total_cost_usd")
    return {
        "model": str((status.get("model") or {}).get("display_name", "")),
        "limits": [x for x in (_limit("5-hour session", (limits.get("five_hour") or {}).get("used_percentage"),
                                      (limits.get("five_hour") or {}).get("resets_at")),
                               _limit("Week", (limits.get("seven_day") or {}).get("used_percentage"),
                                      (limits.get("seven_day") or {}).get("resets_at"))) if x],
        "context": {"percent": float(used), "size": int(size or 0), "used": tokens, "parts": []}
                   if isinstance(used, (int, float)) else None,
        "notes": [f"This session has cost ${cost:,.2f} at API prices"] if isinstance(cost, (int, float)) else [],
    }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # gone since it was listed
        return 0.0


def _codex_log(folder_key: str, root: Path) -> Path | None:
    """The newest Codex session log in that folder (its key: the folder's path with odd characters as _)."""
    for log in sorted(root.glob("*/*/*/rollout-*.jsonl"), key=_mtime, reverse=True)[:30]:
        try:
            with log.open("rb") as f:
                meta = json.loads(f.readline(1024 * 1024) or b"{}")
        except (OSError, ValueError):
            continue
        cwd = str((meta.get("payload") or {}).get("cwd", "")) if isinstance(meta, dict) else ""
        if cwd and re.sub(r"[^A-Za-z0-9_.-]+", "_", cwd)[-120:] == folder_key[-120:]:
            return log
    return None


def codex_name(session: str, root: Path = CODEX_SESSIONS, index: Path | None = None) -> str:
    """The name a Codex conversation was given (/rename, or Codex's own title), from Codex's session index: the
    newest log in that folder names its thread id, the index its newest name. "" when it has none."""
    log = _codex_log(session.removeprefix("codex-"), root)
    if not log:
        return ""
    try:
        with log.open("rb") as f:
            thread = str((json.loads(f.readline(1024 * 1024) or b"{}").get("payload") or {}).get("id", ""))
    except (OSError, ValueError, AttributeError):
        return ""
    name = ""
    try:
        with (index or root.parent / "session_index.jsonl").open("rb") as f:
            for raw in f:
                if thread.encode() in raw:
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if entry.get("id") == thread and entry.get("thread_name"):
                        name = str(entry["thread_name"]).strip()  # the last one wins: renamed again later
    except OSError:
        return ""
    return name


def codex(session: str, root: Path = CODEX_SESSIONS) -> dict:
    log = _codex_log(session.removeprefix("codex-"), root)
    if not log:
        return {}
    last = None
    try:
        with log.open("rb") as f:
            for raw in f:
                if b'"token_count"' in raw:
                    last = raw
    except OSError:
        return {}
    try:
        payload = (json.loads(last) or {}).get("payload") or {} if last else {}
    except ValueError:
        payload = {}
    limits = payload.get("rate_limits") or {}
    info = payload.get("info") or {}
    size = info.get("model_context_window")
    used = ((info.get("last_token_usage") or {}).get("input_tokens")) if isinstance(info, dict) else None
    found = []
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        if isinstance(window, dict):
            item = _limit(_window_name(window.get("window_minutes")), window.get("used_percent"), window.get("resets_at"))
            if item:
                found.append(item)
    context = None
    if isinstance(size, int) and size and isinstance(used, int):
        context = {"percent": round(100 * used / size, 1), "size": size, "used": used, "parts": []}
    plan = limits.get("plan_type")
    return {"model": "", "limits": found, "context": context,
            "notes": [f"Plan: {plan}"] if isinstance(plan, str) and plan else []}


CURSOR_ROOM = STATE / "cursor-usage"  # the hidden CLI's own folder: trusted once (--trust), nothing else in it
_plan: tuple[float, list[dict], list[str]] = (0.0, [], [])  # when read, limits, notes
PLAN_FOR = 600  # seconds the plan is kept before the CLI is asked again
FAILED_FOR = 120  # and after a failed read, before trying again (each try starts a CLI, up to 20 s)
PROMPT = "Plan, search, build anything"  # the empty input box of a new CLI chat, and nothing else on screen says it
_plan_lock = threading.Lock()  # one hidden CLI at a time: two would share (and kill) the same tmux server


def parse_cursor_usage(screen: str, today: datetime | None = None) -> tuple[list[dict], list[str]]:
    """The Cursor CLI's /usage panel, as text: (limits, notes). "Included 44% used" rows, "Resets Nov 30"."""
    today = today or datetime.now()
    resets = 0
    found = re.search(r"Resets\s+([A-Z][a-z]{2})\s+(\d{1,2})", screen)
    if found:
        try:
            day = datetime.strptime(f"{found.group(1)} {found.group(2)} {today.year}", "%b %d %Y")
            if day < today.replace(hour=0, minute=0, second=0, microsecond=0):
                day = day.replace(year=today.year + 1)
            resets = int(day.timestamp())
        except ValueError:
            pass
    limits = []
    for row in re.finditer(r"^\s*(Included|Auto|API|[A-Z][\w-]+)\s+(\d+(?:\.\d+)?)%\s+used", screen, re.M):
        label = {"Included": "Month: included", "Auto": "Month: Auto model", "API": "Month: other models"}.get(
            row.group(1), f"Month: {row.group(1)}")
        limits.append({"label": label, "percent": float(row.group(2)), "resets": resets})
    notes = []
    plan = re.search(r"Usage\s*•\s*(\S[^\n]*?)\s{2,}", screen)
    if plan:
        notes.append(f"Plan: {plan.group(1).strip()}")
    if re.search(r"On-demand usage is off", screen):
        notes.append("On-demand usage is off")
    return limits, notes


def cursor_plan(wait: float = 20.0, fresh: bool = False) -> tuple[list[dict], list[str]]:
    """Cursor's plan usage, as its CLI shows it under /usage: asked of a CLI started in its own tmux server, out of
    sight, and closed again; kept ten minutes. ([], []) when the CLI isn't there or didn't show it."""
    with _plan_lock:  # a second caller waits, then gets the first one's answer
        # fresh (a pull-down on the Usage tab): asked again, unless another caller just did
        if time.time() - _plan[0] < (5 if fresh else PLAN_FOR if _plan[1] else FAILED_FOR):
            return _plan[1], _plan[2]
        limits, notes = _ask_cursor(wait)
        _plan_set(limits, notes)
        return limits, notes


def _plan_set(limits: list[dict], notes: list[str]) -> None:
    global _plan
    _plan = (time.time(), limits, notes)


def _ask_cursor(wait: float) -> tuple[list[dict], list[str]]:
    """The hidden CLI's /usage. Nothing is typed until its empty input box is on screen, and nothing but /usage:
    a key typed into a question answers it (an "a" once trusted a folder, 2026-11-17)."""
    if not shutil.which("agent") or not shutil.which("tmux"):
        return [], []
    CURSOR_ROOM.mkdir(parents=True, exist_ok=True)
    tmux = ["tmux", "-L", "henchline-usage"]

    def screen() -> str:
        done = subprocess.run([*tmux, "capture-pane", "-p", "-t", "u"], capture_output=True, text=True, timeout=5)
        return done.stdout

    try:
        subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)
        subprocess.run([*tmux, "new-session", "-d", "-s", "u", "-x", "160", "-y", "40", "-c", str(CURSOR_ROOM),
                        "agent --trust"], capture_output=True, timeout=10, check=True)
        until = time.monotonic() + wait
        while PROMPT not in screen() and time.monotonic() < until:
            time.sleep(0.5)
        if PROMPT not in screen():  # a question, a login, an update: nothing is typed into it
            return [], []
        subprocess.run([*tmux, "send-keys", "-t", "u", "-l", "/usage"], timeout=5)
        time.sleep(0.5)
        if "/usage" not in screen():  # the words didn't land in its box: no Enter
            return [], []
        subprocess.run([*tmux, "send-keys", "-t", "u", "Enter"], timeout=5)
        shown = ""
        while time.monotonic() < until:
            shown = screen()
            if "% used" in shown or "Disabled" in shown:
                time.sleep(0.5)
                shown = screen()
                break
            time.sleep(0.5)
        limits, notes = parse_cursor_usage(shown)
    except (OSError, subprocess.SubprocessError):
        limits, notes = [], []
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)
    return limits, notes


def cursor(record: dict) -> dict:
    """From Cursor's own record of the chat (cursor_bridge.composer)."""
    if not record:
        return {}
    used, size, percent = record.get("contextTokensUsed"), record.get("contextTokenLimit"), record.get("contextUsagePercent")
    breakdown = record.get("promptTokenBreakdown") or {}
    parts = [{"label": str(c.get("label") or c.get("id") or "?"), "tokens": int(c.get("estimatedTokens") or 0)}
             for c in (breakdown.get("categories") or []) if isinstance(c, dict) and c.get("estimatedTokens")]
    context = None
    if isinstance(percent, (int, float)):
        context = {"percent": round(float(percent), 1), "size": int(size or 0), "used": int(used or 0), "parts": parts}
    model = (record.get("modelConfig") or {}).get("modelName") if isinstance(record.get("modelConfig"), dict) else ""
    limits, notes = cursor_plan()
    return {"model": str(model or ""), "limits": limits, "context": context, "notes": notes}


def _amount(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def words(found: dict, which: str) -> str:
    """The readout typed into the agent. `usage` is the plan limits; `context` is the window. The bars stay on the phone."""
    if which == "context":
        context = found.get("context") if isinstance(found.get("context"), dict) else None
        if not context or not isinstance(context.get("percent"), (int, float)):
            return "Context hasn't been reported for this chat."
        line = f"Context is {context['percent']:g}% full"
        size, used = int(context.get("size") or 0), int(context.get("used") or 0)
        if size:
            line += f", {_amount(used)} of {_amount(size)}"
        line += "."
        parts = [p for p in (context.get("parts") or []) if isinstance(p, dict)]
        if parts:
            line += " " + ", ".join(f"{p.get('label', '?')} {_amount(int(p.get('tokens') or 0))}" for p in parts[:8]) + "."
        return line
    limits = [item for item in (found.get("limits") or []) if isinstance(item, dict)]
    notes = [str(note) for note in (found.get("notes") or []) if str(note).strip()]
    if not limits and not notes:
        return "Usage hasn't been reported here."
    line = "Usage: " + ", ".join(f"{item.get('label', 'Limit')} {item.get('percent', 0):g}%" for item in limits) + "."
    if notes:
        line += " " + " ".join(notes)
        if not line.endswith("."):
            line += "."
    return line


def overview(fresh: bool = False) -> list[dict]:
    """Every AI on the laptop at once, for the phone's Usage tab: each one's plan limits (the account's, not one
    chat's), from its newest report. [{agent, limits, notes}]."""
    found = []
    newest = sorted(CLAUDE_STATUS.glob("*.json"), key=_mtime, reverse=True)
    if newest:
        got = claude(f"claude-{newest[0].stem}")
        if got.get("limits"):
            found.append({"agent": "claude", "limits": got["limits"], "notes": ["From your newest Claude session"]})
    logs = sorted(CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"), key=_mtime, reverse=True)
    for log in logs[:5]:
        last = None
        try:
            with log.open("rb") as f:
                for raw in f:
                    if b'"rate_limits"' in raw:
                        last = raw
        except OSError:
            continue
        if last:
            try:
                payload = json.loads(last).get("payload") or {}
            except ValueError:
                continue
            limits = payload.get("rate_limits") or {}
            items = [_limit(_window_name((limits.get(k) or {}).get("window_minutes")), (limits.get(k) or {}).get("used_percent"),
                            (limits.get(k) or {}).get("resets_at")) for k in ("primary", "secondary")]
            plan = limits.get("plan_type")
            found.append({"agent": "codex", "limits": [i for i in items if i],
                          "notes": [f"Plan: {plan}"] if isinstance(plan, str) and plan else []})
            break
    limits, notes = cursor_plan(fresh=fresh)
    if limits:
        found.append({"agent": "cursor", "limits": limits, "notes": notes})
    return found
