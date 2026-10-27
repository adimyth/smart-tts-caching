"""The offline stages of docs/evaluation/PLAN.md for the frozen deployments.

    python -m tts_cache.testplan.core freeze --out harness/out/core/<id>
    python -m tts_cache.testplan.core safety --out harness/out/core/<id>
    python -m tts_cache.testplan.core scope  --out harness/out/core/<id>
    python -m tts_cache.testplan.core replay --out harness/out/core/<id>
    python -m tts_cache.testplan.core stored --out harness/out/core/<id>
    python -m tts_cache.testplan.core memory --out harness/out/core/<id>
    python -m tts_cache.testplan.core live   --out harness/out/core/<id>   # after live_bench --plan core
    python -m tts_cache.testplan.core cost   --out harness/out/core/<id>

Each stage writes ``<out>/<stage>/`` with per-sentence rows (JSONL) and a
``summary.json``. The workers share the dedicated Redis on port 6380 with the
offline matrix (databases 8 and up) and take its lock; ``scope`` and
``stored`` use databases 14 and 15. The live stages run in
``harness/live_bench.py``.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import multiprocessing as mp
import platform
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from tts_cache.canonical import canonicalise
from tts_cache.corpus import (
    DECOY_LABELS,
    LabelledCorpus,
    LabelledItem,
    filler_variants,
    load_pool,
    punct_inner_variants,
    punct_question_variant,
)
from tts_cache.scope import Scope
from tts_cache.testplan import offline
from tts_cache.testplan.cells import PRICE_DATE, PRICE_NOTES, USD_PER_MCHARS
from tts_cache.testplan.oracle import Explainer, annotate, summary
from tts_cache.testplan.pools import pool_path
from tts_cache.testplan.stats import bootstrap, pct

REPO = Path(__file__).resolve().parents[5]
FIXTURES = REPO / "packages" / "tts-cache" / "tests" / "fixtures"
WORKERS = 6
SCOPE_DB, STORED_DB = 14, 15
CONFIGS = ("A+B", "A+B+C+J")
# A+B+C+J failed the safety gate (wrong serves), so by section 5 step 2 it does not proceed.
REPLAY_CONFIGS = ("OFF", "A+B")
REPLAY_RATES = (0.25, 0.5)
SEEDS = tuple(range(1, 11))
FORMS = ("base", "paraphrase", "filler")
TURN = 4
MIN_PER_DECOY = 300
NOT_SERVABLE = ("new", *DECOY_LABELS)

# The two frozen deployments: the scope each live service reports (scope_from_service), its price.
DEPLOYMENTS: dict[str, dict[str, Any]] = {
    "en": {
        "service": "elevenlabs_ws_fc",
        "provider": "elevenlabs",
        "transport": "websocket",
        "scope": Scope(
            service="elevenlabs",
            model="eleven_flash_v2_5",
            voice="txk8uOzZ0iCh0B9mFSRG",
            language="en-IN",
            sample_rate=24_000,
            settings={"language": "en"},
        ),
        "neighbours": {"voice": "21m00Tcm4TlvDq8ikWAM", "model": "eleven_turbo_v2_5", "language": "en-US"},
        "setting": ("language", "en-GB"),
    },
    "hi": {
        "service": "sarvam_ws_flush",
        "provider": "sarvam",
        "transport": "websocket",
        "scope": Scope(
            service="sarvam",
            model="bulbul:v4-flash",
            voice="shubh_enhi_ads",
            language="hi-IN",
            sample_rate=24_000,
            settings={"enable_preprocessing": True, "pace": 1.0, "language": "hi-IN"},
        ),
        "neighbours": {"voice": "anushka", "model": "bulbul:v3", "language": "en-IN"},
        "setting": ("pace", 1.1),
    },
}

# The miss budget and turn-start target, stated before the run (docs/evaluation/PLAN.md 4.3).
MISS_BUDGET_MS = 20.0
L1_TARGET_MS = 100.0
CALLS_PER_MONTH = (10_000, 100_000, 1_000_000)
SENTENCES_PER_CALL = 20

# A deployment's skip list for sentences that carry an identifier (docs/evaluation/PLAN.md 4.7): runs of
# four or more digits in any script, phone and card numbers, e-mail addresses, one-time codes.
PII_PATTERNS = (
    r"\d{4,}",
    r"\d[\d -]{8,}\d",
    r"[\w.+-]+@[\w-]+\.[\w.]+",
    r"(?i)\b(otp|pin|cvv|password|passcode)\b",
)


def _lock() -> Any:
    f = open(Path(tempfile.gettempdir()) / f"tts_cache_testplan_offline_{offline.REDIS_PORT}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            f"another offline run holds the Redis databases on port {offline.REDIS_PORT}"
        ) from None
    return f


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _str(v: Any) -> str:
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


def _rule_of_three(n: int) -> float | None:
    return round(3 / n, 5) if n else None


# ---------------------------------------------------------------------------
# Freeze (section 5, step 1).
# ---------------------------------------------------------------------------


def _git(*a: str) -> str:
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=False).stdout.strip()


def _redis_version(port: int) -> str | None:
    out = subprocess.run(["redis-cli", "-p", str(port), "info", "server"], capture_output=True, text=True)
    m = re.search(r"redis_version:(\S+)", out.stdout)
    return m.group(1) if m else None


def _version(package: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(package)
    except PackageNotFoundError:
        return None


def environment() -> dict[str, Any]:
    diff = subprocess.run(["git", "diff", "HEAD"], cwd=REPO, capture_output=True, check=False).stdout
    untracked = _git("ls-files", "--others", "--exclude-standard").splitlines()
    return {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit": _git("rev-parse", "HEAD"),
        "dirty_files": _git("diff", "--name-only", "HEAD").splitlines(),
        "untracked": [u for u in untracked if not u.startswith("harness/out/")],
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "versions": {
            "python": platform.python_version(),
            **{
                p: _version(p)
                for p in ("smart-tts-cache", "smart-tts-cache-pipecat", "pipecat-ai", "redis", "onnxruntime")
            },
            "redis_server_6380": _redis_version(6380),
        },
        "machine": {
            "platform": platform.platform(),
            "cpu": platform.processor() or platform.machine(),
            "cores": mp.cpu_count(),
            "ram_gb": round(
                int(
                    subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0
                )
                / 2**30
            ),
        },
        "network": "home connection; the provider region is the provider's default",
    }


def freeze(out: Path) -> dict[str, Any]:
    env = environment()
    for lang, d in DEPLOYMENTS.items():
        prov = d["provider"]
        record = {
            **env,
            "deployment": {
                "language": lang,
                "service": d["service"],
                "provider": prov,
                "transport": d["transport"],
                "scope": d["scope"].as_dict(),
            },
            "store": "local Redis on the same machine (no network hop); TieredStore deadline 20 ms",
            "policies": {
                "A+B": "DEFAULT_POLICY: Probation(48h, 30d), MinChars(3, 400), "
                "LevelB({punctuation, filler}); a question mark added or removed is class 'question', "
                "never served",
                "A+B+C+J": "LEVEL_C_POLICY with default_matcher(judge=True): mDeBERTa NLI judge, "
                "two-way entailment >= 0.70, after the slot and span vetoes",
                "skip_patterns_evaluated": list(PII_PATTERNS),
            },
            "inputs": {
                "safety": "harness/corpus/<lang>.yaml bases with rule-made and authored decoys, "
                "three stored forms, plus tests/fixtures/{semantic,near,judge}",
                "replay": "no production log exists: the synthetic labelled corpus at r = 0.25 and 0.5, "
                "seeds 1-10 (an assumption, not a forecast)",
                "low_repeat_control": "every base once (r = 0), seeds 1-10",
            },
            "price": {
                "date": PRICE_DATE,
                "usd_per_million_chars": USD_PER_MCHARS[prov],
                "note": PRICE_NOTES[prov],
            },
            "latency_budget": {
                "miss_overhead_p95_ms": MISS_BUDGET_MS,
                "miss_overhead_note": "cache work on a miss, plus audio decode on a hit",
                "l1_p50_improvement_target_ms": L1_TARGET_MS,
            },
            "volume_assumption": {
                "calls_per_month": list(CALLS_PER_MONTH),
                "sentences_per_call": SENTENCES_PER_CALL,
                "note": "the adopter's volume is unknown: scenarios, labelled as assumptions",
            },
        }
        path = out / lang / "environment.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
    return env


# ---------------------------------------------------------------------------
# Safety corpus (4.1, 3.1).
# ---------------------------------------------------------------------------


def _corpus(lang: str, sessions: list[list[tuple[str, str, str, str]]], name: str) -> LabelledCorpus:
    items: list[LabelledItem] = []
    i = turn = 0
    for s, texts in enumerate(sessions):
        for k in range(0, len(texts), TURN):
            for j, (text, label, base, note) in enumerate(texts[k : k + TURN]):
                items.append(LabelledItem(i, s, turn, j == 0, text, label, base, note))
                i += 1
            turn += 1
    return LabelledCorpus(lang, 1, 0.0, 0.0, name, items)


def _stored_form(b: Any, form: str, lang: str, rng: random.Random) -> str:
    if form == "base":
        return b.text
    if form == "paraphrase":
        return b.paraphrase
    return rng.choice(filler_variants(b.text, lang))


def form_corpus(lang: str, form: str, split: int) -> LabelledCorpus:
    """Half the bases stored in one form; their decoys, an exact repeat and the other half's bases asked for.

    Session 0 says each stored base once, as the base, its paraphrase or a
    filler variant. Session 1 asks, in a shuffled order, for every decoy of
    those bases (the authored meaning change, the stored form with ``?``
    added or removed, each single-slot change of the stored form), an exact
    repeat of the stored form as a positive control, and every base of the
    other half, never said before (``new``).
    """
    pool = load_pool(pool_path(lang))
    rng = random.Random(f"{lang}:{form}:{split}")
    stored = [b for i, b in enumerate(pool.bases) if i % 2 == split]
    held = [b for i, b in enumerate(pool.bases) if i % 2 != split]
    first: list[tuple[str, str, str, str]] = []
    second: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()

    def ask(text: str, label: str, base: str, note: str) -> None:
        c = canonicalise(text, lang)
        if c in seen:
            return
        seen.add(c)
        second.append((text, label, base, note))

    forms = {}
    for b in stored:
        f = _stored_form(b, form, lang, rng)
        forms[b.id] = f
        first.append((f, "new", b.id, form))
        seen.add(canonicalise(f, lang))
    for b in stored:
        f = forms[b.id]
        ask(b.meaning_change, "meaning_change", b.id, "authored")
        q = punct_question_variant(f, lang)
        if q is not None:
            ask(q, "punct_question", b.id, form)
        for s in b.slots:
            src = f if s.span in f else b.text
            for alt in s.alts:
                ask(src.replace(s.span, alt, 1), "slot_change", b.id, f"{s.span}->{alt}")
        second.append((f, "exact", b.id, "control"))
    for b in held:
        ask(b.text, "new", b.id, "held out")
    rng.shuffle(second)
    return _corpus(lang, [first, second], f"safety_{form}_{split}")


FIXTURE_LABEL = {
    "near_miss": "slot_change",
    "changed": "meaning_change",
    "slot": "slot_change",
    "other": "meaning_change",
    "question": "punct_question",
    "punctuation": "punct_inner",
    "filler": "filler",
    "paraphrase": "paraphrase",
}


def fixture_pairs(lang: str) -> list[tuple[str, str, str, str]]:
    """(stored, requested, label, source) from the semantic, near-duplicate and judge fixtures."""
    import yaml

    out = []
    for c in yaml.safe_load((FIXTURES / "semantic" / f"{lang}.yaml").read_text(encoding="utf-8")):
        out.append((c["a"], c["b"], FIXTURE_LABEL[c["label"]], "semantic"))
    for c in yaml.safe_load((FIXTURES / "near" / f"{lang}.yaml").read_text(encoding="utf-8")):
        out.append((c["b"], c["a"], FIXTURE_LABEL[c["class"]], "near"))
    for c in yaml.safe_load((FIXTURES / "judge" / "level_c_serves.yaml").read_text(encoding="utf-8")):
        if c["lang"] == lang:
            out.append((c["a"], c["b"], FIXTURE_LABEL[c["label"]], "judge"))
    return out


def fixture_corpus(lang: str) -> LabelledCorpus:
    """Every fixture's stored side in session 0, every requested side in session 1."""
    rng = random.Random(f"{lang}:fixtures")
    base_of: dict[str, str] = {}
    first: list[tuple[str, str, str, str]] = []
    pairs = fixture_pairs(lang)
    for stored, _, _, source in pairs:
        c = canonicalise(stored, lang)
        if c not in base_of:
            base_of[c] = f"fx-{len(base_of):03d}"
            first.append((stored, "new", base_of[c], source))
    second: list[tuple[str, str, str, str]] = []
    asked: set[str] = set()
    for stored, requested, label, source in pairs:
        c = canonicalise(requested, lang)
        # A requested side that is itself a stored sentence, or asked twice, would hit Level A.
        if c in base_of or c in asked:
            continue
        asked.add(c)
        second.append((requested, label, base_of[canonicalise(stored, lang)], source))
    rng.shuffle(second)
    return _corpus(lang, [first, second], "safety_fixtures")


def safety_corpora(lang: str) -> list[LabelledCorpus]:
    return [form_corpus(lang, f, s) for f in FORMS for s in (0, 1)] + [fixture_corpus(lang)]


def safety_plan(langs: list[str]) -> dict[str, Any]:
    """N per label and the unique texts behind it, stated before the run."""
    plan: dict[str, Any] = {}
    for lang in langs:
        n: Counter[str] = Counter()
        unique: dict[str, set[str]] = defaultdict(set)
        for corpus in safety_corpora(lang):
            for it in corpus.items:
                if it.session == 0:
                    continue
                n[it.label] += 1
                unique[it.label].add(canonicalise(it.text, lang))
        plan[lang] = {
            "requests": dict(n),
            "unique_texts": {k: len(v) for k, v in unique.items()},
            "decoys_meet_minimum": all(n[k] >= MIN_PER_DECOY for k in NOT_SERVABLE),
        }
    return plan


# ---------------------------------------------------------------------------
# One corpus through the core, on a worker's Redis database.
# ---------------------------------------------------------------------------


async def run_corpus(
    corpus: LabelledCorpus, config: str, url: str, *, policy: Any = None, keep_db: bool = False
) -> tuple[list[dict[str, Any]], Any]:
    from tts_cache.bench import SimulatedProvider, run_core

    await offline.flush(url)
    explainer = Explainer(corpus, config) if config != "OFF" else None
    cell = offline.Cell(corpus.language, config, corpus.r, corpus.seed, mix=corpus.mix)
    with tempfile.TemporaryDirectory() as tmp:
        run = await run_core(
            corpus,
            corpus.r,
            cache_on=config != "OFF",
            profile="serverless",
            provider=SimulatedProvider(seed=corpus.seed),
            workdir=Path(tmp),
            redis_url=url,
            language=f"{corpus.language}-IN",
            levels=None if config == "OFF" else config,
            session_of_turn=offline.session_of_turn(corpus),
            explain=explainer,
            policy=policy,
            sample_rss=True,
        )
    rows = annotate(corpus, offline.rows_of(run, corpus, cell), config, run.reject_keys)
    for row, it in zip(rows, corpus.items, strict=True):
        row["text"] = it.text
        row["note"] = it.note
    if not keep_db:
        await offline.flush(url)
    return rows, run


def _safety_task(lang: str, name: str, config: str, out: str) -> dict[str, Any]:
    corpus = next(c for c in safety_corpora(lang) if c.mix == name)
    rows, run = asyncio.run(run_corpus(corpus, config, offline.redis_url(offline._WORKER["id"])))
    _write_jsonl(Path(out) / "rows" / f"{lang}_{config}_{name}.jsonl", rows)
    return {"lang": lang, "config": config, "corpus": name, "billed_chars": run.billed_chars, **summary(rows)}


def _pool(tasks: list[tuple[Any, tuple[Any, ...]]], workers: int) -> list[Any]:
    ctx = mp.get_context("spawn")
    ids = ctx.Manager().Queue()
    for i in range(workers):
        ids.put(i)
    results = []
    with ProcessPoolExecutor(workers, mp_context=ctx, initializer=offline._init, initargs=(ids,)) as ex:
        futures = {ex.submit(fn, *args): args for fn, args in tasks}
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"args": [str(a) for a in futures[fut]], "error": f"{type(e).__name__}: {e}"})
            print(f"{n}/{len(tasks)}", file=sys.stderr, flush=True)
    return results


def safety(out: Path, langs: list[str], workers: int) -> dict[str, Any]:
    d = out / "safety"
    d.mkdir(parents=True, exist_ok=True)
    plan = safety_plan(langs)
    (d / "plan.json").write_text(json.dumps(plan, indent=1))
    tasks = [
        (_safety_task, (lang, c.mix, config, str(d)))
        for lang in langs
        for c in safety_corpora(lang)
        for config in CONFIGS
    ]
    runs = _pool(tasks, workers)
    _write_jsonl(d / "runs.jsonl", runs)
    res = {lang: safety_summary(d, lang, plan[lang]) for lang in langs}
    (d / "summary.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


def safety_summary(d: Path, lang: str, plan: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"plan": plan}
    for config in CONFIGS:
        rows = [r for p in sorted((d / "rows").glob(f"{lang}_{config}_*.jsonl")) for r in _read_jsonl(p)]
        asked = [r for r in rows if r["session"] >= 1]
        by_label: dict[str, Any] = {}
        for label in sorted({r["label"] for r in asked}):
            rs = [r for r in asked if r["label"] == label]
            served = [r for r in rs if r["hit"]]
            wrong = [r for r in served if r["wrong"]]
            by_label[label] = {
                "n": len(rs),
                "served": len(served),
                "wrong": len(wrong),
                # Rule of three: with 0 of n served, the serve rate is under 3/n at 95 percent.
                "upper_95": _rule_of_three(len(rs)) if label in NOT_SERVABLE and not served else None,
                "by_level": dict(Counter(r.get("level") for r in served)),
            }
        failures = [
            {
                "corpus": r["mix"],
                "label": r["label"],
                "requested": r["text"],
                "served": r.get("spoken_text"),
                "level": r.get("level"),
                "diff_class": r.get("diff_class"),
                "served_from": r.get("served_from"),
            }
            for r in asked
            if r["hit"] and r["wrong"]
        ]
        blocked = sum(by_label.get(k, {}).get("served", 0) for k in NOT_SERVABLE)
        out[config] = {
            "by_label": by_label,
            "wrong_serves": len(failures),
            "not_servable_served": blocked,
            "pass": blocked == 0 and not failures,
            "failures": failures,
        }
    return out


# ---------------------------------------------------------------------------
# Scope isolation (4.1).
# ---------------------------------------------------------------------------


def _variants(lang: str) -> dict[str, Scope]:
    d = DEPLOYMENTS[lang]
    frozen: Scope = d["scope"]
    key, value = d["setting"]
    return {
        "voice": replace(frozen, voice=d["neighbours"]["voice"]),
        "model": replace(frozen, model=d["neighbours"]["model"]),
        "language": replace(frozen, language=d["neighbours"]["language"]),
        "sample_rate": replace(frozen, sample_rate=16_000),
        "channels": replace(frozen, channels=2),
        "settings": replace(frozen, settings={**frozen.settings, key: value}),
    }


async def scope_isolation(lang: str) -> dict[str, Any]:
    """Store the bases under the frozen scope; ask for them, and servable variants, under a neighbour."""
    from tts_cache import MemoryStore, TieredStore, TTSCache
    from tts_cache.bench import config_policy, synthetic_pcm
    from tts_cache.metrics import LogSink
    from tts_cache.policy import AllOf, Namespace
    from tts_cache.stores.redis import RedisStore

    frozen: Scope = DEPLOYMENTS[lang]["scope"]
    url = f"redis://localhost:{offline.REDIS_PORT}/{SCOPE_DB}"
    pool = load_pool(pool_path(lang))
    rng = random.Random(lang)
    stored = [b.text for b in pool.bases]
    probes = []
    for b in pool.bases:
        probes.append((b.text, "exact"))
        probes.append((rng.choice(filler_variants(b.text, lang)), "filler"))
        inner = punct_inner_variants(b.text)
        if inner:
            probes.append((rng.choice(inner), "punct_inner"))
        probes.append((b.paraphrase, "paraphrase"))

    async def open_cache(scope: Scope, namespaced: bool) -> tuple[Any, Any]:
        policy, matcher = config_policy("A+B+C+J")
        if namespaced:
            policy = AllOf(policy, Namespace("tenant"))
        sink = LogSink(log=None)
        # A generous deadline: a lookup that fails open is a miss, which would pass for isolation.
        cache = TTSCache(
            scope,
            TieredStore(MemoryStore(), RedisStore(url), deadline_ms=200),
            policy=policy,
            matcher=matcher,
            metrics=sink,
        )
        await cache.start()
        return cache, sink

    async def store_all(namespaced: bool) -> None:
        await offline.flush(url)
        cache, _ = await open_cache(frozen, namespaced)
        for text in stored:
            await cache.capture(
                text, synthetic_pcm(text, frozen.sample_rate), frozen.sample_rate, tags={"tenant": "a"}
            )
        await cache.drain(60)
        await cache.close()

    async def ask(scope: Scope, namespaced: bool, tenant: str) -> dict[str, Any]:
        cache, sink = await open_cache(scope, namespaced)
        hits: Counter[str] = Counter()
        n: Counter[str] = Counter()
        examples = []
        for text, label in probes:
            n[label] += 1
            found = await cache.probe(text, tags={"tenant": tenant})
            if found.hit is not None:
                hits[label] += 1
                if len(examples) < 5:
                    examples.append(
                        {"asked": text, "served": found.hit.spoken_text, "level": found.hit.level}
                    )
        await cache.close()
        return {
            "n": dict(n),
            "hits": dict(hits),
            "total_hits": sum(hits.values()),
            "deadline_misses": sink.counts.get("deadline", 0),
            "examples": examples,
        }

    res: dict[str, Any] = {"frozen": frozen.as_dict(), "stored": len(stored), "probes": len(probes)}
    await store_all(namespaced=False)
    res["control"] = await ask(frozen, False, "a")
    res["variants"] = {}
    for field, scope in _variants(lang).items():
        res["variants"][field] = {
            "value": getattr(scope, field) if field != "settings" else dict(scope.settings)
        }
        res["variants"][field].update(await ask(scope, False, "a"))
    await store_all(namespaced=True)
    res["namespace_control"] = await ask(frozen, True, "a")
    res["variants"]["namespace"] = {
        "value": "tenant=b (stored under tenant=a)",
        **(await ask(frozen, True, "b")),
    }
    await offline.flush(url)
    res["pass"] = (
        all(v["total_hits"] == 0 for v in res["variants"].values()) and res["control"]["total_hits"] > 0
    )
    return res


def scope_stage(out: Path, langs: list[str]) -> dict[str, Any]:
    from tts_cache.match.semantic import load_model

    load_model()
    res = {lang: asyncio.run(scope_isolation(lang)) for lang in langs}
    d = out / "scope"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


# ---------------------------------------------------------------------------
# Replay (4.2) and the low-repeat control (3.3).
# ---------------------------------------------------------------------------


def replay_cells(langs: list[str]) -> list[offline.Cell]:
    cells = []
    for lang in langs:
        for seed in SEEDS:
            for r in (0.0, *REPLAY_RATES):
                for config in REPLAY_CONFIGS:
                    cells.append(offline.Cell(lang, config, r, seed))
    return cells


MISS_REASON = {
    "same_turn": "same turn",
    "same_session": "same session",
    "outside_warm_set": "outside warm set",
    "rejected": "rejected at capture",
    "evicted": "eviction",
    "deadline": "deadline",
    "provider_failed": "provider failure",
    "below_t_lang": "rejected candidate (similarity)",
    "slot_veto": "rejected candidate (veto)",
    "b_other": "rejected candidate (Level B class)",
    "b_index_miss": "Level B index miss",
}


def miss_reason(row: dict[str, Any], config: str) -> str:
    if row["label"] == "new":
        return "new sentence"
    if row["label"] in DECOY_LABELS:
        return "decoy (refused)"
    why = row.get("why")
    if why is None:
        return "not servable in this configuration"
    head = why.split(":", 1)[0]
    if head == "unexplained" and config == "A+B+C+J" and row["label"] == "paraphrase":
        facts = row.get("explain") or {}
        if facts.get("c_sim", 0) >= 0.90 and not facts.get("c_veto"):
            return "rejected candidate (judge)"
    return MISS_REASON.get(head, f"unexplained ({why[:60]})")


def replay_summary(d: Path, lang: str) -> dict[str, Any]:
    runs = {r["id"]: r for r in offline.load_runs(d) if "error" not in r}
    out: dict[str, Any] = {}
    for r in (0.0, *REPLAY_RATES):
        per: dict[str, Any] = {}
        for config in REPLAY_CONFIGS:
            ids = [offline.Cell(lang, config, r, s).id for s in SEEDS]
            rows_by_seed = {
                s: _read_jsonl(d / "rows" / f"{i}.jsonl") for s, i in zip(SEEDS, ids, strict=True)
            }
            rates = [runs[i]["char_hit_rate"] for i in ids if i in runs]
            sent = [runs[i]["sentence_hit_rate"] for i in ids if i in runs]
            session_rates, zero = [], 0
            levels: Counter[str] = Counter()
            reasons: Counter[str] = Counter()
            requests = avoided = chars = served_chars = 0
            for rows in rows_by_seed.values():
                by: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for row in rows:
                    by[row["session"]].append(row)
                    chars += row["chars"]
                    if row["hit"] and not row["wrong"]:
                        served_chars += row["chars"]
                        avoided += 1
                        k = row.get("level") or "?"
                        levels[f"B:{row['diff_class']}" if k == "B" and row.get("diff_class") else k] += 1
                    elif not row["hit"]:
                        requests += 1
                        if config != "OFF":
                            reasons[miss_reason(row, config)] += 1
                for srows in by.values():
                    c = sum(x["chars"] for x in srows)
                    h = sum(x["chars"] for x in srows if x["hit"] and not x["wrong"])
                    session_rates.append(h / c if c else 0.0)
                    zero += h == 0
            per[config] = {
                "runs": len(rates),
                "char_hit_rate": bootstrap(rates),
                "sentence_hit_rate": bootstrap(sent),
                "provider_requests": requests,
                "requests_avoided": avoided,
                "chars": chars,
                "served_chars": served_chars,
                "hits_by_level": dict(levels),
                "session_char_hit_rate": {
                    "n": len(session_rates),
                    "p10": pct(session_rates, 0.10),
                    "p50": pct(session_rates, 0.50),
                    "p90": pct(session_rates, 0.90),
                },
                "zero_hit_session_share": zero / len(session_rates) if session_rates else None,
                "misses_by_reason": dict(reasons.most_common()),
                "wrong": sum(runs[i]["wrong"] for i in ids if i in runs),
                "wrong_by_label": dict(
                    sum((Counter(runs[i]["wrong_by_label"]) for i in ids if i in runs), Counter())
                ),
                "v1_all": all(runs[i]["v1"] for i in ids if i in runs),
                "v10_all": all(runs[i]["v10"] for i in ids if i in runs),
                "warm_start_ms": pct(
                    [s["start_ms"] for i in ids if i in runs for s in runs[i]["sessions"]], 0.5
                ),
                "warm_start_ms_p95": pct(
                    [s["start_ms"] for i in ids if i in runs for s in runs[i]["sessions"]], 0.95
                ),
                "rss_mb_max": max(
                    (s.get("rss", 0) for i in ids if i in runs for s in runs[i]["sessions"]), default=0
                )
                / 2**20,
                "stored_bytes_per_clip": (
                    sum(runs[i]["stored_bytes"] for i in ids if i in runs)
                    / max(1, sum(runs[i]["stored"] for i in ids if i in runs))
                ),
            }
        # Cache work on the host's path: lookup on a miss, lookup plus first decoded chunk on a hit.
        for config in REPLAY_CONFIGS[1:]:
            rows = [
                x
                for s in SEEDS
                for x in _read_jsonl(d / "rows" / f"{offline.Cell(lang, config, r, s).id}.jsonl")
            ]
            miss = [x["lookup_ms"] for x in rows if not x["hit"]]
            hit = [x["lookup_ms"] + x["first_chunk_ms"] for x in rows if x["hit"]]
            per[config]["miss_lookup_ms"] = {
                "n": len(miss),
                "p50": pct(miss, 0.5),
                "p95": pct(miss, 0.95),
                "max": max(miss, default=None),
            }
            per[config]["hit_first_audio_ms"] = {"n": len(hit), "p50": pct(hit, 0.5), "p95": pct(hit, 0.95)}
        out[str(r)] = per
    return out


def replay(out: Path, langs: list[str], workers: int) -> dict[str, Any]:
    d = out / "replay"
    done = {r["id"] for r in offline.load_runs(d) if "error" not in r}
    offline.run_cells(replay_cells(langs), d, workers, done=done)
    res = {lang: replay_summary(d, lang) for lang in langs}
    (d / "summary.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


# ---------------------------------------------------------------------------
# Stored data (4.7).
# ---------------------------------------------------------------------------


def _digits(text: str) -> int:
    return max((len(m) for m in re.findall(r"\d+", text)), default=0)


def sensitive_kinds(text: str, base: Any) -> list[str]:
    """The personal-data categories in a stored text, from its base's slots: the labelled pass.

    A number slot whose values have four or more digits is an identifier (account, order,
    complaint, phone); the text's digits are counted in any script, since a surface variant
    may write them in the native one.
    """
    kinds = []
    for s in getattr(base, "slots", ()):
        values = (s.span, *s.alts)
        if s.kind == "number":
            if max(len(re.sub(r"\D", "", v)) for v in values) >= 4 and _digits(text) >= 4:
                kinds.append("identifier")
        elif any(v in text for v in values):
            kinds.append({"name": "person name", "amount": "amount", "date": "date"}[s.kind])
    if re.search(PII_PATTERNS[2], text):
        kinds.append("email")
    return sorted(set(kinds))


async def stored_inventory(lang: str, url: str, policy: Any = None) -> dict[str, Any]:
    """One representative replay (r = 0.5, seed 1, A+B) left in Redis, then read back field by field."""
    import redis.asyncio as aioredis

    corpus = offline.corpus_for(lang, 0.5, 1)
    rows, _ = await run_corpus(corpus, "A+B", url, policy=policy, keep_db=True)
    pool = {b.id: b for b in load_pool(pool_path(lang)).bases}
    client = aioredis.from_url(url)
    entries = []
    fields: set[str] = set()
    try:
        async for k in client.scan_iter(match="tts:meta:*", count=500):
            meta = {_str(x): _str(v) for x, v in (await client.hgetall(k)).items()}
            fields |= set(meta)
            key = "tts:" + _str(k).removeprefix("tts:meta:")
            entries.append(
                {
                    "key": key,
                    "text": meta.get("text"),
                    "ttl_s": await client.ttl(key),
                    "hit_count": int(meta.get("hit_count", 0)),
                    "audio_bytes": await client.strlen(key),
                    "memory_usage_bytes": (await client.memory_usage(key) or 0)
                    + (await client.memory_usage(k) or 0),
                }
            )
        key_names = sorted({_str(k).split(":")[1] for k in await client.keys("tts:*")})
    finally:
        await client.aclose()
    base_of = {canonicalise(r["text"], lang): r["base"] for r in rows}
    for e in entries:
        b = pool.get(base_of.get(canonicalise(e["text"] or "", lang), ""))
        e["sensitive"] = sensitive_kinds(e["text"] or "", b)
        e["skip_pattern"] = next((p for p in PII_PATTERNS if re.search(p, e["text"] or "")), None)
    skips: Counter[str] = Counter()
    asked = [r for r in rows if not r["hit"]]
    for r in asked:
        n = len(r["text"].strip())
        if n < 3 or n > 400:
            skips["MinChars"] += 1
        elif r.get("reject") == "policy":
            skips["SkipIfMatches"] += 1
        elif r.get("reject"):
            skips[f"ingest:{r['reject']}"] += 1
    return {
        "sentences": len(rows),
        "misses": len(asked),
        "stored": len(entries),
        "stored_share_of_sentences": len(entries) / len(rows),
        "stored_share_of_misses": len(entries) / len(asked) if asked else None,
        "skips": dict(skips),
        "not_configured": ["TurnPosition", "RateLimit"] + ([] if policy is not None else ["SkipIfMatches"]),
        "probation_ttl_entries": sum(1 for e in entries if 0 < e["ttl_s"] <= 48 * 3600),
        "promoted_ttl_entries": sum(1 for e in entries if e["ttl_s"] > 48 * 3600),
        "redis_meta_fields": sorted(fields),
        "redis_key_kinds": key_names,
        "bytes_per_clip_mean": statistics.fmean(e["memory_usage_bytes"] for e in entries)
        if entries
        else None,
        "sensitive_stored": [e for e in entries if e["sensitive"]],
        "pattern_misses": [
            e for e in entries if set(e["sensitive"]) & {"identifier", "email"} and e["skip_pattern"] is None
        ],
        "entries": entries,
        "char_hit_rate": summary(rows)["char_hit_rate"],
    }


async def retention_clock(lang: str) -> dict[str, Any]:
    """Probation and renewal against a clock the test controls (SQLite and memory tiers).

    Presence is read from the store, not through a lookup, so checking never renews an entry.
    """
    from tts_cache import MemoryStore, SQLiteStore, TieredStore, TTSCache, build_key
    from tts_cache.bench import bench_scope, synthetic_pcm
    from tts_cache.policy import DEFAULT_POLICY

    scope = bench_scope(f"{lang}-IN")
    pool = load_pool(pool_path(lang))
    once, twice = pool.bases[0].text, pool.bases[1].text
    t0 = 1_800_000_000.0
    clock = [t0]
    hour, day = 3600.0, 86400.0
    with tempfile.TemporaryDirectory() as tmp, mock.patch("time.time", lambda: clock[0]):
        db = Path(tmp) / "clock.db"

        def fresh() -> TTSCache:
            return TTSCache(
                scope, TieredStore(MemoryStore(), SQLiteStore(db, sweep_interval_s=0)), policy=DEFAULT_POLICY
            )

        async def present(text: str) -> bool:
            store = SQLiteStore(db, sweep_interval_s=0)
            try:
                return (await store.get(build_key(scope, canonicalise(text, lang)))) is not None
            finally:
                await store.close()

        async def served(text: str) -> bool:
            c = fresh()
            await c.start()
            try:
                return (await c.probe(text)).hit is not None
            finally:
                await c.drain(5)
                await c.close()

        cache = fresh()
        await cache.start()
        for t in (once, twice):
            await cache.capture(t, synthetic_pcm(t, scope.sample_rate), scope.sample_rate)
        await cache.drain(10)
        await cache.close()
        checks: dict[str, tuple[bool, bool]] = {}  # name -> (observed, expected)
        clock[0] = t0 + hour
        checks["second sentence hit once at 1 h"] = (await served(twice), True)
        hit_at = clock[0]
        clock[0] = t0 + 47 * hour
        checks["said once: present at 47 h"] = (await present(once), True)
        clock[0] = t0 + 48 * hour + 60
        checks["said once: gone at 48 h + 1 min"] = (not await present(once), True)
        checks["said once: next request misses at 48 h + 1 min"] = (not await served(once), True)
        clock[0] = hit_at + 30 * day - 60
        checks["hit once: present 30 d - 1 min after the hit"] = (await present(twice), True)
        clock[0] = hit_at + 30 * day + 60
        checks["hit once: gone 30 d + 1 min after the hit"] = (not await present(twice), True)
    return {
        "checks": {k: v[0] for k, v in checks.items()},
        "pass": all(obs == exp for obs, exp in checks.values()),
        "store": "SQLiteStore behind a MemoryStore, time.time patched",
    }


async def deletion(lang: str, url: str) -> dict[str, Any]:
    """delete and purge through one process; ask through it and through a second, already warm process."""
    import redis.asyncio as aioredis

    from tts_cache import MemoryStore, TieredStore, TTSCache, build_key
    from tts_cache.bench import config_policy, synthetic_pcm
    from tts_cache.stores.redis import RedisStore

    frozen: Scope = DEPLOYMENTS[lang]["scope"]
    pool = load_pool(pool_path(lang))
    rng = random.Random(f"{lang}:delete")
    texts = [b.text for b in pool.bases[:30]]
    tags = [{"campaign": "spring"} if i % 3 == 0 else {"campaign": "other"} for i in range(len(texts))]
    await offline.flush(url)

    def make() -> TTSCache:
        policy, matcher = config_policy("A+B+C+J")
        return TTSCache(
            frozen,
            TieredStore(MemoryStore(), RedisStore(url), deadline_ms=200),
            policy=policy,
            matcher=matcher,
        )

    writer = make()
    await writer.start()
    for t, tg in zip(texts, tags, strict=True):
        await writer.capture(t, synthetic_pcm(t, frozen.sample_rate), frozen.sample_rate, tags=tg)
    await writer.drain(30)
    await writer.close()
    a, b = make(), make()  # two serving processes, both warm before anything is removed
    await a.start()
    await b.start()

    def key_of(t: str) -> str:
        return build_key(frozen, canonicalise(t, lang))

    async def served(cache: TTSCache, t: str) -> dict[str, bool]:
        variants = {"exact": t, "filler": rng.choice(filler_variants(t, lang)), "paraphrase": None}
        b_ = next(x for x in pool.bases if x.text == t)
        variants["paraphrase"] = b_.paraphrase
        out = {}
        for name, v in variants.items():
            found = await cache.probe(v)
            out[name] = found.hit is not None
        return out

    client = aioredis.from_url(url)

    async def redis_traces(t: str) -> list[str]:
        k = key_of(t)
        digest = k.split(":")[-1]
        found = []
        for name in await client.keys("tts:*"):
            n = _str(name)
            if digest in n:
                found.append(n)
            elif (await client.type(name)) == b"zset" and await client.zscore(name, digest) is not None:
                found.append(n + " (member)")
            elif (await client.type(name)) == b"set" and await client.sismember(name, k):
                found.append(n + " (member)")
        return found

    async def count_served(cache: TTSCache, ts: list[str]) -> int:
        return sum([any((await served(cache, t)).values()) for t in ts])

    async def count_traces(ts: list[str]) -> int:
        return sum([len(await redis_traces(t)) for t in ts])

    res: dict[str, Any] = {}
    target = texts[1]
    before = {"a": await served(a, target), "b": await served(b, target)}
    await a.store.delete(key_of(target))
    res["delete"] = {
        "before": before,
        "after_same_process": await served(a, target),
        "after_other_process": await served(b, target),
        "redis_traces": await redis_traces(target),
    }
    spring = [t for t, tg in zip(texts, tags, strict=True) if tg["campaign"] == "spring" and t != target]
    n_purged = await a.store.purge(tags={"campaign": "spring"})
    res["purge_tag"] = {
        "purged": n_purged,
        "expected": len(spring),
        "served_after_same_process": await count_served(a, spring),
        "served_after_other_process": await count_served(b, spring),
        "redis_traces": await count_traces(spring),
    }
    word = max((w.strip(",.?!।") for w in texts[2].split()), key=len)
    matched = [t for t in texts if word.lower() in t.lower() and t not in spring and t != target]
    n_purged = await a.store.purge(match=word)
    res["purge_match"] = {
        "match": word,
        "purged": n_purged,
        "expected": len(matched),
        "served_after_same_process": await count_served(a, matched),
        "served_after_other_process": await count_served(b, matched),
        "redis_traces": await count_traces(matched),
    }
    c = make()  # a process started after the purge
    await c.start()
    res["new_process_serves_removed"] = await count_served(c, [target, *spring, *matched])
    for cache in (a, b, c):
        await cache.close()
    await client.aclose()
    await offline.flush(url)
    res["pass_same_process"] = (
        not any(res["delete"]["after_same_process"].values())
        and res["purge_tag"]["served_after_same_process"] == 0
        and res["purge_match"]["served_after_same_process"] == 0
    )
    res["pass_other_process"] = (
        not any(res["delete"]["after_other_process"].values())
        and res["purge_tag"]["served_after_other_process"] == 0
        and res["purge_match"]["served_after_other_process"] == 0
    )
    res["pass_redis_clean"] = (
        not res["delete"]["redis_traces"]
        and not res["purge_tag"]["redis_traces"]
        and not res["purge_match"]["redis_traces"]
    )
    return res


async def redis_ttls(lang: str, url: str) -> dict[str, Any]:
    """The TTL Redis holds after a store and after one hit (real Redis cannot be fast-forwarded)."""
    import redis.asyncio as aioredis

    from tts_cache import MemoryStore, TieredStore, TTSCache, build_key
    from tts_cache.bench import synthetic_pcm
    from tts_cache.policy import DEFAULT_POLICY
    from tts_cache.stores.redis import RedisStore

    frozen: Scope = DEPLOYMENTS[lang]["scope"]
    pool = load_pool(pool_path(lang))
    once, twice = pool.bases[0].text, pool.bases[1].text
    await offline.flush(url)
    cache = TTSCache(
        frozen, TieredStore(MemoryStore(), RedisStore(url), deadline_ms=100), policy=DEFAULT_POLICY
    )
    await cache.start()
    for t in (once, twice):
        await cache.capture(t, synthetic_pcm(t, frozen.sample_rate), frozen.sample_rate)
    await cache.drain(10)
    hit = (await cache.probe(twice)).hit is not None
    await cache.drain(10)
    await asyncio.sleep(0.2)
    client = aioredis.from_url(url)
    try:
        ttl_once = await client.ttl(build_key(frozen, canonicalise(once, lang)))
        ttl_twice = await client.ttl(build_key(frozen, canonicalise(twice, lang)))
    finally:
        await client.aclose()
    await cache.close()
    await offline.flush(url)
    return {
        "hit": hit,
        "ttl_said_once_h": round(ttl_once / 3600, 3),
        "ttl_after_one_hit_d": round(ttl_twice / 86400, 3),
    }


def stored_stage(out: Path, langs: list[str]) -> dict[str, Any]:
    from tts_cache.match.semantic import load_model
    from tts_cache.policy import DEFAULT_POLICY, AllOf, SkipIfMatches

    load_model()
    url = f"redis://localhost:{offline.REDIS_PORT}/{STORED_DB}"
    d = out / "stored"
    d.mkdir(parents=True, exist_ok=True)
    res: dict[str, Any] = {}
    for lang in langs:
        shipped = asyncio.run(stored_inventory(lang, url))
        patterned = asyncio.run(
            stored_inventory(lang, url, policy=AllOf(DEFAULT_POLICY, SkipIfMatches(PII_PATTERNS)))
        )
        _write_jsonl(d / f"{lang}_entries_shipped.jsonl", shipped.pop("entries"))
        _write_jsonl(d / f"{lang}_entries_patterns.jsonl", patterned.pop("entries"))
        res[lang] = {
            "shipped_policy": shipped,
            "with_skip_patterns": patterned,
            "clock": asyncio.run(retention_clock(lang)),
            "redis_ttl": asyncio.run(redis_ttls(lang, url)),
            "deletion": asyncio.run(deletion(lang, url)),
        }
        asyncio.run(offline.flush(url))
    (d / "summary.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


# ---------------------------------------------------------------------------
# Process memory (4.4): resident size after one replay, each configuration in a fresh process.
# ---------------------------------------------------------------------------

_RSS_SCRIPT = """
import asyncio, json, sys
from tts_cache.bench import rss_bytes
from tts_cache.testplan import core, offline
lang, config, db = sys.argv[1], sys.argv[2], sys.argv[3]
before = rss_bytes()
asyncio.run(core.run_corpus(offline.corpus_for(lang, 0.5, 1), config, db))
print(json.dumps({"rss_mb": rss_bytes() / 2**20, "rss_before_mb": before / 2**20}))
"""


def memory_stage(out: Path, langs: list[str]) -> dict[str, Any]:
    url = f"redis://localhost:{offline.REDIS_PORT}/{STORED_DB}"
    res: dict[str, Any] = {}
    for lang in langs:
        res[lang] = {}
        for config in ("OFF", "A+B", "A+B+C+J"):
            got = subprocess.run(
                [sys.executable, "-c", _RSS_SCRIPT, lang, config, url],
                capture_output=True,
                text=True,
                check=False,
            )
            line = (got.stdout.strip().splitlines() or ["{}"])[-1]
            res[lang][config] = json.loads(line) if line.startswith("{") else {"error": got.stderr[-500:]}
    d = out / "memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(res, indent=1))
    return res


# ---------------------------------------------------------------------------
# Live (4.3, 4.4, 4.5): the rows of harness/live_bench.py --plan core and --plan core-fault.
# ---------------------------------------------------------------------------


def _weighted_quantile(groups: list[tuple[list[float], float]], q: float) -> float | None:
    """The ``q`` quantile of a mixture: each group's values share the group's weight."""
    pts = sorted((v, w / len(vals)) for vals, w in groups if vals for v in vals)
    if not pts:
        return None
    total = sum(w for _, w in pts)
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= q * total:
            return v
    return pts[-1][0]


def _clean(x: dict[str, Any]) -> bool:
    return (
        not x["retried"]
        and not x["turn_disrupted"]
        and not x.get("provider_failed")
        and not x.get("lost_in_crash")
        and x.get("ttfa_ms") is not None
    )


def _d(xs: list[float]) -> dict[str, Any]:
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs) if xs else None,
        "p50": pct(xs, 0.5),
        "p95": pct(xs, 0.95),
        "max": max(xs) if xs else None,
    }


def live_cell(rows_by_run: dict[str, list[dict[str, Any]]], lang: str) -> dict[str, Any]:
    from tts_cache.testplan.cells import CORE_PLAN

    service = CORE_PLAN[lang]

    def rows(config: str, r: float) -> list[dict[str, Any]]:
        return rows_by_run.get(f"{lang}_{service}_{config}_{r}", [])

    out: dict[str, Any] = {"service": service}
    for r in (0.5, 0.0):
        off, on = rows("OFF", r), rows("A+B", r)
        l1_off = [x["ttfa_ms"] for x in off if x["first_in_turn"] and _clean(x)]
        l1_on = [x["ttfa_ms"] for x in on if x["first_in_turn"] and _clean(x)]
        first = [x for x in on if x["first_in_turn"] and _clean(x)]
        h = sum(1 for x in first if x["hit"]) / len(first) if first else 0.0
        hit_l1 = [x["ttfa_ms"] for x in first if x["hit"]]
        predicted = _weighted_quantile([(hit_l1, h), (l1_off, 1 - h)], 0.5)
        miss_lookup = [x["lookup_ms"] for x in on if not x["hit"] and x.get("lookup_ms") is not None]
        out[str(r)] = {
            "sentences": {"OFF": len(off), "A+B": len(on)},
            "l1_off": _d(l1_off),
            "l1_on": _d(l1_on),
            "l1_p50_improvement_ms": (pct(l1_off, 0.5) or 0) - (pct(l1_on, 0.5) or 0)
            if l1_off and l1_on
            else None,
            "l1_p95_improvement_ms": (pct(l1_off, 0.95) or 0) - (pct(l1_on, 0.95) or 0)
            if l1_off and l1_on
            else None,
            "first_sentence_hit_share": h,
            "l1_on_p50_predicted_from_mixture": predicted,
            # A later sentence's audio queues behind the previous sentence's in the service, so
            # only a turn's first sentence shows the hit's own time to first audio.
            "l2_hit_first_in_turn": _d(
                [x["ttfa_ms"] for x in on if x["hit"] and x["first_in_turn"] and _clean(x)]
            ),
            "l2_hit_later_in_turn": _d(
                [x["ttfa_ms"] for x in on if x["hit"] and not x["first_in_turn"] and _clean(x)]
            ),
            "l2_miss": _d([x["ttfa_ms"] for x in on if not x["hit"] and _clean(x)]),
            "l2_off": _d([x["ttfa_ms"] for x in off if _clean(x)]),
            "miss_lookup_ms": _d(miss_lookup),
            "char_hit_rate": (
                sum(x["chars"] for x in on if x["hit"]) / sum(x["chars"] for x in on) if on else None
            ),
            "billed_chars": {
                c: sum(x["chars"] * x["attempts"] for x in rs if not x["hit"])
                for c, rs in (("OFF", off), ("A+B", on))
            },
            "requests": {
                c: sum(x["attempts"] for x in rs if not x["hit"]) for c, rs in (("OFF", off), ("A+B", on))
            },
            "errors": {c: sum(len(x["errors"]) for x in rs) for c, rs in (("OFF", off), ("A+B", on))},
        }
    return out


def fault_cell(d: Path, lang: str) -> dict[str, Any]:
    """Host errors, wrong serves, cache time and hits during a 5 s CLIENT PAUSE and a 3 s restart."""
    from tts_cache.testplan.analysis import jsonl
    from tts_cache.testplan.cells import CORE_PLAN

    rid = f"{lang}_{CORE_PLAN[lang]}_A+B_0.5_fault"
    path = d / "rows" / f"{rid}.jsonl"
    if not path.exists():
        return {}
    rows = sorted(jsonl(path), key=lambda x: x["index"])
    events = jsonl(d / "events.jsonl")
    pause = next((e for e in events if e["event"] == "fault_redis_pause"), None)
    kill = next((e for e in events if e["event"] == "fault_redis_kill"), None)
    restart = next((e for e in events if e["event"] == "fault_redis_restart"), None)
    windows = {}
    if pause:
        windows["pause"] = (pause["t"], pause["t"] + pause["ms"] / 1000)
    if kill and restart:
        windows["restart"] = (kill["t"], restart["t"])
    first_fault = min((w[0] for w in windows.values()), default=float("inf"))

    def stats(rs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "sentences": len(rs),
            "hits": sum(1 for x in rs if x["hit"]),
            "cache_lookup_ms": _d([x["lookup_ms"] for x in rs if x.get("lookup_ms") is not None]),
            "ttfa_ms": _d([x["ttfa_ms"] for x in rs if x.get("ttfa_ms") is not None]),
            "errors": sum(len(x["errors"]) for x in rs),
            "provider_failed": sum(1 for x in rs if x.get("provider_failed")),
        }

    out: dict[str, Any] = {"run": rid, "baseline": stats([x for x in rows if x["t_release"] < first_fault])}
    for name, (a, b) in windows.items():
        out[name] = stats([x for x in rows if a <= x["t_release"] <= b + 0.1])
    if restart:
        after = [x for x in rows if x["t_release"] > restart["t"]]
        first_hit = next((x for x in after if x["hit"]), None)
        out["after_restart"] = stats(after)
        out["recovery"] = {
            "first_hit_s_after_restart": None if first_hit is None else first_hit["t_release"] - restart["t"],
            "sentences_until_first_hit": None if first_hit is None else after.index(first_hit),
        }
    out["host_errors"] = sum(len(x["errors"]) for x in rows)
    return out


def live_stage(out: Path, langs: list[str]) -> dict[str, Any]:
    from tts_cache.testplan import analysis

    res: dict[str, Any] = {}
    main_dir, fault_dir = out / "live", out / "live-fault"
    lv = analysis.live(main_dir, {}) if main_dir.exists() else {}
    rows_by_run = (
        {
            p.stem: sorted(analysis.jsonl(p), key=lambda x: x["index"])
            for p in sorted((main_dir / "rows").glob("*.jsonl"))
        }
        if (main_dir / "rows").exists()
        else {}
    )
    fl = analysis.live(fault_dir, {}) if fault_dir.exists() else {}
    for lang in langs:
        res[lang] = {
            "cell": live_cell(rows_by_run, lang),
            "v1_requests_equal_misses": [v for v in lv.get("v1", []) if v["run"].startswith(f"{lang}_")],
            "v2_wrong_serves": [v for v in lv.get("v2", []) if v["run"].startswith(f"{lang}_")],
            "critical": [c for c in lv.get("critical", []) if c["run"].startswith(f"{lang}_")],
            "l3_stall_ms": {k: v for k, v in lv.get("l3", {}).items() if k.startswith(f"{lang}|")},
            "reliability": {k: v for k, v in lv.get("reliability", {}).items() if k.startswith(f"{lang}|")},
            "fault": fault_cell(fault_dir, lang) if fault_dir.exists() else {},
            "fault_v2": [v for v in fl.get("v2", []) if v["run"].startswith(f"{lang}_")],
            "fault_v1": [v for v in fl.get("v1", []) if v["run"].startswith(f"{lang}_")],
        }
    d = out / "live-summary"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    return res


# ---------------------------------------------------------------------------
# Cost (4.4): the monthly model, from the saved rows only.
# ---------------------------------------------------------------------------

# ElastiCache Serverless list prices, US East, looked up 2026-10-20 (secondary sources; the
# official page varies by region): data stored per GB-hour, and ECPUs (1 per KB moved) per million.
REDIS_PRICES = {
    "elasticache_serverless_valkey": {"gb_hour": 0.084, "ecpu_per_million": 0.0023, "min_gb": 0.1},
    "elasticache_serverless_redis_oss": {"gb_hour": 0.125, "ecpu_per_million": 0.0034, "min_gb": 1.0},
}
HOURS_PER_MONTH = 730
# Container memory, for a judge's 2.5 GB: Fargate's list rate per GB-hour (an assumption, stated).
CONTAINER_GB_HOUR = 0.004445


def cost_stage(out: Path, langs: list[str]) -> dict[str, Any]:
    replay_ = json.loads((out / "replay" / "summary.json").read_text())
    stored = json.loads((out / "stored" / "summary.json").read_text())
    memory = (
        json.loads((out / "memory" / "summary.json").read_text())
        if (out / "memory" / "summary.json").exists()
        else {}
    )
    live = (
        json.loads((out / "live-summary" / "summary.json").read_text())
        if (out / "live-summary" / "summary.json").exists()
        else {}
    )
    res: dict[str, Any] = {"redis_prices": REDIS_PRICES, "price_date_redis": "2026-10-20"}
    for lang in langs:
        prov = DEPLOYMENTS[lang]["provider"]
        usd_per_char = USD_PER_MCHARS[prov] / 1e6
        per_rate: dict[str, Any] = {}
        for r in REPLAY_RATES:
            ab = replay_[lang][str(r)]["A+B"]
            off = replay_[lang][str(r)]["OFF"]
            chars_per_sentence = off["chars"] / (off["provider_requests"] or 1)
            hit = ab["char_hit_rate"]["mean"]
            inv = stored[lang]["shipped_policy"]
            # Every miss is stored for 48 h; a clip hit once lives 30 days. Upper bound on the
            # Redis working set: two days of stored clips plus the promoted ones of a month.
            stored_per_sentence = inv["stored"] / inv["sentences"]
            promoted_share = inv["promoted_ttl_entries"] / max(1, inv["stored"])
            bytes_per_clip = inv["bytes_per_clip_mean"]
            scen = []
            for calls in CALLS_PER_MONTH:
                sentences = calls * SENTENCES_PER_CALL
                chars = sentences * chars_per_sentence
                gross = chars * hit * usd_per_char
                stored_month = sentences * stored_per_sentence
                entries = stored_month * (2 / 30) + stored_month * promoted_share
                gb = entries * bytes_per_clip / 1e9
                ecpu = (
                    sentences * hit + stored_month
                ) * bytes_per_clip / 1000 + calls * 4096  # hits, stores, warm starts
                redis = {}
                for name, p in REDIS_PRICES.items():
                    cost = (
                        max(gb, p["min_gb"]) * p["gb_hour"] * HOURS_PER_MONTH
                        + ecpu / 1e6 * p["ecpu_per_million"]
                    )
                    redis[name] = {
                        "usd": round(cost, 2),
                        "net_usd": round(gross - cost, 2),
                        "break_even_char_hit_rate": round(cost / (chars * usd_per_char), 5),
                    }
                scen.append(
                    {
                        "calls_per_month": calls,
                        "chars": round(chars),
                        "chars_avoided": round(chars * hit),
                        "requests_avoided": round(sentences * ab["sentence_hit_rate"]["mean"]),
                        "gross_usd": round(gross, 2),
                        "redis_gb_upper_bound": round(gb, 3),
                        "redis": redis,
                    }
                )
            per_rate[str(r)] = {
                "char_hit_rate": ab["char_hit_rate"],
                "chars_per_sentence": round(chars_per_sentence, 1),
                "stored_per_sentence": round(stored_per_sentence, 3),
                "promoted_share": round(promoted_share, 3),
                "bytes_per_clip": round(bytes_per_clip),
                "scenarios": scen,
            }
        m = memory.get(lang, {})
        rss = {k: round(v.get("rss_mb", 0)) for k, v in m.items() if "rss_mb" in v}
        judge_gb = (rss.get("A+B+C+J", 0) - rss.get("A+B", 0)) / 1024
        cell = live.get(lang, {}).get("cell", {}).get("0.5", {})
        res[lang] = {
            "provider": prov,
            "usd_per_million_chars": USD_PER_MCHARS[prov],
            "price_date": PRICE_DATE,
            "by_rate": per_rate,
            "process_rss_mb": rss,
            "judge_container_usd_per_process_month": round(judge_gb * CONTAINER_GB_HOUR * HOURS_PER_MONTH, 2),
            "live_billed_chars": cell.get("billed_chars"),
            "live_billed_ratio": (
                cell["billed_chars"]["A+B"] / cell["billed_chars"]["OFF"]
                if cell.get("billed_chars") and cell["billed_chars"].get("OFF")
                else None
            ),
        }
    d = out / "cost"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(res, indent=1))
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tts_cache.testplan.core", description=__doc__)
    ap.add_argument(
        "stage", choices=("freeze", "plan", "safety", "scope", "replay", "stored", "memory", "live", "cost")
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--langs", default=",".join(DEPLOYMENTS))
    ap.add_argument("--workers", type=int, default=WORKERS)
    a = ap.parse_args(argv)
    langs = [x for x in a.langs.split(",") if x]
    a.out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if a.stage == "freeze":
        res: Any = freeze(a.out)
    elif a.stage == "plan":
        res = safety_plan(langs)
    elif a.stage == "live":
        res = live_stage(a.out, langs)
    elif a.stage == "cost":
        res = cost_stage(a.out, langs)
    else:
        lock = _lock()
        try:
            if a.stage == "safety":
                res = safety(a.out, langs, a.workers)
            elif a.stage == "scope":
                res = scope_stage(a.out, langs)
            elif a.stage == "replay":
                res = replay(a.out, langs, a.workers)
            elif a.stage == "memory":
                res = memory_stage(a.out, langs)
            else:
                res = stored_stage(a.out, langs)
        finally:
            lock.close()
    print(json.dumps({"stage": a.stage, "wall_s": round(time.time() - t0, 1)}))
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
