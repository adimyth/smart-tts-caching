"""Redis (or Valkey, or any Redis-protocol server): the shared cache (SOLUTION.md 4.2, 4.2a, 4.2c).

Key families, per section 4.2c:

- ``tts:{scope}:{hash}``: the encoded clip;
- ``tts:meta:{scope}:{hash}``: a hash of the entry's metadata;
- ``tts:tag:{k}={v}``: a set of the audio keys carrying that tag;
- ``tts:gen:{scope}``: the generation counter;
- ``tts:hot:{language}:{scope}``: a zset of text hashes ordered by decayed hit count.

The hot zset uses forward decay: each use adds ``2 ** ((now - T0) / half_life)``
to the member's score, so ordering by score is ordering by hit count decayed
with that half-life, and no score ever needs rewriting. The same increment
accumulates in the metadata field ``hot_fwd``, from which ``Entry.hot_score``
(the decayed count now) is derived.

Expiry is Redis's own: the audio and metadata keys carry the entry's expiry
(``PEXPIREAT``), so probation and the sliding TTL need no sweep. Eviction under
memory pressure is the server's ``allkeys-lfu`` policy (4.5).
"""

import asyncio
import base64
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tts_cache.key import generation_key, hot_key, meta_key, split_key, tag_key
from tts_cache.scope import Scope, scope_hash
from tts_cache.store import HOT_HALF_LIFE_S, Entry, TouchMeta

try:
    import redis.asyncio as aioredis
except ImportError:  # the [redis] extra is not installed
    aioredis = None  # type: ignore[assignment]

# Forward-decay epoch: 2026-01-01T00:00:00Z.
T0 = 1767225600.0


def forward(now: float) -> float:
    return 2.0 ** ((now - T0) / HOT_HALF_LIFE_S)


_FIELDS = (
    "codec",
    "sample_rate",
    "channels",
    "duration_ms",
    "bytes",
    "text",
    "canonical_text",
    "language",
    "tags",
    "created_at",
    "last_hit_at",
    "hit_count",
    "expires_at",
    "verify_score",
    "ingest_version",
    "hot_fwd",
)


# Warm start in one round trip (4.2a): walk the hot zset, take clips until the
# byte budget is spent, and return their audio and metadata together with the
# canonical texts of the top entries (for the prefix prefetch). Members whose
# entry has expired are removed from the zset on the way.
WARM_SCRIPT = """
local scope = ARGV[1]
local budget = tonumber(ARGV[2])
local text_limit = tonumber(ARGV[4])
local members = redis.call('ZREVRANGE', KEYS[1], 0, tonumber(ARGV[3]) - 1)
local clips, texts, gone = {}, {}, {}
local used, full = 0, false
for i, m in ipairs(members) do
  local mk = 'tts:meta:' .. scope .. ':' .. m
  local size = redis.call('HGET', mk, 'bytes')
  if not size then
    table.insert(gone, m)
  else
    if #texts < 2 * text_limit then
      table.insert(texts, m)
      table.insert(texts, redis.call('HGET', mk, 'canonical_text'))
    end
    if not full then
      size = tonumber(size)
      if used + size > budget then
        full = true
      else
        local audio = redis.call('GET', 'tts:' .. scope .. ':' .. m)
        if audio then
          used = used + size
          table.insert(clips, m)
          table.insert(clips, audio)
          table.insert(clips, redis.call('HGETALL', mk))
        end
      end
    end
  end
end
for i = 1, #gone, 500 do
  redis.call('ZREM', KEYS[1], unpack(gone, i, math.min(i + 499, #gone)))
end
return {clips, texts}
"""


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    s = _s(v)
    return None if s == "" else float(s)


class RedisStore:
    """The shared store on ``redis.asyncio``; works against Valkey.

    ``socket_timeout`` bounds every command so a hung server cannot hold a
    background task forever; lookups are bounded more tightly by the tiered
    store's deadline.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        socket_timeout: float = 1.0,
        hot_scan: int = 2000,
        client: Any = None,
    ) -> None:
        if client is None:
            if aioredis is None:
                raise ImportError("RedisStore needs the [redis] extra: pip install 'smart-tts-cache[redis]'")
            client = aioredis.from_url(
                url, socket_timeout=socket_timeout, socket_connect_timeout=socket_timeout
            )
        self.url = url
        self.client = client
        self.hot_scan = hot_scan
        # Text hash -> hot zset name, learned from gets and puts, so a touch
        # needs no extra round trip to find the entry's language.
        self._hot_names: dict[str, str] = {}

    # Encoding.

    @staticmethod
    def _meta(entry: Entry, hot_fwd: float) -> dict[str, str]:
        return {
            "codec": entry.codec,
            "sample_rate": str(entry.sample_rate),
            "channels": str(entry.channels),
            "duration_ms": str(entry.duration_ms),
            "bytes": str(entry.nbytes),
            "text": entry.text,
            "canonical_text": entry.canonical_text,
            "language": entry.language,
            "tags": json.dumps(dict(entry.tags), sort_keys=True),
            "created_at": repr(entry.created_at),
            "last_hit_at": "" if entry.last_hit_at is None else repr(entry.last_hit_at),
            "hit_count": str(entry.hit_count),
            "expires_at": "" if entry.expires_at is None else repr(entry.expires_at),
            "verify_score": "" if entry.verify_score is None else repr(entry.verify_score),
            "ingest_version": str(entry.ingest_version),
            "hot_fwd": repr(hot_fwd),
        }

    @staticmethod
    def _entry(key: str, audio: bytes, meta: Mapping[Any, Any], now: float) -> Entry:
        m = {_s(k): v for k, v in meta.items()}
        hot_fwd = _opt_float(m.get("hot_fwd")) or 0.0
        return Entry(
            key=key,
            audio=bytes(audio),
            codec=_s(m["codec"]),  # type: ignore[arg-type]
            sample_rate=int(_s(m["sample_rate"])),
            channels=int(_s(m["channels"])),
            duration_ms=int(_s(m["duration_ms"])),
            text=_s(m["text"]),
            canonical_text=_s(m["canonical_text"]),
            language=_s(m["language"]),
            tags=json.loads(_s(m.get("tags", "{}"))),
            created_at=float(_s(m["created_at"])),
            last_hit_at=_opt_float(m.get("last_hit_at")),
            hit_count=int(_s(m.get("hit_count", "0"))),
            expires_at=_opt_float(m.get("expires_at")),
            verify_score=_opt_float(m.get("verify_score")),
            ingest_version=int(_s(m.get("ingest_version", "1"))),
            hot_score=hot_fwd / forward(now),
        )

    def _hot_name(self, key: str, language: str) -> str:
        scope, _ = split_key(key)
        lang = language.replace("_", "-").split("-")[0].lower()
        return f"tts:hot:{lang}:{scope}"

    # CacheStore.

    async def get(self, key: str) -> Entry | None:
        pipe = self.client.pipeline(transaction=False)
        pipe.get(key)
        pipe.hgetall(meta_key(key))
        audio, meta = await pipe.execute()
        if audio is None or not meta:
            return None
        now = time.time()
        entry = self._entry(key, audio, meta, now)
        if entry.expired(now):
            return None
        self._hot_names[split_key(key)[1]] = self._hot_name(key, entry.language)
        entry.tier = "redis"
        return entry

    async def put(self, key: str, entry: Entry) -> None:
        now = time.time()
        _, digest = split_key(key)
        hot = self._hot_name(key, entry.language)
        self._hot_names[digest] = hot
        inc = forward(now)
        old_tags = await self.client.hget(meta_key(key), "tags")
        pipe = self.client.pipeline(transaction=True)
        pipe.set(key, entry.audio)
        pipe.delete(meta_key(key))
        pipe.hset(meta_key(key), mapping=self._meta(entry, entry.hot_score * forward(now) + inc))
        if entry.expires_at is not None:
            ms = int(entry.expires_at * 1000)
            pipe.pexpireat(key, ms)
            pipe.pexpireat(meta_key(key), ms)
        for k, v in json.loads(_s(old_tags)).items() if old_tags else []:
            pipe.srem(tag_key(k, v), key)
        for k, v in entry.tags.items():
            pipe.sadd(tag_key(k, v), key)
        # A capture counts as one use, so a new clip enters the hot set at once.
        pipe.zincrby(hot, inc, digest)
        await pipe.execute()

    async def touch(self, key: str, meta: TouchMeta) -> None:
        _, digest = split_key(key)
        hot = self._hot_names.get(digest)
        if hot is None:
            language = await self.client.hget(meta_key(key), "language")
            if language is None:
                return
            hot = self._hot_name(key, _s(language))
        inc = forward(meta.hit_at) * meta.hits
        pipe = self.client.pipeline(transaction=False)
        pipe.exists(meta_key(key))
        pipe.hincrby(meta_key(key), "hit_count", meta.hits)
        pipe.hincrbyfloat(meta_key(key), "hot_fwd", inc)
        pipe.hset(
            meta_key(key),
            mapping={
                "last_hit_at": repr(meta.hit_at),
                "expires_at": "" if meta.expires_at is None else repr(meta.expires_at),
            },
        )
        if meta.expires_at is not None:
            ms = int(meta.expires_at * 1000)
            pipe.pexpireat(key, ms)
            pipe.pexpireat(meta_key(key), ms)
        pipe.zincrby(hot, inc, digest)
        existed, *_ = await pipe.execute()
        if not existed:
            # The entry expired or was purged in the meantime; do not resurrect a bare metadata hash.
            await self.client.delete(meta_key(key))
            await self.client.zrem(hot, digest)

    async def _delete_keys(self, keys: list[str]) -> int:
        if not keys:
            return 0
        pipe = self.client.pipeline(transaction=False)
        for key in keys:
            pipe.hmget(meta_key(key), ["language", "tags"])
        metas = await pipe.execute()
        pipe = self.client.pipeline(transaction=False)
        for key, (language, tags) in zip(keys, metas, strict=True):
            _, digest = split_key(key)
            pipe.delete(key, meta_key(key))
            if language is not None:
                pipe.zrem(self._hot_name(key, _s(language)), digest)
            for k, v in json.loads(_s(tags)).items() if tags else []:
                pipe.srem(tag_key(k, v), key)
            self._hot_names.pop(digest, None)
        await pipe.execute()
        return sum(1 for language, _ in metas if language is not None)

    async def delete(self, key: str) -> None:
        await self._delete_keys([key])

    async def _meta_keys(self) -> list[str]:
        return [_s(k) async for k in self.client.scan_iter(match="tts:meta:*", count=500)]

    async def purge(self, *, tags: Mapping[str, str] | None = None, match: str | None = None) -> int:
        if not tags and not match:
            raise ValueError("purge needs tags or match")
        if tags:
            members = await self.client.sinter([tag_key(k, v) for k, v in tags.items()])
            keys = sorted(_s(m) for m in members)
        else:
            keys = ["tts:" + k.removeprefix("tts:meta:") for k in await self._meta_keys()]
        if match:
            needle = match.lower()
            pipe = self.client.pipeline(transaction=False)
            for key in keys:
                pipe.hget(meta_key(key), "text")
            texts = await pipe.execute()
            keys = [k for k, t in zip(keys, texts, strict=True) if t is not None and needle in _s(t).lower()]
        return await self._delete_keys(keys)

    async def _top(self, scope: Scope, limit: int) -> list[str]:
        members = await self.client.zrevrange(hot_key(scope), 0, limit - 1)
        sh = scope_hash(scope)
        return [f"tts:{sh}:{_s(m)}" for m in members]

    async def hot(self, scope: Scope, limit_bytes: int) -> list[Entry]:
        """The hot set within ``limit_bytes`` of encoded audio, hottest first: three round trips."""
        keys = await self._top(scope, self.hot_scan)
        if not keys:
            return []
        pipe = self.client.pipeline(transaction=False)
        for key in keys:
            pipe.hget(meta_key(key), "bytes")
        sizes = await pipe.execute()
        gone = [split_key(k)[1] for k, size in zip(keys, sizes, strict=True) if size is None]
        if gone:
            # Members whose entry expired or was evicted: drop them from the zset.
            await self.client.zrem(hot_key(scope), *gone)
        chosen, used = [], 0
        for key, size in zip(keys, sizes, strict=True):
            if size is None:
                continue
            n = int(_s(size))
            if used + n > limit_bytes:
                break
            chosen.append(key)
            used += n
        if not chosen:
            return []
        pipe = self.client.pipeline(transaction=False)
        for key in chosen:
            pipe.get(key)
            pipe.hgetall(meta_key(key))
        values = await pipe.execute()
        now = time.time()
        out = []
        for i, key in enumerate(chosen):
            audio, meta = values[2 * i], values[2 * i + 1]
            if audio is None or not meta:
                continue
            entry = self._entry(key, audio, meta, now)
            if not entry.expired(now):
                out.append(entry)
        return out

    async def warm(
        self, scope: Scope, limit_bytes: int, text_limit: int
    ) -> tuple[list[Entry], list[tuple[str, str]]]:
        """The hot set within ``limit_bytes`` and the top ``text_limit`` texts, in one round trip."""
        sh = scope_hash(scope)
        clips, texts = await self.client.eval(
            WARM_SCRIPT, 1, hot_key(scope), sh, limit_bytes, max(self.hot_scan, text_limit), text_limit
        )
        now = time.time()
        entries = []
        for i in range(0, len(clips), 3):
            digest, audio, flat = _s(clips[i]), clips[i + 1], clips[i + 2]
            meta = {flat[j]: flat[j + 1] for j in range(0, len(flat), 2)}
            entry = self._entry(f"tts:{sh}:{digest}", audio, meta, now)
            if not entry.expired(now):
                entries.append(entry)
        pairs = [(f"tts:{sh}:{_s(texts[i])}", _s(texts[i + 1])) for i in range(0, len(texts), 2)]
        return entries, pairs

    async def hot_texts(self, scope: Scope, limit: int) -> list[tuple[str, str]]:
        """``(key, canonical_text)`` for the top ``limit`` hot entries, for the prefix prefetch."""
        keys = await self._top(scope, limit)
        if not keys:
            return []
        pipe = self.client.pipeline(transaction=False)
        for key in keys:
            pipe.hget(meta_key(key), "canonical_text")
        texts = await pipe.execute()
        return [(k, _s(t)) for k, t in zip(keys, texts, strict=True) if t is not None]

    async def generation(self, scope: Scope) -> int:
        value = await self.client.get(generation_key(scope))
        return int(_s(value)) if value is not None else 0

    async def bump_generation(self, scope: Scope) -> int:
        return int(await self.client.incr(generation_key(scope)))

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()

    # Rebuilds (4.2): a nightly export to object storage, and import into a new instance.

    async def export(self, path: str | Path) -> int:
        """Write every entry and generation counter to ``path`` as JSON lines; returns the entry count."""
        lines: list[str] = []
        now = time.time()
        for mk in await self._meta_keys():
            key = "tts:" + mk.removeprefix("tts:meta:")
            pipe = self.client.pipeline(transaction=False)
            pipe.get(key)
            pipe.hgetall(mk)
            audio, meta = await pipe.execute()
            if audio is None or not meta:
                continue
            e = self._entry(key, audio, meta, now)
            if e.expired(now):
                continue
            record = {k: v for k, v in e.__dict__.items() if k not in ("audio", "pcm", "tier")}
            record["audio"] = base64.b64encode(e.audio).decode()
            lines.append(json.dumps({"entry": record}, ensure_ascii=False))
        n = len(lines)
        async for gk in self.client.scan_iter(match="tts:gen:*", count=500):
            value = await self.client.get(gk)
            if value is not None:
                lines.append(json.dumps({"generation": _s(gk), "value": int(_s(value))}))
        text = "".join(line + "\n" for line in lines)
        await asyncio.to_thread(Path(path).write_text, text, encoding="utf-8")
        return n

    async def import_(self, path: str | Path) -> int:
        """Load an :meth:`export` file; returns the entry count."""
        n = 0
        text = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        for line in text.splitlines():
            record = json.loads(line)
            if "generation" in record:
                await self.client.set(record["generation"], record["value"])
                continue
            data = record["entry"]
            data["audio"] = base64.b64decode(data["audio"])
            entry = Entry(**data)
            await self.put(entry.key, entry)
            n += 1
        return n
