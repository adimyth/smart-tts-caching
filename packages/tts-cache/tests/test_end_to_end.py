"""200 sentences at repetition rate 0.5 through TTSCache.synthesize with SQLiteStore and a fake provider.

Characters sent to the provider must fall by the character-weighted hit rate
the cache reports, and every sentence must get its own audio, including after
the store is deleted mid-run.
"""

import asyncio
import random

import numpy as np
from helpers import SR, scope, tone_pcm

from tts_cache import Audio, TTSCache
from tts_cache.metrics import LogSink
from tts_cache.stores.sqlite import SQLiteStore

SUBJECTS = [
    "your account",
    "the refund",
    "your order",
    "the appointment",
    "your plan",
    "the payment",
    "your card",
]
VERBS = [
    "has been updated",
    "is on its way",
    "was confirmed",
    "needs one more step",
    "is ready",
    "was received",
]
TAILS = [
    "",
    " today",
    " this morning",
    " as you asked",
    " and you will get an email shortly",
    " within two working days",
]


def corpus(n: int = 200, r: float = 0.5, seed: int = 11) -> tuple[list[list[str]], int]:
    """Turns of 1 to 5 sentences; a fraction ``r`` of draws repeat an earlier sentence, Zipf-weighted."""
    rng = random.Random(seed)
    fresh = iter(f"{s[0].upper()}{s[1:]} {v}{t}." for s in SUBJECTS for v in VERBS for t in TAILS)
    seen: list[str] = []
    draws: list[str] = []
    repeats = 0
    for i in range(n):
        if i and rng.random() < r:
            weights = [1 / (k + 1) for k in range(len(seen))]
            draws.append(rng.choices(seen, weights)[0])
            repeats += 1
        else:
            seen.append(next(fresh))
            draws.append(seen[-1])
    turns, i = [], 0
    while i < n:
        size = rng.randint(1, 5)
        turns.append(draws[i : i + size])
        i += size
    return turns, repeats


def same_clip(a: bytes, b: bytes) -> bool:
    if len(a) != len(b):
        return False
    x = np.frombuffer(a, dtype="<i2").astype(np.float64)
    y = np.frombuffer(b, dtype="<i2").astype(np.float64)
    return float(np.corrcoef(x, y)[0, 1]) > 0.9


async def run(tmp_path, *, delete_at: int | None = None) -> dict:
    db = tmp_path / "e2e.db"
    store = SQLiteStore(db)
    sink = LogSink(log=None)
    cache = TTSCache(scope(), store, metrics=sink)
    await cache.start()
    billed: list[str] = []

    async def provider(text: str) -> Audio:
        billed.append(text)
        await asyncio.sleep(0.002)
        return Audio(tone_pcm(text), SR)

    turns, repeats = corpus()
    total_chars, wrong, n = 0, 0, 0
    for turn in turns:
        for text in turn:
            if delete_at is not None and n == delete_at:
                await store.close()
                for path in tmp_path.glob("e2e.db*"):
                    path.unlink()
            audio = await cache.synthesize(text, provider)
            total_chars += len(text.strip())
            wrong += not same_clip(audio.pcm, tone_pcm(text))
            n += 1
        await asyncio.sleep(0.03)  # the gap between turns
    await cache.close()
    billed_chars = sum(len(t.strip()) for t in billed)
    return {
        "n": n,
        "repeats": repeats,
        "wrong": wrong,
        "hits": sink.counts["hit"],
        "hit_rate": sink.hit_rate(),
        "saving": 1 - billed_chars / total_chars,
        "fail_open": sink.counts["fail_open"],
        "sink": sink,
    }


async def test_billed_characters_fall_by_the_hit_rate(tmp_path) -> None:
    r = await run(tmp_path)
    assert r["n"] == 200 and r["wrong"] == 0
    assert abs(r["saving"] - r["hit_rate"]) <= 0.02, r
    # Ingest runs behind the provider (the fake answers in 2 ms, a real one plays
    # for seconds), so a repeat that follows its first occurrence within a few
    # milliseconds can still miss; most repeats hit.
    assert r["hits"] >= 0.75 * r["repeats"] and r["hit_rate"] > 0.35, r
    assert r["fail_open"] == 0


async def test_store_deleted_mid_run_keeps_audio_correct(tmp_path) -> None:
    r = await run(tmp_path, delete_at=100)
    assert r["n"] == 200 and r["wrong"] == 0
    assert r["fail_open"] > 0
    assert abs(r["saving"] - r["hit_rate"]) <= 0.02, r
