import pytest
from redis_guard import test_redis_url
from store_contract import CHECKS

from tts_cache.store import CacheStore
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.sqlite import SQLiteStore
from tts_cache.stores.tiered import TieredStore


async def memory(tmp_path) -> CacheStore:
    return MemoryStore()


async def sqlite(tmp_path) -> CacheStore:
    s = SQLiteStore(tmp_path / "c.db")
    await s.start()
    return s


async def tiered(tmp_path) -> CacheStore:
    return TieredStore(MemoryStore(), await sqlite(tmp_path), deadline_ms=200)


async def redis(tmp_path) -> CacheStore:
    from tts_cache.stores.redis import RedisStore

    s = RedisStore(test_redis_url())
    await s.client.flushdb()  # the dedicated test instance, never the shared one
    return s


FACTORIES = {"memory": memory, "sqlite": sqlite, "tiered": tiered}
REDIS = {"redis": pytest.param(redis, marks=pytest.mark.redis)}


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("factory", [*FACTORIES.values(), *REDIS.values()], ids=[*FACTORIES, *REDIS])
async def test_store_contract(factory, check, tmp_path) -> None:
    store = await factory(tmp_path)
    try:
        assert isinstance(store, CacheStore)
        await check(store)
    finally:
        await store.close()
