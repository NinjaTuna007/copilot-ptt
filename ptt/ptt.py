#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""Hold a configured key or chord on any keyboard to record, then paste and Enter.

    ptt            the push-to-talk listener itself (ptt.service runs it); no other arguments

The laptop Copilot key is F23. An external keyboard uses the chord in
~/.config/ptt/config.json (default: right Ctrl + right Shift).
Default mic route only: pw-record with no --target.

keys.py notices the hotkey, paste.py types the words, tray.py is the icon;
this file records and transcribes in between.
"""
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
from evdev import ecodes

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE / "core"), str(HERE.parent)]  # push-to-talk's own modules (core/), and the repository for voice/
import ptt_tray as tray  # noqa: E402  (named apart from hands-free's tray: never two in one process)
from keys import HotkeyCapture, Hold, Keyboards  # noqa: E402
from voice import state as shared  # noqa: E402
from voice.desktop import current as desktop  # noqa: E402
from voice.state import BY_PTT, PTT_LOG, PTT_WAV, Phase  # noqa: E402
from voice.state import ptt_log as log  # noqa: E402
from voice.speech import nemotron  # noqa: E402
from voice.speech.whisper import Config, Transcriber  # noqa: E402

SHORTEST = 0.35  # seconds: a shorter press is a slip, not speech
SMALLEST = 1000  # bytes: a recording this small has no audio in it
RECORD = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]


class Recording:
    """pw-record from the default mic while the key is held: its samples, as they come, go to `live` (Nemotron
    hears the words while you talk), and into PTT_WAV once the key is let go (tone marks; Whisper's fallback)."""

    def __init__(self, live=None) -> None:
        if shutil.which("spd-say"):  # desktop speech stops; talkback pauses by itself from the phase
            subprocess.Popen(["spd-say", "-C"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        PTT_WAV.parent.mkdir(parents=True, exist_ok=True)
        PTT_WAV.unlink(missing_ok=True)
        self.live, self.audio, self.fed = live, bytearray(), 0
        # "-": raw 16-bit samples on stdout, read as they come
        self.proc = subprocess.Popen([*RECORD, "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.reader = threading.Thread(target=self._read, name="ptt-mic", daemon=True)
        self.reader.start()
        self.started = time.monotonic()

    def _read(self) -> None:
        while chunk := self.proc.stdout.read1(3200):  # 0.1 s at most per read
            self.audio += chunk
            if self.live:  # whole samples only, from where the last feed ended: a read may split one
                end = len(self.audio) - len(self.audio) % 2
                if end > self.fed:
                    self.live.feed(np.frombuffer(bytes(self.audio[self.fed:end]), dtype="<i2").astype(np.float32) / 32768)
                    self.fed = end

    def stop(self) -> Path | None:
        """The recording, or None if it was too short to hold speech."""
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        self.reader.join(timeout=2)
        if time.monotonic() - self.started < SHORTEST or len(self.audio) < SMALLEST:
            return None
        with wave.open(str(PTT_WAV), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(bytes(self.audio[: len(self.audio) - len(self.audio) % 2]))
        return PTT_WAV


class PushToTalk:
    def __init__(self, cfg: Config, stop: threading.Event) -> None:
        self.cfg, self.stop = cfg, stop
        self.capture = HotkeyCapture()
        self.phases: queue.Queue[Phase] = queue.Queue()  # for the tray icon
        self.texts: queue.Queue[tuple[str, str]] = queue.Queue()  # (plain, with tone marks), pasted by the tray
        self.transcriber: Transcriber | None = None  # loaded when the key listener starts
        self.streamer = None  # Nemotron, hearing the words while the key is held (when installed)

    def set_phase(self, phase: Phase) -> None:
        """For the tray icon, and for talkback, which goes quiet while we record."""
        self.phases.put(phase)
        shared.set_phase(phase, by=BY_PTT)

    def run(self) -> None:
        keyboards = Keyboards(self.cfg, self.capture)
        self.transcriber = Transcriber(self.cfg, PTT_LOG)
        if self.cfg.streaming and nemotron.available():
            try:
                self.streamer = nemotron.Streamer(self.cfg.hotwords, PTT_LOG)
            except Exception as exc:  # Whisper as before
                log(f"nemotron didn't load ({exc}): Whisper transcribes")
        log("ready")
        while not self.stop.is_set():
            ready = keyboards.wait()
            for dev in ready:
                keyboards.read(dev, self.hold)
            if ready:
                continue
            self.transcriber.settle()  # mains or battery; back to the GPU once a training run lets go
            keyboards.rescan()

    def hold(self, dev, event, hold: Hold) -> None:
        """Record while the hotkey is held, then transcribe."""
        log(f"record {ecodes.KEY.get(event.code, str(event.code))} on {dev.name}")
        self.set_phase(Phase.RECORDING)
        live = self.streamer.begin() if self.streamer else None
        recording = Recording(live)
        try:
            hold.until_released()
        except BaseException:
            recording.stop()
            if live:
                live.cancel()
            raise
        path = recording.stop()
        if path is None:
            if live:
                live.cancel()
            self.set_phase(Phase.IDLE)
            log("too short")
        else:
            self.transcribe(path, live)

    def heard_live(self, path: Path, live) -> tuple[str, str] | None:
        """Nemotron's words for the recording (with tone marks), or None: Whisper then transcribes it."""
        began = time.monotonic()
        heard = live.finish()
        if not heard or not heard[0]:
            log("nemotron heard nothing: Whisper transcribes")
            return None
        text = nemotron.tidy(heard[0], self.cfg.hotwords)
        log(f"heard by nemotron {time.monotonic() - began:.2f}s after the key was let go")
        rich = text
        if self.cfg.tone:
            try:
                from voice.speech import tone

                rich = tone.annotate(path, heard[1], text)
            except Exception as exc:  # tone is extra; the words must still get through
                log(f"tone failed: {exc}")
        return text, rich

    def transcribe(self, path: Path, live=None) -> None:
        self.set_phase(Phase.TRANSCRIBING)
        try:
            got = self.heard_live(path, live) if live else None
            if not got:
                began = time.monotonic()
                got = self.transcriber.file(path)  # out of GPU memory: on the CPU, words kept
                log(f"heard by whisper {time.monotonic() - began:.2f}s after the key was let go")
            text, rich = got
        except Exception as exc:
            log(f"transcribe failed: {exc}")
            return
        finally:
            self.set_phase(Phase.IDLE)
        if not text:
            log("empty")
            return
        log(f"text: {text}")
        if rich != text:
            log(f"tone: {rich}")
        self.texts.put((text, rich))


if __name__ == "__main__":
    if {"-h", "--help"} & set(sys.argv[1:]):  # not a second listener (it was, before 2026-11-16)
        print(__doc__)
        sys.exit(0)
    # The desktop restores a tray app it saved at logout, on top of the service. That second copy is the
    # extra headset. The service is the one that should be running.
    if any(a == "-session" or a.startswith("-session=") for a in sys.argv[1:]):
        sys.exit(0)
    try:
        desktop().prepare_threads()
        cfg = Config.load(PTT_LOG)
        cfg.save()  # every setting in the file, so it can be edited
        stop = threading.Event()
        ptt = PushToTalk(cfg, stop)
        threading.Thread(target=ptt.run, daemon=True).start()
        tray.run(cfg, stop, ptt.capture, ptt.phases, ptt.texts)
    except KeyboardInterrupt:
        sys.exit(0)
