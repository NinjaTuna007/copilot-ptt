"""Pictures of what an agent is looking at, for `handsfree show`: its terminal pane drawn as an image (from tmux's
own text, so it works with the screen locked and no window raised), or the whole screen.
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
         "/usr/share/fonts/TTF/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf")
BACKGROUND, FOREGROUND = (22, 25, 31), (214, 218, 226)
SIZE = 15
MAX_LINES = 200
SGR = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]")
BASIC = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188), (17, 168, 205),
         (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67), (59, 142, 234),
         (214, 112, 214), (41, 184, 219), (255, 255, 255)]


def palette(n: int) -> tuple[int, int, int]:
    """One of the 256 terminal colours."""
    if n < 16:
        return BASIC[n]
    if n < 232:
        n -= 16
        return tuple(0 if v == 0 else 55 + 40 * v for v in (n // 36, n // 6 % 6, n % 6))  # type: ignore[return-value]
    grey = 8 + 10 * (n - 232)
    return grey, grey, grey


def runs(line: str) -> list[tuple[str, tuple, tuple, bool]]:
    """A line of tmux's text with colour codes as (text, foreground, background, bold) runs."""
    fg, bg, bold = FOREGROUND, None, False
    out, at = [], 0

    def put(text: str) -> None:
        if text:
            out.append((text, fg, bg, bold))

    for found in SGR.finditer(line):
        put(OTHER_ESCAPES.sub("", line[at:found.start()]))
        at = found.end()
        codes = [int(c) if c.isdigit() else 0 for c in re.split(r"[;:]", found.group(1))] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                fg, bg, bold = FOREGROUND, None, False
            elif c == 1:
                bold = True
            elif c == 22:
                bold = False
            elif 30 <= c <= 37:
                fg = BASIC[c - 30 + (8 if bold else 0)]
            elif 90 <= c <= 97:
                fg = BASIC[c - 90 + 8]
            elif c == 39:
                fg = FOREGROUND
            elif 40 <= c <= 47:
                bg = BASIC[c - 40]
            elif 100 <= c <= 107:
                bg = BASIC[c - 100 + 8]
            elif c == 49:
                bg = None
            elif c in (38, 48) and i + 1 < len(codes):
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    colour, i = palette(codes[i + 2] & 255), i + 2
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    colour, i = tuple(min(v, 255) for v in codes[i + 2:i + 5]), i + 4
                else:
                    colour = None
                if colour:
                    fg, bg = (colour, bg) if c == 38 else (fg, colour)
            i += 1
    put(OTHER_ESCAPES.sub("", line[at:]))
    return out


def draw(text: str, path: Path) -> Path:
    """The terminal text (with its colour codes) as a PNG."""
    from PIL import Image, ImageDraw, ImageFont
    font_file = next((f for f in FONTS if Path(f).exists()), None)
    font = ImageFont.truetype(font_file, SIZE) if font_file else ImageFont.load_default()
    cell_w = int(font.getlength("M")) or 9
    cell_h = SIZE + 5
    lines = text.rstrip("\n").split("\n")[-MAX_LINES:]
    while lines and not re.sub(r"\x1b\[[0-9;:]*m", "", lines[-1]).strip():  # the empty rest of the pane
        lines.pop()
    lines = lines or [""]
    parsed = [runs(line) for line in lines]
    columns = max([sum(len(t) for t, *_ in line) for line in parsed] + [20])
    pad = 12
    image = Image.new("RGB", (columns * cell_w + 2 * pad, len(lines) * cell_h + 2 * pad), BACKGROUND)
    pen = ImageDraw.Draw(image)
    for row, line in enumerate(parsed):
        x, y = pad, pad + row * cell_h
        for piece, fg, bg, bold in line:
            width = len(piece) * cell_w
            if bg:
                pen.rectangle([x, y, x + width, y + cell_h], fill=bg)
            pen.text((x, y), piece, font=font, fill=fg)
            if bold:
                pen.text((x + 1, y), piece, font=font, fill=fg)
            x += width
    image.save(path, "PNG", optimize=True)
    return path


def pane_text(pane: str) -> str | None:
    """What a tmux pane shows now, colour codes kept, its scroll-back's last screenful with it."""
    try:
        done = subprocess.run(["tmux", "capture-pane", "-p", "-e", "-S", "-60", "-t", pane], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def screen(path: Path) -> Path | None:
    """The whole screen, as the desktop's own tool takes it. While the screen is locked this is the lock screen."""
    if shutil.which("spectacle"):
        cmd = ["spectacle", "-b", "-n", "-f", "-o", str(path)]
    elif shutil.which("import"):
        cmd = ["import", "-window", "root", str(path)]
    else:
        return None
    try:
        subprocess.run(cmd, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


_SKIP = ("plasmashell", "kwin", "ksmserver", "krunner", "spectacle", "kscreenlocker")


def windows() -> list[dict]:
    """Windows that can be pictured by id, without raising them: [{id, app, title}]. The desktop and the lock
    screen are left out. Reading a window by id does not need the screen unlocked."""
    from voice.desktop import x11

    found = []
    for wid in x11.windows():
        cls = x11.window_class(wid)
        if any(skip in cls for skip in _SKIP):
            continue
        names = re.findall(r'"([^"]+)"', cls)
        app = (names[-1] if names else "").strip()
        title = " ".join(x11.window_title(wid).split())
        if not app and not title:
            continue
        found.append({"id": wid, "app": app[:40], "title": title[:140]})
    return found


def matching(found: list[dict], query: str) -> list[dict]:
    """Windows whose app or title contains the query."""
    needle = query.strip().lower()
    if not needle:
        return []
    return [item for item in found if needle in item["app"].lower() or needle in item["title"].lower()]


def window(wid: str, path: Path) -> Path | None:
    """That window's own picture, by its id, without raising it or pressing a key. Works while the screen is
    locked: the pixels are the window's, not whatever is painted over the desktop."""
    if not re.fullmatch(r"0x[0-9a-fA-F]+", wid) or not shutil.which("import"):
        return None
    try:
        subprocess.run(["import", "-window", wid, str(path)], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


def folder() -> Path:
    """Where the pictures wait to be sent: in the user's own state folder, cleaned of old ones."""
    from voice.state import STATE
    where = STATE / "show"
    where.mkdir(mode=0o700, parents=True, exist_ok=True)
    for old in where.glob("*.png"):
        if time.time() - old.stat().st_mtime > 86400:
            old.unlink(missing_ok=True)
    return where


def name() -> str:
    return time.strftime("show-%H%M%S-") + secrets.token_hex(2) + ".png"


def announce(of: str) -> None:
    """The phone is told a picture of a window or the whole screen was taken. Any trusted caller can ask for one,
    so it must not happen unseen. The topic is never printed."""
    title = "A picture was taken"
    try:
        topic_file = Path.home() / ".config/ptt/ntfy-topic"
        topic = topic_file.read_text().strip() if topic_file.exists() else ""
        if not topic:
            return
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=f"A picture of {of} was taken.".encode(),
            headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "lock"}), timeout=10).close()
    except (OSError, UnicodeError, ValueError):
        from voice.state import HANDSFREE_LOG, log
        log(HANDSFREE_LOG, f"phone notice not sent: {title}")


def take(kind: str, wid: str = "", pane: str = "", of: str = "") -> tuple[Path | None, str]:
    """A picture for the phone: the terminal pane (drawn, so a locked screen is fine), one window by id (its own
    pixels, not the lock screen painted over it), or the whole screen. A window or the whole screen is also told
    to the phone."""
    path = folder() / name()
    if kind == "screen":
        got = screen(path)
        if got:
            announce(of or "the whole screen")
        return (got, "") if got else (None, "Couldn't take the screen.")
    if kind == "window":
        got = window(wid, path)
        if got:
            announce(of or "a window")
        return (got, "") if got else (None, "Couldn't take that window.")
    text = pane_text(pane) if pane else None
    if text is None:
        return None, "This agent has no terminal to draw."
    try:
        draw(text, path)
    except (ImportError, OSError):
        return None, "Couldn't draw the terminal."
    return path, ""
