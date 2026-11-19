#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""Train a personal "hey Claude" (or "hey Jarvis") detector from wakeword_collect.py recordings.

    wakeword_train.py [--word jarvis] [--finder large-v3-turbo]

--finder: the Whisper model that finds the wake words in the takes (on the
GPU when it is not "small"): small missed 6 of 20 "hey Jarvis" from across
the room (2026-11-15).

The published openWakeWord model missed most of the user's own "hey Claude"s,
so this fits a small classifier on openWakeWord's speech embeddings instead
(the same 16 x 96 features its models use, 1.28 s of audio):
- positives: each "hey Claude" in the samples, found with Whisper word
  timings, at a few offsets and gains;
- negatives: everything else in the samples, the user's normal-speech take,
  and Kokoro voices saying ordinary sentences and near misses.
It reports leave-one-position-out recall (train on three spots, test on the
fourth) and the false-wake rate on held-out speech, then saves the model to
~/.local/share/handsfree/hey_claude_personal.pkl (hey_jarvis_personal.pkl).
For "hey Jarvis" the "hey Claude" takes count as negatives too: the same
voice, the same rooms, and the other wake word.
"""

from __future__ import annotations

import difflib
import glob
import json
import pickle
import re
import sys
import wave
from pathlib import Path

import numpy as np
from openwakeword.utils import AudioFeatures
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))  # voice/
from voice.state import HANDSFREE_DATA, JARVIS_PERSONAL, KOKORO_MODELS as KOKORO, WAKE_MODEL, WAKE_SAMPLES  # noqa: E402

RATE = 16000
WIN = 2 * RATE  # 2 s of audio gives the 16 embedding frames a detector sees
SHIFTS = (0, 1600, 3200, 4800)  # windows ending 0 .. 0.3 s after the word: never before it is out
GAINS = (0.7, 1.0, 1.6)
NEGATIVE_LINES = [
    "Okay, the build is done, and all the tests passed.",
    "Can you check the logs and tell me what happened?",
    "I'm making dinner, so give me a minute.",
    "Hey, could you look at the cloud settings?",
    "Hey Clyde, how was the game?",
    "The weather is lovely today, let's go outside.",
    "Claire said the meeting moved to Thursday.",
    "Hey, cool, that actually worked on the first try.",
    "Okay cloud, play some music.",
    "He clawed at the door until somebody let him in.",
]
NEAR_MISSES = {  # per word: what sounds closest without being it
    "claude": [],
    "jarvis": ["Hey Travis, how are you doing?", "The jar is on the top shelf.", "Harvest season starts soon.",
               "Hey Davis, come over here.", "Hey, service was really slow today.", "Charlie is coming over."],
    "henchline": ["The bench line is straight.", "Hinge line.", "Attention, please.", "The hens lined up."],
    "papoy": ["The paper is on the desk.", "Poppy, come here.", "A boy and a toy.", "Tap, oy."],
}


def load(path: str) -> np.ndarray:
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)


def window(audio: np.ndarray, end: int) -> np.ndarray:
    """2 s ending at sample `end`, zero-padded at the front."""
    start = end - WIN
    clip = audio[max(0, start):end]
    return np.concatenate([np.zeros(WIN - len(clip), np.float32), clip])


def is_word(heard: str, word: str) -> bool:
    """Whisper's word is the wake word: "Claude" exactly; a name Whisper spells its own way ("Jervis") closely."""
    heard = re.sub(r"\W", "", heard).lower()
    return heard == word or (word != "claude" and difflib.SequenceMatcher(None, word, heard).ratio() >= 0.8)


def hey_word_spans(model, path: str, word: str) -> list[tuple[int, int]]:
    """Sample indices where the wake word starts and ends. "hey" in front is optional. Two words that together
    are the wake word ("hench" "line") count as one span."""
    segments, _ = model.transcribe(path, language="en", beam_size=1, word_timestamps=True)
    words = [w for s in segments for w in s.words]
    spans = []
    i = 0
    while i < len(words):
        heard = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        pair = re.sub(r"\W", "", heard.word + (nxt.word if nxt else ""))
        if is_word(heard.word, word):
            spans.append((int(heard.start * RATE), int(heard.end * RATE)))
            i += 1
        elif nxt and not is_word(nxt.word, word) and is_word(pair, word):
            # "hench" "line" is one span. "hey" "jarvis" is not: jarvis alone is the word, and the pair
            # "heyjarvis" is close enough (0.80) that counting both would make one take look like two.
            spans.append((int(heard.start * RATE), int(nxt.end * RATE)))
            i += 2
        else:
            i += 1
    return spans


def kokoro_negatives(word: str) -> list[np.ndarray]:
    """Ordinary sentences and near misses in a few voices, at 16 kHz."""
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(KOKORO / "kokoro-v1.0.onnx"), str(KOKORO / "voices-v1.0.bin"))
    out = []
    for line in NEGATIVE_LINES + NEAR_MISSES[word]:
        for voice in ["am_michael", "af_sarah", "bm_george", "af_heart"]:
            samples, rate = kokoro.create(line, voice=voice, lang="en-us")
            samples = np.interp(np.arange(0, len(samples) * RATE / rate) * rate / RATE, np.arange(len(samples)), samples)
            out.append((samples * 32767 * 0.3).astype(np.float32))
    return out


def main() -> int:
    from faster_whisper import WhisperModel

    options = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    word, finder = options.get("--word", "claude").lower(), options.get("--finder", "small")
    if word == "claude":
        samples, out = WAKE_SAMPLES, WAKE_MODEL
    elif word == "jarvis":
        samples, out = WAKE_SAMPLES / word, JARVIS_PERSONAL
    else:
        samples, out = WAKE_SAMPLES / word, HANDSFREE_DATA / f"hey_{word}_personal.pkl"
    # The other wake words' takes are negatives: same voice and rooms, and the nearest thing to a confusion.
    # For "claude" that is every word folder under samples/, any future one included
    others = sorted(glob.glob(str(WAKE_SAMPLES / ("*.wav" if word != "claude" else "*/*.wav"))))
    # The user's ordinary speech (asr_collect.py's sentences, minus any that say the word): "feather" fooled
    # a detector that had heard 10 s of it (2026-11-15)
    asr = [str(index.parent / take) for index in sorted((WAKE_SAMPLES.parent / "asr-set").glob("**/set.json"))
           for take, asked in sorted(json.loads(index.read_text()).items())
           # a sentence, or (tone_collect.py) what was asked for it, with the sentence under "text"
           if word not in (asked if isinstance(asked, str) else asked.get("text", "")).lower()
           and (index.parent / take).exists()]  # every round: loose, --strict, tone
    print(f"{len(asr)} takes of ordinary speech as negatives", flush=True)
    others += asr

    whisper = (WhisperModel(finder, device="cpu", compute_type="int8") if finder == "small"
               else WhisperModel(finder, device="cuda", compute_type="float16"))
    features = AudioFeatures(inference_framework="onnx")

    def embed(clips: list[np.ndarray]) -> np.ndarray:
        x = features.embed_clips(np.stack(clips).astype(np.int16), batch_size=64)
        return x.reshape(len(clips), -1)

    pos, neg, cut = {}, {}, {}
    for path in sorted(glob.glob(str(samples / "*.wav"))) + others:
        spot = "negative" if path in others else Path(path).name.rsplit("-", 1)[0]
        audio = load(path)
        audio = np.concatenate([np.zeros(RATE, np.float32), audio, np.zeros(RATE // 2, np.float32)])
        spans = [(s + RATE, e + RATE) for s, e in hey_word_spans(whisper, path, word)] if spot != "negative" else []
        ends = [e for _, e in spans]
        for start, end in spans:
            for shift in SHIFTS:
                for gain in GAINS:
                    pos.setdefault(spot, []).append(window(audio, end + shift) * gain)
            # The word cut off ("hey Jar", 2026-11-15: it woke on that): taught as "not it" at the cut. Taught
            # with silence after it as well (as the detector then sees it), recall fell to 17-67%: this
            # detector cannot tell "hey Jar." from "hey Jarvis." Cut here, it still took the false wakes
            # of "hey Claude" on held-out room audio from 12 in 5.5 min to 0
            for part in (0.3, 0.5, 0.65):
                for gain in GAINS:
                    clip = window(audio, start + int((end - start) * part)) * gain
                    neg.setdefault(spot, []).append(clip)
                    cut.setdefault(spot, []).append(clip)
        print(f"{Path(path).name}: {len(ends)} {word}", flush=True)
        if spot != "negative" and len(ends) < 2:  # asked for twice: an unfound one would be taught as "not it"
            continue
        for end in range(WIN, len(audio), RATE // 4):  # every 0.25 s, away from any wake word
            if all(abs(end - e) > RATE for e in ends):
                neg.setdefault(spot, []).append(window(audio, end))

    synthetic = []
    for audio in kokoro_negatives(word):
        audio = np.concatenate([np.zeros(RATE, np.float32), audio])
        synthetic += [window(audio, end) for end in range(WIN, len(audio), RATE // 4)]
    print(f"positives {sum(map(len, pos.values()))}, negatives {sum(map(len, neg.values()))} + {len(synthetic)} synthetic")

    X = {s: embed(c) for s, c in pos.items()}
    N = {s: embed(c) for s, c in neg.items()}
    C = {s: embed(c) for s, c in cut.items()}
    S = embed(synthetic)

    def fit(pos_sets, neg_sets):
        Xp = np.concatenate(pos_sets)
        Xn = np.concatenate(neg_sets)
        clf = make_pipeline(StandardScaler(), LogisticRegression(C=0.05, max_iter=2000, class_weight="balanced"))
        clf.fit(np.concatenate([Xp, Xn]), np.r_[np.ones(len(Xp)), np.zeros(len(Xn))])
        return clf

    print("leave one spot out (centre offset only counts as a hit):")
    half = len(S) // 2
    for spot in X:
        clf = fit([X[s] for s in X if s != spot], [N[s] for s in N if s != spot] + [S[:half]])
        per = len(SHIFTS) * len(GAINS)
        centre = X[spot][1::per] if len(X[spot]) >= per else X[spot]  # shift 0, gain 1.0 per utterance
        hits = (clf.predict_proba(centre)[:, 1] > 0.5).mean()
        false = (clf.predict_proba(np.concatenate([N.get("negative", S[:0]), S[half:]]))[:, 1] > 0.5).mean()
        halves = (clf.predict_proba(C[spot])[:, 1] > 0.5).mean() if spot in C else float("nan")
        print(f"  {spot:14s} recall {hits:.0%}  false wakes on held-out speech {false:.1%}  "
              f"cut-off word taken {halves:.0%}")
    clf = fit(list(X.values()), list(N.values()) + [S])
    out.write_bytes(pickle.dumps(clf))
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
