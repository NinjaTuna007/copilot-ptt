"""What hands-free senses besides the mic stream itself: whether a frame
sounds like a voice, the "hey Claude" wake word, earbud taps, how loud the
speakers are playing, and the echo canceller that feeds the mic stream."""

from __future__ import annotations

import collections
import difflib
import pickle
import re
import select
import subprocess
import threading
import time

import numpy as np
from faster_whisper.vad import VadOptions

from voice.state import (EC_SOURCE, EC_UNIT, HANDSFREE_LOG, JARVIS_MODEL, JARVIS_PERSONAL, WAKE_MODEL, log,
                         speaker_monitor)

RATE = 16000
FRAME = 480  # 30 ms at 16 kHz
PER_SECOND = RATE // FRAME  # 33 frames
LEVEL_SPAN = PER_SECOND // 2 + 1  # frames compared against the playback: the last half second
# Silero must find a voice before anything counts. While a clip plays the canceller
# squashes the user's voice, but 0.3/150 ms cut talkback off on its own echo and
# 0.45/200 ms on a throat clear (2026-11-14): 0.5/300 ms
SPEECH = VadOptions(threshold=0.6, min_speech_duration_ms=250, min_silence_duration_ms=100, speech_pad_ms=0)
SPEECH_OVER = VadOptions(threshold=0.5, min_speech_duration_ms=300, min_silence_duration_ms=100, speech_pad_ms=0)
VOICE_SHARE = 0.5  # share of a frame's energy below 1 kHz for it to sound like a voice


def level(frame: np.ndarray) -> float:
    """Loudness of a frame in dB (full scale)."""
    return float(20 * np.log10(np.sqrt((frame ** 2).mean()) + 1e-6))


def voiced(frame: np.ndarray) -> bool:
    """Speech carries most of its energy below 1 kHz; a spoon on a mug does not."""
    power = np.abs(np.fft.rfft(frame * np.hanning(len(frame)))) ** 2
    freqs = np.fft.rfftfreq(len(frame), 1 / RATE)
    total = power[freqs >= 80].sum()
    return bool(total) and power[(freqs >= 80) & (freqs < 1000)].sum() / total > VOICE_SHARE


def mic_stream(device: str) -> subprocess.Popen:
    """Raw 16 kHz mono s16 from a PipeWire source, read FRAME samples at a time."""
    return subprocess.Popen(["parec", f"--device={device}", "--format=s16le", f"--rate={RATE}",
                             "--channels=1", "--raw", "--latency-msec=30"], stdout=subprocess.PIPE)


def read_frame(stream: subprocess.Popen) -> np.ndarray | None:
    data = stream.stdout.read(FRAME * 2)
    if len(data) < FRAME * 2:
        return None
    return np.frombuffer(data, dtype="<i2")


def start_canceller(restart: bool = False) -> float:
    """Start (or restart) the echo canceller unit; return when it started (monotonic).

    It is its own unit so restarting hands-free does not reset it: a fresh
    canceller has to learn the room again, and leaks our voice meanwhile.
    """
    # Running is not enough: when PipeWire itself restarts (it crashed during a Zoom meeting, 2026-11-16), the
    # canceller's process lives on with its source gone, and "start" does nothing. Missing after a wait: restarted
    for attempt in (restart, True):
        subprocess.run(["systemctl", "--user", "restart" if attempt else "start", EC_UNIT], check=False)
        for _ in range(50):
            sources = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
            if EC_SOURCE in sources:
                since = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveEnterTimestampMonotonic",
                                        "--value", EC_UNIT], capture_output=True, text=True).stdout.strip()
                return int(since) / 1e6 if since.isdigit() else time.monotonic()
            time.sleep(0.1)
        if attempt:
            break
    raise SystemExit("echo canceller did not come up")


class EchoWatch:
    """How close our own voice comes back to the playback, clip by clip.

    A healthy canceller kept our echo 20 dB or more under the playback; it
    drifted to 10 dB under (2026-11-14, laptop overloaded), and from 15 dB a
    clip gets cut. The drift shows in short bursts, not on average: a 90th
    percentile per clip stayed near 58 dB under while bursts that sounded like
    speech came within 19 dB (2026-11-15). So a burst that sounds like speech,
    was taken for echo, and came within EARLY of the playback is a near miss,
    and a near miss asks for a fresh canceller before any cut happens; so does
    a clip whose loudest echo came that close (9 and 12 dB under, just before
    two cuts on 2026-11-15).
    """

    EARLY = -22.0  # dB: a near miss, or a clip's closest echo, nearer the playback than this asks for a restart
    SPEAKING = -40.0  # dB: playback quieter than this is a pause, not a voice to compare against
    REST = 5 * 60.0  # seconds between restarts: if a fresh canceller does no better, restarting again won't help

    def __init__(self, restarted: float = 0.0) -> None:
        self.closest: float | None = None  # this clip's echo, nearest to the playback (mic minus playback, dB)
        self.restarted = restarted  # a canceller younger than REST is still learning: no restart yet

    def add(self, mic_db: float, played_db: float | None) -> None:
        """Loudest mic and playback of the last half second, the measure a cut uses."""
        if played_db is not None and played_db > self.SPEAKING:
            margin = mic_db - played_db
            self.closest = margin if self.closest is None else max(self.closest, margin)

    def may_restart(self) -> bool:
        return time.monotonic() - self.restarted > self.REST

    def near_miss(self, margin: float) -> bool:
        """Speech-like echo, kept out; True when it came close enough to ask for a restart."""
        return margin > self.EARLY and self.may_restart()

    def discard(self) -> None:
        """The user talked over this clip: it says nothing about the canceller."""
        self.closest = None

    def clip_over(self) -> bool:
        """The clip ended; True when its echo came close enough to ask for a restart."""
        closest, self.closest = self.closest, None
        if closest is None:
            return False
        drifting = self.near_miss(closest)
        side = "under" if closest < 0 else "over"
        log(HANDSFREE_LOG, f"clip echo: {abs(closest):.0f} dB {side} the playback at the closest"
                           f"{', canceller drifting: restart when quiet' if drifting else ''}")
        return drifting


class Output:
    """The default output: is it the laptop speakers (echo possible), checked every 2 s."""

    def __init__(self) -> None:
        self.at, self.speakers = 0.0, True

    def on_speakers(self) -> bool:
        if time.monotonic() - self.at > 2:
            name = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout
            self.at, self.speakers = time.monotonic(), not re.search(r"bluez|headphone|headset|usb", name, re.I)
        return self.speakers


class Detector:
    """One wake word's scores on openWakeWord speech embeddings (16 frames, 80 ms apart): it fires after RUN
    scores in a row above its threshold, then not again until a score drops below REARM."""

    def __init__(self, word: str, threshold: float, score, run: int = 2, hint: bool = True,
                 settle: float = 0.0) -> None:
        self.word, self.threshold, self.score, self.need = word, threshold, score, run
        self.hint = hint  # whether Whisper is told to expect the word when confirming it
        self.settle = settle  # seconds of audio after it fires before Whisper checks: the rest of the word
        self.rearm = min(0.3, threshold / 2)  # a score below this lets it fire again
        self.run, self.armed = 0, True

    def step(self, features: np.ndarray) -> bool:
        score = self.score(features)
        self.run = self.run + 1 if score > self.threshold else 0
        if score < self.rearm:
            self.armed = True
        if self.run >= self.need and self.armed:
            self.armed = False
            return True
        return False


CLOSE = 0.8  # similarity at which a spelling counts as the word: "Jervis" 0.83 yes, "Charvis" 0.77 no


def closeness(word: str, text: str) -> float:
    """How near the text comes to the word (1.0: the word itself). One heard word, or two neighbours joined, so
    "hench line" still counts as henchline."""
    words = re.findall(r"[a-z]+", text.lower())
    heard = words + [a + b for a, b in zip(words, words[1:])]
    return max((difflib.SequenceMatcher(None, word.lower(), piece).ratio() for piece in heard), default=0.0)


def said(word: str, text: str) -> bool:
    """The word, or a close spelling of it ("Jervis", "Javis"): without a hint Whisper spells a name its way."""
    return closeness(word, text) >= CLOSE


class Wake:
    """ "Hey Claude", and "hey Jarvis" as a second: a detector nominates, Whisper confirms.

    "Hey Claude" is the user's personal detector (wakeword_train.py); "hey
    Jarvis" is openWakeWord's published model. Both score the same speech
    embeddings every 80 ms, and no audio is kept beyond the last two seconds.
    Only when one fires are those two seconds transcribed, to check for its
    word; the snippet is discarded either way and its words never logged.
    """

    CHUNK = 1280  # samples per openWakeWord step (80 ms)

    def __init__(self) -> None:
        from openwakeword.utils import AudioFeatures

        self.features = AudioFeatures(inference_framework="onnx")
        self.ring: collections.deque = collections.deque(maxlen=2 * PER_SECOND)
        self.pending = np.zeros(0, dtype="<i2")
        self.detectors = []
        if WAKE_MODEL.exists():
            clf = pickle.loads(WAKE_MODEL.read_bytes())
            self.detectors.append(Detector("Claude", 0.9, lambda f: clf.predict_proba(f.reshape(1, -1))[0, 1]))
        # No hint for Whisper with "Jarvis": told to expect it, Whisper heard it in 19 of 20 clips of plain
        # noise; without, 0 of 20, and every synthetic "hey Jarvis" still came out right (2026-11-15)
        if JARVIS_PERSONAL.exists():  # the user's own, trained like "hey Claude": it fires at the word's end
            personal = pickle.loads(JARVIS_PERSONAL.read_bytes())
            # One rule for both words: the detector nominates, Whisper must hear the word. Trusting this
            # detector with any voice instead (it beat Whisper from the hall) let "feather" wake it 4 of 4
            # (2026-11-15); with the user's ordinary speech as negatives it fires on far less
            self.detectors.append(Detector("Jarvis", 0.9, lambda f: personal.predict_proba(f.reshape(1, -1))[0, 1],
                                           hint=False))
        elif JARVIS_MODEL.exists():
            import onnxruntime

            jarvis = onnxruntime.InferenceSession(str(JARVIS_MODEL), providers=["CPUExecutionProvider"])
            name = jarvis.get_inputs()[0].name
            score = lambda f: jarvis.run(None, {name: f.reshape(1, 16, 96)})[0][0, 0]  # noqa: E731
            # The published model scores the user low from across the room (hit or miss beyond arm's length
            # at 0.5 twice): one score over 0.25 nominates. That fires on "hey" before "Jarvis" is out, so
            # Whisper waits 0.7 s for the word, and a checked nomination re-arms for the "Jarvis" after it
            self.detectors.append(Detector("Jarvis", 0.25, score, run=1, hint=False, settle=0.7))
        self.nominated: set[str] = set()  # words whose detector fired since the last confirm
        self.due = 0  # 30 ms frames still to take in before the nominated words are checked
        self.word = ""  # the word Whisper confirmed last
        self.near: float | None = None  # how near the nearest heard word came to an unhinted name

    @classmethod
    def available(cls) -> Wake | None:
        return cls() if any(m.exists() for m in (WAKE_MODEL, JARVIS_PERSONAL, JARVIS_MODEL)) else None

    def feed(self, samples: np.ndarray) -> bool:
        """Add 30 ms of audio; True when a detector says "maybe hey Claude" (or Jarvis)."""
        self.ring.append(samples)
        self.pending = np.concatenate([self.pending, samples])
        while len(self.pending) >= self.CHUNK:
            chunk, self.pending = self.pending[:self.CHUNK], self.pending[self.CHUNK:]
            self.features._streaming_features(chunk)
            features = self.features.get_features(n_feature_frames=16).astype(np.float32)
            for detector in self.detectors:  # all of them: "hey Claude"'s detector also likes "hey Jarvis"
                if detector.step(features):
                    self.due = max(self.due, round(detector.settle * PER_SECOND)) if self.nominated else \
                        round(detector.settle * PER_SECOND)
                    self.nominated.add(detector.word)
        if not self.nominated:
            return False
        self.due -= 1
        return self.due < 0

    def forget(self) -> None:
        """Drop a nomination: the listener stopped feeding (paused, push-to-talk), so its audio is gone."""
        self.nominated.clear()
        self.due = 0

    def confirm(self, whisper) -> bool:
        """Whether Whisper hears one of the nominated words; self.word says which."""
        return self.confirm_by(lambda audio, hints: " ".join(seg.text for seg in whisper.transcribe(
            audio, language="en", beam_size=1, vad_filter=False, hotwords=hints)[0]))

    def confirm_by(self, hear) -> bool:
        """Whether hear(audio, hints) hears one of the nominated words (Whisper, or Whistle on battery)."""
        words, self.nominated = sorted(self.nominated), set()
        for detector in self.detectors:  # whatever Whisper says, the word may still be coming: fire again on it
            if detector.word in words and detector.settle:
                detector.armed, detector.run = True, 0
        self.word = "/".join(words)
        hints = " ".join(d.word for d in self.detectors if d.hint and d.word in words) or None
        audio = np.concatenate(self.ring).astype(np.float32) / 32768
        text = hear(audio, hints)
        close = {d.word for d in self.detectors if not d.hint}  # unhinted names are spelled freely; "Claude" exact
        heard = next((word for word in words
                      if (said(word, text) if word in close else re.search(rf"\b{word}\b", text, re.I))), "")
        self.word = heard or self.word
        # A number, never the words: how near a rejected unhinted name came, to tune CLOSE from the room
        self.near = max((closeness(word, text) for word in words if word in close), default=None)
        return bool(heard)


class EarbudButton(threading.Thread):
    """Bluetooth earbud taps (AVRCP media keys) as a silent "hey Claude".

    While hands-free runs it grabs every "… (AVRCP)" input device, so a tap
    does not also pause music; when hands-free stops, the grab ends. Earbuds
    come and go, so it rescans. Taps only arrive in the music profile (A2DP).
    """

    TAP = {"KEY_PLAYCD", "KEY_PAUSECD", "KEY_PLAYPAUSE"}
    DEBOUNCE = 2.0  # seconds: one press, not its echo

    def __init__(self, stop: threading.Event) -> None:
        super().__init__(daemon=True)
        self.stop, self.tap = stop, threading.Event()

    def run(self) -> None:
        from evdev import InputDevice, ecodes, list_devices

        held: dict[str, InputDevice] = {}
        last = 0.0
        while not self.stop.is_set():
            for path in list_devices():
                if path in held:
                    continue
                try:
                    device = InputDevice(path)
                    if device.name.endswith("(AVRCP)"):
                        device.grab()
                        held[path] = device
                        log(HANDSFREE_LOG, f"earbud button: {device.name}")
                except OSError:
                    continue
            if not held:
                self.stop.wait(3)
                continue
            ready, _, _ = select.select(list(held.values()), [], [], 3)
            for device in ready:
                try:
                    for event in device.read():
                        if event.type != ecodes.EV_KEY or event.value != 1:
                            continue
                        name = ecodes.KEY.get(event.code, "")
                        if name in self.TAP and time.monotonic() - last > self.DEBOUNCE:
                            last = time.monotonic()
                            self.tap.set()
                        else:
                            log(HANDSFREE_LOG, f"earbud {name} ignored")
                except OSError:  # disconnected
                    held.pop(device.path, None)
                    log(HANDSFREE_LOG, "earbud button gone")


class Playback(threading.Thread):
    """Loudness of what the speakers are playing (the default output's monitor).

    Our own voice reaches the cancelled mic far quieter than it is played,
    since the canceller removes most of it; the user's voice does not get
    quieter for that. Measured on 2026-11-14: echo stayed 20 dB or more under
    the playback, the user often within a few dB of it or above.
    """

    def __init__(self, stop: threading.Event) -> None:
        super().__init__(daemon=True)
        self.stop = stop
        self.levels: collections.deque = collections.deque(maxlen=LEVEL_SPAN)
        self.at = 0.0

    def run(self) -> None:
        while not self.stop.is_set():
            device = speaker_monitor()
            stream = mic_stream(device)
            while (not self.stop.is_set() and (samples := read_frame(stream)) is not None
                   and device == speaker_monitor()):  # a call started or ended: follow it
                self.levels.append(level(samples.astype(np.float32) / 32768))
                self.at = time.monotonic()
            stream.terminate()
            self.stop.wait(1)  # the output changed or parec ended: start again

    def loudest(self) -> float | None:
        """Loudest playback level in the last half second, or None if unknown."""
        return max(self.levels) if self.levels and time.monotonic() - self.at < 1 else None
