"""The strategy layer: every "should we" decision goes through one interface (SOLUTION.md 4.6).

Leaf policies answer only the questions they own and return ``None`` (no
opinion) for TTL and match levels; :class:`AllOf`, :class:`AnyOf` and
:class:`ByTag` combine the opinions and always return a concrete answer, so a
composed policy satisfies :class:`CachePolicy` as section 4.6 states it.
"""

import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from tts_cache.scope import Scope
from tts_cache.store import Entry

Level = Literal["A", "B", "C"]
LEVELS: tuple[Level, ...] = ("A", "B", "C")
EntryMeta = Entry

DEFAULT_TTL_S = 30 * 24 * 3600


def parse_duration(value: str | int | float) -> int:
    """Seconds in ``"48h"``, ``"30d"``, ``"15m"``, ``"90s"`` or a plain number of seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", value)
    if not m:
        raise ValueError(f"not a duration: {value!r}")
    return int(float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])


@dataclass(frozen=True)
class SentenceCtx:
    """One sentence as the policy sees it. ``text`` is the provider text; ``canonical`` its key form."""

    text: str
    canonical: str
    language: str
    scope: Scope
    tags: Mapping[str, str] = field(default_factory=dict)
    source: Literal["llm", "speak"] = "llm"
    turn_index: int | None = None
    from_end: int | None = None
    now: float = field(default_factory=time.time)

    @property
    def chars(self) -> int:
        return len(self.text.strip())


@dataclass(frozen=True)
class Clip:
    duration_ms: int
    sample_rate: int
    channels: int
    nbytes: int


@dataclass(frozen=True)
class TurnCtx:
    sentences: int = 0
    tags: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class CachePolicy(Protocol):
    def should_lookup(self, s: SentenceCtx) -> bool: ...
    def should_store(self, s: SentenceCtx, clip: Clip) -> bool: ...
    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int: ...
    def allowed_levels(self, s: SentenceCtx) -> set[Level]: ...
    def on_hit(self, s: SentenceCtx, meta: EntryMeta) -> None: ...
    def on_interruption(self, turn: TurnCtx) -> None: ...


class Policy:
    """Base class: no restrictions and no opinions."""

    def should_lookup(self, s: SentenceCtx) -> bool:
        return True

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return True

    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int | None:
        return None

    def allowed_levels(self, s: SentenceCtx) -> set[Level] | None:
        return None

    def on_hit(self, s: SentenceCtx, meta: EntryMeta) -> None:
        pass

    def on_interruption(self, turn: TurnCtx) -> None:
        pass

    # Hooks beyond section 4.6, read by TTSCache when a policy defines them.

    def serves(self, s: SentenceCtx) -> bool:
        """False to look up and count without serving (``Shadow``)."""
        return True

    def key_namespace(self, s: SentenceCtx) -> str | None:
        """A string folded into the key to isolate entries (``Namespace``)."""
        return None

    def level_b_classes(self, s: SentenceCtx) -> frozenset[str]:
        """Near-duplicate classes that may be served (``LevelB``)."""
        return frozenset()


class AlwaysCache(Policy):
    def __init__(self, ttl: str | int = "30d") -> None:
        self.ttl = parse_duration(ttl)

    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int:
        return self.ttl

    def allowed_levels(self, s: SentenceCtx) -> set[Level]:
        return {"A"}


class Shadow(Policy):
    """Look up and count, never serve; stores as ``inner`` does so the counts are real."""

    def __init__(self, inner: "Policy | None" = None) -> None:
        self.inner = inner or AllOf(Probation("48h", "30d"), MinChars(3, 400))

    def should_lookup(self, s: SentenceCtx) -> bool:
        return self.inner.should_lookup(s)

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return self.inner.should_store(s, clip)

    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int | None:
        return self.inner.ttl_for(s, existing)

    def allowed_levels(self, s: SentenceCtx) -> set[Level] | None:
        return self.inner.allowed_levels(s)

    def serves(self, s: SentenceCtx) -> bool:
        return False

    def key_namespace(self, s: SentenceCtx) -> str | None:
        return self.inner.key_namespace(s)

    def level_b_classes(self, s: SentenceCtx) -> frozenset[str]:
        return self.inner.level_b_classes(s)


class Probation(Policy):
    """A new clip lives ``short``; its first hit moves it to a sliding ``long`` TTL (4.4)."""

    def __init__(self, short: str | int = "48h", long: str | int = "30d") -> None:
        self.short = parse_duration(short)
        self.long = parse_duration(long)

    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int:
        return self.long if existing is not None and existing.hit_count > 0 else self.short

    def allowed_levels(self, s: SentenceCtx) -> set[Level]:
        return {"A"}


class MinChars(Policy):
    """Skip sentences whose canonical text is shorter than ``lo`` or longer than ``hi`` characters."""

    def __init__(self, lo: int = 3, hi: int = 400) -> None:
        self.lo, self.hi = lo, hi

    def _ok(self, s: SentenceCtx) -> bool:
        return self.lo <= len(s.canonical) <= self.hi

    def should_lookup(self, s: SentenceCtx) -> bool:
        return self._ok(s)

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return self._ok(s)


class SkipIfMatches(Policy):
    """Never store a sentence matching any of ``patterns`` (PII, variable content)."""

    def __init__(self, patterns: Iterable[str | re.Pattern[str]]) -> None:
        self.patterns = [re.compile(p) if isinstance(p, str) else p for p in patterns]

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return not any(p.search(s.text) for p in self.patterns)


class TurnPosition(Policy):
    """Only the first ``first_n`` and last ``last_n`` sentences of a turn."""

    def __init__(self, first_n: int = 1, last_n: int = 1) -> None:
        self.first_n, self.last_n = first_n, last_n

    def _ok(self, s: SentenceCtx) -> bool:
        first = s.turn_index is not None and s.turn_index < self.first_n
        last = s.from_end is not None and s.from_end < self.last_n
        return first or last

    def should_lookup(self, s: SentenceCtx) -> bool:
        return self._ok(s)

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return self._ok(s)


class SpeakFramesOnly(Policy):
    """Only scripted utterances the application sends itself, not LLM output."""

    def should_lookup(self, s: SentenceCtx) -> bool:
        return s.source == "speak"

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return s.source == "speak"


class LevelB(Policy):
    """Which near-duplicate classes may be served (5). ``slot`` and ``other`` never are."""

    SERVABLE = frozenset({"punctuation", "filler"})

    def __init__(self, classes: Iterable[str] = ()) -> None:
        self.classes = frozenset(classes) & self.SERVABLE

    def allowed_levels(self, s: SentenceCtx) -> set[Level] | None:
        return {"A", "B"} if self.classes else None

    def level_b_classes(self, s: SentenceCtx) -> frozenset[str]:
        return self.classes


class LevelC(Policy):
    """Allow Level C, semantic paraphrase, where the language maps to a model (5). Off unless used."""

    def allowed_levels(self, s: SentenceCtx) -> set[Level] | None:
        return {"A", "C"}


class Namespace(Policy):
    """Fold the value of tag ``tag`` into the key, isolating entries per value."""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def key_namespace(self, s: SentenceCtx) -> str | None:
        return f"{self.tag}={s.tags.get(self.tag, '')}"


class RateLimit(Policy):
    """Bound ingest writes with a token bucket of ``stores_per_minute``."""

    def __init__(self, stores_per_minute: float) -> None:
        self.rate = stores_per_minute / 60.0
        self.capacity = max(1.0, stores_per_minute)
        self._tokens = self.capacity
        self._at = time.monotonic()

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._at) * self.rate)
        self._at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


def _hook(p: object, name: str, *args: object) -> object:
    fn = getattr(p, name, None)
    return fn(*args) if fn is not None else None


def _resolve_levels(opinions: list[set[Level] | None]) -> set[Level]:
    given = [o for o in opinions if o is not None]
    if not given:
        return {"A"}
    if any(not o for o in given):
        return set()
    return set().union(*given)


class AllOf(Policy):
    """Look up and store only if every policy agrees; the shortest TTL wins; level grants add up."""

    def __init__(self, *policies: CachePolicy | Policy) -> None:
        self.policies = policies

    def should_lookup(self, s: SentenceCtx) -> bool:
        return all(p.should_lookup(s) for p in self.policies)

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return all(p.should_store(s, clip) for p in self.policies)

    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int:
        ttls = [t for p in self.policies if (t := p.ttl_for(s, existing)) is not None]
        return min(ttls) if ttls else DEFAULT_TTL_S

    def allowed_levels(self, s: SentenceCtx) -> set[Level]:
        return _resolve_levels([p.allowed_levels(s) for p in self.policies])

    def on_hit(self, s: SentenceCtx, meta: EntryMeta) -> None:
        for p in self.policies:
            p.on_hit(s, meta)

    def on_interruption(self, turn: TurnCtx) -> None:
        for p in self.policies:
            p.on_interruption(turn)

    def serves(self, s: SentenceCtx) -> bool:
        return all(_hook(p, "serves", s) is not False for p in self.policies)

    def key_namespace(self, s: SentenceCtx) -> str | None:
        parts = [ns for p in self.policies if isinstance(ns := _hook(p, "key_namespace", s), str)]
        return "&".join(parts) if parts else None

    def level_b_classes(self, s: SentenceCtx) -> frozenset[str]:
        out: frozenset[str] = frozenset()
        for p in self.policies:
            got = _hook(p, "level_b_classes", s)
            if isinstance(got, frozenset):
                out |= got
        return out & LevelB.SERVABLE


class AnyOf(AllOf):
    """Look up and store if any policy agrees; the longest TTL wins."""

    def should_lookup(self, s: SentenceCtx) -> bool:
        return any(p.should_lookup(s) for p in self.policies)

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return any(p.should_store(s, clip) for p in self.policies)

    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int:
        ttls = [t for p in self.policies if (t := p.ttl_for(s, existing)) is not None]
        return max(ttls) if ttls else DEFAULT_TTL_S


class ByTag(Policy):
    """Route each sentence to the policy for its value of tag ``tag``, else ``default``."""

    def __init__(
        self, tag: str, routes: Mapping[str, CachePolicy | Policy], default: CachePolicy | Policy
    ) -> None:
        self.tag = tag
        self.routes = dict(routes)
        self.default = default

    def _pick(self, tags: Mapping[str, str]) -> CachePolicy | Policy:
        value = tags.get(self.tag)
        return self.routes.get(value, self.default) if value is not None else self.default

    def should_lookup(self, s: SentenceCtx) -> bool:
        return self._pick(s.tags).should_lookup(s)

    def should_store(self, s: SentenceCtx, clip: Clip) -> bool:
        return self._pick(s.tags).should_store(s, clip)

    def ttl_for(self, s: SentenceCtx, existing: EntryMeta | None) -> int:
        ttl = self._pick(s.tags).ttl_for(s, existing)
        return DEFAULT_TTL_S if ttl is None else ttl

    def allowed_levels(self, s: SentenceCtx) -> set[Level]:
        return _resolve_levels([self._pick(s.tags).allowed_levels(s)])

    def on_hit(self, s: SentenceCtx, meta: EntryMeta) -> None:
        self._pick(s.tags).on_hit(s, meta)

    def on_interruption(self, turn: TurnCtx) -> None:
        self._pick(turn.tags).on_interruption(turn)

    def serves(self, s: SentenceCtx) -> bool:
        return _hook(self._pick(s.tags), "serves", s) is not False

    def key_namespace(self, s: SentenceCtx) -> str | None:
        ns = _hook(self._pick(s.tags), "key_namespace", s)
        return ns if isinstance(ns, str) else None

    def level_b_classes(self, s: SentenceCtx) -> frozenset[str]:
        got = _hook(self._pick(s.tags), "level_b_classes", s)
        return got & LevelB.SERVABLE if isinstance(got, frozenset) else frozenset()


# Probation and length bounds alone: Level A only. The default adds Level B for
# punctuation and filler differences. Level C is opt-in: the test plan of
# 2026-10-19 found it serving a changed meaning in 11 of 12 languages, so it
# needs LEVEL_C_POLICY and a matcher with a Level C matcher, preferably with
# the judge (``default_matcher(judge=True)``). It serves only in languages the
# routing map sends to an encoder.
BASE_POLICY = AllOf(Probation("48h", "30d"), MinChars(3, 400))
DEFAULT_POLICY = AllOf(BASE_POLICY, LevelB(LevelB.SERVABLE))
LEVEL_C_POLICY = AllOf(BASE_POLICY, LevelB(LevelB.SERVABLE), LevelC())
