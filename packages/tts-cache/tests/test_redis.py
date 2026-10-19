"""Redis tiers, warm start, prefetch and ping against the dedicated instance.

Run with ``make redis-up`` then ``make test-redis``; skipped when
TTS_CACHE_REDIS_URL is unset, and refused against port 6379.
"""

import asyncio
import random
import subprocess
import sys
import time

import pytest
from helpers import SR, scope, tone_pcm
from redis_guard import test_redis_url
from store_contract import make_entry

from tts_cache import MemoryStore, TieredStore, TTSCache, build_key, canonicalise, scope_hash
from tts_cache.metrics import LogSink
from tts_cache.store import Entry
from tts_cache.stores.redis import RedisStore

pytestmark = pytest.mark.redis


class CountingRedis(RedisStore):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.gets = 0
        self.puts = 0

    async def get(self, key: str) -> Entry | None:
        self.gets += 1
        return await super().get(key)

    async def put(self, key: str, entry: Entry) -> None:
        self.puts += 1
        await super().put(key, entry)


@pytest.fixture
async def url():
    u = test_redis_url()
    r = RedisStore(u)
    await r.client.flushdb()
    await r.close()
    return u


async def test_memory_hit_never_touches_redis(url) -> None:
    shared = CountingRedis(url)
    t = TieredStore(MemoryStore(), shared, deadline_ms=20)
    e = make_entry("Already in process memory.")
    await t.put(e.key, e)
    shared.gets = 0
    got = await t.get(e.key)
    assert got is not None and got.tier == "memory" and shared.gets == 0
    await t.close()


async def test_shared_hit_lands_in_memory(url) -> None:
    writer = RedisStore(url)
    e = make_entry("Captured by another container.")
    await writer.put(e.key, e)
    await writer.close()
    shared = CountingRedis(url)
    t = TieredStore(MemoryStore(), shared, deadline_ms=20)
    first = await t.get(e.key)
    assert first is not None and first.tier == "shared" and first.pcm is not None
    second = await t.get(e.key)
    assert second is not None and second.tier == "memory" and shared.gets == 1
    await t.close()


async def test_client_pause_is_a_miss_within_the_deadline(url) -> None:
    writer = RedisStore(url)
    e = make_entry("Redis is about to stall.")
    await writer.put(e.key, e)
    cache = TTSCache(
        scope(),
        TieredStore(MemoryStore(), RedisStore(url), deadline_ms=20),
        metrics=(sink := LogSink(log=None)),
    )
    await writer.client.execute_command("CLIENT", "PAUSE", "600", "ALL")
    try:
        t0 = time.perf_counter()
        hit = await cache.lookup(e.text)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        await writer.client.execute_command("CLIENT", "UNPAUSE")
        await writer.close()
    assert hit is None and elapsed_ms < 30, elapsed_ms
    assert sink.counts["deadline"] == 1 and sink.counts["miss:deadline"] + sink.counts["miss"] >= 1
    await cache.close()


async def test_fifty_concurrent_misses_store_once(url) -> None:
    shared = CountingRedis(url)
    sink = LogSink(log=None)
    cache = TTSCache(scope(), TieredStore(MemoryStore(), shared, deadline_ms=50), metrics=sink)
    await cache.start()
    text = "Everybody says this sentence at the same moment."
    found = await asyncio.gather(*(cache.probe(text) for _ in range(50)))
    assert all(f.hit is None for f in found) and shared.gets == 1  # one read shared by all 50
    pcm = tone_pcm(text)
    await asyncio.gather(*(cache.capture(text, pcm, SR, key=f.key) for f in found))
    await cache.drain(10)
    assert shared.puts == 1 and sink.counts["store"] == 1 and sink.counts["store_coalesced"] == 49
    await cache.close()


async def test_export_and_import(url, tmp_path) -> None:
    src = RedisStore(url)
    entries = [make_entry(f"Exported sentence {i}.") for i in range(3)]
    for e in entries:
        await src.put(e.key, e)
    await src.bump_generation(scope(voice="gen"))
    assert await src.export(tmp_path / "dump.jsonl") == 3
    await src.client.flushdb()
    assert await src.get(entries[0].key) is None
    assert await src.import_(tmp_path / "dump.jsonl") == 3
    got = await src.get(entries[2].key)
    assert got is not None and got.audio == entries[2].audio
    assert await src.generation(scope(voice="gen")) == 1
    await src.close()


async def test_failed_warm_start_is_a_cold_cache() -> None:
    dead = RedisStore("redis://localhost:6399/0", socket_timeout=0.2)
    sink = LogSink(log=None)
    cache = TTSCache(scope(), TieredStore(MemoryStore(), dead, deadline_ms=20), metrics=sink)
    await cache.start()
    assert sink.counts["warm_start_failed"] == 1 or sink.counts["fail_open"] >= 1
    assert await cache.lookup("Hello, and thank you for calling.") is None
    await cache.close()


async def test_warm_is_one_round_trip_matching_hot(url) -> None:
    store = RedisStore(url)
    sc = scope()
    for i in range(12):
        e = make_entry(f"Warm start sentence number {i}.", sc)
        await store.put(e.key, e)
    gone = make_entry("This one has expired.", sc, expires_at=time.time() - 1)
    await store.put(gone.key, gone)
    budget = 5 * make_entry("Warm start sentence number 1.", sc).nbytes
    calls = []
    real = store.client.execute_command

    async def counted(*args, **kwargs):
        calls.append(args[0])
        return await real(*args, **kwargs)

    store.client.execute_command = counted  # type: ignore[method-assign]
    entries, pairs = await store.warm(sc, budget, 8)
    assert calls == ["EVAL"]
    store.client.execute_command = real  # type: ignore[method-assign]
    assert [e.key for e in entries] == [e.key for e in await store.hot(sc, budget)]
    assert pairs == await store.hot_texts(sc, 8)
    assert 4 <= len(entries) <= 5 and len(pairs) == 8
    assert await store.client.zscore(f"tts:hot:en:{scope_hash(sc)}", gone.key.split(":")[2]) is None
    await store.close()


def test_ping_cli(url) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "tts_cache.cli", "ping", "--url", url, "-n", "500"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "p50=" in out.stdout and "p99=" in out.stdout and "PASS" in out.stdout
    p99 = float(out.stdout.split("p99=")[1].split(" ")[0])
    assert p99 < 0.2, out.stdout  # local round trip, the M3 gate


# The serverless profile: a fresh cache per session, as one container per session runs it.

OPENER = "Hello, and thank you for calling Acme support."
HOT = [
    f"{a} {b}."
    for a, b in [
        ("Let me check that", "for you right away"),
        ("Your order", "is on its way and should arrive soon"),
        ("I can see your account", "here on my screen"),
        ("Please hold", "while I look into this"),
        ("Your refund", "has been processed successfully"),
        ("Is there anything else", "I can help you with today"),
        ("Thank you for your patience", "while I checked"),
        ("I have updated", "your delivery address"),
        ("Could you confirm", "the email on your account"),
        ("Your appointment", "is confirmed for tomorrow morning"),
        ("We have sent", "a confirmation message to your phone"),
        ("Sorry about", "the trouble with your last order"),
        ("Your payment", "went through without any problem"),
        ("I will transfer you", "to a specialist now"),
        ("That plan", "includes unlimited calls and data"),
        ("You can also", "do this from the mobile app"),
        ("Have a wonderful day", "and take care"),
        ("Our team", "will call you back within an hour"),
        ("Your ticket number", "is in the message we just sent"),
        ("The store", "opens at nine in the morning"),
    ]
]


async def run_session(url: str, n: int, rng: random.Random, log: list[dict]) -> None:
    # Room for about nine clips: a whole session's, but not the whole hot set, so the
    # tail of the hot set reaches memory through the prefix prefetch.
    memory = MemoryStore(warm_start_bytes=96 << 10)
    sink = LogSink(log=None)
    cache = TTSCache(scope(), TieredStore(memory, RedisStore(url), deadline_ms=20), metrics=sink)
    await cache.start()
    warm_audio = {e.key for e in memory._warm.values()}
    weights = [1 / (k + 1) for k in range(len(HOT))]
    turn = [OPENER] + [
        rng.choices(HOT, weights)[0]
        if rng.random() < 0.6
        else f"Session {n} item {i} has its own unique wording."
        for i in range(rng.randint(2, 5))
    ]
    for text in turn:
        parked, prefix = None, ""
        for word in text.split(" "):
            prefix += word + " "
            if parked is None:
                parked = cache.prefetch(prefix)
            await asyncio.sleep(0.002)  # the LLM is still streaming
        key = build_key(cache.scope, canonicalise(text, cache.scope.language))
        warm_texts = set(memory.warm_texts.get(scope_hash(cache.scope), {}).values())
        hit = (await cache.probe(text)).hit
        log.append(
            {
                "session": n,
                "text": text,
                "hit": hit is not None,
                "tier": hit.meta["tier"] if hit else None,
                "warm_audio": key in warm_audio,
                "warm_text": key in warm_texts,
                "prefetched": parked is not None and parked == key,
            }
        )
        if hit is None:
            await cache.capture(text, tone_pcm(text), SR)
    await cache.close()


@pytest.mark.slow
async def test_serverless_simulation(url) -> None:
    rng = random.Random(5)
    log: list[dict] = []
    for n in range(200):
        await run_session(url, n, rng, log)
    first_seen: dict[str, int] = {}
    later = []
    for row in log:
        if row["text"] in first_seen and first_seen[row["text"]] < row["session"]:
            later.append(row)
        first_seen.setdefault(row["text"], row["session"])
    # Captured in session N, hit in any later session, through Redis.
    assert later and all(r["hit"] for r in later), [r for r in later if not r["hit"]][:3]
    # Warm start serves the opener from memory in every session after the first.
    openers = [r for r in log if r["text"] == OPENER and r["session"] > 0]
    assert len(openers) == 199 and all(
        r["hit"] and r["warm_audio"] and r["tier"] == "memory" for r in openers
    )
    # Hot-set sentences whose clip was not in the warm audio set: the prefix
    # prefetch fetched them while they streamed, so the lookup found them in memory.
    beyond = [r for r in later if r["warm_text"] and not r["warm_audio"]]
    covered = [r for r in beyond if r["prefetched"] and r["tier"] == "memory"]
    warm = [r for r in later if r["warm_audio"]]
    print(
        f"later hits {len(later)}, warm-audio {len(warm)} ({sum(r['prefetched'] for r in warm)} prefetched), "
        f"hot texts beyond it {len(beyond)}, prefetched {len(covered)}"
    )
    assert beyond and len(covered) / len(beyond) > 0.5
    assert sum(r["prefetched"] for r in warm) / len(warm) > 0.5
