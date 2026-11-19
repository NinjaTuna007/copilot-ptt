"""Henchline's one icon in the system tray, for push-to-talk and hands-free alike (the user's design, 2026-11-17).

KDE's own headset (the icon theme's "audio-headset"), a little smaller, its colour the state: grey idle, cyan while
hands-free listens, red while you are recorded, amber with a spinning arc while your words are written down, a
green ring on a phone call, a red dot when a service stopped. A click opens a small panel with each service's
state and the last timings; the menu restarts a service (never during a call or while words are being heard) and
holds push-to-talk's settings. Hands-free hides its own icon while this one runs.

Qt wants the main thread, so push-to-talk's words to paste come here through a queue and are pasted here.
"""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import paste
from keys import HotkeyCapture
from voice.state import AWAY, HANDSFREE_LOG, HANDSFREE_MODE, LAST, Phase, in_call, phase_now, read_json
from voice.state import ptt_log as log
from voice.speech.whisper import Config, ding

POLL_MS = 100
SPIN_MS = 70  # the transcribing icon's breathing step
IDLE_MS = 400  # otherwise: a look at the state a few times a second (the laptop may be on battery)
LANGUAGES = [("English", "en"), ("Hindi", "hi"), ("Detect", "auto")]
POWER = [("Auto, GPU on AC", "auto"), ("CPU", "cpu"), ("GPU", "cuda")]
UNITS = [("Push-to-talk", "ptt.service"), ("Hands-free", "handsfree.service"), ("Phone calls", "call.service")]
UNIT_DIR = Path.home() / ".config/systemd/user"
GREY, CYAN, RED, AMBER, GREEN = "#8a93a3", "#2bc4d9", "#e5484d", "#f5a623", "#3dbe6b"
WHITE = "#dfe5ec"  # hands-free on, only the wake word gets in: the panel's own light colour, calm
TIPS = {"idle": "Henchline: idle", "waiting": 'Henchline: hands-free on, waiting for "hey Claude"',
        "listening": "Henchline: listening for your answer", "recording": "Henchline: recording you",
        "transcribing": "Henchline: writing your words down", "call": "Henchline: on a phone call",
        "away": "Henchline: away. Not listening; agents text your phone instead of speaking"}


def unit_states() -> dict[str, str]:
    """Each installed service's state ("active", "inactive", "failed", …); not installed: left out."""
    states = {}
    for _name, unit in UNITS:
        if not (UNIT_DIR / unit).exists():
            continue
        done = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5)
        states[unit] = done.stdout.strip() or "unknown"
    return states


def state_now(units: dict[str, str]) -> str:
    """What the icon shows: a call first, then your voice being recorded or written down, then listening."""
    if in_call():
        return "call"
    phase, _by = phase_now()
    if phase is Phase.RECORDING:
        return "recording"
    if phase is Phase.TRANSCRIBING:
        return "transcribing"
    if AWAY.exists():  # push-to-talk still records and writes down (above); nothing else listens or speaks
        return "away"
    if units.get("handsfree.service") == "active":
        # Actively listening only in the moments after an agent spoke (no wake word needed then); otherwise it waits
        try:
            mode = HANDSFREE_MODE.read_text().strip()
        except OSError:
            mode = ""
        return "listening" if mode == "listening" else "waiting"
    return "idle"


def last_lines(path: Path, pattern: str, count: int = 400) -> str:
    """The newest log line matching (timings only: these lines hold no words of the user's)."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64 * 1024))
            lines = f.read().decode(errors="replace").splitlines()[-count:]
    except OSError:
        return ""
    return next((line for line in reversed(lines) if re.search(pattern, line)), "")


HEADSETS = [Path("/usr/share/icons") / theme / "devices" / size / "audio-headset.svg"
            for theme in ("breeze-dark", "breeze") for size in ("22", "64")]
_headset = []


def headset_icon():
    """KDE's headset: from the icon theme, else straight from Breeze's file (the theme lookup came back empty in
    the service, and the tray showed plain dots, 2026-11-17)."""
    from PyQt5.QtGui import QIcon

    if not _headset:
        found = QIcon.fromTheme("audio-headset")
        if found.isNull():
            found = next((QIcon(str(path)) for path in HEADSETS if path.exists()), found)
        _headset.append(found)
    return _headset[0]


def icon(state: str, problem: bool, angle: int = 0):
    """Hands-free's old icon, as it was (the user's wish, 2026-11-17: the cleaner look): KDE's headset at its own
    22 px, plain while hands-free waits for the wake word, dimmed when it is off, tinted for the rest. Transcribing
    breathes (its colour fades in and out); a small red dot marks a stopped service."""
    from PyQt5.QtCore import QRectF, Qt
    from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

    headset = headset_icon()
    tint = {"listening": CYAN, "recording": RED, "transcribing": AMBER, "call": GREEN}.get(state)
    out = QPixmap(22, 22)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    if headset.isNull():  # no headset anywhere: a plain disc, so the state still shows
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(tint or WHITE))
        painter.drawEllipse(QRectF(5, 5, 12, 12))
    else:
        glyph = headset.pixmap(22, 22)
        if tint:
            tinter = QPainter(glyph)
            tinter.setCompositionMode(QPainter.CompositionMode_SourceIn)
            tinter.fillRect(glyph.rect(), QColor(tint))
            tinter.end()
        if state in ("idle", "away"):
            painter.setOpacity(0.45)  # hands-free off: there, but quiet
        elif state == "transcribing":
            painter.setOpacity(0.55 + 0.45 * abs((angle % 360) - 180) / 180)  # breathing while it works
        painter.drawPixmap(0, 0, glyph)
        painter.setOpacity(1)
    if problem:
        painter.setPen(QPen(QColor("#232629"), 1.5))
        painter.setBrush(QColor(RED))
        painter.drawEllipse(QRectF(14.5, 0.5, 7, 7))
    painter.end()
    return QIcon(out)


class Panel:
    """The left-click panel: each service, where your voice goes, the last timings, and anything wrong."""

    def __init__(self, cfg: Config) -> None:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget

        self.cfg = cfg
        self.window = QWidget(None, Qt.Popup)
        layout = QVBoxLayout(self.window)
        layout.setContentsMargins(14, 12, 14, 12)
        self.text = QLabel()
        self.text.setTextFormat(Qt.RichText)
        self.text.setMinimumWidth(0)  # as wide as its rows, no wider (an empty right half, 2026-11-17)
        layout.addWidget(self.text)

    def show(self, units: dict[str, str], where) -> None:
        """A small card: one row per service with a dot, where your voice goes, and one quiet line of timings (the
        first version, every detail in a list, was too busy, 2026-11-17)."""
        mode = ""
        try:
            mode = HANDSFREE_MODE.read_text().strip()
        except OSError:
            pass
        words = {"ptt.service": {"active": "ready"},
                 "handsfree.service": {"active": "listening" if mode == "listening" else "waiting for the wake word"},
                 "call.service": {"active": "on a call" if in_call() else "ready"}}
        rows = []
        for name, unit in UNITS:
            state = units.get(unit)
            if state is None:
                continue
            said = words[unit].get(state, "off" if state == "inactive" else state)
            dot = GREEN if state == "active" else (RED if state == "failed" else GREY)
            rows.append(f'<tr valign="middle"><td style="color:{dot}; padding-right:8px">●</td><td><b>{name}</b></td>'
                        f'<td style="color:#9aa3b0; padding-left:14px">{said}</td></tr>')
        speaker = read_json(LAST)
        to = (f'<div style="margin-top:8px">Talking to <b>{speaker["agent"].capitalize()}</b>'
              f'<span style="color:#9aa3b0"> · {speaker.get("where") or "?"}</span></div>') if speaker.get("agent") else ""
        timings = []
        for label, line in (("wake", last_lines(HANDSFREE_LOG, r"wake word confirmed by")),
                            ("last turn", last_lines(HANDSFREE_LOG, r"heard by (nemotron|whisper) [0-9.]+s"))):
            found = re.search(r"([0-9.]+)s", line.split(" by ", 1)[-1]) if line else None
            if found:
                timings.append(f"{label} {found.group(1)} s")
        quiet = (f'<div style="margin-top:6px; color:#9aa3b0; font-size:small">{" · ".join(timings)}</div>'
                 if timings else "")
        self.text.setText(f'<table cellspacing="3">{"".join(rows)}</table>{to}{quiet}')
        self.window.adjustSize()
        from PyQt5.QtWidgets import QApplication

        screen = (QApplication.screenAt(where) or QApplication.primaryScreen()).availableGeometry()
        x = min(max(where.x() - self.window.width() // 2, screen.left()), screen.right() - self.window.width())
        y = where.y() - self.window.height() - 8  # above the icon on a bottom panel, below it on a top one
        if y < screen.top():
            y = where.y() + 16
        self.window.move(x, y)
        self.window.show()


def run(cfg: Config, stop: threading.Event, capture: HotkeyCapture, phases: queue.Queue,
        texts: queue.Queue) -> None:
    from PyQt5.QtCore import QCoreApplication, Qt, QTimer
    from PyQt5.QtGui import QCursor
    from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon

    # systemd starts this. The desktop must not save it and start another after a reboot.
    QCoreApplication.setAttribute(Qt.AA_DisableSessionManager, True)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    box = [unit_states()]  # the services' states, swapped whole by the watcher (never seen half-filled)
    shown = {"state": "", "problem": None, "angle": 0}
    tray = QSystemTrayIcon(icon("idle", False))
    tray.setToolTip(TIPS["idle"])
    panel = Panel(cfg)
    menu = QMenu()

    # --- each service: its state, and a restart when it is safe -----------------------------------------------
    status: dict[str, QAction] = {}
    restart: dict[str, QAction] = {}
    for name, unit in UNITS:
        status[unit] = QAction(name, menu)
        status[unit].setEnabled(False)
        menu.addAction(status[unit])
    menu.addSeparator()
    for name, unit in UNITS:
        restart[unit] = QAction(f"Restart {name.lower()}", menu)
        restart[unit].triggered.connect(
            lambda _c, u=unit: subprocess.Popen(["systemctl", "--user", "restart", u], start_new_session=True))
        menu.addAction(restart[unit])
    handsfree = QAction("Turn hands-free on", menu)
    handsfree.triggered.connect(lambda: subprocess.Popen(
        [str(Path.home() / ".local/bin/handsfree"),
         "off" if box[0].get("handsfree.service") == "active" else "on"], start_new_session=True))
    menu.addAction(handsfree)
    # Away (the user's wish, 2026-11-17): on another call or with people; the phone's /away is the same switch
    away = QAction("Away", menu)
    away.setToolTip("Nothing listens at the laptop; agents text your phone")
    away.setCheckable(True)

    def toggle_away(on: bool) -> None:
        if on:
            AWAY.parent.mkdir(parents=True, exist_ok=True)
            AWAY.touch()
        else:
            AWAY.unlink(missing_ok=True)
        log(f"tray: away {'on' if on else 'off'}")

    away.toggled.connect(toggle_away)
    menu.aboutToShow.connect(lambda: (away.blockSignals(True), away.setChecked(AWAY.exists()), away.blockSignals(False)))
    menu.addAction(away)
    menu.addSeparator()

    def refresh_menu() -> None:
        """Just before it opens: states as they are, and no restart that would cut words off."""
        units = box[0]
        phase, by = phase_now()
        busy = phase is not Phase.IDLE
        for name, unit in UNITS:
            state = units.get(unit)
            status[unit].setText(f"{name}: {state or 'not installed'}")
            restart[unit].setVisible(state is not None)
            why = ""
            if unit in ("handsfree.service", "call.service") and in_call():
                why = "on a call"
            elif unit == "handsfree.service" and busy and by != "ptt":
                why = "hearing you"
            elif unit == "ptt.service" and busy and by == "ptt":
                why = "hearing you"
            restart[unit].setEnabled(not why)
            restart[unit].setText(f"Restart {name.lower()}" + (f" (not now: {why})" if why else ""))
        handsfree.setVisible("handsfree.service" in units)
        handsfree.setText("Turn hands-free off" if units.get("handsfree.service") == "active" else "Turn hands-free on")

    menu.aboutToShow.connect(refresh_menu)

    # --- push-to-talk's settings, as before -------------------------------------------------------------------
    def choose(key: str, value: str, actions: dict) -> None:
        setattr(cfg, key, value)
        cfg.save()
        for code, action in actions.items():
            action.setChecked(code == value)

    def add_group(title: str, key: str, options: list[tuple[str, str]]) -> None:
        sub = menu.addMenu(title)
        actions: dict[str, QAction] = {}
        current = getattr(cfg, key)
        for label, code in options:
            action = QAction(label, sub)
            action.setCheckable(True)
            action.setChecked(code == current)
            action.triggered.connect(lambda _checked, c=code, a=actions: choose(key, c, a))
            sub.addAction(action)
            actions[code] = action

    add_group("Language", "language", LANGUAGES)
    add_group("Power", "device", POWER)
    hotkey = QAction(capture.IDLE_LABEL, menu)

    def arm_hotkey() -> None:
        capture.arm()
        hotkey.setText(capture.ARMED_LABEL)

    hotkey.triggered.connect(arm_hotkey)
    menu.addAction(hotkey)
    chime = QAction("Chime", menu)
    chime.setCheckable(True)
    chime.setChecked(cfg.chime)

    def toggle_chime(checked: bool) -> None:
        cfg.chime = bool(checked)
        cfg.save()

    chime.toggled.connect(toggle_chime)
    menu.addAction(chime)
    menu.addSeparator()
    quit_action = QAction("Quit push-to-talk", menu)
    quit_action.triggered.connect(lambda: (stop.set(), app.quit()))
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: panel.show(box[0], QCursor.pos())
                           if reason == QSystemTrayIcon.Trigger else None)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log("no status tray; listener still running")
    tray.show()

    # --- the services' states, asked off the main thread (systemctl can be slow) ------------------------------
    def watch_units() -> None:
        while not stop.is_set():
            try:
                box[0] = unit_states()
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(2)

    threading.Thread(target=watch_units, name="tray-units", daemon=True).start()

    def draw() -> None:
        units = box[0]
        state = state_now(units)
        problem = any(v in ("failed", "inactive") for u, v in units.items() if u in ("call.service",)) or \
            any(v == "failed" for v in units.values())
        if state == "transcribing":
            shown["angle"] = (shown["angle"] + 18) % 360
        if (state, problem) != (shown["state"], shown["problem"]) or state == "transcribing":
            tray.setIcon(icon(state, problem, shown["angle"]))
            if state != shown["state"]:
                tray.setToolTip(TIPS[state])
            shown["state"], shown["problem"] = state, problem
        wanted = SPIN_MS if state == "transcribing" else IDLE_MS  # a fast tick only while it breathes
        if spinner.interval() != wanted:
            spinner.setInterval(wanted)

    def poll() -> None:
        while not phases.empty():
            phases.get_nowait()  # the icon reads the shared phase itself (push-to-talk's and hands-free's)
        while not texts.empty():
            if paste.deliver(*texts.get_nowait()):
                ding(cfg)
        with capture.lock:
            label = capture.label
        if hotkey.text() != label:
            hotkey.setText(label)

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(POLL_MS)
    spinner = QTimer()  # the icon: every SPIN_MS (the arc turns while transcribing; otherwise a cheap no-op)
    spinner.timeout.connect(draw)
    spinner.start(SPIN_MS)
    app.exec_()
    stop.set()
