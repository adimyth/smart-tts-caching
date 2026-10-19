"""The offline usability tests of TEST_PLAN.md section 10, and V5.

- U3, footprint: install size per extra in a fresh venv, import times, the
  Level C model's download size and load time, and process memory and
  serverless start time per configuration.
- U5, the store failing: a 200-sentence serverless run per fault injected into
  its own Redis (port 7100 and up).
- U6, observability: hit rate, saved characters and rejects from the cache's
  emitted metric log alone, against the oracle.
- U4 and V5: ``tts_cache.replay`` over a corpus's log against the measured
  hit rate, live (U4) and offline (V5).

U2 comes from the matrix and is computed in ``analysis``; U8 needs Pipecat and
runs in ``harness/live_bench.py u8``. Each test writes
``<out>/usability/<name>.json``.
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from tts_cache.bench import SimulatedProvider, bench_scope, config_policy, reuse_encoded, rss_bytes, run_core
from tts_cache.testplan.oracle import annotate, summary
from tts_cache.testplan.stats import pct

REPO = Path(__file__).resolve().parents[5]
PACKAGE = REPO / "packages" / "tts-cache"
U5_PORT = 7100
U5_LANG, U5_R, U5_SEED, U5_CONFIG = "en", 0.25, 1, "A+B+C"
FAULT_AT = 60  # the sentence at which each fault starts: enough is stored by then to hit
PACE_S = 0.1  # between sentences, so every fault window has sentences before and after it
DEADLINE_MS = 20.0
FAULTS = ("kill_restart", "client_pause", "debug_sleep", "noeviction_full", "corrupt_entry")
DEGRADED = ("deadline", "fail_open", "breaker")
EXTRAS = ("", "redis", "similarity", "redis,similarity")


def _write(out: Path, name: str, data: Any) -> Path:
    d = out / "usability"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return p


def _cli(port: int, *args: str) -> str:
    if port == 6379:
        raise ValueError("port 6379 is the shared Redis; never touch it")
    return subprocess.run(
        ["redis-cli", "-p", str(port), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


# ---------------------------------------------------------------------------
# U3, footprint.
# ---------------------------------------------------------------------------


def _du(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def install_sizes(tmp: Path) -> dict[str, Any]:
    """Site-packages size of a fresh venv with the package and each extra, against an empty venv."""
    out: dict[str, Any] = {}
    empty = tmp / "venv-empty"
    subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(empty)], check=True)
    base = _du(empty / "lib")
    for extra in EXTRAS:
        name = extra or "core"
        venv = tmp / f"venv-{name.replace(',', '-')}"
        subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(venv)], check=True)
        spec = f"{PACKAGE}[{extra}]" if extra else str(PACKAGE)
        t = time.perf_counter()
        res = subprocess.run(
            ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), spec],
            capture_output=True,
            text=True,
            check=False,
        )
        out[name] = {
            "ok": res.returncode == 0,
            "bytes": _du(venv / "lib") - base if res.returncode == 0 else None,
            "install_s": round(time.perf_counter() - t, 1),
            "error": res.stderr[-400:] if res.returncode else None,
        }
    return out


def import_times(n: int = 5) -> dict[str, Any]:
    """Wall-clock of ``import <module>`` in a fresh interpreter, median of ``n``."""
    out = {}
    for mod in ("tts_cache", "tts_cache_pipecat", "tts_cache.match.semantic"):
        ts = []
        for _ in range(n):
            res = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"import time; t = time.perf_counter(); import {mod}; print(time.perf_counter() - t)",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                ts.append(float(res.stdout.strip().splitlines()[-1]) * 1000)
        out[mod] = {"ms_p50": round(statistics.median(ts), 1) if ts else None, "n": len(ts)}
    return out


def model_footprint() -> dict[str, Any]:
    """The encoder's files on disk and its load time in a fresh interpreter (from the local cache)."""
    from huggingface_hub import hf_hub_download

    from tts_cache.match.semantic import DEFAULT_MODEL

    files = {f: Path(hf_hub_download(DEFAULT_MODEL, f)) for f in ("onnx/model.onnx", "onnx/tokenizer.json")}
    code = (
        "import time; t = time.perf_counter(); "
        "from tts_cache.match.semantic import load_model; m = load_model(); "
        "t1 = time.perf_counter(); m.encode(['warm up']); "
        "print(t1 - t, time.perf_counter() - t1)"
    )
    loads = []
    for _ in range(3):
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            a, b = res.stdout.strip().splitlines()[-1].split()
            loads.append((float(a) * 1000, float(b) * 1000))
    return {
        "model": DEFAULT_MODEL,
        "download_bytes": sum(p.stat().st_size for p in files.values()),
        "files": {k: p.stat().st_size for k, p in files.items()},
        "load_ms_p50": round(statistics.median(x for x, _ in loads), 1) if loads else None,
        "first_encode_ms_p50": round(statistics.median(y for _, y in loads), 1) if loads else None,
    }


async def config_footprint(config: str, port: int) -> dict[str, Any]:
    """Process memory and serverless start time of one configuration, in this (fresh) process."""
    from tts_cache.testplan.offline import corpus_for, session_of_turn

    rss0 = rss_bytes()
    corpus = corpus_for(U5_LANG, U5_R, U5_SEED)
    _cli(port, "flushall")
    with tempfile.TemporaryDirectory() as tmp:
        run = await run_core(
            corpus,
            U5_R,
            cache_on=True,
            profile="serverless",
            provider=SimulatedProvider(seed=U5_SEED),
            workdir=Path(tmp),
            redis_url=f"redis://localhost:{port}/0",
            language=f"{U5_LANG}-IN",
            levels=config,
            session_of_turn=session_of_turn(corpus),
            sample_rss=True,
        )
    starts = [s["start_ms"] for s in run.sessions]
    rss = [s["rss"] for s in run.sessions if s["rss"]]
    return {
        "config": config,
        "rss_before_mb": round(rss0 / 2**20, 1),
        "rss_max_mb": round(max(rss) / 2**20, 1) if rss else None,
        "first_start_ms": round(starts[0], 1) if starts else None,
        "start_ms_p50": pct(starts[1:], 0.5),
        "start_ms_max": max(starts[1:]) if len(starts) > 1 else None,
        "sessions": len(starts),
    }


def u3(out: Path, *, installs: bool = True) -> dict[str, Any]:
    from tts_cache.testplan.sizes import start_redis, stop_redis

    res: dict[str, Any] = {"import": import_times(), "model": model_footprint()}
    port = U5_PORT + 10
    start_redis(port)
    try:
        per = {}
        for config in ("A", "A+B", "A+B+C"):
            p = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tts_cache.testplan.usability",
                    "u3-config",
                    config,
                    "--port",
                    str(port),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            per[config] = (
                json.loads(p.stdout.strip().splitlines()[-1])
                if p.returncode == 0
                else {"error": p.stderr[-400:]}
            )
        res["configs"] = per
    finally:
        stop_redis(port)
    if installs:
        with tempfile.TemporaryDirectory() as tmp:
            res["install"] = install_sizes(Path(tmp))
    _write(out, "u3", res)
    return res


# ---------------------------------------------------------------------------
# U5, the store failing.
# ---------------------------------------------------------------------------


def _audio_keys(port: int) -> list[str]:
    keys = _cli(port, "--scan", "--pattern", "tts:*").splitlines()
    skip = ("tts:meta:", "tts:hot:", "tts:tag:", "tts:gen:", "tts:split:")
    return [k for k in keys if k and not k.startswith(skip)]


async def _inject(fault: str, port: int, window: dict[str, float], done: asyncio.Event) -> None:
    from tts_cache.testplan.sizes import start_redis, stop_redis

    window["start"] = time.perf_counter()
    if fault == "kill_restart":
        await asyncio.to_thread(stop_redis, port)
        await asyncio.sleep(3.0)
        await asyncio.to_thread(start_redis, port, None, "allkeys-lfu", ("--enable-debug-command", "yes"))
    elif fault == "client_pause":
        await asyncio.to_thread(_cli, port, "client", "pause", "5000", "ALL")
        await asyncio.sleep(5.0)
    elif fault == "debug_sleep":
        for _ in range(5):
            # DEBUG SLEEP blocks the server, so it runs on its own connection in a thread.
            await asyncio.to_thread(_cli, port, "debug", "sleep", "1")
            await asyncio.sleep(1.0)
    elif fault == "noeviction_full":
        used = int(_cli(port, "info", "memory").split("used_memory:")[1].split()[0])
        await asyncio.to_thread(_cli, port, "config", "set", "maxmemory-policy", "noeviction")
        await asyncio.to_thread(_cli, port, "config", "set", "maxmemory", str(used))
        await asyncio.sleep(5.0)
        await asyncio.to_thread(_cli, port, "config", "set", "maxmemory", "0")
    elif fault == "corrupt_entry":
        keys = await asyncio.to_thread(_audio_keys, port)
        for k in keys:
            await asyncio.to_thread(_cli, port, "set", k, "not opus at all")
        window["corrupted"] = len(keys)
    window["end"] = time.perf_counter()
    done.set()


async def u5_run(fault: str, port: int) -> dict[str, Any]:
    """One 200-sentence serverless run with ``fault`` injected at sentence FAULT_AT."""
    from tts_cache.cache import TTSCache
    from tts_cache.metrics import LogSink
    from tts_cache.stores.memory import MemoryStore
    from tts_cache.stores.redis import RedisStore
    from tts_cache.stores.tiered import TieredStore
    from tts_cache.testplan.offline import corpus_for, session_of_turn
    from tts_cache.testplan.sizes import start_redis, stop_redis

    stop_redis(port)
    start_redis(port, None, "allkeys-lfu", ("--enable-debug-command", "yes"))
    url = f"redis://localhost:{port}/0"
    corpus = corpus_for(U5_LANG, U5_R, U5_SEED)
    sess_of = session_of_turn(corpus)
    provider = SimulatedProvider(seed=U5_SEED)
    sink = LogSink(log=None)
    host_errors: list[str] = []
    rows: list[dict[str, Any]] = []
    window: dict[str, float] = {}
    done = asyncio.Event()
    injector: asyncio.Task[None] | None = None
    cache: Any = None
    current: int | None = None

    def make() -> Any:
        policy, matcher = config_policy(U5_CONFIG)
        store = TieredStore(MemoryStore(), RedisStore(url), deadline_ms=DEADLINE_MS)
        return TTSCache(bench_scope(f"{U5_LANG}-IN"), store, policy=policy, matcher=matcher, metrics=sink)

    async def guarded(what: str, coro: Any) -> Any:
        try:
            return await coro
        except Exception as e:  # anything the cache lets through reaches the host
            host_errors.append(f"{what}: {type(e).__name__}: {e}")
            return None

    index = 0
    try:
        for ti, turn in enumerate(corpus.turns):
            if sess_of[ti] != current:
                if cache is not None:
                    await guarded("close", cache.close())
                cache = make()
                await guarded("start", cache.start())
                current = sess_of[ti]
            assert cache is not None
            for text in turn:
                if index == FAULT_AT and injector is None:
                    injector = asyncio.create_task(_inject(fault, port, window, done))
                    await asyncio.sleep(0)
                before = {r: sink.counts[f"miss:{r}"] for r in DEGRADED}
                t = time.perf_counter()
                found = await guarded("probe", cache.probe(text))
                lookup_ms = (time.perf_counter() - t) * 1000
                degraded = next((r for r in DEGRADED if sink.counts[f"miss:{r}"] > before[r]), None)
                row = {
                    "index": index,
                    "t": t,
                    "lookup_ms": lookup_ms,
                    "hit": False,
                    "tier": None,
                    "degraded": degraded,
                    "chars": len(text.strip()),
                    "decode_ms": None,
                }
                if found is not None and found.hit is not None:
                    t1 = time.perf_counter()
                    try:
                        # The first decoded chunk is what the caller waits for; the rest
                        # decodes ahead of playback, off the critical path (SOLUTION.md 5).
                        stream = iter(found.hit.pcm_stream())
                        first = next(stream, b"")
                        row["decode_ms"] = (time.perf_counter() - t1) * 1000
                        pcm = first + b"".join(stream)
                        row.update(hit=bool(pcm), tier=found.hit.meta.get("tier"))
                    except Exception as e:
                        host_errors.append(f"pcm_stream: {type(e).__name__}: {e}")
                if not row["hit"]:
                    audio = await provider(text)
                    await guarded(
                        "capture",
                        cache.capture(text, audio.pcm, audio.sample_rate, key=found.key if found else None),
                    )
                rows.append(row)
                index += 1
                await asyncio.sleep(PACE_S)
            if cache is not None:
                await guarded("drain", cache.drain(10.0))
        if injector is not None:
            await injector
    finally:
        if cache is not None:
            await guarded("close", cache.close())
        stop_redis(port)
    return _u5_summary(fault, rows, window, host_errors)


def _u5_summary(
    fault: str, rows: list[dict[str, Any]], window: dict[str, float], host_errors: list[str]
) -> dict[str, Any]:
    t0, t1 = window.get("start", 0.0), window.get("end", 0.0)
    before = [r for r in rows if r["t"] < t0]
    during = [r for r in rows if t0 <= r["t"] <= t1]
    after = [r for r in rows if r["t"] > t1]
    base = pct([r["lookup_ms"] for r in before], 0.5) or 0.0
    lat = [r["lookup_ms"] for r in during]
    # Section 10's bound: the 20 ms deadline plus decode, the first chunk being the decode a
    # caller waits for. No further allowance.
    decode = max([r["decode_ms"] or 0.0 for r in rows] or [0.0])

    def rate(rs: list[dict[str, Any]]) -> float | None:
        c = sum(r["chars"] for r in rs)
        return sum(r["chars"] for r in rs if r["hit"]) / c if c else None

    healthy = next(
        (
            r
            for r in after
            if (r["hit"] and r["tier"] == "shared") or (not r["hit"] and r["degraded"] is None)
        ),
        None,
    )
    worst = max(lat) if lat else None
    return {
        "fault": fault,
        "sentences": len(rows),
        "host_errors": host_errors,
        "window_s": round(t1 - t0, 2),
        "during_n": len(during),
        "baseline_lookup_ms_p50": round(base, 3),
        "added_ms": {
            "p50": None if not lat else round((pct(lat, 0.5) or 0.0) - base, 3),
            "p99": None if not lat else round((pct(lat, 0.99) or 0.0) - base, 3),
            "max": None if worst is None else round(worst - base, 3),
        },
        "bound_ms": round(DEADLINE_MS + decode, 3),
        "within_deadline": worst is None or worst - base <= DEADLINE_MS + decode,
        "degraded": dict(Counter(r["degraded"] for r in rows if r["degraded"])),
        "hit_rate": {"before": rate(before), "during": rate(during), "after": rate(after)},
        "recovery_s": None if healthy is None else round(healthy["t"] - t1, 3),
        "corrupted": window.get("corrupted"),
        "pass": not host_errors,
    }


def u5(out: Path) -> dict[str, Any]:
    res = {}
    for i, fault in enumerate(FAULTS):
        res[fault] = asyncio.run(u5_run(fault, U5_PORT + i))
    res["pass"] = all(v["pass"] for v in res.values() if isinstance(v, dict))
    _write(out, "u5", res)
    return res


# ---------------------------------------------------------------------------
# U6, observability: the metric log alone.
# ---------------------------------------------------------------------------


def metrics_only(lines: list[str]) -> dict[str, Any]:
    """Hit rate, saved characters and rejects by reason from ``LogSink`` JSON lines, nothing else."""
    hit_chars = miss_chars = hits = misses = 0
    rejects: Counter[str] = Counter()
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        name = ev.get("event")
        if name == "hit":
            hits += 1
            hit_chars += int(ev.get("chars") or 0)
        elif name == "miss":
            misses += 1
            miss_chars += int(ev.get("chars") or 0)
        elif name == "reject":
            rejects[str(ev.get("reason"))] += 1
    total = hit_chars + miss_chars
    return {
        "hits": hits,
        "misses": misses,
        "saved_chars": hit_chars,
        "chars": total,
        "char_hit_rate": hit_chars / total if total else 0.0,
        "rejects": dict(rejects),
    }


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def u6_cell(lang: str, config: str, r: float, seed: int, port: int) -> dict[str, Any]:
    from tts_cache.testplan.offline import Cell, corpus_for, rows_of, session_of_turn

    cell = Cell(lang, config, r, seed)
    corpus = corpus_for(lang, r, seed)
    _cli(port, "flushall")
    log = logging.getLogger(f"tts_cache.u6.{lang}.{config}.{r}.{seed}")
    log.propagate = False
    log.setLevel(logging.INFO)
    handler = _Lines()
    log.addHandler(handler)
    with tempfile.TemporaryDirectory() as tmp:
        run = await run_core(
            corpus,
            r,
            cache_on=True,
            profile="serverless",
            provider=SimulatedProvider(seed=seed),
            workdir=Path(tmp),
            redis_url=f"redis://localhost:{port}/0",
            language=f"{lang}-IN",
            levels=config,
            session_of_turn=session_of_turn(corpus),
            metrics_log=log,
        )
    log.removeHandler(handler)
    rows = annotate(corpus, rows_of(run, corpus, cell), config, run.reject_keys)
    truth = summary(rows)
    rejects_truth = dict(Counter(run.reject_keys.values()))
    calc = metrics_only(handler.lines)
    ok = (
        calc["saved_chars"] == truth["hit_chars"]
        and calc["hits"] == truth["hits"]
        and calc["chars"] == truth["chars"]
        and all(calc["rejects"].get(k, 0) >= v for k, v in rejects_truth.items())
    )
    return {
        "cell": cell.id,
        "metrics": calc,
        "truth": {
            "hits": truth["hits"],
            "saved_chars": truth["hit_chars"],
            "chars": truth["chars"],
            "rejects": rejects_truth,
        },
        "log_lines": len(handler.lines),
        "pass": ok,
    }


def u6(out: Path, langs: list[str]) -> dict[str, Any]:
    from tts_cache.testplan.sizes import start_redis, stop_redis

    port = U5_PORT + 11
    start_redis(port)
    try:
        cells = [
            asyncio.run(u6_cell(lang, config, 0.25, 1, port))
            for lang in langs
            for config in ("A", "A+B", "A+B+C")
        ]
    finally:
        stop_redis(port)
    res = {"cells": cells, "pass": all(c["pass"] for c in cells)}
    _write(out, "u6", res)
    return res


# ---------------------------------------------------------------------------
# U4 and V5: replay against measured hit rates.
# ---------------------------------------------------------------------------


def replay_rate(lang: str, r: float, seed: int) -> float:
    from tts_cache.corpus import Event
    from tts_cache.replay import simulate
    from tts_cache.testplan.offline import corpus_for

    events = [
        Event(e["ts"], e["session"], e["text"], e["language"], e["scope"])
        for e in corpus_for(lang, r, seed).events()
    ]
    return simulate(events).hit_rate


def v5(out: Path, offline_dir: Path) -> dict[str, Any]:
    """Replay's character hit rate against the offline Level A rate, per language and r (M4: 2 points)."""
    from tts_cache.testplan.analysis import jsonl, latest

    runs = [
        x
        for x in latest(jsonl(offline_dir / "runs.jsonl"))
        if "error" not in x and x["config"] == "A" and x["mix"] == "labelled" and x["profile"] == "serverless"
    ]
    cells = []
    for x in sorted(runs, key=lambda x: (x["lang"], x["r"], x["seed"])):
        if x["r"] == 0.0:
            continue
        rep = replay_rate(x["lang"], x["r"], x["seed"])
        cells.append(
            {
                "lang": x["lang"],
                "r": x["r"],
                "seed": x["seed"],
                "replay": round(rep, 4),
                "bench": round(x["char_hit_rate"], 4),
                "diff_points": round(abs(rep - x["char_hit_rate"]) * 100, 2),
            }
        )
    worst = max((c["diff_points"] for c in cells), default=None)
    res = {"cells": cells, "max_diff_points": worst, "pass": None if worst is None else worst <= 2.0}
    _write(out, "v5", res)
    return res


def u4(out: Path, live_dir: Path) -> dict[str, Any]:
    """Replay over each live run's corpus against that run's measured character hit rate."""
    runs = []
    for p in sorted((live_dir / "rows").glob("*.jsonl")):
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        if not rows or rows[0]["config"] == "OFF":
            continue
        chars = sum(x["chars"] for x in rows)
        measured = sum(x["chars"] for x in rows if x["hit"]) / chars if chars else 0.0
        rep = replay_rate(rows[0]["lang"], rows[0]["r"], 1)
        runs.append(
            {
                "run": p.stem,
                "config": rows[0]["config"],
                "replay": round(rep, 4),
                "measured": round(measured, 4),
                "diff_points": round((rep - measured) * 100, 2),
            }
        )
    by: dict[str, list[float]] = {}
    for x in runs:
        by.setdefault(x["config"], []).append(x["diff_points"])
    res = {
        "runs": runs,
        "by_config": {
            k: {"mean": round(statistics.fmean(v), 2), "min": min(v), "max": max(v)} for k, v in by.items()
        },
        "note": "replay models Level A; its gap to A+B and A+B+C is what Levels B and C add",
    }
    _write(out, "u4", res)
    return res


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tts_cache.testplan.usability")
    ap.add_argument("test", choices=["u3", "u3-config", "u4", "u5", "u6", "v5", "all"])
    ap.add_argument("config", nargs="?")
    ap.add_argument("--out", type=Path, default=REPO / "harness" / "out" / "testplan" / "run")
    ap.add_argument("--langs", default="en,hi,ta")
    ap.add_argument("--port", type=int, default=U5_PORT + 10)
    ap.add_argument("--no-installs", action="store_true")
    a = ap.parse_args(argv)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    reuse_encoded()
    if a.test == "u3-config":
        print(json.dumps(asyncio.run(config_footprint(a.config, a.port))))
        return 0
    langs = a.langs.split(",")
    done = {}
    if a.test in ("u3", "all"):
        done["u3"] = u3(a.out, installs=not a.no_installs)
    if a.test in ("u5", "all"):
        done["u5"] = u5(a.out)["pass"]
    if a.test in ("u6", "all"):
        done["u6"] = u6(a.out, langs)["pass"]
    if a.test in ("v5", "all"):
        done["v5"] = v5(a.out, a.out / "offline")["pass"]
    if a.test in ("u4", "all") and (a.out / "live" / "rows").exists():
        done["u4"] = bool(u4(a.out, a.out / "live")["runs"])
    print(json.dumps({k: (v if not isinstance(v, dict) else "written") for k, v in done.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
