"""One turn: record the user until they stop, transcribe it, and act on it.

The listener decides when a turn starts (a reply window, talking over a clip,
the wake word, an earbud tap); this records until a pause, "over" or the
limit, and then routes the words: to an agent, to a switch, or nowhere.
"""

from __future__ import annotations

import re
import threading
import time
import wave
from enum import Enum
from typing import Callable

import numpy as np
from faster_whisper.vad import get_speech_timestamps

import routing
from routing import Heard, Meaning
from sensing import PER_SECOND, RATE, SPEECH, level
from talkover import TalkOver, our_own_words
from voicegate import START_DB
from voice.speech import nemotron, whisper
from voice.state import (BY_HANDSFREE, CALL_HANGUP, CALL_PUSH, HANDSFREE_LOG, HANDSFREE_WAV, Phase, in_call,
                         last_speaker, log, ptt_busy, release_phase, set_phase, set_target)

THINK_WINDOW = 180  # seconds after "give me a minute"
WAIT_FOR_WORDS = 5.0  # after the wake word, seconds to start talking before it gives up
CHIME_GRACE = 1.2  # the listening chime is loud in the mic; it must not count as them starting, then the pause close the turn
END_DB, END_FRAMES = 6, 50  # 1.5 s at most END_DB over the floor ends the turn
CALL_END_FRAMES = 30  # 0.9 s on a phone call: the user found 1.5 s slow there; a longer pause becomes a follow-on
# Nemotron's words so far end a sentence ("…tests.", not "…and uh"): the turn ends after this much quiet instead
# (the user's wish, 2026-11-17: answers sooner, now that the words are ready as they speak)
EARLY_END_FRAMES, CALL_EARLY_END_FRAMES = 27, 20  # 0.8 s; 0.6 s on a call
NO_STOP_FRAMES = 10  # its last full stop often doesn't come: then 0.3 s more, still short of the full pause
TRAILING = {"uh", "um", "er", "erm", "hmm", "and", "but", "or", "so", "because", "the", "a", "an", "to", "of",
            "with", "like", "if", "then", "that", "which", "is", "are", "my", "your", "for", "in", "on"}
BELOW_PEAK = 25  # dB under their loudest also counts as quiet, for a noisy room
OVER_FRAMES = 20  # after 0.6 s of quiet, check whether the last word was "over"
# On a call the line can carry steady hiss (Henchline's clear sound has no call-mode noise suppression) that never
# gets quiet enough to end a turn: "Hearing you…" for 55 s (2026-11-15). There a second without speech (Silero),
# looked at every 0.3 s, ends the turn as well
VOICE_EVERY, VOICE_GONE = 10, 1.0
OVER_TAIL = 4 * PER_SECOND  # frames Whisper looks at for "over"
PTT_CHECK = 10  # frames between checks that push-to-talk has not taken the mic
MAX_SECONDS = 120  # the user talks in long stretches; 60 s cut them off
WAKE_PREFIX = re.compile(r"^\W*((hey|hi|okay|ok)\W+)?claude\W*", re.I)
OVER = re.compile(r"[\s,.!?]*\bover\b[\s.!?]*$", re.I)
TONE_NOTE = re.compile(r"\s*\[tone: [^\]]*\]$")


class Mode(str, Enum):
    """What the tray shows."""

    WAITING = "waiting"  # only the wake word gets in
    LISTENING = "listening"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"


def only_hint_words(text: str, hotwords: list[str]) -> bool:
    """Whisper given noise writes out the names it was told to expect ("Cursor, Codex, Kokoro",
    2026-11-15): a transcript of nothing but those is not the user talking."""
    words = re.findall(r"[a-z']+", text.lower())
    return bool(words) and set(words) <= set(re.findall(r"[a-z']+", " ".join(hotwords).lower()))


class Turn:
    def __init__(self, cfg: whisper.Config, transcriber: whisper.Transcriber, talkover: TalkOver,
                 read: Callable[[], np.ndarray | None], show: Callable[[Mode], None],
                 echo_slipped: Callable[[str], None], stop: threading.Event) -> None:
        self.cfg, self.transcriber, self.talkover = cfg, transcriber, talkover
        self.read, self.show, self.echo_slipped, self.stop = read, show, echo_slipped, stop
        self.last_message: tuple[str, Heard] | None = None  # for "send that to …"
        self.live = None  # this turn, heard as it is recorded (Nemotron), when it is installed
        self.whistle = None  # set by the listener: quick checks (the wake word, "over") without Whisper
        self.streamer = None
        if cfg.streaming and nemotron.available():
            try:
                self.streamer = nemotron.Streamer(cfg.hotwords, HANDSFREE_LOG)
            except Exception as exc:  # Whisper as before
                log(HANDSFREE_LOG, f"nemotron didn't load ({exc}): Whisper transcribes turns")

    def take(self, frames: list[np.ndarray], floor: float, heard: Heard) -> float | None:
        """One turn, end to end; returns seconds to keep listening (0: the default), None if no words."""
        frames = self.record(frames, floor, heard)
        if frames is None:
            return 0.0
        try:
            text, rich = self.transcribe(frames)
            tone = TONE_NOTE.search(rich)
            note = tone.group(0) if tone else ""
            rich = rich[: len(rich) - len(note)] if note else rich
            text, rich = WAKE_PREFIX.sub("", OVER.sub("", text)), WAKE_PREFIX.sub("", OVER.sub("", rich)) + note
            if only_hint_words(text, self.cfg.hotwords):
                log(HANDSFREE_LOG, "only Whisper's hint words: a noise, nothing sent")
                set_target(False)
                return None
            taken = self.talkover.taken
            if heard is Heard.OVER_YOU and taken and our_own_words(text, taken.clip_text):
                self.echo_slipped(f"what stopped the clip was its own words ({len(text.split())}): "
                                  "our echo; the clip plays on, nothing sent")
                set_target(False)
                return None
            return self.act(text, rich, heard)
        finally:
            release_phase(BY_HANDSFREE)  # act() normally has already, before anything is sent

    def record(self, frames: list[np.ndarray], floor: float, heard: Heard) -> list[np.ndarray] | None:
        """Frames until a pause, "over", or the limit; None if push-to-talk took over."""
        set_phase(Phase.RECORDING, BY_HANDSFREE)  # talkback goes quiet and holds
        self.show(Mode.RECORDING)
        if self.live:  # a turn that failed before it was transcribed: its decoding stops
            self.live.cancel()
        self.live = self.streamer.begin() if self.streamer else None
        for frame in frames:  # what was heard before the turn began (the wake word, a word over a clip)
            if self.live:
                self.live.feed(frame)
        since, quiet, peak = time.monotonic(), 0, -120.0
        started = heard is not Heard.WOKE  # after "hey Claude" people pause for the chime
        calling = in_call()
        end = CALL_END_FRAMES if calling else END_FRAMES
        early = (CALL_EARLY_END_FRAMES if calling else EARLY_END_FRAMES) if self.cfg.early_end else 0
        finished = False
        voice_at, spoke = since, False  # when Silero last heard speech in the turn, and whether it ever did (calls)
        voiceless = False
        held = heard is Heard.PUSHED  # the button decides when it ends, not a pause
        released = False
        while (quiet < end or held) and time.monotonic() - since < MAX_SECONDS:
            if held and not CALL_PUSH.exists():
                released = True
                break
            samples = self.read()
            if samples is None:
                break
            if len(frames) % PTT_CHECK == 0 and ptt_busy():
                log(HANDSFREE_LOG, "push-to-talk took over; dropped the hands-free recording")
                if self.live:
                    self.live.cancel()
                    self.live = None
                return None  # it delivers these words; ours would arrive twice
            frame = samples.astype(np.float32) / 32768
            frames.append(frame)
            if self.live:
                self.live.feed(frame)
            db = level(frame)
            peak = max(peak, db)
            if not started:
                waited = time.monotonic() - since
                # The chime plays into the room as the turn opens. One loud moment of it used to start the
                # pause clock, and 1.5 s of quiet then closed the turn before they had said anything (Jarvis, 1.7 s).
                if heard is not Heard.WOKE or waited >= CHIME_GRACE:
                    started = db > floor + START_DB or waited > WAIT_FOR_WORDS
                continue
            quiet = quiet + 1 if db < max(floor + END_DB, peak - BELOW_PEAK) else 0
            if quiet == OVER_FRAMES and self.ends_with_over(frames):
                break
            # From the shorter pause on, looked at again now and then: the full stop can come a moment late
            if early and not held and quiet >= early and (quiet - early) % 5 == 0 and self.sentence_done(quiet - early):
                finished = True
                break
            if calling and not held and len(frames) % VOICE_EVERY == 0:
                recent = np.concatenate(frames[-int(VOICE_GONE * PER_SECOND):])
                if get_speech_timestamps(recent, SPEECH):
                    voice_at, spoke = time.monotonic(), True
                elif time.monotonic() - voice_at > (VOICE_GONE if spoke else WAIT_FOR_WORDS):
                    voiceless = True  # their words ended a second ago, or none came at all
                    break
        reason = ("the button" if released else "limit" if time.monotonic() - since >= MAX_SECONDS
                  else "no voice for a second (steady sound on the line)" if voiceless
                  else "a finished sentence" if finished else "over" if quiet == OVER_FRAMES else "pause")
        log(HANDSFREE_LOG, f"recorded {time.monotonic() - since:.1f}s, ended by {reason} (floor {floor:.0f} dB)")
        return frames

    def transcribe(self, frames: list[np.ndarray]) -> tuple[str, str]:
        """(plain text, text with tone marks); empty when there was no speech."""
        set_phase(Phase.TRANSCRIBING, BY_HANDSFREE)
        self.show(Mode.TRANSCRIBING)
        audio = (np.concatenate(frames) * 32767).astype("<i2") if frames else np.zeros(0, "<i2")
        # A cough or a bare "um" has no speech, and Whisper given only that tends
        # to write out its hint words ("Cursor, Cursor, Kokoro")
        live, self.live = self.live, None
        if not len(audio) or not get_speech_timestamps(audio.astype(np.float32) / 32768, SPEECH):
            log(HANDSFREE_LOG, "no speech in the recording")
            if live:
                live.cancel()
            return "", ""
        with wave.open(str(HANDSFREE_WAV), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(audio.tobytes())
        try:
            began = time.monotonic()
            heard = live.finish() if live else None
            if heard and heard[0]:
                text = nemotron.tidy(heard[0], self.cfg.hotwords)
                log(HANDSFREE_LOG, f"heard by nemotron {time.monotonic() - began:.2f}s after the turn ended")
                rich = text
                if self.cfg.tone:
                    try:
                        from voice.speech import tone

                        rich = tone.annotate(HANDSFREE_WAV, heard[1], text)
                    except Exception as exc:  # tone is extra; the words must still get through
                        log(HANDSFREE_LOG, f"tone failed: {exc}")
                return text, rich
            if live:
                log(HANDSFREE_LOG, "nemotron heard nothing: Whisper transcribes this turn")
            got = self.transcriber.file(HANDSFREE_WAV)
            log(HANDSFREE_LOG, f"heard by whisper {time.monotonic() - began:.2f}s after the turn ended")
            return got
        except Exception as exc:
            log(HANDSFREE_LOG, f"transcribe failed: {exc}")
            return "", ""
        finally:
            HANDSFREE_WAV.unlink(missing_ok=True)

    def act(self, text: str, rich: str, heard: Heard) -> float | None:
        meaning = routing.meaning(text)
        # Before anything is said or sent: talkback may speak again, and knows whether an agent got words
        to = last_speaker() if meaning is Meaning.MESSAGE else None  # who deliver() will type it to
        set_target(meaning is Meaning.MESSAGE, to.session if to else "")
        set_phase(Phase.IDLE, BY_HANDSFREE)
        if meaning is Meaning.TIME:
            log(HANDSFREE_LOG, "heard: give me a minute")
            routing.say("Take your time.")
            return THINK_WINDOW
        if meaning is Meaning.ROUTING:
            if routing.route(text, self.last_message):
                whisper.ding(self.cfg)
            return 0.0
        if meaning is Meaning.HANGUP:
            log(HANDSFREE_LOG, "heard: hang up")
            if in_call():  # it may have ended while the words were transcribed
                goodbye = routing.say("Bye! Hanging up.")
                # Only once the goodbye is queued: the server ends the call when talkback has nothing left to say,
                # and until say.py has run, there is nothing in the queue yet
                threading.Thread(target=lambda: (goodbye.wait(timeout=15), CALL_HANGUP.touch()), daemon=True).start()
            return None
        if meaning is Meaning.OFF:
            log(HANDSFREE_LOG, "heard: hands-free off")
            self.stop.set()
            routing.say("Hands-free off.")
        elif meaning is Meaning.CANCEL:
            log(HANDSFREE_LOG, "heard: never mind")
        elif meaning is Meaning.NOTHING:
            log(HANDSFREE_LOG, f"ignored {len(text.split())} words")
            if heard is Heard.OVER_YOU:  # nothing said over a clip: our echo cut it; no cut-resume-cut loop
                self.echo_slipped("echo cut a clip")
            return None
        elif routing.deliver(rich, heard, to=to, urgent=cut_in(heard, to)):  # the one noted as the target above
            whisper.ding(self.cfg)
            self.last_message = (rich, heard)
            if routing.interrupt_waiting():
                routing.clear_interrupt()
        return THINK_WINDOW if routing.THINK.search(text) else 0.0

    def sentence_done(self, longer: int = 0) -> bool:
        """Nemotron's words so far end a sentence, not on a filler or a word that leads on: no need to wait out the
        whole pause. Its last full stop often doesn't come (the user's catch, 2026-11-17), so without one, once the
        quiet has gone `longer` than NO_STOP_FRAMES past the shorter pause. Without live words, never."""
        if not self.live:
            return False
        try:
            said = self.live.so_far().strip()
        except Exception:
            return False
        words = re.findall(r"[a-z']+", said.lower())
        if not words or words[-1] in TRAILING or OVER.search(said):
            return False
        return said[-1] in ".?!" or longer >= NO_STOP_FRAMES

    def ends_with_over(self, frames: list[np.ndarray]) -> bool:
        """Did they finish with "over"? Nemotron's words so far say it at once, the turn being heard as it is
        recorded (0.6 s of quiet has let its last word through); Whistle, else Whisper, looks at the tail."""
        if self.live:
            try:
                return bool(OVER.search(self.live.so_far().strip()))
            except Exception as exc:
                log(HANDSFREE_LOG, f"nemotron's words so far failed ({exc})")
        tail = np.concatenate(frames[-OVER_TAIL:])
        if self.whistle:
            try:
                return bool(OVER.search(self.whistle.hear(tail, None).strip()))
            except Exception as exc:
                log(HANDSFREE_LOG, f"whistle failed ({exc}): Whisper checks for over")
        return bool(OVER.search(self.transcriber.quick_text(tail).strip()))


ON_THE_CALL = (Heard.CALL, Heard.PUSHED, Heard.OVER_YOU, Heard.FOLLOW_ON, Heard.REPLY)
# Hands-free, including the wake word and "give me a minute": the words are the turn, and the Cursor CLI parks
# them until it finishes. Push-to-talk does not come through here, so it still queues.
HANDS_FREE = (Heard.WOKE, Heard.REPLY, Heard.THINK, Heard.FOLLOW_ON, Heard.OVER_YOU)


def cut_in(heard: Heard, to: routing.Speaker | None) -> bool:
    """A spoken turn cuts into the Cursor CLI, on a call or over hands-free, or it sits in the follow-up queue.
    On a call the phone can choose to let it wait. Off a call, hands-free to that CLI steers, wake word included.
    Claude and Codex wait unless the call says to cut in."""
    if not in_call():
        if heard not in HANDS_FREE:
            return False
        return bool(to and to.agent == "cursor" and not to.in_editor)
    if heard is Heard.WOKE or heard not in ON_THE_CALL:
        return False
    return routing.interrupt_waiting() or routing.cuts_in(to)
