"""Shared test helpers: deterministic audio and a scope."""

import hashlib

import numpy as np

from tts_cache.scope import Scope

SR = 24_000


def scope(**over: object) -> Scope:
    base: dict[str, object] = {
        "service": "fake",
        "model": "m1",
        "voice": "v1",
        "language": "en-US",
        "sample_rate": SR,
        "channels": 1,
        "settings": {"speed": 1.0},
    }
    base.update(over)
    return Scope(**base)  # type: ignore[arg-type]


def tone_pcm(text: str, sample_rate: int = SR, ms_per_char: float = 60.0) -> bytes:
    """A clip derived from the text: a tone whose pitch comes from the text's hash, 60 ms per character."""
    n = int(sample_rate * ms_per_char * max(1, len(text.strip())) / 1000)
    f = 120 + int(hashlib.sha256(text.encode()).hexdigest()[:4], 16) % 400
    t = np.arange(n) / sample_rate
    envelope = np.clip(np.sin(2 * np.pi * 3 * t), 0.2, None)
    return (0.3 * envelope * np.sin(2 * np.pi * f * t) * 32767).astype("<i2").tobytes()


def speechlike(seconds: float = 4.0, sr: int = SR) -> bytes:
    """A voiced signal with a moving pitch and syllable-rate envelope, deterministic."""
    rng = np.random.default_rng(7)
    t = np.arange(int(seconds * sr)) / sr
    pitch = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / sr
    voiced = sum(np.sin(k * phase) / k for k in range(1, 12))
    envelope = np.clip(np.sin(2 * np.pi * 4 * t), 0, None)
    signal = 0.25 * voiced * envelope + 0.01 * rng.standard_normal(t.size)
    return (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()


def digest(pcm: bytes) -> str:
    return hashlib.sha256(pcm).hexdigest()[:16]
