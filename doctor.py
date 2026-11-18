#!/usr/bin/env python3
"""Check each piece and say plainly what is missing, with the command that fixes it.

    ./doctor.py [ptt|talkback|handsfree|agents ...]

Runs on the system Python with the standard library only, so it works when
the venv itself is what is broken. Exit status 1 if anything failed.
"""

from __future__ import annotations

import grp
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # voice/state.py: standard library only
from voice.desktop import kde_session, wayland_session  # noqa: E402
from voice.state import CALL_TLS  # noqa: E402
from voice.state import EC_CONF as CANCELLER_CONF, EC_SOURCE, KOKORO_MODELS as KOKORO, VENV, WAKE_MODEL  # noqa: E402
NEMOTRON = WAKE_MODEL.parent / "nemotron"  # voice/speech/nemotron.py
WHISTLE = WAKE_MODEL.parent / "whistle/whistle.cact"  # voice/speech/whistle.py

HOME = Path.home()
PYTHON = VENV / "bin/python3"
BIN = HOME / ".local/bin"
LOUDEST_SAFE = 80  # % speaker volume above which hands-free hears its own voice (README, 2026-11-15)


class Result(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"


@dataclass
class Check:
    piece: str
    what: str
    result: Result
    detail: str = ""
    fix: str = ""

    def line(self) -> str:
        mark = {Result.OK: "  ok  ", Result.WARN: " warn ", Result.FAIL: " FAIL "}[self.result]
        text = f"[{mark}] {self.what}" + (f": {self.detail}" if self.detail else "")
        return text + (f"\n         fix: {self.fix}" if self.fix and self.result is not Result.OK else "")


def run(*cmd: str) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def venv_imports(*modules: str) -> tuple[bool, str]:
    if not PYTHON.exists():
        return False, "no venv"
    code = "import importlib,sys\n" + "".join(f"importlib.import_module({m!r})\n" for m in modules)
    rc, out = run(str(PYTHON), "-E", "-s", "-c", code)
    return rc == 0, out.splitlines()[-1] if out else ""


def unit_state(unit: str) -> tuple[str, str]:
    return (run("systemctl", "--user", "is-enabled", unit)[1] or "missing",
            run("systemctl", "--user", "is-active", unit)[1] or "inactive")


def link_into_repo(path: Path) -> bool:
    return path.is_symlink() and ROOT in path.resolve().parents


def check(piece: str, what: str, ok: bool, detail: str = "", fix: str = "", warn_only: bool = False) -> Check:
    return Check(piece, what, Result.OK if ok else (Result.WARN if warn_only else Result.FAIL), detail, fix)


# --- the pieces ----------------------------------------------------------------

def common() -> list[Check]:
    session = os.environ.get("XDG_SESSION_TYPE", "")
    kde = kde_session()
    checks = [
        check("all", "private venv", (VENV / "bin/pip").exists(), str(VENV), "./install.sh ptt (or any piece)"),
        check("all", "PyQt5 and evdev from apt, seen by the venv",
              (VENV / "system/PyQt5").exists() and (VENV / "system/evdev").exists(), "",
              "sudo apt install python3-pyqt5 python3-evdev && ./install.sh ptt"),
    ]
    if wayland_session():
        desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
        sway = bool(os.environ.get("SWAYSOCK")) or bool({"SWAY", "I3"} & set(desktops))
        hypr = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) or "HYPRLAND" in desktops
        ydotool, wtype = bool(shutil.which("ydotool")), bool(shutil.which("wtype"))
        checks += [
            check("all", "Wayland session", True, session or os.environ.get("WAYLAND_DISPLAY", "wayland")),
            check("all", "wl-clipboard (the clipboard)",
                  bool(shutil.which("wl-copy") and shutil.which("wl-paste")), "",
                  "sudo apt install wl-clipboard"),
            check("all", "ydotool (keys in the focused window)", ydotool,
                  "" if ydotool else ("wtype instead: wlroots compositors only" if wtype else "no keys"),
                  "sudo apt install ydotool, start ydotoold, and join the input group (or install wtype on sway)"),
        ]
        if kde:
            have_qdbus = bool(shutil.which("qdbus6") or shutil.which("qdbus"))
            checks.append(check("all", "KWin scripting (the focused window, KDE)",
                                have_qdbus and bool(shutil.which("journalctl")),
                                "", "install qdbus (part of Plasma) and systemd's journalctl"))
        elif sway:
            checks.append(check("all", "swaymsg (the focused window, sway)", bool(shutil.which("swaymsg")), "",
                                "install sway"))
        elif hypr:
            checks.append(check("all", "hyprctl (the focused window, Hyprland)", bool(shutil.which("hyprctl")), "",
                                "install Hyprland"))
        else:
            checks.append(check("all", "a compositor that names its windows",
                                False, f"none of KDE, sway or Hyprland ({os.environ.get('XDG_CURRENT_DESKTOP', '?')})",
                                "the clipboard and keys work; the focused window and Cursor's window need "
                                "KDE Plasma, sway or Hyprland", warn_only=True))
    else:
        rc, klipper = run("qdbus", "org.kde.klipper", "/klipper", "getClipboardContents") if kde else (1, "")
        checks += [
            check("all", "X11 session", session in ("x11", ""), session or "unknown",
                  "log in to an X11 session, or install the Wayland tools (see the README)"),
            check("all", "Klipper clipboard (KDE)", rc == 0, "" if rc == 0 else klipper[:80],
                  "start Klipper (KDE clipboard), or enable it in the system tray") if kde else
            check("all", "xclip and xprop (a desktop without KDE)",
                  bool(shutil.which("xclip") and shutil.which("xprop")), "",
                  "install xclip and xprop (Debian/Ubuntu: x11-utils; Fedora: xprop) with your package manager"),
        ]
    return checks


def ptt() -> list[Check]:
    ok, err = venv_imports("faster_whisper", "av", "ctranslate2")
    rc, av = run(str(PYTHON), "-E", "-s", "-c", "import av; print(av.__version__)") if PYTHON.exists() else (1, "")
    enabled, active = unit_state("ptt.service")
    config = HOME / ".config/ptt/config.json"
    try:
        json.loads(config.read_text())
        config_ok, config_err = True, ""
    except (OSError, ValueError) as exc:
        config_ok, config_err = False, str(exc)[:80]
    checks = [
        check("ptt", "Whisper in the venv", ok, err, "./install.sh ptt"),
        check("ptt", "av 18 (av 19 breaks faster-whisper 1.2.1)", av.startswith("18."), av, "./install.sh ptt"),
        check("ptt", "member of the input group (reads the hotkey)",
              "input" in [grp.getgrgid(g).gr_name for g in os.getgroups()], "",
              f"sudo usermod -aG input {os.environ.get('USER', '$USER')} (then log in again)"),
        check("ptt", "pw-record", bool(shutil.which("pw-record")), "", "sudo apt install pipewire-bin"),
        check("ptt", "settings file", config_ok, config_err or str(config), "./install.sh ptt (writes a fresh one)"),
        check("ptt", "ptt command", link_into_repo(BIN / "ptt"), "", "./install.sh ptt"),
        check("ptt", "service running and on at login", enabled == "enabled" and active == "active",
              f"{enabled}, {active}", "systemctl --user enable --now ptt.service"),
    ]
    if shutil.which("nvidia-smi"):
        ok, err = venv_imports("nvidia.cublas", "nvidia.cudnn")
        checks.append(check("ptt", "CUDA libraries for Whisper on the GPU", ok, err, "./install.sh ptt",
                            warn_only=True))
    return checks


def talkback() -> list[Check]:
    ok, err = venv_imports("kokoro_onnx", "soundfile")
    models = [f for f in ("kokoro-v1.0.onnx", "voices-v1.0.bin") if not (KOKORO / f).is_file()]
    return [
        check("talkback", "Kokoro in the venv", ok, err, "./install.sh talkback"),
        check("talkback", "Kokoro voice models", not models, ", ".join(models) and f"missing {', '.join(models)}",
              "./install.sh talkback (downloads them)"),
        check("talkback", "talkback command", link_into_repo(BIN / "talkback"), "", "./install.sh talkback"),
        check("talkback", "pacat (plays the voice)", bool(shutil.which("pacat")), "", "sudo apt install pulseaudio-utils"),
    ]


def speaker_volume() -> Check:
    rc, sink = run("pactl", "get-default-sink")
    _, volume = run("pactl", "get-sink-volume", "@DEFAULT_SINK@")
    percents = [int(v) for v in re.findall(r"(\d+)%", volume)]
    speakers = rc == 0 and not re.search(r"bluez|headphone|headset|usb", sink, re.I)
    loudest = max(percents, default=0)
    return check("handsfree", "speaker volume", not speakers or loudest <= LOUDEST_SAFE,
                 f"{loudest}% on {'the laptop speakers' if speakers else 'earbuds/headphones'}",
                 f"pactl set-sink-volume @DEFAULT_SINK@ {LOUDEST_SAFE}%  (louder, and hands-free hears itself)",
                 warn_only=True)


def handsfree() -> list[Check]:
    ok, err = venv_imports("openwakeword", "sklearn", "scipy")
    features = PYTHON.parent.parent / "lib"
    feature_models = list(features.glob("python3*/site-packages/openwakeword/resources/models/melspectrogram.onnx"))
    conf = CANCELLER_CONF.read_text() if CANCELLER_CONF.exists() else ""
    mic = re.search(r'target\.object\s*=\s*"([^"]+)"', conf)
    rc, sources = run("pactl", "list", "short", "sources")
    c_enabled, c_active = unit_state("talkback-echo-cancel.service")
    h_enabled, h_active = unit_state("handsfree.service")
    return [
        check("handsfree", "wake word and analysis packages in the venv", ok, err, "./install.sh handsfree"),
        check("handsfree", "wake word feature models", bool(feature_models), "", "./install.sh handsfree"),
        check("handsfree", "Nemotron streaming (turns heard as you talk)",
              all((NEMOTRON / f).is_file() for f in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx",
                                                       "tokens.txt", "bpe.vocab")),
              "" if NEMOTRON.joinpath("bpe.vocab").is_file() else "missing: Whisper transcribes turns",
              "./install.sh handsfree", warn_only=True),
        check("handsfree", "Whistle (fast wake words on battery)", WHISTLE.is_file(),
              "" if WHISTLE.is_file() else "missing: Whisper confirms them",
              "./install.sh handsfree", warn_only=True),
        check("handsfree", "personal wake word", WAKE_MODEL.exists(), "",
              "handsfree/tools/wakeword_collect.py, then handsfree/tools/wakeword_train.py", warn_only=True),
        check("handsfree", "echo canceller settings", bool(conf), str(CANCELLER_CONF), "./install.sh handsfree"),
        check("handsfree", "echo canceller's mic exists", bool(mic) and mic.group(1) in sources,
              mic.group(1) if mic else "no mic set (uses the default mic)", "./install.sh handsfree (picks the mic again)",
              warn_only=not mic),
        check("handsfree", "echo canceller running", c_active == "active" and EC_SOURCE in sources, c_active,
              "handsfree on (starts it)", warn_only=h_active != "active"),
        check("handsfree", "hands-free", h_active == "active", f"{h_active}, {h_enabled} at login",
              "handsfree on", warn_only=True),
        check("handsfree", "handsfree command", link_into_repo(BIN / "handsfree"), "", "./install.sh handsfree"),
        speaker_volume(),
    ]


def agents() -> list[Check]:
    claude_md = HOME / ".claude/CLAUDE.md"
    checks = [check("agents", "talkback skill link", link_into_repo(HOME / ".local/share/talkback/skill"), "",
                    "./install.sh agents")]
    if (HOME / ".claude").is_dir():
        imported = claude_md.exists() and "agents/AGENTS-snippet.md" in claude_md.read_text()
        checks.append(check("agents", "Claude Code imports the voice rules", imported, str(claude_md),
                            "./install.sh agents"))
    if (HOME / ".cursor").is_dir():
        checks.append(check("agents", "Cursor voice rule", (HOME / ".cursor/rules/voice.mdc").exists(), "",
                            "./install.sh agents"))
    if (HOME / ".codex").is_dir():
        codex = HOME / ".codex/AGENTS.md"
        checks.append(check("agents", "Codex reads the voice rules",
                            codex.exists() and "## Talkback" in codex.read_text(), str(codex), "./install.sh agents"))
    return checks


def call() -> list[Check]:
    ok, err = venv_imports("aiohttp")
    enabled, active = unit_state("call.service")
    rc, ip = run("tailscale", "ip", "-4")
    _, served = run("tailscale", "serve", "status")
    return [
        check("call", "web server package in the venv", ok, err, "./install.sh call"),
        check("call", "Tailscale up (the phone reaches the page through it)", rc == 0, ip.splitlines()[0] if ip else "",
              "sudo tailscale up"),
        check("call", "the page's certificate", (CALL_TLS / "cert.pem").exists(), str(CALL_TLS), "./install.sh call"),
        check("call", "a proper address (tailscale serve, a real certificate)", ":8790" in served,
              next((line for line in served.splitlines() if line.startswith("https://")), "self-signed instead"),
              "sudo tailscale set --operator=$USER; systemctl --user restart call", warn_only=True),
        check("call", "call page running", active == "active", f"{active}, {enabled} at login",
              "./install.sh call", warn_only=True),
    ]


PIECES = {"ptt": ptt, "talkback": talkback, "handsfree": handsfree, "call": call, "agents": agents}


def main() -> int:
    if {"-h", "--help"} & set(sys.argv[1:]):
        print(__doc__)
        return 0
    wanted = sys.argv[1:] or list(PIECES)
    unknown = [w for w in wanted if w not in PIECES]
    if unknown:
        sys.exit(f"unknown piece: {', '.join(unknown)} (pieces: {', '.join(PIECES)})")
    checks = common() + [c for name in wanted for c in PIECES[name]()]
    piece = None
    for c in checks:
        if c.piece != piece:
            piece = c.piece
            print(f"\n{piece}")
        print(c.line())
    failed = sum(c.result is Result.FAIL for c in checks)
    warned = sum(c.result is Result.WARN for c in checks)
    print(f"\n{failed} failed, {warned} to look at, {len(checks) - failed - warned} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
