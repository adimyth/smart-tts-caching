"""The store contract every CacheStore passes (shared by the M1 and M3 suites)."""

import time
from collections.abc import Awaitable, Callable

from helpers import SR, scope, tone_pcm

from tts_cache import codec
from tts_cache.canonical import canonicalise
from tts_cache.key import build_key
from tts_cache.scope import Scope
from tts_cache.store import CacheStore, Entry, TouchMeta


def make_entry(text: str, sc: Scope | None = None, **over: object) -> Entry:
    sc = sc or scope()
    canonical = canonicalise(text, sc.language)
    pcm = tone_pcm(text)
    fields: dict[str, object] = {
        "key": build_key(sc, canonical),
        "audio": codec.encode(pcm, SR, 1, "opus"),
        "codec": "opus",
        "sample_rate": SR,
        "channels": 1,
        "duration_ms": round(len(pcm) / (SR * 2) * 1000),
        "text": text,
        "canonical_text": canonical,
        "language": sc.language,
        "tags": {"tenant": "acme"},
        "expires_at": time.time() + 3600,
    }
    fields.update(over)
    return Entry(**fields)  # type: ignore[arg-type]


StoreFactory = Callable[[], Awaitable[CacheStore]]


async def check_get_missing(store: CacheStore) -> None:
    assert await store.get(build_key(scope(), "nothing here")) is None


async def check_put_get(store: CacheStore) -> None:
    e = make_entry("Hello, and thank you for calling.")
    await store.put(e.key, e)
    got = await store.get(e.key)
    assert got is not None
    assert got.audio == e.audio and got.codec == "opus" and got.sample_rate == SR and got.channels == 1
    assert got.text == e.text and got.canonical_text == e.canonical_text and got.language == "en-US"
    assert dict(got.tags) == {"tenant": "acme"} and got.duration_ms == e.duration_ms
    assert got.key == e.key and got.tier
    assert got.decoded().result() == codec.decode(e.audio, "opus", SR, 1)


async def check_reingest_replaces(store: CacheStore) -> None:
    e = make_entry("Replace me please.")
    await store.put(e.key, e)
    e2 = make_entry(
        "Replace me please.", audio=codec.encode(tone_pcm("other"), SR, 1, "opus"), ingest_version=2
    )
    await store.put(e2.key, e2)
    got = await store.get(e.key)
    assert got is not None and got.audio == e2.audio and got.ingest_version == 2


async def check_touch(store: CacheStore) -> None:
    e = make_entry("Touch this entry.")
    await store.put(e.key, e)
    now = time.time()
    await store.touch(e.key, TouchMeta(hit_at=now, expires_at=now + 7200))
    await store.touch(e.key, TouchMeta(hit_at=now + 1, expires_at=now + 9000))
    got = await store.get(e.key)
    assert got is not None and got.hit_count == 2
    assert got.last_hit_at is not None and abs(got.last_hit_at - (now + 1)) < 1e-3
    assert got.expires_at is not None and abs(got.expires_at - (now + 9000)) < 1e-3
    assert got.hot_score > 1.5


async def check_expired_invisible(store: CacheStore) -> None:
    e = make_entry("Already expired.", expires_at=time.time() - 1)
    await store.put(e.key, e)
    assert await store.get(e.key) is None


async def check_delete(store: CacheStore) -> None:
    e = make_entry("Delete me.")
    await store.put(e.key, e)
    await store.delete(e.key)
    assert await store.get(e.key) is None


async def check_purge_tags(store: CacheStore) -> None:
    a = make_entry("Tenant A sentence.", tags={"tenant": "a", "flow": "x"})
    b = make_entry("Tenant B sentence.", tags={"tenant": "b", "flow": "x"})
    for e in (a, b):
        await store.put(e.key, e)
    assert await store.purge(tags={"tenant": "a"}) >= 1
    assert await store.get(a.key) is None
    assert await store.get(b.key) is not None


async def check_purge_match(store: CacheStore) -> None:
    a = make_entry("Your refund is on its way.")
    b = make_entry("Your order is on its way.")
    for e in (a, b):
        await store.put(e.key, e)
    assert await store.purge(match="refund") >= 1
    assert await store.get(a.key) is None
    assert await store.get(b.key) is not None


async def check_hot(store: CacheStore) -> None:
    sc, other = scope(), scope(voice="someone-else")
    now = time.time()
    entries = [make_entry(f"Hot sentence number {i}.", sc) for i in range(5)]
    for i, e in enumerate(entries):
        await store.put(e.key, e)
        for _ in range(i):
            await store.touch(e.key, TouchMeta(hit_at=now, expires_at=now + 3600))
    elsewhere = make_entry("Other voice sentence.", other)
    await store.put(elsewhere.key, elsewhere)
    gone = make_entry("Expired hot sentence.", sc, expires_at=now - 1)
    await store.put(gone.key, gone)
    hot = await store.hot(sc, 10 << 20)
    assert [e.key for e in hot] == [e.key for e in reversed(entries)]
    budget = entries[4].nbytes + entries[3].nbytes
    assert [e.key for e in await store.hot(sc, budget)] == [entries[4].key, entries[3].key]


async def check_generation(store: CacheStore) -> None:
    sc = scope(voice="gen-voice")
    assert await store.generation(sc) == 0
    assert await store.bump_generation(sc) == 1
    assert await store.bump_generation(sc.with_generation(1)) == 2
    assert await store.generation(sc) == 2
    assert await store.generation(scope(voice="gen-other")) == 0


CHECKS = [
    check_get_missing,
    check_put_get,
    check_reingest_replaces,
    check_touch,
    check_expired_invisible,
    check_delete,
    check_purge_tags,
    check_purge_match,
    check_hot,
    check_generation,
]
