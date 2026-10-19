"""Faults injected into every store method and every core stage leave lookup a miss and capture silent.

SOLUTION.md section 2, "Fail open, defined": a broken cache and an absent
cache differ only in a metric.
"""

import asyncio
import time

import pytest
from helpers import SR, scope, tone_pcm

from tts_cache import TTSCache, canonical
from tts_cache.metrics import LogSink
from tts_cache.store import CacheStore, Entry
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.sqlite import SQLiteStore
from tts_cache.stores.tiered import TieredStore

TEXT = "Hello, and thank you for calling."
METHODS = ["get", "put", "touch", "delete", "purge", "hot", "generation", "bump_generation", "close"]


class Faulty:
    """Wraps a store; ``method`` raises or hangs on every call."""

    def __init__(self, inner: CacheStore, method: str, mode: str) -> None:
        self.inner, self.method, self.mode = inner, method, mode
        self.calls = 0

    def __getattr__(self, name: str):
        target = getattr(self.inner, name)
        if name != self.method:
            return target

        async def faulty(*args, **kwargs):
            self.calls += 1
            if self.mode == "raise":
                raise ConnectionError(f"injected fault in {name}")
            await asyncio.sleep(30)
            return await target(*args, **kwargs)

        return faulty


async def prime(store: CacheStore) -> None:
    good = TTSCache(scope(), store, metrics=LogSink(log=None))
    await good.capture(TEXT, tone_pcm(TEXT), SR)
    await good.close(close_store=False)


def stores(tmp_path) -> dict[str, CacheStore]:
    return {
        "memory": MemoryStore(),
        "sqlite": SQLiteStore(tmp_path / "c.db"),
        "tiered": TieredStore(MemoryStore(), SQLiteStore(tmp_path / "t.db"), deadline_ms=20),
    }


@pytest.mark.parametrize("mode", ["raise", "hang"])
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("backend", ["memory", "sqlite", "tiered"])
async def test_store_fault(backend: str, method: str, mode: str, tmp_path) -> None:
    inner = stores(tmp_path)[backend]
    await prime(inner)
    store = Faulty(inner, method, mode)
    sink = LogSink(log=None)
    cache = TTSCache(scope(), store, metrics=sink, warm_start_timeout_s=0.1)  # type: ignore[arg-type]

    t0 = time.perf_counter()
    await asyncio.wait_for(cache.start(), 1.0)
    hit = await cache.lookup(TEXT)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, elapsed
    if method == "get":
        assert hit is None
        assert sink.counts["fail_open"] + sink.counts["deadline"] >= 1
    else:
        assert hit is not None and hit.pcm  # a fault elsewhere does not stop serving

    other = "A different sentence to capture."
    await cache.capture(other, tone_pcm(other), SR)  # never raises
    assert cache._queue is not None
    try:
        await asyncio.wait_for(cache._queue.join(), 0.5)
    except TimeoutError:
        assert mode == "hang" and method == "put"
    if method == "put" and mode == "raise":
        assert sink.counts["reject:error"] == 1 and sink.counts["fail_open"] >= 1
    if method == "touch":
        await asyncio.sleep(0.05)
        assert mode == "hang" or sink.counts["fail_open"] >= 1
    if method == "generation":
        assert sink.counts["fail_open"] >= 1
    await asyncio.wait_for(cache.close(timeout=0.2), 2.0)


@pytest.mark.parametrize("stage", ["canonicalise", "matcher", "policy", "verifier", "encode", "metrics"])
async def test_core_stage_fault(stage: str, monkeypatch) -> None:
    store = MemoryStore()
    await prime(store)
    sink = LogSink(log=None)
    cache = TTSCache(scope(), store, metrics=sink)

    def boom(*a, **k):
        raise RuntimeError(f"injected fault in {stage}")

    async def aboom(*a, **k):
        raise RuntimeError(f"injected fault in {stage}")

    if stage == "canonicalise":
        monkeypatch.setattr("tts_cache.cache.canonicalise", boom)
    elif stage == "matcher":
        monkeypatch.setattr(cache.matcher, "find", aboom)
    elif stage == "policy":
        monkeypatch.setattr(cache.policy, "should_lookup", boom)
        monkeypatch.setattr(cache.policy, "should_store", boom)
    elif stage == "verifier":
        monkeypatch.setattr(cache.verifier, "verify", aboom)
    elif stage == "encode":
        monkeypatch.setattr("tts_cache.codec.encode", boom)
    elif stage == "metrics":
        monkeypatch.setattr(sink, "event", boom)

    hit = await cache.lookup(TEXT)
    if stage in ("canonicalise", "matcher", "policy"):
        assert hit is None
        assert sink.counts["fail_open"] >= 1
    else:
        assert hit is not None
    other = "A different sentence to capture."
    await cache.capture(other, tone_pcm(other), SR)
    assert cache._queue is not None
    await asyncio.wait_for(cache._queue.join(), 1.0)
    if stage in ("canonicalise", "policy", "verifier", "encode"):
        assert sink.counts["reject:error"] == 1
    await cache.close()


async def test_corrupt_stored_audio_is_a_miss() -> None:
    store = MemoryStore()
    await prime(store)
    for e in [*store._session.values()]:
        store._session[e.key] = Entry(**{**e.__dict__, "audio": b"not opus", "pcm": None})
    cache = TTSCache(scope(), store, metrics=(sink := LogSink(log=None)))
    assert await cache.lookup(TEXT) is None
    assert sink.counts["fail_open"] == 1


def test_canonical_module_is_patchable() -> None:
    assert callable(canonical.canonicalise)
