"""The synthetic benchmark: does the cache save latency and cost, and what does it add when useless (9.3)?

Each run drives one corpus through the plain core twice, cache off and cache
on, against a simulated provider: time to first byte drawn from a lognormal
(median 250 ms by default), a per-character price, and audio derived from the
text. Provider time is simulated, not slept, so a sweep runs in seconds; the
cache's own work (lookup, first decoded chunk, capture) is measured on the
wall clock and added to it. Between turns the run waits for queued captures,
as a real conversation's playback time would.

Profiles: ``single`` is one process (process memory in front of SQLite);
``sessions`` is a fresh cache and process memory per turn in front of one
SQLite file, so every turn starts with a warm start (and, with Level B, a
near-duplicate index over the hot set); ``serverless`` is the same against the
Redis at ``redis_url`` (one container per session).
"""

import asyncio
import hashlib
import inspect
import json
import math
import random
import statistics
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from tts_cache.cache import Audio, TTSCache
from tts_cache.corpus import Corpus, generate, write_log
from tts_cache.metrics import LogSink
from tts_cache.policy import BASE_POLICY, DEFAULT_POLICY
from tts_cache.scope import Scope
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.sqlite import SQLiteStore
from tts_cache.stores.tiered import TieredStore

R_VALUES = (0.0, 0.1, 0.25, 0.5, 0.75, 0.9)
SR = 24_000


def synthetic_pcm(text: str, sample_rate: int = SR, ms_per_char: float = 66.0) -> bytes:
    """Deterministic audio for ``text``: a tone whose pitch comes from the text's hash."""
    n = int(sample_rate * ms_per_char * max(1, len(text.strip())) / 1000)
    f = 120 + int(hashlib.sha256(text.encode()).hexdigest()[:4], 16) % 400
    t = np.arange(n) / sample_rate
    envelope = np.clip(np.sin(2 * np.pi * 3 * t), 0.2, None)
    return (0.3 * envelope * np.sin(2 * np.pi * f * t) * 32767).astype("<i2").tobytes()


@dataclass
class SimulatedProvider:
    """Lognormal time to first byte and a per-character price; nothing is slept unless ``realtime``."""

    ttfb_median_ms: float = 250.0
    ttfb_sigma: float = 0.5
    price_per_char: float = 30e-6
    sample_rate: int = SR
    seed: int = 7
    realtime: bool = False
    chars: int = 0
    calls: int = 0
    last_ttfb_ms: float = 0.0
    _rng: random.Random = field(default_factory=random.Random, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    async def __call__(self, text: str) -> Audio:
        self.calls += 1
        self.chars += len(text.strip())
        self.last_ttfb_ms = self.ttfb_median_ms * math.exp(self._rng.gauss(0.0, self.ttfb_sigma))
        if self.realtime:
            await asyncio.sleep(self.last_ttfb_ms / 1000)
        return Audio(synthetic_pcm(text, self.sample_rate), self.sample_rate)

    @property
    def cost(self) -> float:
        return self.chars * self.price_per_char


def simulated_provider_factory(scope: Scope) -> SimulatedProvider:
    """A provider factory for ``tts-cache warm --provider tts_cache.bench:simulated_provider_factory``."""
    return SimulatedProvider(sample_rate=scope.sample_rate)


def bench_scope(language: str = "en-US") -> Scope:
    return Scope(service="simulated", model="sim-1", voice="v1", language=language, sample_rate=SR)


@dataclass
class Sample:
    hit: bool
    tier: str | None
    chars: int
    first_in_turn: bool
    lookup_ms: float = 0.0
    first_chunk_ms: float = 0.0
    capture_ms: float = 0.0
    ttfb_ms: float = 0.0
    # Per-sentence detail for the test plan (TEST_PLAN.md 8); empty in the plain sweep.
    index: int = -1
    session: int = -1
    turn: int = -1
    level: str | None = None
    key: str | None = None
    served_key: str | None = None
    spoken_text: str | None = None
    diff_class: str | None = None
    reason: str | None = None
    near_class: str | None = None
    explain: dict[str, Any] | None = None

    @property
    def added_ms(self) -> float:
        """Cache work on the host's path for this sentence."""
        return self.lookup_ms + self.first_chunk_ms + self.capture_ms

    @property
    def ttfa_ms(self) -> float:
        return self.lookup_ms + (self.first_chunk_ms if self.hit else self.ttfb_ms)


class _BenchSink(LogSink):
    stored_bytes = 0

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.recent: list[tuple[str, dict[str, Any]]] = []
        self.all: list[dict[str, Any]] | None = None
        self.reject_keys: dict[str, str] = {}

    def event(self, name: str, **fields: Any) -> None:
        super().event(name, **fields)
        self.recent.append((name, fields))
        if self.all is not None:
            self.all.append({"event": name, **fields})
        if name == "store":
            self.stored_bytes += int(fields.get("bytes", 0))
        if name == "reject" and fields.get("key"):
            self.reject_keys[str(fields["key"])] = str(fields.get("reason"))


def _q(xs: Sequence[float], q: float) -> float:
    if not xs:
        return 0.0
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


@dataclass
class Run:
    r: float
    cache: bool
    profile: str
    samples: list[Sample]
    billed_chars: int
    cost: float
    stored: int = 0
    stored_bytes: int = 0
    rejects: int = 0
    histogram: dict[str, int] = field(default_factory=dict)
    level_b_hits: dict[str, int] = field(default_factory=dict)
    levels: str | None = None
    sessions: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] | None = None
    reject_keys: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    event_chars: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        s = self.samples
        chars = sum(x.chars for x in s)
        hit_chars = sum(x.chars for x in s if x.hit)
        hits = [x for x in s if x.hit]
        misses = [x for x in s if not x.hit]
        first = [x for x in s if x.first_in_turn]
        by_tier: dict[str, list[float]] = {}
        for x in s:
            by_tier.setdefault(x.tier or "miss", []).append(x.lookup_ms)
        return {
            "r": self.r,
            "cache": "on" if self.cache else "off",
            "profile": self.profile,
            "sentences": len(s),
            "chars": chars,
            "billed_chars": self.billed_chars,
            "cost": round(self.cost, 6),
            "hit_rate": round(hit_chars / chars, 4) if chars else 0.0,
            "hits": len(hits),
            "ttfa_p50_ms": round(_q([x.ttfa_ms for x in s], 0.5), 3),
            "ttfa_first_p50_ms": round(_q([x.ttfa_ms for x in first], 0.5), 3),
            "ttfa_hit_p50_ms": round(_q([x.ttfa_ms for x in hits], 0.5), 3),
            "ttfa_hit_p99_ms": round(_q([x.ttfa_ms for x in hits], 0.99), 3),
            "ttfa_miss_p50_ms": round(_q([x.ttfa_ms for x in misses], 0.5), 3),
            "added_mean_ms": round(statistics.fmean([x.added_ms for x in s]), 4) if s else 0.0,
            "added_p50_ms": round(_q([x.added_ms for x in s], 0.5), 4),
            "added_p99_ms": round(_q([x.added_ms for x in s], 0.99), 4),
            "lookup_ms": {
                t: {"p50": round(_q(v, 0.5), 4), "p99": round(_q(v, 0.99), 4)} for t, v in by_tier.items()
            },
            "stored": self.stored,
            "stored_bytes": self.stored_bytes,
            "rejects": self.rejects,
            "near_histogram": dict(self.histogram),
            "level_b_hits": dict(self.level_b_hits),
        }


def reuse_encoded(max_entries: int = 50_000) -> None:
    """Encode each distinct clip once per process (TEST_PLAN.md 15, item 3).

    The cache encodes on its ingest worker, off the request path, so reuse
    changes no lookup, first-chunk or capture timing, only the wall clock of
    a sweep. Decoding is left alone, so hit-path timings stay real.
    """
    from tts_cache import codec

    if getattr(codec.encode, "_reused", False):
        return
    inner = codec.encode
    memo: dict[tuple[bytes, int, int, str], bytes] = {}

    def encode(pcm: bytes, sample_rate: int, channels: int = 1, codec_: Any = "opus") -> bytes:
        k = (hashlib.sha256(pcm).digest(), sample_rate, channels, codec_)
        got = memo.get(k)
        if got is None:
            got = inner(pcm, sample_rate, channels, codec_)
            if len(memo) < max_entries:
                memo[k] = got
        return got

    encode._reused = True  # type: ignore[attr-defined]
    codec.encode = encode  # type: ignore[assignment]


CONFIGS = ("OFF", "A", "A+B", "A+B+C")


def config_policy(levels: str) -> tuple[Any, Any]:
    """The policy and matcher of each configuration of TEST_PLAN.md section 4."""
    from tts_cache.cache import default_matcher
    from tts_cache.match import ExactMatcher, Matchers
    from tts_cache.match.near import NearDuplicateMatcher
    from tts_cache.policy import AllOf, LevelB

    if levels == "A":
        return BASE_POLICY, ExactMatcher()
    if levels == "A+B":
        return AllOf(BASE_POLICY, LevelB({"punctuation", "filler"})), Matchers(near=NearDuplicateMatcher())
    if levels == "A+B+C":
        return DEFAULT_POLICY, default_matcher()
    raise ValueError(f"unknown configuration {levels!r}")


class _TimedMatcher:
    """Delegates to a matcher and times its index refresh at warm start."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.refresh_ms = 0.0
        if not hasattr(inner, "handles"):
            self.handles = None  # type: ignore[assignment]

    def handles(self, level: Any) -> bool:
        return self.inner.handles(level)

    async def find(self, *a: Any, **kw: Any) -> Any:
        return await self.inner.find(*a, **kw)

    def refresh(self, scope: Scope, memory: Any) -> None:
        t = time.perf_counter()
        fn = getattr(self.inner, "refresh", None)
        if fn is not None:
            fn(scope, memory)
        self.refresh_ms += (time.perf_counter() - t) * 1000


def rss_bytes() -> int:
    """Resident set size of this process, from ``ps`` (no extra dependency)."""
    import os
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True)
        return int(out.stdout.strip() or 0) * 1024
    except Exception:
        return 0


async def run_core(
    corpus: Corpus | Any,
    r: float,
    *,
    cache_on: bool,
    profile: str = "single",
    provider: SimulatedProvider,
    workdir: Path,
    redis_url: str | None = None,
    language: str = "en-US",
    level_b: frozenset[str] | None = None,
    db_name: str | None = None,
    levels: str | None = None,
    session_of_turn: Sequence[int] | None = None,
    warm_start_bytes: int = 4 << 20,
    warm_texts: int = 2000,
    session_bytes: int = 64 << 20,
    sqlite_max_bytes: int | None = None,
    sweep_after_session: bool = False,
    explain: Any = None,
    keep_events: bool = False,
    sample_rss: bool = False,
    scope: Scope | None = None,
    shared_factory: Any = None,
    metrics_log: Any = None,
) -> Run:
    """One corpus through the plain core, cache off or on.

    ``levels`` picks a configuration of TEST_PLAN.md section 4 ("A", "A+B",
    "A+B+C"); without it the policy is the shipped default, or Level A plus
    ``level_b`` when given. In the ``sessions`` and ``serverless`` profiles a
    fresh cache starts per turn, or per session when ``session_of_turn`` gives
    each turn's session. ``explain(cache, text, sample)`` runs after every miss
    and may return a dict kept on the sample. The size knobs are those of
    TEST_PLAN.md section 9. ``metrics_log`` is a logger that receives the
    cache's metric events as JSON lines, as ``LogSink`` writes them.
    """
    scope = scope or bench_scope(language)
    sink = _BenchSink(log=metrics_log)
    if keep_events:
        sink.all = []
    samples: list[Sample] = []
    sessions_info: list[dict[str, Any]] = []

    def make_cache() -> TTSCache:
        if shared_factory is not None:
            shared: Any = shared_factory()
        elif profile == "serverless":
            from tts_cache.stores.redis import RedisStore

            shared = RedisStore(redis_url or "redis://localhost:6380/0")
        else:
            shared = SQLiteStore(
                workdir / (db_name or f"bench-{r}.db"), max_bytes=sqlite_max_bytes, sweep_interval_s=0
            )
        policy: Any = DEFAULT_POLICY
        matcher: Any = None
        if levels is not None:
            policy, matcher = config_policy(levels)
        elif level_b is not None:
            from tts_cache.match import Matchers
            from tts_cache.match.near import NearDuplicateMatcher
            from tts_cache.policy import AllOf, LevelB

            policy = AllOf(BASE_POLICY, LevelB(level_b))
            matcher = Matchers(near=NearDuplicateMatcher())
        if matcher is not None:
            matcher = _TimedMatcher(matcher)
        memory = MemoryStore(warm_start_bytes=warm_start_bytes, session_bytes=session_bytes)
        store = TieredStore(memory, shared, deadline_ms=20)
        return TTSCache(
            scope,
            store,
            policy=policy,
            matcher=matcher,
            metrics=sink,
            warm_start_bytes=warm_start_bytes,
            warm_texts=warm_texts,
        )

    async def open_cache() -> TTSCache:
        c = make_cache()
        shared = c.store.shared if isinstance(c.store, TieredStore) else None
        fetch_ms = [0.0]
        warm = getattr(shared, "warm", None)
        if warm is not None:

            async def timed_warm(*a: Any, _w: Any = warm, **kw: Any) -> Any:
                t = time.perf_counter()
                try:
                    return await _w(*a, **kw)
                finally:
                    fetch_ms[0] += (time.perf_counter() - t) * 1000

            shared.warm = timed_warm  # type: ignore[union-attr]
        t0 = time.perf_counter()
        await c.start()
        total = (time.perf_counter() - t0) * 1000
        refresh = c.matcher.refresh_ms if isinstance(c.matcher, _TimedMatcher) else 0.0
        warm_ev = next((f for n, f in reversed(sink.recent) if n == "warm_start"), {})
        sessions_info.append(
            {
                "start_ms": round(total, 3),
                "fetch_ms": round(fetch_ms[0], 3),
                "index_ms": round(refresh, 3),
                "load_ms": round(max(0.0, total - fetch_ms[0] - refresh), 3),
                "warm_entries": warm_ev.get("entries", 0),
                "warm_bytes": warm_ev.get("bytes", 0),
                "warm_texts": warm_ev.get("texts", 0),
                "rss": rss_bytes() if sample_rss else 0,
            }
        )
        return c

    async def close_cache(c: TTSCache) -> None:
        await c.drain(10.0)
        if sweep_after_session and isinstance(c.store, TieredStore):
            sweep = getattr(c.store.shared, "sweep", None)
            if sweep is not None:
                await sweep()
        await c.close()

    per_turn = profile in ("serverless", "sessions")
    cache: TTSCache | None = None
    if cache_on and not per_turn:
        cache = await open_cache()
    index = 0
    current_session: int | None = None
    for ti, turn in enumerate(corpus.turns):
        sess = session_of_turn[ti] if session_of_turn is not None else ti
        if cache_on and per_turn and sess != current_session:
            if cache is not None:
                await close_cache(cache)
            cache = await open_cache()
            current_session = sess
        for i, text in enumerate(turn):
            chars = len(text.strip())
            base: dict[str, Any] = {"index": index, "session": sess, "turn": ti}
            index += 1
            if not cache_on or cache is None:
                await provider(text)
                samples.append(Sample(False, None, chars, i == 0, ttfb_ms=provider.last_ttfb_ms, **base))
                continue
            sink.recent = []
            t0 = time.perf_counter()
            found = await cache.probe(text)
            lookup_ms = (time.perf_counter() - t0) * 1000
            events = sink.recent
            near = next((f.get("diff_class") for n, f in events if n == "near"), None)
            miss_reason = next((f.get("reason") for n, f in events if n == "miss"), None)
            if found.hit is not None:
                t1 = time.perf_counter()
                next(found.hit.pcm_stream())
                first_ms = (time.perf_counter() - t1) * 1000
                samples.append(
                    Sample(
                        True,
                        found.hit.meta.get("tier"),
                        chars,
                        i == 0,
                        lookup_ms,
                        first_ms,
                        level=found.hit.level,
                        key=found.key,
                        served_key=found.hit.key,
                        spoken_text=found.hit.spoken_text,
                        diff_class=found.hit.meta.get("diff_class"),
                        near_class=near,
                        **base,
                    )
                )
                continue
            audio = await provider(text)
            sample = Sample(
                False,
                None,
                chars,
                i == 0,
                lookup_ms,
                ttfb_ms=provider.last_ttfb_ms,
                key=found.key,
                reason=miss_reason,
                near_class=near,
                **base,
            )
            if explain is not None:
                got = explain(cache, text, sample)
                sample.explain = await got if inspect.isawaitable(got) else got
            t2 = time.perf_counter()
            await cache.capture(text, audio.pcm, audio.sample_rate, key=found.key)
            sample.capture_ms = (time.perf_counter() - t2) * 1000
            samples.append(sample)
        if cache is not None:
            await cache.drain(10.0)  # the turn plays out; captures land meanwhile
            if per_turn and session_of_turn is None:
                await close_cache(cache)
                cache = None
    if cache is not None:
        await close_cache(cache)
    return Run(
        r=r,
        cache=cache_on,
        profile=profile,
        samples=samples,
        billed_chars=provider.chars,
        cost=provider.cost,
        stored=sink.counts["store"],
        stored_bytes=sink.stored_bytes,
        rejects=sum(v for k, v in sink.counts.items() if k == "reject"),
        histogram={k.split(":", 1)[1]: v for k, v in sink.counts.items() if k.startswith("near:")},
        level_b_hits={c: sink.counts[f"hit:{c}"] for c in ("punctuation", "filler", "slot", "other")},
        levels=levels,
        sessions=sessions_info,
        events=sink.all,
        reject_keys=sink.reject_keys,
        counts=dict(sink.counts),
        event_chars=dict(sink.chars),
    )


async def sweep(
    r_values: Sequence[float] = R_VALUES,
    *,
    n: int = 200,
    seed: int = 1,
    language: str = "en-US",
    profile: str = "single",
    ttfb_median_ms: float = 250.0,
    ttfb_sigma: float = 0.5,
    price_per_char: float = 30e-6,
    redis_url: str | None = None,
    log_dir: Path | None = None,
) -> list[dict[str, Any]]:
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for r in r_values:
            corpus = generate(n, r, seed=seed, language=language)
            if log_dir is not None:
                await asyncio.to_thread(_write_corpus_log, log_dir, r, corpus)
            for on in (False, True):
                if on and profile == "serverless" and redis_url:
                    from tts_cache.stores.redis import RedisStore

                    flush = RedisStore(redis_url)
                    await flush.client.flushdb()
                    await flush.close()
                provider = SimulatedProvider(ttfb_median_ms, ttfb_sigma, price_per_char, seed=seed)
                run = await run_core(
                    corpus,
                    r,
                    cache_on=on,
                    profile=profile,
                    provider=provider,
                    workdir=work,
                    redis_url=redis_url,
                    language=language,
                )
                rows.append(run.summary() | {"repeats": corpus.repeats})
    return rows


async def level_b_comparison(
    r: float = 0.5,
    *,
    n: int = 200,
    seed: int = 1,
    variants: float = 0.4,
    classes: frozenset[str] = frozenset({"punctuation"}),
    language: str = "en-US",
) -> list[dict[str, Any]]:
    """The same corpus, with near-duplicate repeats, with Level B serving ``classes`` off and on.

    Both runs probe Level B on every sentence, so both report the diff-class
    histogram; only the second may serve. Profile ``sessions``: the near index
    is rebuilt over the hot set at each session's warm start.
    """
    corpus = generate(n, r, seed=seed, language=language, variants=variants)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for label, allowed in (("level_b_off", frozenset()), ("level_b_on", classes)):
            provider = SimulatedProvider(seed=seed)
            run = await run_core(
                corpus,
                r,
                cache_on=True,
                profile="sessions",
                provider=provider,
                workdir=Path(tmp),
                language=language,
                level_b=allowed,
                db_name=f"{label}.db",
            )
            rows.append(run.summary() | {"mode": label, "classes": sorted(allowed), "variants": variants})
    return rows


def _write_corpus_log(log_dir: Path, r: float, corpus: Corpus) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    write_log(log_dir / f"bench-r{r}.jsonl", corpus.events())


def format_table(rows: Sequence[dict[str, Any]]) -> str:
    head = (
        f"{'r':>5} {'cache':>5} {'billed':>8} {'cost $':>9} {'hit%':>6} {'ttfa p50':>9} {'first p50':>9} "
        f"{'hit p50':>8} {'added p50':>9} {'added p99':>9} {'stored':>6}"
    )
    lines = [head]
    for x in rows:
        lines.append(
            f"{x['r']:>5} {x['cache']:>5} {x['billed_chars']:>8} {x['cost']:>9.4f} "
            f"{x['hit_rate'] * 100:>6.1f} "
            f"{x['ttfa_p50_ms']:>9.2f} {x['ttfa_first_p50_ms']:>9.2f} {x['ttfa_hit_p50_ms']:>8.3f} "
            f"{x['added_p50_ms']:>9.4f} {x['added_p99_ms']:>9.4f} {x['stored']:>6}"
        )
    return "\n".join(lines)


def plot_svg(rows: Sequence[dict[str, Any]], path: str | Path) -> None:
    """Cost and p50 time to first audio against r, cache off and on; plain SVG, no plotting library."""
    w, h, pad = 360, 220, 44
    panels = [("cost", "cost ($)"), ("ttfa_p50_ms", "p50 time to first audio (ms)")]
    rs = sorted({x["r"] for x in rows})

    def series(metric: str, mode: str) -> list[tuple[float, float]]:
        return [(x["r"], x[metric]) for x in rows if x["cache"] == mode]

    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{2 * w}" height="{h}"'
    parts = [
        svg + ' font-family="sans-serif" font-size="11">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    span = (rs[-1] - rs[0]) or 1
    for i, (metric, label) in enumerate(panels):
        ox = i * w
        ymax = max([x[metric] for x in rows] + [1e-9]) * 1.1

        def xs(r: float, ox: float = ox) -> float:
            return ox + pad + (r - rs[0]) / span * (w - 2 * pad)

        def ys(v: float, ymax: float = ymax) -> float:
            return h - pad - v / ymax * (h - 2 * pad)

        parts.append(
            f'<text x="{ox + w / 2}" y="16" text-anchor="middle">{label} vs repetition rate r</text>'
        )
        parts.append(
            f'<line x1="{ox + pad}" y1="{h - pad}" x2="{ox + w - pad}" y2="{h - pad}" stroke="#444"/>'
        )
        parts.append(f'<line x1="{ox + pad}" y1="{pad}" x2="{ox + pad}" y2="{h - pad}" stroke="#444"/>')
        for r in rs:
            parts.append(f'<text x="{xs(r)}" y="{h - pad + 14}" text-anchor="middle">{r:g}</text>')
        parts.append(f'<text x="{ox + 4}" y="{pad - 6}">{ymax:.3g}</text>')
        for mode, color in (("off", "#b0413e"), ("on", "#2d6a9f")):
            pts = series(metric, mode)
            if pts:
                d = " ".join(f"{xs(r):.1f},{ys(v):.1f}" for r, v in pts)
                parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
                parts.append(
                    f'<text x="{ox + w - pad}" y="{ys(pts[-1][1]) - 4:.1f}" fill="{color}">{mode}</text>'
                )
    parts.append("</svg>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def save(rows: Sequence[dict[str, Any]], out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bench.json").write_text(json.dumps(list(rows), indent=2), encoding="utf-8")
    (out / "bench.txt").write_text(format_table(rows) + "\n", encoding="utf-8")
    plot_svg(rows, out / "bench.svg")


__all__ = ["R_VALUES", "Run", "Sample", "SimulatedProvider", "asdict", "format_table", "save", "sweep"]


def word_salad(words: Sequence[str], n: int, *, seed: int = 1, lang: str = "en") -> list[str]:
    """``n`` distinct sentences of 4 to 14 words drawn from ``words``, for filling a scope."""
    from tts_cache.corpus import DANDA_LANGUAGES

    rng = random.Random(seed)
    end = "।" if lang in DANDA_LANGUAGES else "."
    out: list[str] = []
    seen: set[str] = set()
    vocab = sorted(set(words))
    while len(out) < n:
        text = " ".join(rng.choice(vocab) for _ in range(rng.randint(4, 14)))
        text = text[:1].upper() + text[1:] + end
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


async def fill(
    store: Any,
    scope: Scope,
    n: int,
    kind: str = "distinct",
    *,
    texts: Sequence[str] | None = None,
    scores: Sequence[float] | None = None,
    value_bytes: int = 1024,
    seed: int = 1,
    ms_per_char: float = 66.0,
) -> dict[str, Any]:
    """Put ``n`` entries into ``store`` under ``scope`` directly, without the cache (TEST_PLAN.md 9).

    ``kind`` is ``distinct`` (``value_bytes`` of PCM per entry, numbered texts),
    ``texts`` (the given ``texts``, ``value_bytes`` of PCM each) or ``real``
    (the given ``texts`` with real-size Opus clips of the synthetic voice; one
    encode per text length). ``scores`` set each entry's hot score, so the
    store's hot set has a chosen order. Returns entries, bytes and time.
    """
    from tts_cache import codec
    from tts_cache.canonical import canonicalise
    from tts_cache.key import build_key
    from tts_cache.store import Entry

    lang = scope.language.split("-")[0].lower()
    if kind == "distinct":
        texts = [f"Filler entry number {i} of {n}." for i in range(n)]
    assert texts is not None and len(texts) >= n
    t0 = time.perf_counter()
    by_len: dict[int, tuple[bytes, int]] = {}
    total = 0
    pcm_blob = bytes(value_bytes)
    for i in range(n):
        text = texts[i]
        canonical = canonicalise(text, lang)
        key = build_key(scope, canonical)
        if kind == "real":
            chars = max(1, len(text.strip()))
            got = by_len.get(chars)
            if got is None:
                pcm = synthetic_pcm("x" * chars, scope.sample_rate, ms_per_char)
                got = (codec.encode(pcm, scope.sample_rate), int(len(pcm) / 2 / scope.sample_rate * 1000))
                by_len[chars] = got
            audio, dur = got
            c: Any = "opus"
        else:
            audio, dur, c = pcm_blob, int(value_bytes / 2 / scope.sample_rate * 1000), "pcm"
        entry = Entry(
            key=key,
            audio=audio,
            codec=c,
            sample_rate=scope.sample_rate,
            channels=1,
            duration_ms=max(1, dur),
            text=text,
            canonical_text=canonical,
            language=scope.language,
            hot_score=float(scores[i]) if scores is not None else 0.0,
            hit_count=int(scores[i]) if scores is not None else 0,
        )
        await store.put(key, entry)
        total += len(audio)
    return {"entries": n, "bytes": total, "ms": round((time.perf_counter() - t0) * 1000, 1)}
