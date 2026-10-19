"""The cache-size sweeps of TEST_PLAN.md section 9.

- ``capacity``: 20,000-sentence streams under configuration A against Redis
  instances capped at fractions of the unbounded peak (``allkeys-lfu``), and an
  SQLite arm with ``max_bytes`` and a sweep after each session (9.1); plus the
  labelled corpora at fractions of their own SQLite peak under every
  configuration ("B and C under pressure").
- ``memory``: a Redis scope filled from a 20,000-sentence stream, then the
  labelled corpus in the serverless profile across ``warm_start_bytes`` and
  ``warm_texts`` (9.2).
- ``population``: N entries in one scope; latency per level and tier, index
  build time and memory, Redis memory per entry, and paraphrase recall and
  wrong serves of the pool's labelled queries against N (9.3).

Every Redis here is a dedicated instance in its sweep's own port block (capacity 6700,
memory 6800, population 6900; the live harness uses 6600 and usability 7100), started and
stopped by this module; port 6379 is refused.

    python -m tts_cache.testplan.sizes capacity|memory|population --out harness/out/testplan/sizes
"""

import argparse
import asyncio
import json
import multiprocessing as mp
import random
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from tts_cache.bench import (
    SR,
    SimulatedProvider,
    bench_scope,
    fill,
    reuse_encoded,
    rss_bytes,
    run_core,
    synthetic_pcm,
    word_salad,
)
from tts_cache.corpus import (
    POOL_LANGUAGES,
    filler_variants,
    generate,
    load_pool,
    punct_inner_variants,
    punct_question_variant,
)
from tts_cache.testplan.pools import pool_path

REAL_BYTES_PER_4S = 15_800  # TEST_REPORT.md: real speech, Opus, 4 s clip
CAPACITY_FRACTIONS = (0.05, 0.10, 0.25, 0.50, 1.0)
PRESSURE_FRACTIONS = (0.10, 0.25, 0.50, 1.0)
WARM_BYTES_MB = (0, 1, 4, 16, 64)
WARM_TEXTS = (0, 500, 2000, 10_000)
POPULATION = (1_000, 2_000, 10_000, 100_000)
# Disjoint port blocks, so sweeps never meet each other or the live harness (6600).
CAPACITY_PORTS = 6700
MEMORY_PORTS = 6800
POPULATION_PORTS = 6900


# Redis instances.


def _pong(port: int) -> bool:
    out = subprocess.run(["redis-cli", "-p", str(port), "ping"], capture_output=True, text=True)
    return out.stdout.strip() == "PONG"


def start_redis(
    port: int, maxmemory: int | None = None, policy: str = "allkeys-lfu", extra: tuple[str, ...] = ()
) -> str:
    """A fresh dedicated instance on ``port``.

    An instance already answering there (left by a crashed sweep) is shut down
    first: ``redis-server --daemonize yes`` exits 0 even when it cannot bind, so
    a stale instance would otherwise be used silently, with its own keys and
    ``maxmemory``. The new instance's ``maxmemory`` is checked after it starts.
    """
    if port == 6379:
        raise ValueError("port 6379 is the shared Redis; never touch it")
    if _pong(port):
        stop_redis(port)
        for _ in range(100):
            if not _pong(port):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(f"a stale redis on {port} would not shut down")
    args = ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no", "--daemonize", "yes"]
    if maxmemory is not None:
        args += ["--maxmemory", str(maxmemory), "--maxmemory-policy", policy]
    args += list(extra)
    subprocess.run(args, check=True, capture_output=True)
    for _ in range(100):
        if _pong(port):
            got = subprocess.run(
                ["redis-cli", "-p", str(port), "config", "get", "maxmemory"], capture_output=True, text=True
            ).stdout.split()
            if got[-1:] != [str(maxmemory or 0)]:
                raise RuntimeError(f"redis on {port} has maxmemory {got[-1:]}, expected {maxmemory or 0}")
            return f"redis://localhost:{port}/0"
        time.sleep(0.05)
    raise RuntimeError(f"redis on {port} did not start")


def stop_redis(port: int) -> None:
    if port == 6379:
        raise ValueError("port 6379 is the shared Redis; never touch it")
    subprocess.run(["redis-cli", "-p", str(port), "shutdown", "nosave"], capture_output=True)


def redis_info(port: int) -> dict[str, str]:
    out = subprocess.run(["redis-cli", "-p", str(port), "info"], capture_output=True, text=True).stdout
    info = {}
    for line in out.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            info[k] = v.strip()
    return info


# Calibration (9.1): synthetic tones against real speech's Opus bytes per second.


def calibrate() -> dict[str, float]:
    from tts_cache import codec

    pcm = synthetic_pcm("x" * 61, SR)  # 61 characters at 66 ms each: about 4 s
    seconds = len(pcm) / 2 / SR
    nbytes = len(codec.encode(pcm, SR))
    per_4s = nbytes / seconds * 4
    ratio = REAL_BYTES_PER_4S / per_4s
    factor = 1.0 if 0.8 <= ratio <= 1.2 else ratio
    return {"synthetic_bytes_per_4s": round(per_4s, 1), "ratio": round(ratio, 3), "length_factor": factor}


class LengthProvider(SimulatedProvider):
    """Audio that depends only on the text's length, so each length encodes once per process."""

    ms_per_char: float = 66.0

    async def __call__(self, text: str) -> Any:
        from tts_cache.cache import Audio

        self.calls += 1
        self.chars += len(text.strip())
        self.last_ttfb_ms = 0.0
        return Audio(synthetic_pcm("x" * max(1, len(text.strip())), self.sample_rate, self.ms_per_char), SR)


# 9.1 Store capacity.


def _stream(lang: str, s: float, seed: int, n: int) -> Any:
    return generate(n, 0.5, seed=seed, language=lang, zipf_s=s)


async def capacity_redis(
    lang: str, s: float, seed: int, port: int, frac: float, peak: int, n: int, factor: float
) -> dict[str, Any]:
    maxmemory = None if frac >= 1.0 else max(1 << 20, int(frac * peak))
    url = start_redis(port, maxmemory)
    try:
        from tts_cache.stores.redis import RedisStore

        base = int(redis_info(port).get("used_memory", 0))
        provider = LengthProvider(seed=seed)
        provider.ms_per_char = 66.0 * factor
        corpus = _stream(lang, s, seed, n)
        with tempfile.TemporaryDirectory() as tmp:
            run = await run_core(
                corpus,
                0.5,
                cache_on=True,
                profile="single",
                provider=provider,
                workdir=Path(tmp),
                language=f"{lang}-IN",
                levels="A",
                warm_start_bytes=0,
                warm_texts=0,
                session_bytes=0,
                shared_factory=lambda: RedisStore(url),
            )
        info = redis_info(port)
        chars = sum(x.chars for x in run.samples)
        hit_chars = sum(x.chars for x in run.samples if x.hit)
        return {
            "arm": "redis",
            "lang": lang,
            "s": s,
            "seed": seed,
            "frac": frac,
            "maxmemory": maxmemory,
            "char_hit_rate": hit_chars / chars,
            "hits": sum(1 for x in run.samples if x.hit),
            "sentences": len(run.samples),
            "evicted_keys": int(info.get("evicted_keys", 0)),
            "peak_bytes": int(info.get("used_memory_peak", 0)) - base,
            "stored": run.stored,
            "stored_bytes": run.stored_bytes,
            "fail_open": run.counts.get("fail_open", 0),
            "errors": 0,
        }
    finally:
        stop_redis(port)


async def capacity_sqlite(
    lang: str, s: float, seed: int, frac: float, peak: int, n: int, factor: float
) -> dict[str, Any]:
    provider = LengthProvider(seed=seed)
    provider.ms_per_char = 66.0 * factor
    corpus = _stream(lang, s, seed, n)
    max_bytes = None if frac >= 1.0 else int(frac * peak)
    sessions = [t // 5 for t in range(len(corpus.turns))]
    with tempfile.TemporaryDirectory() as tmp:
        run = await run_core(
            corpus,
            0.5,
            cache_on=True,
            profile="sessions",
            provider=provider,
            workdir=Path(tmp),
            language=f"{lang}-IN",
            levels="A",
            warm_start_bytes=0,
            warm_texts=0,
            session_bytes=0,
            sqlite_max_bytes=max_bytes,
            sweep_after_session=True,
            session_of_turn=sessions,
        )
    chars = sum(x.chars for x in run.samples)
    hit_chars = sum(x.chars for x in run.samples if x.hit)
    return {
        "arm": "sqlite",
        "lang": lang,
        "s": s,
        "seed": seed,
        "frac": frac,
        "max_bytes": max_bytes,
        "char_hit_rate": hit_chars / chars,
        "hits": sum(1 for x in run.samples if x.hit),
        "sentences": len(run.samples),
        "stored": run.stored,
        "stored_bytes": run.stored_bytes,
        "peak_bytes": run.stored_bytes,
        "fail_open": run.counts.get("fail_open", 0),
    }


def _cap_task(kind: str, args: tuple[Any, ...]) -> dict[str, Any]:
    reuse_encoded()
    if kind == "redis":
        return asyncio.run(capacity_redis(*args))
    return asyncio.run(capacity_sqlite(*args))


STREAMS = (("en", 0.8), ("en", 1.0), ("en", 1.2), ("hi", 1.0), ("ta", 1.0))


def run_capacity(
    out: Path, workers: int = 8, n: int = 20_000, seeds: tuple[int, ...] = (1, 2, 3)
) -> list[dict[str, Any]]:
    out.mkdir(parents=True, exist_ok=True)
    cal = calibrate()
    factor = cal["length_factor"]
    (out / "calibration.json").write_text(json.dumps(cal, indent=1))
    ctx = mp.get_context("spawn")
    results: list[dict[str, Any]] = []
    port = CAPACITY_PORTS
    # The unbounded runs first: their peak sets every capacity.
    with ProcessPoolExecutor(workers, mp_context=ctx) as ex:
        futs = {}
        for lang, s in STREAMS:
            for seed in seeds:
                futs[ex.submit(_cap_task, "redis", (lang, s, seed, port, 1.0, 0, n, factor))] = (
                    lang,
                    s,
                    seed,
                )
                port += 1
        futs_sql = {
            ex.submit(_cap_task, "sqlite", ("en", 1.0, seed, 1.0, 0, n, factor)): ("en", 1.0, seed)
            for seed in seeds
        }
        peaks: dict[tuple[Any, ...], int] = {}
        for f in as_completed(futs):
            r = f.result()
            peaks[("redis", *futs[f])] = r["peak_bytes"]
            results.append(r)
        for f in as_completed(futs_sql):
            r = f.result()
            peaks[("sqlite", *futs_sql[f])] = r["stored_bytes"]
            results.append(r)
        futs2 = []
        for (arm, lang, s, seed), peak in peaks.items():
            for frac in CAPACITY_FRACTIONS[:-1]:
                if arm == "redis":
                    futs2.append(ex.submit(_cap_task, "redis", (lang, s, seed, port, frac, peak, n, factor)))
                    port += 1
                else:
                    futs2.append(ex.submit(_cap_task, "sqlite", (lang, s, seed, frac, peak, n, factor)))
        for f in as_completed(futs2):
            results.append(f.result())
    with open(out / "capacity.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")
    return results


def pressure_cells(
    langs: tuple[str, ...] = ("en", "hi", "ta"), seeds: tuple[int, ...] = tuple(range(1, 11))
) -> list[Any]:
    """Labelled corpora at fractions of their own SQLite peak, every configuration (9.1, last item)."""
    from tts_cache.testplan.offline import Cell

    return [
        Cell(lang, config, 0.5, seed, profile="sessions")
        for lang in langs
        if pool_path(lang).exists()
        for seed in seeds
        for config in ("A", "A+B", "A+B+C")
    ]


def run_pressure(
    out: Path, workers: int = 8, langs: tuple[str, ...] = ("en", "hi", "ta")
) -> list[dict[str, Any]]:
    from tts_cache.testplan.offline import run_cells

    cells = pressure_cells(langs)
    base = run_cells(cells, out / "pressure_full", workers, knobs={"sweep_after_session": True})
    peak = {r["id"]: r.get("stored_bytes", 0) for r in base if "error" not in r}
    from dataclasses import replace

    capped, per = [], {}
    for frac in PRESSURE_FRACTIONS[:-1]:
        for c in cells:
            cc = replace(c, tag=f"cap{frac}")
            capped.append(cc)
            per[cc.id] = {"sqlite_max_bytes": max(1, int(frac * peak.get(c.id, 0)))}
    return list(base) + run_cells(
        capped, out / "pressure_capped", workers, knobs={"sweep_after_session": True}, cell_knobs=per
    )


# 9.2 Process memory.


async def memory_run(lang: str, seed: int, port: int, wsb: int, wt: int, fill_n: int) -> dict[str, Any]:
    from tts_cache.stores.redis import RedisStore
    from tts_cache.testplan.offline import corpus_for, session_of_turn

    url = start_redis(port)
    try:
        scope = bench_scope(f"{lang}-IN")
        store = RedisStore(url)
        texts, scores = fill_stream(lang, fill_n, seed)
        filled = await fill(store, scope, len(texts), "real", texts=texts, scores=scores)
        await store.close()
        corpus = corpus_for(lang, 0.25, seed)
        rss0 = rss_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            run = await run_core(
                corpus,
                0.25,
                cache_on=True,
                profile="serverless",
                provider=SimulatedProvider(seed=seed),
                workdir=Path(tmp),
                redis_url=url,
                language=f"{lang}-IN",
                levels="A+B+C",
                session_of_turn=session_of_turn(corpus),
                warm_start_bytes=wsb,
                warm_texts=wt,
                sample_rss=True,
            )
        hits = [x for x in run.samples if x.hit]
        tiers = Counter(x.tier for x in hits)
        levels = Counter(x.level for x in hits)
        l5: dict[str, list[float]] = {}
        for x in hits:
            l5.setdefault(x.tier or "?", []).append(x.lookup_ms + x.first_chunk_ms)
        first = [x for x in run.samples if x.first_in_turn]
        return {
            "lang": lang,
            "seed": seed,
            "warm_start_bytes": wsb,
            "warm_texts": wt,
            "fill": filled,
            "rss_before": rss0,
            "sessions": run.sessions,
            "tiers": dict(tiers),
            "levels": dict(levels),
            "l5_p50": {k: statistics.median(v) for k, v in l5.items()},
            "first_turn_hits": sum(1 for x in first if x.hit),
            "turns": len(first),
            "first_turn": [
                {"hit": x.hit, "ms": x.lookup_ms + x.first_chunk_ms, "tier": x.tier} for x in first
            ],
            "char_hit_rate": sum(x.chars for x in hits) / sum(x.chars for x in run.samples),
            "miss_lookup_ms": [x.lookup_ms for x in run.samples if not x.hit],
        }
    finally:
        stop_redis(port)


def fill_stream(lang: str, n: int, seed: int) -> tuple[list[str], list[float]]:
    """The distinct sentences of an ``n``-sentence stream and their hit counts, hottest first."""
    if lang in ("en", "hi", "ta"):
        corpus = generate(n, 0.5, seed=seed + 100, language=lang)
        counts = Counter(t for turn in corpus.turns for t in turn)
    else:
        pool = load_pool(pool_path(lang))
        words = [w for b in pool.bases for w in b.text.split()]
        distinct = word_salad(words, n // 2, seed=seed + 100, lang=lang)
        rng = random.Random(seed)
        weights = [1.0 / (i + 1) for i in range(len(distinct))]
        counts = Counter({t: 1 for t in distinct})
        for t in rng.choices(distinct, weights, k=n - len(distinct)):
            counts[t] += 1
    texts = [t for t, _ in counts.most_common()]
    # A capture counts one use; repeats add the rest.
    return texts, [float(counts[t] - 1) for t in texts]


def _mem_task(args: tuple[Any, ...]) -> dict[str, Any]:
    reuse_encoded()
    from tts_cache.match.semantic import load_model

    load_model()
    return asyncio.run(memory_run(*args))


def run_memory(
    out: Path,
    workers: int = 8,
    langs: tuple[str, ...] = ("en", "hi", "ta"),
    seeds: tuple[int, ...] = (1, 2, 3),
    fill_n: int = 20_000,
) -> list[dict[str, Any]]:
    out.mkdir(parents=True, exist_ok=True)
    settings = sorted({(mb << 20, 2000) for mb in WARM_BYTES_MB} | {(4 << 20, wt) for wt in WARM_TEXTS})
    ctx = mp.get_context("spawn")
    port = MEMORY_PORTS
    results = []
    with ProcessPoolExecutor(workers, mp_context=ctx) as ex:
        futs = []
        for lang in langs:
            if not pool_path(lang).exists():
                continue
            for seed in seeds:
                for wsb, wt in settings:
                    futs.append(ex.submit(_mem_task, (lang, seed, port, wsb, wt, fill_n)))
                    port += 1
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"error": f"{type(e).__name__}: {e}"})
    with open(out / "memory.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")
    return results


# 9.3 Population.


def population_texts(
    lang: str, n: int, seed: int = 1
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """``(text, base id, kind)`` to store and ``(text, base id, label)`` to probe, for ``n`` entries.

    Stored: every base sentence, slot expansions with each slot's first two
    alternatives, the meaning change of every even-numbered base, then filler
    sentences. Probed: each base's paraphrase, a comma, filler and end
    punctuation variant, a slot change with the third alternative (never
    stored), the meaning change of every odd-numbered base, and the ``?``
    variant.
    """
    pool = load_pool(pool_path(lang))
    store: list[tuple[str, str, str]] = [(b.text, b.id, "base") for b in pool.bases]
    expansions = [
        (b.text.replace(s.span, a, 1), b.id, "slot") for b in pool.bases for s in b.slots for a in s.alts[:2]
    ]
    negations = [(b.meaning_change, b.id, "meaning") for i, b in enumerate(pool.bases) if i % 2 == 0]
    store += negations + expansions
    probes: list[tuple[str, str, str]] = []
    for i, b in enumerate(pool.bases):
        probes.append((b.paraphrase, b.id, "paraphrase"))
        inner = punct_inner_variants(b.text)
        if inner:
            probes.append((inner[0], b.id, "punct_inner"))
        fv = filler_variants(b.text, lang)
        if fv:
            probes.append((fv[0], b.id, "filler"))
        for s in b.slots[:1]:
            probes.append((b.text.replace(s.span, s.alts[2], 1), b.id, "slot_change"))
        if i % 2 == 1:
            probes.append((b.meaning_change, b.id, "meaning_change"))
        q = punct_question_variant(b.text, lang)
        if q:
            probes.append((q, b.id, "punct_question"))
    store = store[:n]
    if len(store) < n:
        words = [w for b in pool.bases for w in b.text.split()]
        rest = word_salad(words, n - len(store), seed=seed, lang=lang)
        store += [(t, "", "salad") for t in rest]
    return store, probes


async def population_run(lang: str, n: int, port: int) -> dict[str, Any]:
    from tts_cache.bench import _TimedMatcher, config_policy
    from tts_cache.cache import TTSCache
    from tts_cache.canonical import canonicalise
    from tts_cache.key import build_key
    from tts_cache.metrics import LogSink
    from tts_cache.stores.memory import MemoryStore
    from tts_cache.stores.sqlite import SQLiteStore
    from tts_cache.stores.tiered import TieredStore
    from tts_cache.testplan.oracle import ALLOWED

    scope = bench_scope(f"{lang}-IN")
    stored, probes = population_texts(lang, n)
    by_key = {build_key(scope, canonicalise(t, lang)): (bid, kind) for t, bid, kind in stored}
    out: dict[str, Any] = {"lang": lang, "n": n}
    with tempfile.TemporaryDirectory() as tmp:
        # Levels B and C: the pool's neighbours in one scope, all of them indexed (warm_texts = N).
        sql = SQLiteStore(Path(tmp) / "pop.db", sweep_interval_s=0)
        scores = [
            3.0 if kind == "base" else 2.0 if kind in ("slot", "meaning") else 1.0 for _, _, kind in stored
        ]
        out["fill_bc"] = await fill(
            sql, scope, len(stored), "texts", texts=[t for t, _, _ in stored], scores=scores, value_bytes=480
        )
        policy, matcher = config_policy("A+B+C")
        timed = _TimedMatcher(matcher)
        cache = TTSCache(
            scope,
            TieredStore(MemoryStore(), sql, deadline_ms=20),
            policy=policy,
            matcher=timed,
            metrics=LogSink(log=None),
            warm_texts=n,
        )
        rss0 = peak_rss()
        t0 = time.perf_counter()
        await cache.start()
        out["start_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["index_build_ms"] = round(timed.refresh_ms, 1)
        out["index_peak_rss_growth"] = peak_rss() - rss0
        out["rss"] = rss_bytes()
        near, sem = matcher.near, matcher.semantic
        results = []
        b_ms, c_ms, lookup_ms = [], [], []
        for text, bid, label in probes:
            canon = canonicalise(text, lang)
            t = time.perf_counter()
            near.probe(scope, canon, text)
            b_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            sem.probe(scope, canon, text)
            c_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            found = await cache.probe(text)
            lookup_ms.append((time.perf_counter() - t) * 1000)
            await _settle(cache)
            hit = found.hit
            served = by_key.get(hit.key) if hit is not None else None
            wrong = hit is not None and (
                label not in ALLOWED["A+B+C"] or served is None or served[0] != bid or served[1] != "base"
            )
            results.append(
                {
                    "label": label,
                    "hit": hit is not None,
                    "level": hit.level if hit else None,
                    "wrong": wrong,
                    # A serve of a slot expansion's or a negation's audio is as harmful as serving the decoy.
                    "critical": hit is not None
                    and (
                        label in ("slot_change", "meaning_change")
                        or (served or ("", ""))[1] in ("slot", "meaning")
                    ),
                    "served": served,
                    "text": text,
                }
            )
        await cache.close()
        out["probes"] = len(results)
        out["recall"] = {
            lab: sum(1 for r in results if r["label"] == lab and r["hit"] and not r["wrong"])
            / max(1, sum(1 for r in results if r["label"] == lab))
            for lab in ("paraphrase", "punct_inner", "filler")
        }
        out["wrong"] = Counter(r["label"] for r in results if r["wrong"])
        out["critical"] = sum(1 for r in results if r["critical"])
        out["critical_examples"] = [
            {k: r[k] for k in ("label", "text", "served", "level")} for r in results if r["critical"]
        ][:20]
        out["b_probe_ms"] = _pct(b_ms)
        out["c_probe_ms"] = _pct(c_ms)
        out["lookup_ms"] = _pct(lookup_ms)
        # Level A: distinct 1 KB clips (real size at 10,000) in Redis and SQLite; lookups by tier.
        if n in (1_000, 10_000, 100_000):
            out["a"] = await population_a(lang, n, port, Path(tmp), real=False)
            if n == 10_000:
                out["a_real"] = await population_a(lang, n, port, Path(tmp), real=True)
    return out


async def population_a(lang: str, n: int, port: int, tmp: Path, *, real: bool) -> dict[str, Any]:
    from tts_cache.bench import config_policy
    from tts_cache.cache import TTSCache
    from tts_cache.metrics import LogSink
    from tts_cache.stores.memory import MemoryStore
    from tts_cache.stores.redis import RedisStore
    from tts_cache.stores.sqlite import SQLiteStore
    from tts_cache.stores.tiered import TieredStore

    scope = bench_scope(f"{lang}-IN")
    texts = [f"Filler entry number {i} of {n}." for i in range(n)]
    rng = random.Random(n)
    queries = rng.sample(texts, min(2000, n))
    out: dict[str, Any] = {"real": real}
    url = start_redis(port)
    try:
        base_mem = int(redis_info(port)["used_memory"])
        for arm in ("redis", "sqlite"):
            shared: Any = (
                RedisStore(url)
                if arm == "redis"
                else SQLiteStore(tmp / f"a-{n}-{real}.db", sweep_interval_s=0)
            )
            kind = "real" if real else "texts"
            f = await fill(shared, scope, n, kind, texts=texts, value_bytes=1024)
            if arm == "redis":
                out["redis_bytes_per_entry"] = (int(redis_info(port)["used_memory"]) - base_mem) / n
            by_tier: dict[str, list[float]] = {}
            # Warm: the default process memory, so the warm set serves from memory. Cold: no process
            # memory, so every hit comes from the shared store.
            for warm in (True, False):
                policy, matcher = config_policy("A")
                memory = MemoryStore() if warm else MemoryStore(warm_start_bytes=0, session_bytes=0)
                cache = TTSCache(
                    scope,
                    TieredStore(memory, shared, deadline_ms=20),
                    policy=policy,
                    matcher=matcher,
                    metrics=LogSink(log=None),
                    warm_start_bytes=4 << 20 if warm else 0,
                    warm_texts=2000 if warm else 0,
                )
                await cache.start()
                for q in queries:
                    t = time.perf_counter()
                    found = await cache.probe(q)
                    ms = (time.perf_counter() - t) * 1000
                    await _settle(cache)
                    tier = found.hit.meta.get("tier") if found.hit else "miss"
                    by_tier.setdefault(str(tier), []).append(ms)
                await cache.drain(10.0)
                if not warm:  # closing the warm cache would close the shared store too
                    await cache.close()
            out[arm] = {"fill": f, "lookup_ms": {k: _pct(v) for k, v in by_tier.items()}}
    finally:
        stop_redis(port)
    return out


async def _settle(cache: Any) -> None:
    """Let a hit's background touch finish before the next probe, as playback time would."""
    pending = list(getattr(cache, "_background", ()))
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def peak_rss() -> int:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _pct(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {}
    s = sorted(xs)

    def q(p: float) -> float:
        return round(s[min(len(s) - 1, int(p * len(s)))], 4)

    return {"n": len(s), "p50": q(0.5), "p99": q(0.99), "p999": q(0.999), "max": round(s[-1], 4)}


def _pop_task(args: tuple[Any, ...]) -> dict[str, Any]:
    from tts_cache.match.semantic import load_model

    load_model()
    r = asyncio.run(population_run(*args))
    r["wrong"] = dict(r["wrong"])
    return r


def run_population(
    out: Path, workers: int = 8, langs: tuple[str, ...] = POOL_LANGUAGES, sizes: tuple[int, ...] = POPULATION
) -> list[dict[str, Any]]:
    out.mkdir(parents=True, exist_ok=True)
    ctx = mp.get_context("spawn")
    port = POPULATION_PORTS
    results = []
    with ProcessPoolExecutor(workers, mp_context=ctx) as ex:
        futs = []
        for lang in langs:
            if not pool_path(lang).exists():
                continue
            for n in sizes:
                futs.append(ex.submit(_pop_task, (lang, n, port)))
                port += 1
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"error": f"{type(e).__name__}: {e}"})
    with open(out / "population.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sweep", choices=["capacity", "pressure", "memory", "population", "calibrate"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--langs", default=None)
    ap.add_argument("--quick", action="store_true", help="small sizes, for the rehearsal")
    args = ap.parse_args(argv)
    t0 = time.time()
    langs = tuple(args.langs.split(",")) if args.langs else None
    if args.sweep == "calibrate":
        print(json.dumps(calibrate()))
        return 0
    if args.sweep == "capacity":
        res = run_capacity(
            args.out, args.workers, n=2000 if args.quick else 20_000, seeds=(1,) if args.quick else (1, 2, 3)
        )
    elif args.sweep == "pressure":
        res = run_pressure(args.out, args.workers, langs or ("en", "hi", "ta"))
    elif args.sweep == "memory":
        res = run_memory(
            args.out,
            args.workers,
            langs or ("en", "hi", "ta"),
            seeds=(1,) if args.quick else (1, 2, 3),
            fill_n=2000 if args.quick else 20_000,
        )
    else:
        res = run_population(
            args.out,
            args.workers,
            langs or POOL_LANGUAGES,
            sizes=(1_000, 2_000) if args.quick else POPULATION,
        )
    errors = [r for r in res if "error" in r]
    print(
        json.dumps(
            {
                "sweep": args.sweep,
                "results": len(res),
                "errors": len(errors),
                "wall_s": round(time.time() - t0, 1),
            }
        )
    )
    for e in errors[:5]:
        print(e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
