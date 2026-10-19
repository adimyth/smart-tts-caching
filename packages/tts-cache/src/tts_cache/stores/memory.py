"""Process memory: the warm set loaded at start plus this session's clips, as PCM (4.2, 4.5)."""

import bisect
import re
import time
from collections.abc import Iterable, Mapping

from tts_cache.key import split_key
from tts_cache.scope import Scope, scope_base_hash, scope_hash
from tts_cache.store import Entry, TouchMeta, decayed


class FrequencySketch:
    """A count-min sketch with 4-bit counters and periodic halving, as W-TinyLFU uses."""

    def __init__(self, width: int = 4096, depth: int = 4) -> None:
        self._width = width
        self._rows = [[0] * width for _ in range(depth)]
        self._additions = 0
        self._reset_at = 10 * width

    def _slots(self, key: str) -> Iterable[tuple[list[int], int]]:
        for i, row in enumerate(self._rows):
            yield row, hash((i, key)) % self._width

    def add(self, key: str) -> None:
        for row, j in self._slots(key):
            if row[j] < 15:
                row[j] += 1
        self._additions += 1
        if self._additions >= self._reset_at:
            for row in self._rows:
                for j in range(self._width):
                    row[j] >>= 1
            self._additions //= 2

    def estimate(self, key: str) -> int:
        return min(row[j] for row, j in self._slots(key))


class MemoryStore:
    """Decoded PCM under two byte budgets.

    The warm set (loaded by :meth:`load_warm`, at most ``warm_start_bytes`` of
    encoded audio) is pinned for the life of the process and decodes on first
    use. Everything else sits in the session set behind ``session_bytes`` of
    PCM, admitted and evicted by W-TinyLFU size-aware scoring: estimated
    frequency times characters saved, divided by bytes.
    """

    def __init__(self, warm_start_bytes: int = 4 << 20, session_bytes: int = 64 << 20) -> None:
        self.warm_start_bytes = warm_start_bytes
        self.session_bytes = session_bytes
        self._warm: dict[str, Entry] = {}
        self._session: dict[str, Entry] = {}
        self._session_used = 0
        self._sketch = FrequencySketch()
        self._generations: dict[str, int] = {}
        # scope hash -> canonical text -> key, for the prefix prefetch. It covers
        # the hot set's texts, which can reach beyond the clips held in memory.
        self.warm_texts: dict[str, dict[str, str]] = {}
        self._sorted: dict[str, list[str]] = {}
        self.evictions = 0
        self.rejections = 0

    def __len__(self) -> int:
        return len(self._warm) + len(self._session)

    @property
    def session_used(self) -> int:
        return self._session_used

    def _find(self, key: str) -> Entry | None:
        return self._warm.get(key) or self._session.get(key)

    def peek(self, key: str) -> Entry | None:
        """The entry without counting an access or starting a decode."""
        return self._find(key)

    async def get(self, key: str) -> Entry | None:
        self._sketch.add(key)
        entry = self._find(key)
        if entry is None:
            return None
        if entry.expired():
            self._remove(key)
            return None
        entry.decoded()
        entry.tier = "memory"
        return entry

    def _score(self, entry: Entry) -> float:
        return self._sketch.estimate(entry.key) * max(1, len(entry.canonical_text)) / max(1, entry.pcm_nbytes)

    async def put(self, key: str, entry: Entry) -> None:
        if key in self._warm:
            self._warm[key] = entry
            return
        size = entry.pcm_nbytes
        old = self._session.pop(key, None)
        if old is not None:
            self._session_used -= old.pcm_nbytes
        if size > self.session_bytes:
            self.rejections += 1
            return
        if self._session_used + size > self.session_bytes:
            candidate = self._score(entry)
            victims = sorted(self._session.values(), key=self._score)
            freed, chosen = 0, []
            for v in victims:
                if self._session_used - freed + size <= self.session_bytes:
                    break
                if self._score(v) > candidate:
                    self.rejections += 1
                    return
                chosen.append(v)
                freed += v.pcm_nbytes
            for v in chosen:
                self._remove(v.key)
                self.evictions += 1
        entry.decoded()
        self._session[key] = entry
        self._session_used += size

    def load_warm(self, scope: Scope, entries: Iterable[Entry]) -> int:
        """Pin ``entries`` as the warm set, within ``warm_start_bytes``; returns encoded bytes loaded."""
        texts = self.warm_texts.setdefault(scope_hash(scope), {})
        used = 0
        for e in entries:
            if used + e.nbytes > self.warm_start_bytes:
                break
            used += e.nbytes
            old = self._session.pop(e.key, None)
            if old is not None:
                self._session_used -= old.pcm_nbytes
            self._warm[e.key] = e
            texts[e.canonical_text] = e.key
        self._sorted.pop(scope_hash(scope), None)
        return used

    def add_warm_texts(self, scope: Scope, pairs: Iterable[tuple[str, str]]) -> int:
        """Remember ``(key, canonical_text)`` pairs of the hot set for the prefix prefetch."""
        sh = scope_hash(scope)
        texts = self.warm_texts.setdefault(sh, {})
        n = len(texts)
        for key, canonical in pairs:
            texts.setdefault(canonical, key)
        self._sorted.pop(sh, None)
        return len(texts) - n

    def prefix_candidates(self, scope: Scope, prefix: str, limit: int = 2) -> list[str]:
        """Keys of up to ``limit`` hot-set texts that start with ``prefix``."""
        sh = scope_hash(scope)
        texts = self.warm_texts.get(sh)
        if not texts:
            return []
        ordered = self._sorted.get(sh)
        if ordered is None:
            ordered = self._sorted[sh] = sorted(texts)
        out = []
        i = bisect.bisect_left(ordered, prefix)
        while i < len(ordered) and len(out) < limit and ordered[i].startswith(prefix):
            out.append(texts[ordered[i]])
            i += 1
        return out

    def _remove(self, key: str) -> None:
        if (e := self._session.pop(key, None)) is not None:
            self._session_used -= e.pcm_nbytes
        if (e := self._warm.pop(key, None)) is not None:
            for sh, texts in self.warm_texts.items():
                if texts.get(e.canonical_text) == key:
                    del texts[e.canonical_text]
                    self._sorted.pop(sh, None)

    async def touch(self, key: str, meta: TouchMeta) -> None:
        entry = self._find(key)
        if entry is None:
            return
        entry.hot_score = decayed(entry.hot_score, entry.last_hit_at, meta.hit_at) + meta.hits
        entry.last_hit_at = meta.hit_at
        entry.hit_count += meta.hits
        entry.expires_at = meta.expires_at

    async def delete(self, key: str) -> None:
        self._remove(key)

    async def purge(self, *, tags: Mapping[str, str] | None = None, match: str | None = None) -> int:
        pattern = re.compile(re.escape(match), re.IGNORECASE) if match else None
        doomed = [
            k
            for k, e in [*self._warm.items(), *self._session.items()]
            if (tags is None or all(e.tags.get(t) == v for t, v in tags.items()))
            and (pattern is None or pattern.search(e.text))
        ]
        for k in doomed:
            self._remove(k)
        return len(doomed)

    async def hot(self, scope: Scope, limit_bytes: int) -> list[Entry]:
        sh = scope_hash(scope)
        now = time.time()
        candidates = [
            e
            for e in [*self._warm.values(), *self._session.values()]
            if split_key(e.key)[0] == sh and not e.expired(now)
        ]
        candidates.sort(key=lambda e: decayed(e.hot_score, e.last_hit_at, now), reverse=True)
        out, used = [], 0
        for e in candidates:
            if used + e.nbytes > limit_bytes:
                break
            out.append(e)
            used += e.nbytes
        return out

    async def generation(self, scope: Scope) -> int:
        return self._generations.get(scope_base_hash(scope), 0)

    async def bump_generation(self, scope: Scope) -> int:
        base = scope_base_hash(scope)
        self._generations[base] = self._generations.get(base, 0) + 1
        return self._generations[base]

    async def close(self) -> None:
        pass
