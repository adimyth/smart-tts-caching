"""Matching levels (SOLUTION.md section 5). Level A, the exact match, is the default."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from tts_cache.key import build_key
from tts_cache.policy import Level
from tts_cache.scope import Scope


@dataclass(frozen=True)
class Match:
    """A candidate entry for a sentence: the cache fetches ``key`` and serves it at ``level``."""

    key: str
    level: Level
    canonical: str
    diff_class: str | None = None
    score: float | None = None


@runtime_checkable
class Matcher(Protocol):
    async def find(
        self,
        scope: Scope,
        canonical: str,
        levels: set[Level],
        *,
        namespace: str | None = None,
        text: str | None = None,
    ) -> Match | None: ...


class ExactMatcher:
    """Level A: the key of the canonical text itself."""

    async def find(
        self,
        scope: Scope,
        canonical: str,
        levels: set[Level],
        *,
        namespace: str | None = None,
        text: str | None = None,
    ) -> Match | None:
        if "A" not in levels:
            return None
        return Match(build_key(scope, canonical, namespace=namespace), "A", canonical)


class Matchers:
    """Levels A, B and C together: each level goes to its own matcher.

    ``near`` is a ``NearDuplicateMatcher`` and ``semantic`` a ``SemanticMatcher``
    (both in the ``[similarity]`` extra); either may be absent.
    """

    def __init__(self, exact: Matcher | None = None, near: Any = None, semantic: Any = None) -> None:
        self.exact = exact or ExactMatcher()
        self.near = near
        self.semantic = semantic

    @property
    def extra_deadline_ms(self) -> float:
        """What the matchers add to a cache's default lookup deadline (the Level C judge's budget)."""
        return float(getattr(self.semantic, "extra_deadline_ms", 0.0))

    def handles(self, level: Level) -> bool:
        return {"A": self.exact, "B": self.near, "C": self.semantic}[level] is not None

    async def find(
        self,
        scope: Scope,
        canonical: str,
        levels: set[Level],
        *,
        namespace: str | None = None,
        text: str | None = None,
    ) -> Match | None:
        for level, m in (("A", self.exact), ("B", self.near), ("C", self.semantic)):
            if level in levels and m is not None:
                found = await m.find(scope, canonical, {level}, namespace=namespace, text=text)
                if found is not None:
                    return found
        return None

    def refresh(self, scope: Scope, memory: Any) -> None:
        """Rebuild the similarity indexes over the hot set in ``memory``."""
        for m in (self.near, self.semantic):
            if m is not None:
                m.refresh(scope, memory)
