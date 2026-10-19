"""``tts-cache``: operational commands (SOLUTION.md 4.2a, 9.2, 9.3, 11.8)."""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

PLACEMENT_RULE_MS = 10.0


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


async def _ping(url: str, n: int, warmup: int) -> dict[str, float]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
    try:
        for _ in range(warmup):
            await client.ping()
        rtts = []
        for _ in range(n):
            t = time.perf_counter()
            await client.ping()
            rtts.append((time.perf_counter() - t) * 1000)
    finally:
        await client.aclose()
    return {
        "n": n,
        "p50_ms": statistics.median(rtts),
        "p99_ms": _percentile(rtts, 0.99),
        "max_ms": max(rtts),
    }


def cmd_ping(args: argparse.Namespace) -> int:
    """Round trip to the configured Redis, and whether it passes the 10 ms p99 placement rule (4.2a)."""
    try:
        r = asyncio.run(_ping(args.url, args.n, args.warmup))
    except Exception as e:
        print(f"ping {args.url}: FAIL, {type(e).__name__}: {e}")
        return 2
    ok = r["p99_ms"] < PLACEMENT_RULE_MS
    stats = f"n={r['n']} p50={r['p50_ms']:.3f} ms p99={r['p99_ms']:.3f} ms max={r['max_ms']:.3f} ms"
    print(f"ping {args.url}: {stats}")
    print(f"placement rule (p99 under {PLACEMENT_RULE_MS:g} ms): {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def parse_policy(spec: str) -> Any:
    """``default``, or ``+``-joined terms such as ``probation:48h,30d+minchars:3,400`` or ``always:30d``."""
    from tts_cache import policy as P

    if spec == "default":
        return P.DEFAULT_POLICY
    parts = []
    for term in spec.split("+"):
        name, _, arg = term.partition(":")
        args = [a for a in arg.split(",") if a]
        match name:
            case "always":
                parts.append(P.AlwaysCache(*args))
            case "probation":
                parts.append(P.Probation(*args))
            case "minchars":
                parts.append(P.MinChars(*(int(a) for a in args)))
            case "skip":
                parts.append(P.SkipIfMatches(args))
            case "shadow":
                parts.append(P.Shadow())
            case _:
                raise SystemExit(f"unknown policy term {name!r}")
    return parts[0] if len(parts) == 1 else P.AllOf(*parts)


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay an application's log through the production splitter, canonicaliser and policy (9.2)."""
    from tts_cache.corpus import read_log
    from tts_cache.replay import format_report, report

    events = read_log(args.log)
    r = report(events, parse_policy(args.policy), rules=not args.no_rules, capture_delay_s=args.capture_delay)
    print(format_report(r))
    if args.out:
        Path(args.out).write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report written to {args.out}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """The synthetic benchmark: cost and time to first audio, cache off and on, swept over r (9.3)."""
    from tts_cache.bench import format_table, level_b_comparison, save, sweep

    r_values = tuple(float(x) for x in args.r.split(","))
    if args.level_b:
        classes = frozenset(c.strip() for c in args.level_b.split(",") if c.strip())
        rows = []
        for r in r_values:
            rows += asyncio.run(
                level_b_comparison(
                    r,
                    n=args.n,
                    seed=args.seed,
                    variants=args.variants,
                    classes=classes,
                    language=args.language,
                )
            )
        for x in rows:
            print(
                f"r={x['r']} {x['mode']:12} hit rate {x['hit_rate']:.1%} billed {x['billed_chars']} "
                f"level B hits {x['level_b_hits']} diff histogram {x['near_histogram']}"
            )
        if args.out:
            Path(args.out).mkdir(parents=True, exist_ok=True)
            (Path(args.out) / "bench_level_b.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        return 0
    rows = asyncio.run(
        sweep(
            r_values,
            n=args.n,
            seed=args.seed,
            language=args.language,
            profile=args.profile,
            ttfb_median_ms=args.ttfb_median_ms,
            ttfb_sigma=args.ttfb_sigma,
            price_per_char=args.price_per_char,
            redis_url=args.redis_url,
            log_dir=Path(args.out) / "logs" if args.out else None,
        )
    )
    print(format_table(rows))
    if args.out:
        save(rows, args.out)
        print(f"bench.json, bench.txt, bench.svg and the corpus logs written to {args.out}")
    return 0


def _store(spec: str) -> Any:
    from tts_cache import MemoryStore, SQLiteStore, TieredStore

    if spec.startswith(("redis://", "rediss://")):
        from tts_cache.stores.redis import RedisStore

        shared: Any = RedisStore(spec)
    elif spec.startswith("sqlite:"):
        shared = SQLiteStore(spec.removeprefix("sqlite:"))
    else:
        raise SystemExit("store must be sqlite:PATH or redis://...")
    return TieredStore(MemoryStore(), shared, deadline_ms=1000)


def cmd_warm(args: argparse.Namespace) -> int:
    """Synthesise the hot set of a replay report once and store it under the runtime's keys (11.8)."""
    from tts_cache import TTSCache
    from tts_cache.scope import Scope
    from tts_cache.warm import load_factory, warm

    report = json.loads(Path(args.from_).read_text(encoding="utf-8"))
    hot: dict[str, list[str]] = report["hot_set"]
    labels = [args.label] if args.label else list(hot)
    texts = [t for label in labels for t in hot.get(label, [])][: args.limit]
    scope = Scope(**json.loads(Path(args.scope).read_text(encoding="utf-8")))

    async def run() -> dict[str, int]:
        cache = TTSCache(scope, _store(args.store), policy=parse_policy(args.policy))
        await cache.start()
        try:
            return await warm(cache, texts, load_factory(args.provider)(scope))
        finally:
            await cache.close(timeout=30.0)

    counts = asyncio.run(run())
    print(f"warmed {len(texts)} sentences: {counts}")
    return 0 if not counts["failed"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tts-cache", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ping", help="measure the Redis round trip and check the placement rule")
    p.add_argument("--url", default=os.environ.get("TTS_CACHE_REDIS_URL", "redis://localhost:6379/0"))
    p.add_argument("-n", type=int, default=1000)
    p.add_argument("--warmup", type=int, default=20)
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("replay", help="replay a transcript log and report the hit rate it would have had")
    p.add_argument(
        "--log", required=True, help="JSONL log: ts, session, text, language, scope (see tts_cache.corpus)"
    )
    p.add_argument("--policy", default="default")
    p.add_argument("--out", help="write the JSON report here (input to tts-cache warm)")
    p.add_argument("--no-rules", action="store_true", help="skip the per-rule incremental hit rates")
    p.add_argument("--capture-delay", type=float, default=2.0, help="seconds before a capture is visible")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("bench", help="synthetic benchmark of cost and latency, cache off and on")
    p.add_argument("--r", default="0,0.1,0.25,0.5,0.75,0.9", help="comma-separated repetition rates")
    p.add_argument("-n", type=int, default=200)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--language", default="en-US")
    p.add_argument("--profile", choices=["single", "sessions", "serverless"], default="single")
    p.add_argument("--level-b", help="compare Level B serving these classes (e.g. punctuation) on and off")
    p.add_argument("--variants", type=float, default=0.4, help="share of repeats that are near-duplicates")
    p.add_argument("--redis-url", default=os.environ.get("TTS_CACHE_REDIS_URL"))
    p.add_argument("--ttfb-median-ms", type=float, default=250.0)
    p.add_argument("--ttfb-sigma", type=float, default=0.5)
    p.add_argument("--price-per-char", type=float, default=30e-6)
    p.add_argument("--out", help="directory for bench.json, bench.txt, bench.svg and the corpus logs")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("warm", help="synthesise a replay's hot set and store it under the same keys")
    p.add_argument("--from", dest="from_", required=True, help="a replay report written with --out")
    p.add_argument("--scope", required=True, help="JSON file with the Scope fields of the deployment")
    p.add_argument("--store", required=True, help="sqlite:PATH or redis://...")
    p.add_argument(
        "--provider", required=True, help="module:factory, factory(scope) -> async (text) -> Audio"
    )
    p.add_argument("--label", help="the replay scope label to warm (default: all)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--policy", default="default")
    p.set_defaults(func=cmd_warm)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
