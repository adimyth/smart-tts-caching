"""The store protocol and the entry it holds (SOLUTION.md sections 4.2 and 4.2c)."""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from tts_cache.codec import Codec, PcmStream
from tts_cache.scope import Scope


@dataclass
class Entry:
    """One stored clip and its metadata. Audio is immutable; metadata is mutable (4.3)."""

    key: str
    audio: bytes
    codec: Codec
    sample_rate: int
    channels: int
    duration_ms: int
    text: str
    canonical_text: str
    language: str
    tags: Mapping[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_hit_at: float | None = None
    hit_count: int = 0
    expires_at: float | None = None
    verify_score: float | None = None
    ingest_version: int = 1
    hot_score: float = 0.0
    # Which tier served the entry ("memory", "shared" or a store name). Never persisted.
    tier: str = field(default="", compare=False)
    # Decoded PCM, present once the entry is in process memory or when ingest
    # already holds it. Never persisted.
    pcm: PcmStream | None = field(default=None, repr=False, compare=False)

    @property
    def nbytes(self) -> int:
        return len(self.audio)

    @property
    def pcm_nbytes(self) -> int:
        return self.sample_rate * self.duration_ms // 1000 * 2 * self.channels

    def expired(self, now: float | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (time.time() if now is None else now)

    def without_pcm(self) -> "Entry":
        return replace(self, pcm=None)

    def decoded(self) -> PcmStream:
        """The entry's PCM, starting a streaming decode if it has none yet."""
        if self.pcm is None:
            self.pcm = PcmStream.decoding(self.audio, self.codec, self.sample_rate, self.channels)
        return self.pcm


@dataclass(frozen=True)
class TouchMeta:
    """What a hit updates (4.3): last hit time, hit count, the sliding expiry, the hot score."""

    hit_at: float
    expires_at: float | None
    hits: int = 1


# Decayed hit count for the hot set: halves every week without hits.
HOT_HALF_LIFE_S = 7 * 24 * 3600.0


def decayed(score: float, last_at: float | None, now: float) -> float:
    if last_at is None or score <= 0:
        return score
    return score * 0.5 ** (max(0.0, now - last_at) / HOT_HALF_LIFE_S)


@runtime_checkable
class CacheStore(Protocol):
    async def get(self, key: str) -> Entry | None: ...
    async def put(self, key: str, entry: Entry) -> None: ...
    async def touch(self, key: str, meta: TouchMeta) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def purge(self, *, tags: Mapping[str, str] | None = None, match: str | None = None) -> int: ...
    async def hot(self, scope: Scope, limit_bytes: int) -> list[Entry]: ...
    async def generation(self, scope: Scope) -> int: ...
    async def bump_generation(self, scope: Scope) -> int: ...
    async def close(self) -> None: ...
