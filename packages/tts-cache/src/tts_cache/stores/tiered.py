"""Process memory in front of a shared store, with a hard deadline on the shared tier (4.2c)."""

import asyncio
import time
from collections import Counter
from collections.abc import Mapping

from tts_cache.metrics import FailOpen, MetricsSink, NullSink
from tts_cache.scope import Scope
from tts_cache.store import CacheStore, Entry, TouchMeta
from tts_cache.stores.memory import MemoryStore


class TieredStore:
    """Memory first, then ``shared`` under ``deadline_ms``.

    A shared hit decodes (streaming, 4.2b) and stays in memory for the rest of
    the process. Concurrent misses of one key share one shared-tier read, and
    concurrent or repeated puts of one key within ``coalesce_s`` write the
    shared tier once, so a sentence many sessions say at once is stored once.
    A read past the deadline returns ``None`` and counts; it keeps running in
    the background and lands in memory if it completes.
    """

    def __init__(
        self,
        memory: MemoryStore,
        shared: CacheStore,
        deadline_ms: float = 20.0,
        *,
        coalesce_s: float = 30.0,
        metrics: MetricsSink | None = None,
    ) -> None:
        self.memory = memory
        self.shared = shared
        self.deadline_s = deadline_ms / 1000.0
        self.coalesce_s = coalesce_s
        self.metrics: MetricsSink = metrics or NullSink()
        self._fail_open = FailOpen(self.metrics)
        self.counters: Counter[str] = Counter()
        self._reads: dict[str, asyncio.Task[Entry | None]] = {}
        self._writes: dict[str, float] = {}
        self._background: set[asyncio.Task[None]] = set()

    def bind_metrics(self, metrics: MetricsSink) -> None:
        """Report through the cache's sink unless one was given."""
        if isinstance(self.metrics, NullSink):
            self.metrics = metrics
            self._fail_open = FailOpen(metrics)

    async def _fetch(self, key: str) -> Entry | None:
        entry = await self.shared.get(key)
        if entry is None or entry.expired():
            return None
        await self.memory.put(key, entry)
        entry.decoded()
        entry.tier = "shared"
        return entry

    async def get(self, key: str) -> Entry | None:
        entry = await self.memory.get(key)
        if entry is not None:
            self.counters["memory_hit"] += 1
            return entry
        task = self._reads.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(key))
            self._reads[key] = task
            task.add_done_callback(lambda t, k=key: self._read_done(k, t))
        else:
            self.counters["read_coalesced"] += 1
        try:
            async with asyncio.timeout(self.deadline_s):
                entry = await asyncio.shield(task)
        except TimeoutError:
            self.counters["deadline"] += 1
            self.metrics.event("deadline", key=key, deadline_ms=round(self.deadline_s * 1000, 1))
            return None
        except Exception as e:  # a broken shared tier is a miss
            self.counters["shared_error"] += 1
            self._fail_open("tiered.get", e)
            return None
        self.counters["shared_hit" if entry is not None else "shared_miss"] += 1
        return entry

    async def prefetch(self, key: str) -> None:
        """Read ``key`` from the shared tier into memory without a deadline, sharing any read in flight."""
        if self.memory.peek(key) is not None:
            return
        task = self._reads.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(key))
            self._reads[key] = task
            task.add_done_callback(lambda t, k=key: self._read_done(k, t))
        await asyncio.shield(task)

    async def start(self) -> None:
        start = getattr(self.shared, "start", None)
        if start is not None:
            await start()

    def _read_done(self, key: str, task: asyncio.Task[Entry | None]) -> None:
        if self._reads.get(key) is task:
            del self._reads[key]
        if not task.cancelled():
            task.exception()  # retrieved, so a late failure is not reported as unhandled

    def recently_stored(self, key: str) -> bool:
        at = self._writes.get(key)
        return at is not None and (at == float("inf") or time.monotonic() - at < self.coalesce_s)

    async def put(self, key: str, entry: Entry) -> None:
        await self.memory.put(key, entry)
        if self.recently_stored(key):
            self.counters["write_coalesced"] += 1
            return
        self._writes[key] = float("inf")
        try:
            await self.shared.put(key, entry.without_pcm())
        except BaseException:
            self._writes.pop(key, None)
            raise
        self._writes[key] = time.monotonic()
        if len(self._writes) > 10_000:
            cutoff = time.monotonic() - self.coalesce_s
            self._writes = {k: t for k, t in self._writes.items() if t >= cutoff}

    async def touch(self, key: str, meta: TouchMeta) -> None:
        await self.memory.touch(key, meta)
        await self.shared.touch(key, meta)

    async def delete(self, key: str) -> None:
        await self.memory.delete(key)
        self._writes.pop(key, None)
        await self.shared.delete(key)

    async def purge(self, *, tags: Mapping[str, str] | None = None, match: str | None = None) -> int:
        await self.memory.purge(tags=tags, match=match)
        self._writes.clear()
        return await self.shared.purge(tags=tags, match=match)

    async def hot(self, scope: Scope, limit_bytes: int) -> list[Entry]:
        return await self.shared.hot(scope, limit_bytes)

    async def generation(self, scope: Scope) -> int:
        return await self.shared.generation(scope)

    async def bump_generation(self, scope: Scope) -> int:
        return await self.shared.bump_generation(scope)

    async def close(self) -> None:
        for t in list(self._reads.values()):
            t.cancel()
        await self.memory.close()
        await self.shared.close()
