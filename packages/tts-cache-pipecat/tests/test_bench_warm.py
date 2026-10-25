import pytest
from fake_tts import FakeTTSService

from tts_cache import MemoryStore, TTSCache
from tts_cache.corpus import generate
from tts_cache.warm import warm
from tts_cache_pipecat import scope_from_service
from tts_cache_pipecat.bench import run_pipecat
from tts_cache_pipecat.warm import rest_provider


@pytest.mark.slow  # a wall-clock budget: a local check, deselected in CI
async def test_pipecat_host_bench(tmp_path) -> None:
    corpus = generate(16, 0.5, seed=2)
    off = await run_pipecat(corpus, 0.5, cache_on=False, ttfb_median_ms=5, ttfb_sigma=0.2, workdir=tmp_path)
    on = await run_pipecat(corpus, 0.5, cache_on=True, ttfb_median_ms=5, ttfb_sigma=0.2, workdir=tmp_path)
    assert off["hit_rate"] == 0 and off["billed_chars"] == sum(len(s) for s in corpus.sentences)
    assert on["hit_rate"] > 0.2 and on["billed_chars"] < off["billed_chars"]
    assert 0 < on["read_side_p50_ms"] < 5


async def test_rest_provider_warms_the_store() -> None:
    tts = FakeTTSService()
    scope = scope_from_service(tts, language="en-US")
    cache = TTSCache(scope, MemoryStore())
    texts = ["Welcome back to Acme.", "Your order is on its way."]
    provider = rest_provider(scope, factory=lambda sc, session: FakeTTSService())
    counts = await warm(cache, texts, provider)
    assert counts == {"stored": 2, "present": 0, "failed": 0}
    assert all([await cache.lookup(t) is not None for t in texts])
    assert (await warm(cache, texts, provider))["present"] == 2
    await cache.close()
