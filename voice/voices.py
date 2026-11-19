"""Which Kokoro voice each agent speaks in, chosen on the phone.

Kept in voices.json: each agent its own, one voice for all, or a surprise pick that stays until they ask
again. Talkback reads it when a clip starts, so a change needs no restart. A preview is one short line,
rendered once and kept.
"""

from __future__ import annotations

import random
import wave
from pathlib import Path

from voice.agents import AGENT_VOICES, DEFAULT_VOICE
from voice.state import STATE, VOICE_CHOICE, read_json, write_json

AGENTS = ("claude", "cursor", "codex")
# A short list the phone can offer. The first is the voice the user likes (Claude's).
CHOICES = (
    ("af_heart", "Heart"),
    ("af_bella", "Bella"),
    ("af_sky", "Sky"),
    ("bf_alice", "Alice"),
    ("bm_george", "George"),
)
MODES = ("own", "one", "surprise")
LINE = "Hello. This is how I sound."
SAMPLES = STATE / "voice-samples"
_IDS = {voice for voice, _ in CHOICES}
_NAMES = dict(CHOICES)


def _blank() -> dict:
    return {
        "mode": "own",
        "voice": "af_heart",
        "agents": {agent: AGENT_VOICES[agent] for agent in AGENTS},
        "surprise": {},
    }


def _known(voice: str) -> str:
    return voice if voice in _IDS else ""


def load() -> dict:
    """The saved choice, with anything missing filled from the built-in voices."""
    raw = read_json(VOICE_CHOICE)
    base = _blank()
    mode = raw.get("mode")
    base["mode"] = mode if mode in MODES else "own"
    one = _known(str(raw.get("voice", "")))
    if one:
        base["voice"] = one
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    surprise = raw.get("surprise") if isinstance(raw.get("surprise"), dict) else {}
    for agent in AGENTS:
        picked = _known(str(agents.get(agent, "")))
        if picked:
            base["agents"][agent] = picked
        lucky = _known(str(surprise.get(agent, "")))
        if lucky:
            base["surprise"][agent] = lucky
    return base


def _pick() -> dict:
    return {agent: random.choice(tuple(_IDS)) for agent in AGENTS}


def _complete(surprise: dict) -> bool:
    return all(surprise.get(agent) in _IDS for agent in AGENTS)


def apply(change: dict) -> dict:
    """Save a change from the phone and return what the sheet should show."""
    data = load()
    mode = change.get("mode", data["mode"])
    data["mode"] = mode if mode in MODES else data["mode"]
    if data["mode"] == "surprise" and (change.get("again") or not _complete(data["surprise"])):
        data["surprise"] = _pick()
    one = _known(str(change.get("voice", "")))
    if data["mode"] == "one" and one:
        data["voice"] = one
    agent = str(change.get("agent", ""))
    if data["mode"] == "own" and agent in AGENTS and one:
        data["agents"][agent] = one
    write_json(VOICE_CHOICE, data)
    return public()


def for_agent(agent: str) -> str:
    """The voice a clip uses when the caller didn't name one."""
    if agent not in AGENTS:
        return AGENT_VOICES.get(agent, DEFAULT_VOICE)
    data = load()
    if data["mode"] == "one":
        return data["voice"]
    if data["mode"] == "surprise" and _complete(data["surprise"]):
        return data["surprise"][agent]
    return data["agents"].get(agent) or AGENT_VOICES[agent]


def showing(data: dict | None = None) -> dict:
    """The voice each agent speaks in now: {agent: {id, name}}."""
    data = data or load()
    found = {}
    for agent in AGENTS:
        voice = data["voice"] if data["mode"] == "one" else (
            data["surprise"].get(agent) if data["mode"] == "surprise" and _complete(data["surprise"])
            else data["agents"].get(agent))
        voice = voice or AGENT_VOICES[agent]
        found[agent] = {"id": voice, "name": _NAMES.get(voice, voice)}
    return found


def public() -> dict:
    data = load()
    return {
        "mode": data["mode"],
        "voice": data["voice"],
        "agents": data["agents"],
        "choices": [{"id": voice, "name": name} for voice, name in CHOICES],
        "showing": showing(data),
    }


def _wav(path: Path, audio, rate: int) -> None:
    import numpy as np

    audio = np.asarray(audio, dtype="float32")
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = audio / peak * 0.8
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((audio * 32767).astype("<i2").tobytes())


def sample(voice: str) -> Path | None:
    """A short clip of that voice, rendered once and kept. None when it isn't one of the choices."""
    if voice not in _IDS:
        return None
    path = SAMPLES / f"{voice}.wav"
    if path.exists() and path.stat().st_size > 1000:
        return path
    from kokoro_onnx import Kokoro

    from voice.state import KOKORO_MODELS

    kokoro = Kokoro(str(KOKORO_MODELS / "kokoro-v1.0.onnx"), str(KOKORO_MODELS / "voices-v1.0.bin"))
    audio, rate = kokoro.create(LINE, voice=voice, lang="en-us")
    _wav(path, audio, rate)
    return path
