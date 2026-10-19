"""Clip verification at ingest (SOLUTION.md 4.1). The first release ships a no-op."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Verdict:
    ok: bool
    score: float | None = None
    reason: str | None = None


@runtime_checkable
class Verifier(Protocol):
    async def verify(
        self, text: str, pcm: bytes, sample_rate: int, channels: int, language: str
    ) -> Verdict: ...


class NoopVerifier:
    """Accepts every clip; the guarantee rests on exact capture (11.2)."""

    async def verify(self, text: str, pcm: bytes, sample_rate: int, channels: int, language: str) -> Verdict:
        return Verdict(ok=True)
