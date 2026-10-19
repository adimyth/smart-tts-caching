"""Ingest checks on a captured clip: plausible duration and silence (SOLUTION.md 4.1, 11.6)."""

import numpy as np

# Plausible duration is affine in the character count: a fixed intercept covers
# leading and trailing silence on very short sentences.
MIN_MS_PER_CHAR = 20.0
INTERCEPT_MS = 600.0
MAX_MS_PER_CHAR_LATIN = 120.0
MAX_MS_PER_CHAR_INDIC = 300.0

# Devanagari through Malayalam, plus Sinhala.
_INDIC = range(0x0900, 0x0E00)


def has_indic(text: str) -> bool:
    return any(ord(ch) in _INDIC for ch in text)


def duration_bounds_ms(text: str) -> tuple[float, float]:
    chars = len(text.strip())
    slope = MAX_MS_PER_CHAR_INDIC if has_indic(text) else MAX_MS_PER_CHAR_LATIN
    return MIN_MS_PER_CHAR * chars, INTERCEPT_MS + slope * chars


def plausible_duration(text: str, duration_ms: float) -> bool:
    lo, hi = duration_bounds_ms(text)
    return lo <= duration_ms <= hi


def pcm_duration_ms(pcm: bytes, sample_rate: int, channels: int = 1) -> float:
    return len(pcm) / (sample_rate * 2 * channels) * 1000.0


# A clip whose loudest sample stays under about -50 dBFS holds no speech.
SILENCE_PEAK = 100


def is_silent(pcm: bytes) -> bool:
    if len(pcm) < 2:
        return True
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")
    return int(np.abs(samples.astype(np.int32)).max()) < SILENCE_PEAK
