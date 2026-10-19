"""The offline matrix of TEST_PLAN.md section 8 on a pool of worker processes.

Each worker owns one database of the dedicated Redis on port 6380 (8 + its
index), flushes it before every run, and loads the Level C model once. A run
writes one JSON line per sentence to ``<out>/rows/<run id>.jsonl`` and one
summary line, with its V1 and V10 checks, to ``<out>/runs.jsonl``.

    python -m tts_cache.testplan.offline --out harness/out/testplan/offline [--seeds 1] [--langs en,hi]
"""

import argparse
import asyncio
import fcntl
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tts_cache.bench import CONFIGS, SimulatedProvider, run_core
from tts_cache.corpus import POOL_LANGUAGES, LabelledCorpus, control, labelled, load_pool
from tts_cache.testplan.oracle import Explainer, annotate, summary
from tts_cache.testplan.pools import pool_path

REDIS_PORT = int(os.environ.get("TESTPLAN_REDIS_PORT", "6380"))
FIRST_DB = 8
WORKERS = 8
RATES = (0.0, 0.25, 0.5)
SEEDS = tuple(range(1, 11))


@dataclass(frozen=True)
class Cell:
    lang: str
    config: str
    r: float
    seed: int
    mix: str = "labelled"
    profile: str = "serverless"
    tag: str = ""

    @property
    def id(self) -> str:
        base = f"{self.lang}_{self.config}_{self.r}_{self.mix}_{self.seed}_{self.profile}"
        return f"{base}_{self.tag}" if self.tag else base


def corpus_for(lang: str, r: float, seed: int, mix: str = "labelled") -> LabelledCorpus:
    pool = load_pool(pool_path(lang))
    if r == 0.0:
        return control(pool, seed)
    return labelled(pool, r=r, seed=seed, mix=mix)


def session_of_turn(corpus: LabelledCorpus) -> list[int]:
    out: dict[int, int] = {}
    for it in corpus.items:
        out[it.turn] = it.session
    return [out[t] for t in sorted(out)]


_WORKER: dict[str, Any] = {}


def _init(ids: Any) -> None:
    _WORKER["id"] = ids.get()
    from tts_cache.bench import reuse_encoded
    from tts_cache.match.semantic import load_model

    reuse_encoded()

    load_model()


def redis_url(worker: int) -> str:
    return f"redis://localhost:{REDIS_PORT}/{FIRST_DB + worker}"


async def flush(url: str) -> None:
    import redis.asyncio as aioredis

    client = aioredis.from_url(url)
    try:
        await client.flushdb()
    finally:
        await client.aclose()


def rows_of(run: Any, corpus: LabelledCorpus, cell: Cell) -> list[dict[str, Any]]:
    rows = []
    for s, it in zip(run.samples, corpus.items, strict=True):
        d = asdict(s)
        d.update(
            lang=cell.lang,
            config=cell.config,
            r=cell.r,
            mix=cell.mix,
            seed=cell.seed,
            profile=cell.profile,
            label=it.label,
            base=it.base,
            first_in_turn=it.first_in_turn,
            reject=run.reject_keys.get(s.key or ""),
        )
        rows.append(d)
    return rows


async def run_cell(cell: Cell, worker: int, out: Path, **knobs: Any) -> dict[str, Any]:
    corpus = corpus_for(cell.lang, cell.r, cell.seed, cell.mix)
    url = redis_url(worker)
    if cell.profile == "serverless":
        await flush(url)
    provider = SimulatedProvider(seed=cell.seed)
    explainer = Explainer(corpus, cell.config)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        run = await run_core(
            corpus,
            cell.r,
            cache_on=cell.config != "OFF",
            profile=cell.profile,
            provider=provider,
            workdir=Path(tmp),
            redis_url=url,
            language=f"{cell.lang}-IN",
            levels=None if cell.config == "OFF" else cell.config,
            session_of_turn=session_of_turn(corpus),
            explain=explainer,
            **knobs,
        )
    wall = time.perf_counter() - t0
    rows = annotate(corpus, rows_of(run, corpus, cell), cell.config, run.reject_keys)
    rows_dir = out / "rows"
    rows_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    await asyncio.to_thread((rows_dir / f"{cell.id}.jsonl").write_text, text, encoding="utf-8")
    s = summary(rows)
    total = sum(r["chars"] for r in rows)
    # V1: what was not billed is exactly what was served.
    v1 = run.billed_chars == total - s["hit_chars"]
    # V10: the emitted metrics alone give the hit rate, saved characters and rejects.
    m_hit, m_miss = run.event_chars.get("hit", 0), run.event_chars.get("miss", 0)
    rejects_metric = {k.split(":", 1)[1]: v for k, v in run.counts.items() if k.startswith("reject:")}
    rejects_truth: dict[str, int] = {}
    for reason in run.reject_keys.values():
        rejects_truth[reason] = rejects_truth.get(reason, 0) + 1
    v10 = cell.config == "OFF" or (
        m_hit == s["hit_chars"]
        and m_hit + m_miss == total
        and run.counts.get("hit", 0) == s["hits"]
        and all(rejects_metric.get(k, 0) >= v for k, v in rejects_truth.items())
    )
    return {
        **asdict(cell),
        "id": cell.id,
        "worker": worker,
        "wall_s": round(wall, 3),
        "billed_chars": run.billed_chars,
        "stored": run.stored,
        "stored_bytes": run.stored_bytes,
        "rejects": rejects_metric,
        "metric_hit_chars": m_hit,
        "metric_miss_chars": m_miss,
        "metric_hits": run.counts.get("hit", 0),
        "sessions": run.sessions,
        "v1": v1,
        "v10": v10,
        **s,
    }


def _task(cell: Cell, out: str, knobs: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(run_cell(cell, _WORKER["id"], Path(out), **knobs))


def matrix(langs: list[str], seeds: tuple[int, ...] = SEEDS) -> list[Cell]:
    cells = []
    for lang in langs:
        for seed in seeds:
            for r in RATES:
                for config in CONFIGS:
                    cells.append(Cell(lang, config, r, seed))
                    if r == 0.0 and config != "OFF":
                        cells.append(Cell(lang, config, r, seed, profile="single"))
            for config in ("OFF", "A"):
                cells.append(Cell(lang, config, 0.5, seed, mix="exact"))
    return cells


def run_cells(
    cells: list[Cell],
    out: Path,
    workers: int = WORKERS,
    knobs: dict[str, Any] | None = None,
    done: set[str] | None = None,
    cell_knobs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run ``cells`` on ``workers`` processes, appending to ``out/runs.jsonl``; skips ``done`` ids."""
    out.mkdir(parents=True, exist_ok=True)
    done = done or set()
    todo = [c for c in cells if c.id not in done]
    ctx = mp.get_context("spawn")
    ids = ctx.Manager().Queue()
    for i in range(workers):
        ids.put(i)
    results = []
    t0 = time.time()
    with (
        ProcessPoolExecutor(workers, mp_context=ctx, initializer=_init, initargs=(ids,)) as ex,
        open(out / "runs.jsonl", "a", encoding="utf-8") as log,
    ):
        cell_knobs = cell_knobs or {}
        futures = {
            ex.submit(_task, c, str(out), {**(knobs or {}), **cell_knobs.get(c.id, {})}): c for c in todo
        }
        for n, fut in enumerate(as_completed(futures), 1):
            cell = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {**asdict(cell), "id": cell.id, "error": f"{type(e).__name__}: {e}"}
            log.write(json.dumps(res, ensure_ascii=False) + "\n")
            log.flush()
            results.append(res)
            if n % 50 == 0 or n == len(todo):
                print(f"{n}/{len(todo)} runs, {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    return results


def load_runs(out: Path) -> list[dict[str, Any]]:
    path = out / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--langs", default=",".join(POOL_LANGUAGES))
    ap.add_argument("--seeds", default="1-10", help="a range a-b or a comma list")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--resume", action="store_true", help="skip runs already in runs.jsonl without an error")
    args = ap.parse_args(argv)
    if "-" in args.seeds:
        a, b = args.seeds.split("-")
        seeds = tuple(range(int(a), int(b) + 1))
    else:
        seeds = tuple(int(x) for x in args.seeds.split(","))
    langs = [lang for lang in args.langs.split(",") if pool_path(lang).exists()]
    missing = set(args.langs.split(",")) - set(langs)
    if missing:
        print(f"no pool for {sorted(missing)}; skipped", file=sys.stderr)
    lock = open(Path(tempfile.gettempdir()) / f"tts_cache_testplan_offline_{REDIS_PORT}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another offline matrix is using the Redis databases on port {REDIS_PORT}", file=sys.stderr)
        return 2
    done = {r["id"] for r in load_runs(args.out) if "error" not in r} if args.resume else set()
    t0 = time.time()
    results = run_cells(matrix(langs, seeds), args.out, args.workers, done=done)
    errors = [r for r in results if "error" in r]
    print(
        json.dumps(
            {
                "runs": len(results),
                "errors": len(errors),
                "wall_s": round(time.time() - t0, 1),
                "v1_fail": sum(1 for r in results if r.get("v1") is False),
                "v10_fail": sum(1 for r in results if r.get("v10") is False),
                "critical": sum(r.get("critical", 0) for r in results),
                "unexplained": sum(r.get("unexplained", 0) for r in results),
            }
        )
    )
    for e in errors[:5]:
        print(e["id"], e["error"], file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
