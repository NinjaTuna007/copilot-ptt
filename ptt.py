#!/usr/bin/env python3
"""Hold a configured key or chord on any keyboard to record, then paste and Enter.

The laptop Copilot key is F23. An external keyboard uses the chord in
~/.config/ptt/config.json (default: right Ctrl + right Shift).
Default mic route only: pw-record with no --target.
"""
import ctypes
import gc
import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from evdev import InputDevice, ecodes, list_devices

STATE = Path.home() / ".local/state/ptt"
LOG = STATE / "ptt.log"
WAV = STATE / "utterance.wav"
CONFIG = Path.home() / ".config/ptt/config.json"
DEFAULT_KEYS = ("F23",)
DEFAULT_CHORDS = (("RIGHTCTRL", "RIGHTSHIFT"),)
PHASE: queue.Queue[str] = queue.Queue()
XK_CONTROL_L = 0xFFE3
XK_SHIFT_L = 0xFFE1
XK_RETURN = 0xFF0D
XK_V = 0x76
TERMINALS = (
    "konsole",
    "yakuake",
    "kitty",
    "alacritty",
    "xterm",
    "gnome-terminal",
    "wezterm",
    "terminator",
    "tilix",
    "ghostty",
)


def log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    with LOG.open("a") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text())
    except json.JSONDecodeError as exc:
        log(f"bad config: {exc}")
        return {}


def keycode(name: str) -> int:
    code = getattr(ecodes, "KEY_" + name.upper(), None)
    if code is None:
        raise SystemExit(f"unknown key {name}")
    return code


def triggers(cfg: dict) -> tuple[set[int], list[frozenset[int]]]:
    keys = {keycode(name) for name in cfg.get("keys", DEFAULT_KEYS)}
    keys.add(keycode("F23"))
    chords = [
        frozenset(keycode(name) for name in chord)
        for chord in cfg.get("chords", DEFAULT_CHORDS)
    ]
    return keys, [chord for chord in chords if chord]


def key_name(code: int) -> str:
    raw = ecodes.KEY.get(code, str(code))
    if isinstance(raw, (list, tuple)):
        raw = raw[0]
    return str(raw).removeprefix("KEY_")


class Capture:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.armed = False
        self.gesture: set[int] = set()
        self.label = "Set hotkey"


CAPTURE = Capture()


def is_keyboard(dev: InputDevice) -> bool:
    if "input-remapper" in dev.name.lower():
        return False
    keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
    return ecodes.KEY_A in keys and ecodes.KEY_ENTER in keys and len(keys) > 40


def open_keyboards() -> dict[str, InputDevice]:
    found = {}
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if is_keyboard(dev):
            found[path] = dev
    return found


def on_battery() -> bool:
    try:
        return Path("/sys/class/power_supply/AC/online").read_text().strip() == "0"
    except OSError:
        return False


def want_cuda(cfg: dict) -> bool:
    device = cfg.get("device", "auto")
    if device == "cpu":
        return False
    if device == "cuda":
        return True
    return not on_battery()


def load_model(cfg: dict, cuda: bool):
    from faster_whisper import WhisperModel

    name = cfg.get("model", "small")
    if cuda:
        try:
            model = WhisperModel(name, device="cuda", compute_type="float16")
            log(f"model {name} on cuda")
            return model
        except Exception as exc:
            log(f"cuda model failed ({exc}); using cpu")
    model = WhisperModel(name, device="cpu", compute_type="int8")
    log(f"model {name} on cpu")
    return model


def set_phase(phase: str) -> None:
    PHASE.put(phase)


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def transcribe(model, path: Path, cfg: dict) -> str:
    lang = cfg.get("language", "en")
    if lang in (None, "", "auto"):
        lang = None
    segments, _info = model.transcribe(
        str(path), language=lang, vad_filter=True, beam_size=1
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def clipboard(text: str) -> bool:
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not bus:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    proc = subprocess.run(
        ["qdbus", "org.kde.klipper", "/klipper", "setClipboardContents", text],
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        log(f"clipboard set failed: {proc.stderr.strip()}")
        return False
    got = subprocess.run(
        ["qdbus", "org.kde.klipper", "/klipper", "getClipboardContents"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return got.stdout.strip() == text


def focused_class() -> str:
    try:
        root = subprocess.check_output(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True, stderr=subprocess.DEVNULL
        )
        wid = root.strip().split()[-1]
        if wid in {"0x0", "0"}:
            return ""
        cls = subprocess.check_output(
            ["xprop", "-id", wid, "WM_CLASS"], text=True, stderr=subprocess.DEVNULL
        )
        return cls.lower()
    except (subprocess.CalledProcessError, IndexError):
        return ""


def paste() -> None:
    os.environ.setdefault("DISPLAY", ":0")
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XFlush.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    disp = x11.XOpenDisplay(os.environ["DISPLAY"].encode())
    if not disp:
        log("no X display")
        return

    def tap(keysym: int, down: int) -> None:
        code = x11.XKeysymToKeycode(disp, keysym)
        if not code:
            log(f"no keycode for {keysym:#x}")
            return
        xtst.XTestFakeKeyEvent(disp, code, down, 0)
        x11.XFlush(disp)
        time.sleep(0.02)

    cls = focused_class()
    terminal = any(name in cls for name in TERMINALS)
    log(f"focus {cls.strip() or 'unknown'} chord={'ctrl+shift+v' if terminal else 'ctrl+v'}")
    if terminal:
        tap(XK_CONTROL_L, 1)
        tap(XK_SHIFT_L, 1)
        tap(XK_V, 1)
        tap(XK_V, 0)
        tap(XK_SHIFT_L, 0)
        tap(XK_CONTROL_L, 0)
    else:
        tap(XK_CONTROL_L, 1)
        tap(XK_V, 1)
        tap(XK_V, 0)
        tap(XK_CONTROL_L, 0)
    time.sleep(0.12)
    tap(XK_RETURN, 1)
    tap(XK_RETURN, 0)
    x11.XCloseDisplay(disp)


def ding() -> None:
    sound = "/usr/share/sounds/freedesktop/stereo/complete.oga"
    if os.path.exists(sound):
        subprocess.Popen(
            ["pw-play", sound],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def start_recording() -> subprocess.Popen:
    STATE.mkdir(parents=True, exist_ok=True)
    if WAV.exists():
        WAV.unlink()
    return subprocess.Popen(
        [
            "pw-record",
            "--rate",
            "16000",
            "--channels",
            "1",
            "--format",
            "s16",
            str(WAV),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_recording(proc: subprocess.Popen, started: float) -> Path | None:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    if time.monotonic() - started < 0.35 or not WAV.exists() or WAV.stat().st_size < 1000:
        return None
    return WAV


def handle_text(model, path: Path, cfg: dict) -> None:
    set_phase("transcribing")
    try:
        text = transcribe(model, path, cfg)
    except Exception as exc:
        log(f"transcribe failed: {exc}")
        return
    finally:
        set_phase("idle")
    if not text:
        log("empty")
        return
    log(f"text: {text}")
    if not clipboard(text):
        return
    time.sleep(0.08)
    paste()
    ding()


def main(cfg: dict, stop: threading.Event) -> None:
    single, chords = triggers(cfg)
    names = cfg.get("keys", list(DEFAULT_KEYS))
    chord_names = cfg.get("chords", [list(chord) for chord in DEFAULT_CHORDS])
    boards = open_keyboards()
    if not boards:
        raise SystemExit("no keyboard found")
    held: dict[int, set[int]] = {dev.fd: set() for dev in boards.values()}
    for dev in boards.values():
        log(f"keyboard {dev.name} {dev.path}")
    log(f"keys {names} chords {chord_names}")
    cuda = want_cuda(cfg)
    model = load_model(cfg, cuda)
    log("ready")
    while True:
        if stop.is_set():
            return
        fds = [dev.fd for dev in boards.values()]
        ready, _, _ = select.select(fds, [], [], 2.0)
        if not ready:
            cuda_now = want_cuda(cfg)
            if cuda_now != cuda:
                log("power source changed; reloading model")
                model = None
                gc.collect()
                model = load_model(cfg, cuda_now)
                cuda = cuda_now
            current = open_keyboards()
            for path, dev in current.items():
                if path in boards:
                    dev.close()
                    continue
                boards[path] = dev
                held[dev.fd] = set()
                log(f"keyboard {dev.name} {dev.path}")
            for path in list(boards):
                if path not in current:
                    dev = boards.pop(path)
                    held.pop(dev.fd, None)
                    dev.close()
                    log(f"removed {path}")
            continue
        for dev in boards.values():
            if dev.fd not in ready:
                continue
            try:
                events = list(dev.read())
            except OSError:
                continue
            down = held[dev.fd]
            pending = list(events)
            index = 0
            while index < len(pending):
                event = pending[index]
                index += 1
                if event.type != ecodes.EV_KEY or event.value == 2:
                    continue
                before = set(down)
                if event.value == 1:
                    down.add(event.code)
                elif event.value == 0:
                    down.discard(event.code)
                if CAPTURE.armed:
                    if event.code == ecodes.KEY_ESC:
                        with CAPTURE.lock:
                            CAPTURE.armed = False
                            CAPTURE.gesture.clear()
                            CAPTURE.label = "Set hotkey"
                        continue
                    if event.value == 1:
                        CAPTURE.gesture.add(event.code)
                        continue
                    if CAPTURE.gesture and CAPTURE.gesture.isdisjoint(down):
                        gesture = set(CAPTURE.gesture)
                        names = [key_name(code) for code in sorted(gesture)]
                        with CAPTURE.lock:
                            CAPTURE.armed = False
                            CAPTURE.gesture.clear()
                            CAPTURE.label = "Set hotkey"
                        if len(names) == 1:
                            cfg["keys"] = list(dict.fromkeys(["F23", *names]))
                            cfg["chords"] = []
                        else:
                            cfg["keys"] = ["F23"]
                            cfg["chords"] = [names]
                        save_config(cfg)
                        log("hotkey " + "+".join(names))
                    continue
                single, chords = triggers(cfg)
                release: set[int] | None = None
                if event.value == 1 and event.code in single:
                    release = {event.code}
                elif event.value == 1:
                    for chord in chords:
                        if chord <= down and not chord <= before:
                            release = set(chord)
                            break
                if release is None:
                    continue
                label = ecodes.KEY.get(event.code, str(event.code))
                log(f"record {label} on {dev.name}")
                set_phase("recording")
                proc = start_recording()
                started = time.monotonic()
                path = None

                def released(nxt) -> bool:
                    if nxt.type != ecodes.EV_KEY or nxt.value == 2:
                        return False
                    if nxt.value == 1:
                        down.add(nxt.code)
                    elif nxt.value == 0:
                        down.discard(nxt.code)
                    return nxt.value == 0 and nxt.code in release

                try:
                    while path is None:
                        for nxt in pending[index:]:
                            index = len(pending)
                            if released(nxt):
                                path = stop_recording(proc, started)
                                proc = None
                                break
                        if path is not None or proc is None:
                            break
                        got, _, _ = select.select([dev.fd], [], [], 60)
                        if not got:
                            break
                        pending = list(dev.read())
                        index = 0
                finally:
                    if proc is not None:
                        path = stop_recording(proc, started)
                index = len(pending)
                if path is None:
                    set_phase("idle")
                    log("too short")
                else:
                    handle_text(model, path, cfg)


def ptt_icon(phase: str):
    from PyQt5.QtCore import QPointF, Qt
    from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    color = {
        "recording": QColor("#e24a4a"),
        "transcribing": QColor("#e0a100"),
    }.get(phase, QColor("#d8d8d8"))
    pen = QPen(color)
    pen.setWidth(2)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush if phase == "idle" else color)
    painter.drawRoundedRect(3, 3, 16, 11, 3, 3)
    painter.drawPolygon([QPointF(7, 14), QPointF(11, 14), QPointF(6, 19)])
    painter.end()
    return QIcon(pixmap)


def run_tray(cfg: dict, stop: threading.Event) -> None:
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    tray = QSystemTrayIcon(ptt_icon("idle"))
    tray.setToolTip("Push to talk")
    menu = QMenu()

    def choose(key: str, value: str, actions: dict) -> None:
        cfg[key] = value
        save_config(cfg)
        for code, action in actions.items():
            action.setChecked(code == value)

    def add_group(title: str, key: str, options: list[tuple[str, str]]) -> None:
        sub = menu.addMenu(title)
        actions: dict[str, QAction] = {}
        current = cfg.get(key, options[0][1])
        for label, code in options:
            action = QAction(label, sub)
            action.setCheckable(True)
            action.setChecked(code == current)
            action.triggered.connect(lambda _checked, c=code, a=actions: choose(key, c, a))
            sub.addAction(action)
            actions[code] = action

    add_group("Language", "language", [("English", "en"), ("Hindi", "hi"), ("Detect", "auto")])
    add_group(
        "Power",
        "device",
        [("Auto, GPU on AC", "auto"), ("CPU", "cpu"), ("GPU", "cuda")],
    )
    hotkey = QAction("Set hotkey", menu)

    def arm_hotkey() -> None:
        with CAPTURE.lock:
            CAPTURE.armed = True
            CAPTURE.gesture = set()
            CAPTURE.label = "Press hotkey"
        hotkey.setText("Press hotkey")

    hotkey.triggered.connect(arm_hotkey)
    menu.addAction(hotkey)
    menu.addSeparator()
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(lambda: (stop.set(), app.quit()))
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log("no status tray; listener still running")
    tray.show()

    def poll() -> None:
        try:
            while True:
                phase = PHASE.get_nowait()
                tray.setIcon(ptt_icon(phase))
        except queue.Empty:
            pass
        with CAPTURE.lock:
            label = CAPTURE.label
        if hotkey.text() != label:
            hotkey.setText(label)

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(100)
    app.exec_()
    stop.set()


if __name__ == "__main__":
    try:
        ctypes.CDLL("libX11.so.6").XInitThreads()
        cfg = load_config()
        cfg.setdefault("model", "small")
        cfg.setdefault("device", "auto")
        cfg.setdefault("language", "en")
        cfg.setdefault("keys", ["F23"])
        cfg.setdefault("chords", [["RIGHTCTRL", "RIGHTSHIFT"]])
        save_config(cfg)
        stop = threading.Event()
        threading.Thread(target=main, args=(cfg, stop), daemon=True).start()
        run_tray(cfg, stop)
    except KeyboardInterrupt:
        sys.exit(0)
