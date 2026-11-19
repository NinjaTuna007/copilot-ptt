"""The one talkback worker: plays queued clips one after another, sentence by
sentence, through Kokoro on the CPU, and goes quiet the moment the mic is busy.

Whoever holds LOCK is the worker. It loads the model on the first clip and
exits when nothing is queued or held; text queued while it lets go starts a
new worker or is picked up here.
"""

from __future__ import annotations

import fcntl
import os
import queue
import re
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from voice import spool
from voice.agents import AGENT_VOICES, DEFAULT_VOICE
from voice.voices import for_agent as chosen_voice
from voice.state import (AWAY, BY_HANDSFREE, CALL_SILENT, KOKORO_MODELS, LOCK, PLAYING, TALKBACK_LOG, WORKER_PID, Playing,
                         Speaker, in_call, last_speaker, log, mic_busy, read_json, speaker_sink, target_session_since,
                         target_since)

THREADS = int(os.environ.get("TALKBACK_THREADS", "4"))  # CPU threads: 4 measured best (latency vs load)
RATE = 24000
PAUSE = 0.25  # seconds between sentences
PIECE = 0.1  # seconds of audio written between checks of the mic
VOICES = {**AGENT_VOICES, BY_HANDSFREE: "bm_george"}  # system notices: the user's "Jarvis" voice, not an agent's
HANDSFREE = Path(__file__).resolve().parents[2] / "handsfree/handsfree.py"
SYSTEM = {BY_HANDSFREE}  # notices that neither introduce themselves nor take the user's replies
TAGS = re.compile(r"\[(?:laugh|chuckle|sigh|gasp|groan|cough|sniff|shush|clear throat|whispering)\]\s*")
PLAYER = ["pacat", "--playback", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=1",
          "--latency-msec=100", "--client-name=talkback", "--stream-name=talkback"]


class Voice:
    """Kokoro, loaded on first use; SIGUSR1 skips the clip being played."""

    def __init__(self) -> None:
        self._kokoro = None
        self.skip = False
        signal.signal(signal.SIGUSR1, self.on_skip)

    def on_skip(self, *_) -> None:
        self.skip = True

    @property
    def kokoro(self):
        if self._kokoro is None:
            import onnxruntime as rt
            from kokoro_onnx import Kokoro

            opts = rt.SessionOptions()
            opts.intra_op_num_threads = THREADS
            opts.inter_op_num_threads = 1
            session = rt.InferenceSession(str(KOKORO_MODELS / "kokoro-v1.0.onnx"), opts,
                                          providers=["CPUExecutionProvider"])
            self._kokoro = Kokoro.from_session(session, str(KOKORO_MODELS / "voices-v1.0.bin"))
        return self._kokoro

    def render(self, sentence: str, voice: str):
        """Audio for one sentence; empty for a bare tag Kokoro cannot say."""
        import numpy as np

        plain = TAGS.sub("", sentence).strip()  # Kokoro would read [laugh] aloud
        if not plain:
            return np.zeros(0, dtype="<f4")
        return self.kokoro.create(plain, voice=voice, lang="en-us")[0]

    def play(self, path: Path) -> None:
        """Speak one clip; put back what is left if the user cuts in."""
        import numpy as np

        clip = spool.Clip.load(path)
        if clip is None:
            path.unlink(missing_ok=True)
            return
        calling = in_call()
        if calling and CALL_SILENT.exists():  # the line dropped: kept until the phone is back, or the call ends
            time.sleep(0.5)
            return
        if AWAY.exists() and not calling:  # queued before Away began: not said into an empty room
            path.unlink(missing_ok=True)
            log(TALKBACK_LOG, f"{clip.agent} clip dropped: the user is away (agents are told to text)")
            return
        if clip.for_call and not calling:  # the call is over: not said aloud at the laptop, sent to the phone
            if clip.agent in SYSTEM:
                path.unlink(missing_ok=True)
                log(TALKBACK_LOG, f"{clip.agent} notice for the call dropped: the call ended")
                return
            try:  # in the background, by its own path (a worker started by a service has no ~/.local/bin)
                subprocess.Popen([sys.executable, str(HANDSFREE), "text", "--as", clip.session, " ".join(clip.sentences)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, env={**os.environ, "TALKBACK_AGENT": clip.agent})
            except OSError as exc:  # kept in the spool: said or texted on a later try, never lost
                log(TALKBACK_LOG, f"{clip.agent} clip for the ended call couldn't be texted ({exc}): kept")
                time.sleep(2)
                return
            path.unlink(missing_ok=True)
            log(TALKBACK_LOG, f"{clip.agent} clip for the call, which ended: texted to the phone instead")
            return
        sentences = clip.sentences
        intro = 0
        if clip.agent not in SYSTEM:
            last = last_speaker()
            if not last or last.session != clip.session:
                name = clip.agent.capitalize()
                sentences, intro = [f"{name} here, from {clip.where}."] + sentences, 1
            Speaker(clip.session, clip.agent, clip.where, clip.tab).remember()
        # Once, with the words, before the clip file goes: no moment when neither says a clip is under way.
        # Hands-free checks what it hears over a clip against the words (its own echo)
        Playing(path.name, clip.session, clip.agent, " ".join(sentences)).save()
        path.unlink(missing_ok=True)
        voice = clip.voice or (VOICES[clip.agent] if clip.agent in SYSTEM else chosen_voice(clip.agent))
        ready: queue.Queue = queue.Queue(maxsize=2)
        done = threading.Event()

        def make() -> None:
            try:
                for sentence in sentences:
                    samples = self.render(sentence, voice)
                    while not done.is_set():
                        try:
                            ready.put(samples, timeout=0.2)
                            break
                        except queue.Full:
                            continue
            finally:
                ready.put(None)

        threading.Thread(target=make, daemon=True).start()
        # On a phone call, into the phone's sink
        player = subprocess.Popen([*PLAYER, f"--device={speaker_sink()}"], stdin=subprocess.PIPE)
        piece = int(RATE * PIECE)
        spoken, cut, self.skip = 0, False, False
        try:
            while (samples := ready.get()) is not None:
                if spoken == 0:
                    log(TALKBACK_LOG, f"{clip.agent} first_audio={time.time() - clip.queued:.2f}s")
                else:
                    samples = np.concatenate([np.zeros(int(RATE * PAUSE), "<f4"), samples])
                data = samples.astype("<f4")
                for n, start in enumerate(range(0, len(data), piece)):
                    # The call started or ended mid-clip: the rest is said where the user is now (or texted), not
                    # carried on at the laptop after a hang-up (the user's catch, 2026-11-17)
                    moved = n % 5 == 0 and (in_call() != calling or calling and CALL_SILENT.exists())
                    # Away switched on mid-sentence (the user's catch, 2026-11-17: a reply went on aloud): stopped
                    gone_away = n % 5 == 0 and not calling and AWAY.exists()
                    if gone_away:
                        cut, self.skip = True, True  # dropped, not kept: the agent is told to text from now on
                        log(TALKBACK_LOG, f"{clip.agent} cut: the user went away")
                        break
                    if mic_busy() or self.skip or moved:
                        cut = True
                        if moved:
                            clip.for_call = True  # resumed on the phone if a call began; texted if it ended
                            log(TALKBACK_LOG, f"{clip.agent} cut: the call {'began' if not calling else 'ended or dropped'}")
                        break
                    player.stdin.write(data[start:start + piece].tobytes())
                    player.stdin.flush()
                if cut:
                    break
                spoken += 1
        finally:
            done.set()
            if cut:
                player.terminate()
            else:
                player.stdin.close()
            player.wait()
            PLAYING.unlink(missing_ok=True)
        if cut and clip.agent in SYSTEM:
            log(TALKBACK_LOG, f"{clip.agent} notice cut and dropped: said later it would be out of place")
        elif cut and not self.skip:
            # The intro, once heard, is not said again; resume from the cut sentence
            clip.sentences = sentences[max(spoken, intro):]
            clip.save(path)
            log(TALKBACK_LOG, f"{clip.agent} cut by the mic, {len(clip.sentences)} sentences kept")


def after_input(since: float) -> None:
    """The mic just went idle: hold waiting clips if the words went to an agent."""
    waiting = spool.queued_sessions(skip_agents=SYSTEM)  # notices are never held: they play now or not at all
    if not waiting:
        return
    deadline = time.time() + 2
    while time.time() < deadline:
        session = target_session_since(since)
        if session:
            # Hands-free knows which agent the words went to: only its clips wait for its answer; the others play
            # on (Cursor's reply was held for a minute and a half while the user talked to Claude, 2026-11-15)
            spool.hold(waiting & {session})
            return
        to_agent = target_since(since)
        if to_agent is not None:
            if to_agent:
                spool.hold(waiting)  # push-to-talk: the window in front, whichever agent's; all wait
            return
        time.sleep(0.1)
    # Nothing was pasted (too short, empty): nobody got new input, play on


def lock() -> int | None:
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def work() -> int:
    fd = lock()
    if fd is None:
        return 0  # another worker holds the lock and will see our text
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    WORKER_PID.write_text(str(os.getpid()))
    t0, voice, since = time.monotonic(), None, None
    while fd is not None:
        if mic_busy():
            since = since or time.time()
            time.sleep(0.1)
            continue
        if since is not None:
            after_input(since - 1)
            since = None
        spool.expire()
        ready = [path for path in spool.queued() if not spool.is_held(read_json(path).get("session", ""))]
        if ready:
            voice = voice or Voice()
            voice.play(ready[0])
            continue
        if spool.queued() or spool.any_held():
            time.sleep(0.2)
            continue
        # Let go, then look once more: text queued while we were letting go
        # either sees the lock free and starts its own worker, or is seen here
        WORKER_PID.unlink(missing_ok=True)
        os.close(fd)
        fd = lock() if spool.queued() else None
        if fd is not None:
            WORKER_PID.write_text(str(os.getpid()))
    if voice is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        log(TALKBACK_LOG, f"worker done wall={time.monotonic() - t0:.1f}s "
                          f"cpu={usage.ru_utime + usage.ru_stime:.1f}s threads={THREADS}")
    return 0
