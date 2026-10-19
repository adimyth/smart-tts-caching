import asyncio
import threading

import pytest
from helpers import SR, scope, speechlike, tone_pcm
from store_contract import make_entry

from tts_cache import Audio, TTSCache
from tts_cache.metrics import LogSink
from tts_cache.policy import AllOf, AlwaysCache, MinChars, Namespace, Shadow, SkipIfMatches
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.sqlite import SQLiteStore
from tts_cache.stores.tiered import TieredStore
from tts_cache.verify import Verdict

TEXT = "Hello, and thank you for calling."


def new_cache(store=None, **kw) -> tuple[TTSCache, LogSink]:
    sink = LogSink(log=None)
    return TTSCache(scope(), store if store is not None else MemoryStore(), metrics=sink, **kw), sink


async def settle(cache: TTSCache) -> None:
    assert cache._queue is not None
    await cache._queue.join()


async def test_miss_capture_hit_round_trip() -> None:
    cache, sink = new_cache()
    await cache.start()
    assert await cache.lookup(TEXT) is None
    pcm = tone_pcm(TEXT)
    await cache.capture(TEXT, pcm, SR)
    await settle(cache)
    hit = await cache.lookup("  hello, and thank you for calling ")
    assert hit is not None and hit.level == "A" and hit.sample_rate == SR and hit.channels == 1
    assert hit.spoken_text == "  hello, and thank you for calling "
    assert hit.pcm == pcm  # process memory holds the PCM that played
    chunks = list(hit.pcm_stream())
    assert b"".join(chunks) == pcm and len(chunks[0]) == SR * 20 // 1000 * 2
    assert sink.counts["store"] == 1 and sink.counts["hit"] == 1 and sink.counts["miss"] == 1
    await cache.close()


async def test_hit_from_sqlite_streams_decoded_opus(tmp_path) -> None:
    cache, _ = new_cache(SQLiteStore(tmp_path / "c.db"))
    await cache.start()
    pcm = speechlike(2.0)
    text = "This is a two second clip of speech."
    await cache.capture(text, pcm, SR)
    await settle(cache)
    hit = await cache.lookup(text)
    assert hit is not None and hit.meta["tier"] == "sqlite"
    out = b"".join(hit.pcm_stream())
    assert len(out) == len(pcm)
    await cache.close()


@pytest.mark.parametrize(
    ("text", "pcm", "rate", "reason"),
    [
        ("Far too long for this clip of audio.", tone_pcm("ab", ms_per_char=10), SR, "duration"),
        ("Far too short.", tone_pcm("x" * 200), SR, "duration"),
        ("This one is silent.", b"\x00\x00" * SR, SR, "silence"),
        ("Wrong sample rate here.", tone_pcm("Wrong sample rate here.", 16_000), 16_000, "format"),
        ("Hi", tone_pcm("Hi"), SR, "policy"),
    ],
)
async def test_ingest_rejects(text: str, pcm: bytes, rate: int, reason: str) -> None:
    cache, sink = new_cache()
    await cache.capture(text, pcm, rate)
    await settle(cache)
    assert sink.counts[f"reject:{reason}"] == 1 and sink.counts["store"] == 0
    await cache.close()


async def test_indic_duration_bound_is_wider() -> None:
    cache, sink = new_cache(policy=AlwaysCache())
    text = "धन्यवाद।"  # 8 characters; 2.4 s is beyond the Latin bound but inside the Indic one
    await cache.capture(text, tone_pcm("x" * 40), SR)
    await settle(cache)
    assert sink.counts["store"] == 1
    await cache.close()


async def test_probation_ttl_and_promotion_on_hit() -> None:
    store = MemoryStore()
    cache, _ = new_cache(store)
    await cache.capture(TEXT, tone_pcm(TEXT), SR)
    await settle(cache)
    hit = await cache.lookup(TEXT)
    assert hit is not None
    entry = store.peek(hit.key)
    assert entry is not None and entry.expires_at is not None
    assert abs(entry.expires_at - entry.created_at - 48 * 3600) < 5
    await asyncio.sleep(0.01)  # the touch runs in the background
    assert entry.hit_count == 1 and abs(entry.expires_at - entry.last_hit_at - 30 * 86400) < 5  # type: ignore[operator]
    await cache.close()


async def test_skip_if_matches_and_namespace() -> None:
    cache, sink = new_cache(policy=AllOf(MinChars(), SkipIfMatches([r"\d{4}"]), Namespace("tenant")))
    await cache.capture("Your code is 1234.", tone_pcm("Your code is 1234."), SR)
    await cache.capture(TEXT, tone_pcm(TEXT), SR, tags={"tenant": "a"})
    await settle(cache)
    assert sink.counts["reject:policy"] == 1
    assert await cache.lookup(TEXT, tags={"tenant": "a"}) is not None
    assert await cache.lookup(TEXT, tags={"tenant": "b"}) is None
    await cache.close()


async def test_shadow_counts_but_never_serves() -> None:
    cache, sink = new_cache(policy=Shadow())
    await cache.capture(TEXT, tone_pcm(TEXT), SR)
    await settle(cache)
    assert await cache.lookup(TEXT) is None
    assert sink.counts["shadow_hit"] == 1 and sink.counts["hit"] == 0
    await cache.close()


async def test_breaker_stops_serving_and_storing() -> None:
    cache, sink = new_cache()
    await cache.capture(TEXT, tone_pcm(TEXT), SR)
    await settle(cache)
    cache.report_key_mismatch("predicted", "prepared")
    assert cache.breaker_tripped
    assert await cache.lookup(TEXT) is None
    probe = await cache.probe(TEXT)
    assert probe.store is False and probe.reason == "breaker"
    assert sink.counts["reject:key_mismatch"] == 1 and sink.counts["breaker"] == 1
    await cache.close()


async def test_verifier_can_reject() -> None:
    class No:
        async def verify(self, text, pcm, sample_rate, channels, language):
            return Verdict(ok=False, score=0.1)

    cache, sink = new_cache(verifier=No())
    await cache.capture(TEXT, tone_pcm(TEXT), SR)
    await settle(cache)
    assert sink.counts["reject:verify"] == 1
    await cache.close()


async def test_capture_key_must_match_scope_at_ingest() -> None:
    cache, sink = new_cache()
    found = await cache.probe(TEXT)
    await cache.set_scope(scope(voice="v2"))
    await cache.capture(TEXT, tone_pcm(TEXT), SR, key=found.key)
    await settle(cache)
    assert sink.counts["reject:key_changed"] == 1
    await cache.close()


async def test_generation_bump_invalidates(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "c.db")
    cache, _ = new_cache(store)
    await cache.start()
    await cache.capture(TEXT, tone_pcm(TEXT), SR)
    await settle(cache)
    assert await cache.lookup(TEXT) is not None
    await store.bump_generation(cache.scope)
    fresh, _ = new_cache(store)
    await fresh.start()
    assert fresh.scope.generation == 1 and await fresh.lookup(TEXT) is None
    await cache.close(close_store=False)
    await fresh.close()


async def test_queue_full_drops_and_counts() -> None:
    cache, sink = new_cache(queue_size=2)
    for i in range(5):
        await cache.capture(f"Sentence {i} goes here.", tone_pcm(f"Sentence {i} goes here."), SR)
    assert sink.counts["reject:queue_full"] == 3
    await cache.close()


async def test_close_drains_then_drops_after_timeout() -> None:
    class Slow(MemoryStore):
        async def put(self, key, entry):
            await asyncio.sleep(0.2)
            await super().put(key, entry)

    cache, sink = new_cache(Slow())
    for i in range(4):
        await cache.capture(f"Sentence {i} goes here.", tone_pcm(f"Sentence {i} goes here."), SR)
    await cache.close(timeout=0.3)
    assert sink.counts["store"] in (1, 2) and sink.counts["capture_dropped"] == 1
    await cache.capture(TEXT, tone_pcm(TEXT), SR)
    assert sink.counts["capture_dropped:closed"] == 1


async def test_synthesize() -> None:
    cache, _ = new_cache()
    calls: list[str] = []

    async def provider(text: str) -> Audio:
        calls.append(text)
        return Audio(tone_pcm(text), SR)

    a = await cache.synthesize(TEXT, provider)
    await settle(cache)
    b = await cache.synthesize(TEXT, provider)
    assert calls == [TEXT] and not a.cached and b.cached and a.pcm == b.pcm
    await cache.close()


async def test_stream_splits_and_serves() -> None:
    cache, _ = new_cache()
    sent: list[str] = []

    async def provider(text: str):
        sent.append(text)
        pcm = tone_pcm(text)
        for i in range(0, len(pcm), 960):
            yield pcm[i : i + 960]

    async def tokens(text: str):
        for tok in text.split(" "):
            yield tok + " "

    turn = "Hello there. Thanks for calling. Hello there."
    out = []
    async for sentence, chunks in cache.stream(tokens(turn), provider):
        out.append((sentence, b"".join([c async for c in chunks])))
        await settle(cache)
    assert [s for s, _ in out] == ["Hello there.", "Thanks for calling.", "Hello there."]
    assert sent == ["Hello there.", "Thanks for calling."]
    assert all(pcm == tone_pcm(s) for s, pcm in out)
    await cache.close()


def test_sync_wrappers_without_a_loop() -> None:
    cache, _ = new_cache()
    cache.capture_sync(TEXT, tone_pcm(TEXT), SR)
    hit = None
    for _ in range(100):
        hit = cache.lookup_sync(TEXT)
        if hit:
            break
        threading.Event().wait(0.01)
    assert hit is not None and hit.pcm == tone_pcm(TEXT)
    cache.close_sync()


async def test_warm_start_loads_hot_set_into_memory(tmp_path) -> None:
    shared = SQLiteStore(tmp_path / "c.db")
    first, _ = new_cache(TieredStore(MemoryStore(), shared, deadline_ms=200))
    await first.start()
    await first.capture(TEXT, tone_pcm(TEXT), SR)
    await settle(first)
    await first.close()

    shared2 = SQLiteStore(tmp_path / "c.db")
    tiered = TieredStore(MemoryStore(), shared2, deadline_ms=200)
    second, sink = new_cache(tiered)
    await second.start()
    assert sink.counts["warm_start"] == 1 and len(tiered.memory) == 1
    hit = await second.lookup(TEXT)
    assert hit is not None and hit.meta["tier"] == "memory"
    await second.close()


def test_entry_helpers() -> None:
    e = make_entry(TEXT)
    assert e.pcm_nbytes == len(tone_pcm(TEXT)) // 2 * 2
