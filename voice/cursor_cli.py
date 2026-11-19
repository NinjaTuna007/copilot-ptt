"""The Cursor CLI's model menu, as the editor's is: one model, then its context, effort and fast.

`cursor-agent models` lists every variant as its own row. The chat's `/model` does not take those, nor the
bracket form its help mentions (tried: both open "no matches"). It filters by the model's own id, and Tab opens
an editor whose rows are the catalog's parameters. A pick on the phone is driven through that editor.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

# The CLI treats a line as a slash command when it is `/word…` or when whitespace, a slash and a word run to the
# end. That opens a picker and the rest of the line is the filter, so the words never arrive as a message.
# As the CLI matches it: whitespace, a slash, then a word running to the end of the line (`/model please`).
# A path (`/home/x/y`) does not match, because a slash follows the word with no space.
SLASH = re.compile(r"^(?P<pre>[\s\S]*?)(?P<gap>\s+)/((?:[\w-]+(?:\s+.*)?)?)$")
ROW = re.compile(r"^\s*(→)?\s*([○●◯◉])\s+(.+?)\s*(✓)?\s*$")
OPEN = ("Edit Parameters", "Tab to modify", "Filter:")
STEP = 0.25  # the editor drops keys that arrive together
# The CLI lists Auto as the slug "auto". Cursor's catalog calls that same model "default".
ALIASES = {"auto": "default"}


def menu(catalog: list[dict], slugs: list[str]) -> list[dict]:
    """Catalog models the CLI actually lists. A slug belongs to the longest catalog id it extends
    (`composer-2.5-fast` is its own model when that id exists, not the fast switch of `composer-2.5`).
    "auto" is the catalog's Auto."""
    ids = [m["id"] for m in catalog if m.get("id")]
    used = set()
    for slug in slugs:
        slug = ALIASES.get(slug, slug)
        hits = [i for i in ids if slug == i or slug.startswith(i + "-")]
        if hits:
            used.add(max(hits, key=len))
    return [m for m in catalog if m["id"] in used]


def cli_slug(model_id: str) -> str:
    """The slug `/model` filters on. Auto's catalog id is "default"; the CLI spells it "auto"."""
    for slug, catalog_id in ALIASES.items():
        if catalog_id == model_id:
            return slug
    return model_id


def on_model(chat: str, model_id: str) -> bool:
    """The chat's own record is this model. Auto is stored as "default" or "auto"."""
    got = saved(chat).get("model") or ""
    if model_id == "default":
        return got in ("default", "auto")
    return got == model_id


def reading(status: str, models: list[dict]) -> dict:
    """What the status line is on: {"model": id, "params": {id: value}}. The longest model name that starts the
    line wins, then each parameter's longest matching value ("Extra High" before "High")."""
    text = " ".join(status.split())
    entry = None
    for model in models:
        name = str(model.get("name", "")).strip()
        if name and text.lower().startswith(name.lower()) and (len(text) == len(name) or text[len(name)] == " "):
            if entry is None or len(name) > len(str(entry["name"])):
                entry = model
    if entry is None:
        return {"model": "", "params": {}}
    rest = text[len(str(entry["name"]).strip()):].strip()
    params = {}
    for param in entry.get("params") or []:
        if param.get("kind") == "bool":
            params[param["id"]] = "true" if param.get("name", "").lower() in rest.lower().split() else "false"
            continue
        values = [v for v in param.get("values") or [] if v.get("name") and v["name"].lower() in rest.lower()]
        if values:
            params[param["id"]] = max(values, key=lambda v: len(v["name"]))["value"]
    return {"model": entry["id"], "params": params}


def _store_meta(chat: str) -> dict:
    """The CLI chat's own meta row. {} when that store isn't there or can't be read."""
    if not re.fullmatch(r"[0-9a-f-]{36}", chat):
        return {}
    stores = list((Path.home() / ".cursor/chats").glob(f"*/{chat}/store.db"))
    if len(stores) != 1:
        return {}
    try:
        with contextlib.closing(sqlite3.connect(f"file:{stores[0]}?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("select value from meta where key = '0'").fetchone()
        raw = row[0] if row else ""
        if isinstance(raw, bytes):
            raw = raw.decode()
        text = bytes.fromhex(raw).decode() if isinstance(raw, str) and re.fullmatch(r"[0-9a-f]+", raw) else str(raw)
        meta = json.loads(text) if text else {}
    except (OSError, sqlite3.Error, ValueError, UnicodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _pairs(raw) -> dict:
    if not isinstance(raw, list):
        return {}
    return {str(item["id"]): str(item["value"]) for item in raw
            if isinstance(item, dict) and item.get("id") is not None and item.get("value") is not None}


def saved(chat: str) -> dict:
    """What this CLI chat is on, from its own records: {"model", "params"}. The store keeps the model id.
    Context, effort and fast are the CLI config's selected model when that is this one, else the parameters
    it last saved for that model. A Konsole tab has no screen to read, and this does not need one."""
    raw = str(_store_meta(chat).get("lastUsedModel") or "")
    model = ALIASES.get(raw, raw)  # the store says "auto"; the menu's id for it is "default"
    if not model:
        return {"model": "", "params": {}}
    try:
        cfg = json.loads((Path.home() / ".cursor/cli-config.json").read_text())
    except (OSError, ValueError, UnicodeError):
        return {"model": model, "params": {}}
    selected = cfg.get("selectedModel") if isinstance(cfg.get("selectedModel"), dict) else {}
    selected_id = ALIASES.get(str(selected.get("modelId") or ""), str(selected.get("modelId") or ""))
    params = _pairs(selected.get("parameters")) if selected_id == model else {}
    if not params:
        table = cfg.get("modelParameters") if isinstance(cfg.get("modelParameters"), dict) else {}
        params = _pairs(table.get(raw) or table.get(model))
    return {"model": model, "params": params}


def described(entry: dict | None, params: dict) -> str:
    """The model, its effort, its context and fast, the way the chip says them."""
    name = str((entry or {}).get("name") or "")
    effort = context = fast = ""
    for param in (entry or {}).get("params") or []:
        value = params.get(param.get("id", ""))
        if param.get("id") in ("reasoning_effort", "reasoning", "effort"):
            effort = next((v["name"] for v in param.get("values") or [] if v.get("value") == value), "")
        elif param.get("id") == "context" and value:
            context = str(value).upper()
        elif param.get("id") == "fast" and value == "true":
            fast = "fast"
    return " · ".join(part for part in (name, effort, context, fast) if part)


def disarm(text: str) -> str:
    """Break a slash the CLI would treat as a command, without changing a real path the pattern does not match.
    A zero-width space after the slash still reads as the slash."""
    found = SLASH.match(text)
    if found and found.group("gap"):
        at = found.end("gap")
        return text[:at + 1] + "\u200b" + text[at + 1:]
    if re.match(r"/[\w-]+(?:\s.*)?$", text):  # the whole line is the command: `/model please`
        return "/\u200b" + text[1:]
    return text


def picker_open(lines: list[str]) -> bool:
    """The model picker is up, or the prompt itself is still `/model …` from a switch that did not clear."""
    tail = lines[-8:]
    if any("models matching" in line.lower() or line.lower().lstrip().startswith("filter:") for line in tail):
        return True
    return any(re.search(r"→\s*/model\b", line) for line in tail)


def clear_picker(address: str) -> None:
    """Close a leftover model picker and clear that line, so the next message is not typed into its filter."""
    from voice.desktop import tmux
    shown = tmux.out("capture-pane", "-p", "-t", address).splitlines()
    if not picker_open(shown):
        return
    tmux.tmux("send-keys", "-t", address, "Escape")
    time.sleep(STEP)
    tmux.tmux("send-keys", "-t", address, "C-u")
    time.sleep(STEP)


def valid(entry: dict, picked: dict) -> bool:
    """Every parameter the model has, each one a value it offers."""
    allowed = {p["id"]: {v["value"] for v in p.get("values") or []} for p in entry.get("params") or []}
    return set(picked) == set(allowed) and all(v in allowed[k] for k, v in picked.items())


def editor_screen(entry: dict, current: dict) -> list[str]:
    """The CLI's parameter editor as it is drawn: catalog order, the arrow on the first choice, current values
    marked on. Konsole has no screen to read, and this is the same layout the tmux path reads."""
    lines = [f" {entry.get('name', '')} — Edit Parameters"]
    first = True
    for param in entry.get("params") or []:
        if param.get("kind") == "bool":
            on = current.get(param.get("id", "")) == "true"
            mark = "◉" if on else "◯"
            arrow = "→ " if first else "  "
            lines.append(f" {arrow}{mark} {param.get('name', '')}")
            first = False
            continue
        lines.append(f"  {param.get('name', '')}")
        for value in param.get("values") or []:
            on = current.get(param.get("id", "")) == value.get("value")
            mark = "●" if on else "○"
            tick = " ✓" if on else ""
            arrow = "→ " if first else "  "
            lines.append(f" {arrow}{mark} {value.get('name', '')}{tick}")
            first = False
    return lines


def remembered(model_id: str) -> dict:
    """The parameters the CLI last saved for this model, from its own config."""
    try:
        cfg = json.loads((Path.home() / ".cursor/cli-config.json").read_text())
    except (OSError, ValueError, UnicodeError):
        return {}
    selected = cfg.get("selectedModel") if isinstance(cfg.get("selectedModel"), dict) else {}
    if selected.get("modelId") in (model_id, cli_slug(model_id)):
        found = _pairs(selected.get("parameters"))
        if found:
            return found
    table = cfg.get("modelParameters") if isinstance(cfg.get("modelParameters"), dict) else {}
    raw = table.get(model_id)
    if not isinstance(raw, list):
        raw = table.get(cli_slug(model_id))
    return _pairs(raw)


def agreed(model_id: str, wanted: dict) -> bool:
    """The CLI's selected model is this one, with these parameters."""
    try:
        cfg = json.loads((Path.home() / ".cursor/cli-config.json").read_text())
    except (OSError, ValueError, UnicodeError):
        return False
    selected = cfg.get("selectedModel") if isinstance(cfg.get("selectedModel"), dict) else {}
    if selected.get("modelId") not in (model_id, cli_slug(model_id)):
        return False
    got = _pairs(selected.get("parameters"))
    return all(got.get(key) == value for key, value in wanted.items())


def _rows(lines: list[str]) -> list[dict]:
    found = []
    for line in lines:
        match = ROW.match(line)
        if match:
            found.append({"arrow": bool(match.group(1)), "on": match.group(2) in "●◉" or bool(match.group(4)),
                          "label": match.group(3).strip()})
    return found


def _label(param: dict, value: str) -> str:
    if param.get("kind") == "bool":
        return str(param.get("name") or "")
    return next((v["name"] for v in param.get("values") or [] if v["value"] == value), "")


def plan(lines: list[str], entry: dict, wanted: dict) -> tuple[list[str], str]:
    """Keys that put the open editor on `wanted`: Down or Up, then Enter. A row already on its value is left.
    ("", why) when the editor isn't the one this model should show."""
    rows = _rows(lines)
    if not rows:
        return [], "Cursor's model editor didn't show its choices."
    pos = next((i for i, row in enumerate(rows) if row["arrow"]), 0)
    keys = []
    for param in entry.get("params") or []:
        label = _label(param, wanted.get(param["id"], ""))
        hits = [i for i, row in enumerate(rows) if row["label"].lower() == label.lower()]
        if len(hits) != 1:
            return [], f"Cursor's editor has no {param.get('name', label)}."
        at = hits[0]
        want_on = wanted.get(param["id"]) == "true" if param.get("kind") == "bool" else True
        if rows[at]["on"] == want_on:
            continue
        keys += ["Down"] * (at - pos) if at >= pos else ["Up"] * (pos - at)
        keys.append("Enter")
        pos = at
    return keys, ""


def status_of(lines: list[str]) -> str:
    """The model the Cursor CLI's status line shows, effort and context included. Run Everything and MAX are the
    usual marks; a line with a percent is the same bar on another approval mode."""
    for line in reversed(lines):
        if "Run Everything" in line or " · MAX" in line or re.search(r"\d+(?:\.\d+)?%", line):
            return re.split(r"\s{2,}| · ", line.strip())[0]
    return ""


def row_title(line: str) -> str:
    """The model name on a picker row, before the gap and the '(Tab to modify)' mark. 'Composer 2.5 Fast' is not
    'Composer 2.5'."""
    title = line.split("(Tab to modify)")[0].replace("→", " ").strip()
    return re.split(r"\s{2,}", title)[0].strip()


def highlighted_title(lines: list[str]) -> str:
    """The picker row that has the cursor. '(Tab to modify)' is only printed on that row."""
    marked = [line for line in lines if "Tab to modify" in line]
    if not marked:
        return ""
    chosen = next((line for line in marked if "→" in line), marked[0])
    return row_title(chosen)


def _lines(address: str) -> list[str]:
    from voice.desktop import tmux
    return tmux.out("capture-pane", "-p", "-t", address).splitlines()


def _key(address: str, key: str) -> bool:
    from voice.desktop import tmux
    done = tmux.tmux("send-keys", "-t", address, key)
    return bool(done and done.returncode == 0)


def _paste(address: str, text: str) -> bool:
    from voice.desktop import tmux
    buffer = f"henchline-{os.getpid()}-{time.monotonic_ns()}"
    loaded = tmux.tmux("load-buffer", "-b", buffer, "-", given=text)
    pasted = loaded and loaded.returncode == 0 and tmux.tmux("paste-buffer", "-d", "-p", "-b", buffer, "-t", address)
    if not pasted or pasted.returncode != 0:
        tmux.tmux("delete-buffer", "-b", buffer)
        return False
    return True


def _open(lines: list[str]) -> bool:
    return any(mark in line for line in lines for mark in OPEN)


def _close(address: str) -> None:
    for _ in range(3):
        if not _open(_lines(address)):
            break
        _key(address, "Escape")
        time.sleep(STEP)
    if any(re.search(r"→\s*/model\b", line) for line in _lines(address)[-8:]):
        _key(address, "C-u")  # Escape closes the picker and leaves `/model …` in the prompt
        time.sleep(STEP)


def _until(address: str, needle: str, seconds: float = 3.0) -> list[str]:
    shown, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        shown = _lines(address)
        if any(needle in line for line in shown):
            return shown
        time.sleep(0.2)
    return shown


def apply(address: str, entry: dict, wanted: dict) -> str:
    """Drive the CLI's `/model` editor onto `wanted`. "" when the status line agrees, else why not."""
    if _open(_lines(address)):
        _close(address)
    if not _paste(address, f"/model {entry['id']}"):
        return "Couldn't type into it."
    shown = _until(address, "Tab to modify")
    if not any("Tab to modify" in line for line in shown):
        _close(address)
        return f"Cursor has no model {entry['id']}."
    title = highlighted_title(shown)
    if title.lower() != entry["name"].lower():
        for _ in range(6):  # the filter also matches a longer name, and that one may be the row already on
            if not _key(address, "Down"):
                break
            time.sleep(STEP)
            shown = _lines(address)
            title = highlighted_title(shown)
            if title.lower() == entry["name"].lower():
                break
        if title.lower() != entry["name"].lower():
            _close(address)
            return f"Cursor highlighted {title or 'another model'} instead of {entry['name']}."
    if not _key(address, "Tab"):
        _close(address)
        return "Couldn't open the model editor."
    shown = _until(address, "Edit Parameters")
    if not any("Edit Parameters" in line for line in shown):
        _close(address)
        return "Cursor didn't open the model editor."
    keys, why = plan(shown, entry, wanted)
    if why:
        _close(address)
        return why
    if not keys:  # every row is already the wanted value: Enter still saves, and that is what switches the model
        keys = ["Enter"]
    for key in keys:
        if not _key(address, key):
            _close(address)
            return "Couldn't set that in the model editor."
        time.sleep(STEP)
    _close(address)
    deadline = time.monotonic() + 4
    status = ""
    while time.monotonic() < deadline:
        status = status_of(_lines(address))
        if reading(status, [entry])["params"] == {p["id"]: wanted[p["id"]] for p in entry.get("params") or []
                                                   if p["id"] in wanted} and reading(status, [entry])["model"] == entry["id"]:
            return ""
        time.sleep(0.4)
    return "Switched, but I couldn't read what Cursor is on now." if not status else f"Switched, but Cursor shows {status}."


# What a Konsole pty receives for those keys while the CLI's editor has asked for application cursor keys.
_KONSOLE_KEYS = {"Down": "\x1bOB", "Up": "\x1bOA", "Tab": "\t", "Enter": "\r", "Escape": "\x1b", "C-u": "\x15"}


def apply_tab(tab, entry: dict, wanted: dict, chat: str = "") -> str:
    """Drive the model editor in tmux, where the screen can be read, or in Konsole, where it cannot.
    `chat` is the CLI chat id, so a Konsole switch is only called done when that chat's own record moves."""
    from voice.desktop import tmux
    if not tab:
        return "That agent isn't running."
    if tab.owner == tmux.OWNER:
        return apply(tab.address, entry, wanted)
    if str(tab.address).startswith("/Sessions/"):
        return apply_konsole(tab, entry, wanted, chat)
    return "That Cursor chat isn't in a terminal whose model editor I can drive."


def longer_model(entry: dict, catalog: list[dict]) -> str:
    """A catalog id that extends this one (`composer-2.5-fast` extends `composer-2.5`). Tab on the first filter
    row would edit that longer model. "" when this id is not a prefix of another."""
    prefix = entry.get("id", "") + "-"
    return next((str(m.get("name") or m.get("id")) for m in catalog
                 if str(m.get("id", "")).startswith(prefix)), "")


def apply_konsole(tab, entry: dict, wanted: dict, chat: str = "") -> str:
    """The same key sequence as tmux, typed into the Konsole session. Checked against the CLI's own config,
    because Konsole does not give the screen back. Refuses when another listed model id extends this one: the
    filter can land on that longer row, and Tab would edit it."""
    from voice.desktop.cursor_bridge import catalog
    from voice.desktop.kde_x11 import qdbus_ok

    longer = longer_model(entry, catalog())
    if longer:
        return f"Cursor also lists {longer}, and this terminal can't show which row is highlighted, so I didn't switch."

    def text(body: str) -> bool:
        return qdbus_ok(tab.owner, tab.address, "sendText", body)

    def press(key: str) -> bool:
        seq = _KONSOLE_KEYS.get(key)
        return bool(seq) and text(seq)

    def shut() -> None:
        for _ in range(3):
            press("Escape")
            time.sleep(STEP)

    if not text(f"/model {cli_slug(entry['id'])}"):
        return "Couldn't type into it."
    time.sleep(0.8)
    if not press("Tab"):
        shut()
        return "Couldn't open the model editor."
    time.sleep(0.5)
    keys, why = plan(editor_screen(entry, remembered(entry["id"])), entry, wanted)
    if why:
        shut()
        return why
    if not keys:
        keys = ["Enter"]
    for key in keys:
        if not press(key):
            shut()
            return "Couldn't set that in the model editor."
        time.sleep(STEP)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        # The global default moves as soon as the editor is touched. The chat's own record is the one the pill shows.
        if agreed(entry["id"], wanted) and (not chat or on_model(chat, entry["id"])):
            press("Escape")  # the editor is still up; Escape leaves the switch in place
            return ""
        time.sleep(0.4)
    shut()
    still = saved(chat).get("model") if chat else ""
    if still in ("default", "auto"):
        still = "Auto"
    if still and still not in (entry["id"], cli_slug(entry["id"]), entry.get("name")):
        return (f"This chat is still on {still}. Cursor's default for a new chat moved, "
                "and this one did not.")
    return f"Cursor didn't switch to {entry.get('name', entry['id'])}."
