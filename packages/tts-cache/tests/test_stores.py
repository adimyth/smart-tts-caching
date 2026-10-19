import asyncio
import time

from helpers import scope
from store_contract import make_entry

from tts_cache.store import Entry, TouchMeta
from tts_cache.stores.memory import FrequencySketch, MemoryStore
from tts_cache.stores.sqlite import SQLiteStore
from tts_cache.stores.tiered import TieredStore

# MemoryStore


def test_frequency_sketch_counts_and_ages() -> None:
    s = FrequencySketch(width=64)
    for _ in range(10):
        s.add("a")
    s.add("b")
    assert s.estimate("a") >= 10 and s.estimate("b") >= 1
    # The sample period is ten times the width; at its end every counter halves.
    for _ in range(10 * 64 - 11):
        s.add("z")
    assert s.estimate("a") == 5


async def test_memory_session_budget_keeps_frequent_entries() -> None:
    entries = [make_entry(f"Sentence number {i} for the budget.") for i in range(6)]
    one = entries[0].pcm_nbytes
    m = MemoryStore(session_bytes=one * 3 + 10)
    for e in entries[:3]:
        await m.put(e.key, e)
    for _ in range(5):
        await m.get(entries[0].key)
        await m.get(entries[1].key)
    await m.put(entries[3].key, entries[3])
    assert m.session_used <= m.session_bytes
    assert await m.get(entries[0].key) is not None and await m.get(entries[1].key) is not None
    assert await m.get(entries[2].key) is None and await m.get(entries[3].key) is not None
    assert m.evictions == 1


async def test_memory_admission_rejects_a_cold_candidate_over_hot_victims() -> None:
    entries = [make_entry(f"Admission sentence {i} here.") for i in range(3)]
    m = MemoryStore(session_bytes=entries[0].pcm_nbytes * 2 + 10)
    for e in entries[:2]:
        await m.put(e.key, e)
        for _ in range(8):
            await m.get(e.key)
    await m.put(entries[2].key, entries[2])
    assert await m.get(entries[2].key) is None
    assert m.rejections == 1


async def test_memory_warm_set_is_pinned_and_decodes_on_first_use() -> None:
    sc = scope()
    entries = [make_entry(f"Warm sentence {i}.", sc) for i in range(3)]
    m = MemoryStore(warm_start_bytes=entries[0].nbytes * 2 + 1, session_bytes=1)
    loaded = m.load_warm(sc, entries)
    assert loaded == entries[0].nbytes + entries[1].nbytes
    assert all(m.peek(e.key) is not None and m.peek(e.key).pcm is None for e in entries[:2])  # type: ignore[union-attr]
    assert m.peek(entries[2].key) is None
    got = await m.get(entries[0].key)
    assert got is not None and got.pcm is not None and got.tier == "memory"
    assert set(next(iter(m.warm_texts.values()))) == {entries[0].canonical_text, entries[1].canonical_text}
    # A session put larger than the session budget does not evict the warm set.
    extra = make_entry("Session sentence.", sc)
    await m.put(extra.key, extra)
    assert m.peek(entries[1].key) is not None


# SQLiteStore


async def test_sqlite_sweep_expires_and_trims(tmp_path) -> None:
    s = SQLiteStore(tmp_path / "c.db", max_bytes=0, sweep_interval_s=0)
    now = time.time()
    old = make_entry("Expired entry.", expires_at=now - 10)
    keep = [make_entry(f"Keep entry {i}.") for i in range(4)]
    for e in [old, *keep]:
        await s.put(e.key, e)
    for _ in range(3):
        await s.touch(keep[0].key, TouchMeta(hit_at=now, expires_at=now + 3600))
    s.max_bytes = sum(e.nbytes for e in keep[:2])
    result = await s.sweep()
    assert result["expired"] == 1 and result["evicted"] == 2
    stats = await s.stats()
    assert stats["entries"] == 2 and stats["bytes"] <= s.max_bytes
    assert await s.get(keep[0].key) is not None
    await s.close()


async def test_sqlite_sweep_runs_on_a_timer(tmp_path) -> None:
    s = SQLiteStore(tmp_path / "c.db", sweep_interval_s=0.05)
    await s.start()
    e = make_entry("Short lived.", expires_at=time.time() + 0.05)
    await s.put(e.key, e)
    await asyncio.sleep(0.3)
    assert (await s.stats())["entries"] == 0
    await s.close()


async def test_sqlite_inspect_and_purge_match_use_fts(tmp_path) -> None:
    s = SQLiteStore(tmp_path / "c.db")
    for t in ["Your refund is processed.", "Refunds take five days.", "Your order shipped."]:
        e = make_entry(t)
        await s.put(e.key, e)
    rows = await s.inspect("refund")
    assert [r["text"] for r in rows] == ["Your refund is processed."]
    assert len(await s.inspect()) == 3
    assert await s.purge(match='order" OR "refund') == 0  # user text is a phrase, never FTS syntax
    assert await s.purge(match="your order") == 1
    await s.close()


async def test_sqlite_survives_reopen(tmp_path) -> None:
    s = SQLiteStore(tmp_path / "c.db")
    e = make_entry("Persisted across processes.")
    await s.put(e.key, e)
    await s.close()
    s2 = SQLiteStore(tmp_path / "c.db")
    got = await s2.get(e.key)
    assert got is not None and got.audio == e.audio
    await s2.close()


# TieredStore


class CountingStore(MemoryStore):
    def __init__(self, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay
        self.gets = 0
        self.puts = 0

    async def get(self, key: str) -> Entry | None:
        self.gets += 1
        await asyncio.sleep(self.delay)
        return await super().get(key)

    async def put(self, key: str, entry: Entry) -> None:
        self.puts += 1
        await asyncio.sleep(self.delay)
        await super().put(key, entry)


async def test_tiered_memory_hit_never_touches_shared() -> None:
    shared = CountingStore()
    t = TieredStore(MemoryStore(), shared, deadline_ms=50)
    e = make_entry("In memory already.")
    await t.memory.put(e.key, e)
    got = await t.get(e.key)
    assert got is not None and got.tier == "memory" and shared.gets == 0


async def test_tiered_shared_hit_lands_in_memory_decoded() -> None:
    shared = CountingStore()
    t = TieredStore(MemoryStore(), shared, deadline_ms=50)
    e = make_entry("Only in the shared tier.")
    await shared.put(e.key, e)
    got = await t.get(e.key)
    assert got is not None and got.tier == "shared" and got.pcm is not None
    again = await t.get(e.key)
    assert again is not None and again.tier == "memory" and shared.gets == 1


async def test_tiered_deadline_is_a_miss_and_the_late_read_still_lands() -> None:
    shared = CountingStore(delay=0.1)
    t = TieredStore(MemoryStore(), shared, deadline_ms=10)
    e = make_entry("Slow shared tier.")
    await MemoryStore.put(shared, e.key, e)
    t0 = time.perf_counter()
    assert await t.get(e.key) is None
    assert time.perf_counter() - t0 < 0.05
    assert t.counters["deadline"] == 1
    await asyncio.sleep(0.15)
    assert t.memory.peek(e.key) is not None


async def test_tiered_coalesces_concurrent_reads_and_writes() -> None:
    shared = CountingStore(delay=0.005)
    t = TieredStore(MemoryStore(), shared, deadline_ms=200)
    e = make_entry("Everyone says this at once.")
    await MemoryStore.put(shared, e.key, e)
    results = await asyncio.gather(*(t.get(e.key) for _ in range(50)))
    assert all(r is not None for r in results) and shared.gets == 1
    f = make_entry("Everyone stores this at once.")
    await asyncio.gather(*(t.put(f.key, f) for _ in range(50)))
    assert shared.puts == 1 and t.recently_stored(f.key)


async def test_tiered_shared_error_is_a_miss() -> None:
    class Broken(MemoryStore):
        async def get(self, key: str) -> Entry | None:
            raise ConnectionError("down")

    t = TieredStore(MemoryStore(), Broken(), deadline_ms=50)
    assert await t.get(make_entry("x y z.").key) is None
    assert t.counters["shared_error"] == 1
