"""The warmer: synthesise a hot set once and store it under the runtime's keys (SOLUTION.md 11.8).

``warm`` takes the sentences a replay reported as the hot set and, for each
one the store does not already hold, calls the provider once and hands the
audio to ``cache.capture``, so the clip passes the same ingest checks and is
stored under the same key a live capture would use. The provider is any
``async (text) -> Audio`` function; ``tts_cache_pipecat.warm.rest_provider``
builds one from a scope for the providers the adapter supports.
"""

import importlib
from collections.abc import Callable, Iterable
from typing import Any

from tts_cache.cache import Audio, TTSCache


async def warm(cache: TTSCache, texts: Iterable[str], provider_fn: Callable[[str], Any]) -> dict[str, int]:
    counts = {"stored": 0, "present": 0, "failed": 0}
    for text in texts:
        found = await cache.probe(text)
        if found.hit is not None:
            counts["present"] += 1
            continue
        try:
            result = await provider_fn(text)
        except Exception:
            counts["failed"] += 1
            continue
        audio = result if isinstance(result, Audio) else Audio(result[0], result[1])
        await cache.capture(text, audio.pcm, audio.sample_rate, audio.channels, key=found.key)
        counts["stored"] += 1
    await cache.drain(30.0)
    return counts


def load_factory(spec: str) -> Callable[..., Any]:
    """``module:attribute`` to the provider factory it names."""
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise ValueError(f"provider must be module:attribute, got {spec!r}")
    return getattr(importlib.import_module(module), attr)
