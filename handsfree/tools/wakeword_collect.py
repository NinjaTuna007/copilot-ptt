#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""Guided "hey Claude" (or "hey Jarvis") recordings around the house, scored as they come in.

    wakeword_collect.py [--word jarvis] [POSITION ...]
    wakeword_collect.py [--word jarvis] --confusers   (20 s of words that fooled it, as "not it")

For each position talkback announces where to stand, gives the user time to
get there, plays a short chime, and records a few seconds from the same
echo-cancelled mic hands-free uses. Each take is scored with the wake-word
model at once, and talkback says how many "hey Claude"s it caught; a miss
is repeated. Hands-free listening is paused meanwhile, so the samples are
not sent to an agent. Clips land in ~/.local/share/handsfree/samples/ for
a later custom verifier (another word's in a folder of its own). A last
"just talk normally" take checks for false wakes. Takes are scored with the
published model, which is what a personal one is needed for: for "hey
Jarvis" (8 of 20 caught) most spots get repeats; the trainer finds the words
with Whisper, so a low score does not spoil a take.
"""

from __future__ import annotations

import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "core"), str(Path(__file__).resolve().parents[2])]  # hands-free's modules; voice/
from collect import CHIME, speak as say  # noqa: E402
from sensing import RATE  # noqa: E402
from voice.state import EC_SOURCE, HANDSFREE_DATA, JARVIS_MODEL, PAUSE, WAKE_SAMPLES  # noqa: E402

THRESHOLD = 0.5
# The published model each word is scored with while recording, and where its takes go. None: no published
# model (henchline). The take is kept anyway; the trainer finds the word in it.
WORDS = {"claude": (HANDSFREE_DATA / "hey_claude.onnx", WAKE_SAMPLES),
         "jarvis": (JARVIS_MODEL, WAKE_SAMPLES / "jarvis"),
         "henchline": (None, WAKE_SAMPLES / "henchline"),
         "papoy": (None, WAKE_SAMPLES / "papoy")}
POSITIONS = [
    ("desk", "at your desk, facing me"),
    ("dining", "at the dining table next to me"),
    ("kitchen-away", "in the kitchen, facing away from me"),
    ("hall-towards", "in the hall, facing towards me"),
]


def record(seconds: float) -> np.ndarray:
    proc = subprocess.run(
        ["timeout", str(seconds), "parec", f"--device={EC_SOURCE}", "--format=s16le",
         f"--rate={RATE}", "--channels=1", "--raw"],
        capture_output=True,
    )
    return np.frombuffer(proc.stdout, dtype="<i2")


def wakes(model: Model, audio: np.ndarray) -> tuple[int, float]:
    """How many separate times it woke, and the best score."""
    model.reset()
    count, best, armed = 0, 0.0, True
    for start in range(0, len(audio) - 1280, 1280):
        score = max(model.predict(audio[start:start + 1280]).values())
        best = max(best, score)
        if score > THRESHOLD and armed:
            count, armed = count + 1, False
        elif score < 0.2:
            armed = True
    return count, best


def save(folder: Path, name: str, audio: np.ndarray) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with wave.open(str(folder / f"{name}-{time.strftime('%H%M%S')}.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(audio.tobytes())


def main() -> int:
    args = sys.argv[1:]
    word = "claude"
    confusers = "--confusers" in args
    args = [a for a in args if a != "--confusers"]
    if "--word" in args:  # anywhere: the rest are positions
        at = args.index("--word")
        word, args = (args[at + 1].lower() if at + 1 < len(args) else ""), args[:at] + args[at + 2:]
    if word not in WORDS or any(a.startswith("-") for a in args):
        print(f"usage: wakeword_collect.py [--word {'|'.join(WORDS)}] [POSITION ...]", file=sys.stderr)
        return 2
    published, folder = WORDS[word]
    wanted = set(args)
    positions = [p for p in POSITIONS if not wanted or p[0] in wanted]
    model = Model(wakeword_models=[str(published)], inference_framework="onnx") if published else None
    phrase = word if word in ("henchline", "papoy") else f"hey {word.capitalize()}"
    PAUSE.touch()
    results = []
    try:
        if confusers:  # "feather" woke "hey Jarvis" every time (2026-11-15): such words become negatives
            say(f"After the chime, for twenty seconds, say the words that fooled {phrase}, a few times each, "
                f"and anything else that sounds a bit like it. But not {phrase} itself.")
            time.sleep(1)
            subprocess.run(["pw-play", CHIME])
            audio = record(20)
            save(folder, "negative", audio)
            if model is None:  # no published model for this word: the take is kept, nothing to score
                print("confusers: saved", flush=True)
                say("Thank you, that's all.")
                return 0
            count, best = wakes(model, audio)
            print(f"confusers: published model woke {count}x, best {best:.2f}", flush=True)
            say("Thank you, that's all.")
            return 0
        say("Wake word recording. For each spot I'll say where to go, then play a chime. "
            f"After the chime, say {phrase} twice, with a short pause in between.")
        for name, where in positions:
            for attempt in range(3):
                say(f"Next: {where}. Chime in ten seconds." if attempt == 0
                    else "Let's do that spot once more. Chime in three seconds.")
                time.sleep(10 if attempt == 0 else 3)
                subprocess.run(["pw-play", CHIME])
                audio = record(6)
                save(folder, name, audio)
                if model is None:  # no published model: keep the take; the trainer finds the word
                    print(f"{name}: saved", flush=True)
                    say("Saved.")
                    break
                count, best = wakes(model, audio)
                results.append((name, attempt, count, best))
                print(f"{name} try {attempt + 1}: woke {count}x, best {best:.2f}", flush=True)
                if count >= 2:
                    say(f"Got both. Score {best:.2f}.")
                    break
                say(f"I only caught {count} of 2." if count else "I didn't catch either.")
        say("Last one, from wherever you are: after the chime, talk normally for ten seconds "
            f"about anything, but don't say {phrase}.")
        time.sleep(1)
        subprocess.run(["pw-play", CHIME])
        audio = record(10)
        save(folder, "negative", audio)
        if model is None:
            print("negative: saved", flush=True)
            say("That's all, thank you.")
            return 0
        count, best = wakes(model, audio)
        print(f"negative: woke {count}x, best {best:.2f}", flush=True)
        say("No false wakes. That's all, thank you." if not count
            else f"That woke it {count} times by mistake. Good to know. That's all, thank you.")
    finally:
        PAUSE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
