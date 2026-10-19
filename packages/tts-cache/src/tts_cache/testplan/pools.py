"""Pool checks for the test plan (TEST_PLAN.md 6.3): structure, dissimilarity and fixture overlap.

``python -m tts_cache.testplan.pools check hi`` prints every problem in
``harness/corpus/hi.yaml``; ``--select 140`` drops the most conflicted bases
until no pair reaches the bound and at most 140 remain, keeping domains
balanced, and rewrites the file.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from tts_cache.canonical import canonicalise
from tts_cache.corpus import POOL_LANGUAGES, Pool, labelled, load_pool, validate_pool

ROOT = Path(__file__).resolve().parents[5]
CORPUS_DIR = ROOT / "harness" / "corpus"
FIXTURES = ROOT / "packages" / "tts-cache" / "tests" / "fixtures" / "semantic"
# No two bases may score this high under the shipped encoder; Level C never serves below it
# (FLOOR_THRESHOLD), so a pair under it cannot be served for one another. Pairs from
# REPORT_FROM upward are written out so the margin is visible.
DISSIMILARITY_BOUND = 0.90
REPORT_FROM = 0.85
POOL_SIZE = 140


def pool_path(lang: str) -> Path:
    return CORPUS_DIR / f"{lang}.yaml"


def similarity_matrix(pool: Pool, texts: list[str] | None = None) -> Any:
    import numpy as np

    from tts_cache.match.semantic import _unit, load_model

    lang = pool.language
    texts = texts if texts is not None else [b.text for b in pool.bases]
    v = _unit(load_model().encode([canonicalise(t, lang) for t in texts]))
    s = v @ v.T
    np.fill_diagonal(s, 0.0)
    return s


def close_pairs(pool: Pool, sims: Any = None, lo: float = REPORT_FROM) -> list[dict[str, Any]]:
    import numpy as np

    s = similarity_matrix(pool) if sims is None else sims
    out = []
    for i, j in zip(*np.where(np.triu(s, 1) >= lo), strict=True):
        a, b = pool.bases[int(i)], pool.bases[int(j)]
        out.append(
            {"a": a.id, "b": b.id, "sim": round(float(s[i, j]), 4), "a_text": a.text, "b_text": b.text}
        )
    return sorted(out, key=lambda p: -p["sim"])


def fixture_texts(lang: str) -> set[str]:
    import yaml

    path = FIXTURES / f"{lang}.yaml"
    if not path.exists():
        return set()
    pairs = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {canonicalise(p[k], lang) for p in pairs for k in ("a", "b")}


def fixture_overlap(pool: Pool) -> list[str]:
    fx = fixture_texts(pool.language)
    out = []
    for b in pool.bases:
        for name, t in (("text", b.text), ("paraphrase", b.paraphrase)):
            if canonicalise(t, pool.language) in fx:
                out.append(f"{b.id}: {name} appears in tests/fixtures/semantic/{pool.language}.yaml")
    return out


# The exact-only mix has no decoys, so at r = 0.25 it would need 150 bases; it runs at r = 0.5 only.
ASSEMBLIES = (("labelled", 0.25), ("labelled", 0.5), ("exact", 0.5))


def assembly_problems(pool: Pool, seeds: range = range(1, 11)) -> list[str]:
    out = []
    for mix, r in ASSEMBLIES:
        for seed in seeds:
            try:
                labelled(pool, 200, r, 0.10, seed, mix=mix)
            except ValueError as e:
                out.append(f"assembly r={r} mix={mix} seed={seed}: {e}")
    return out


def check(pool: Pool, *, full: bool = True) -> dict[str, Any]:
    sims = similarity_matrix(pool)
    pairs = close_pairs(pool, sims)
    over = [p for p in pairs if p["sim"] >= DISSIMILARITY_BOUND]
    problems = validate_pool(pool) + fixture_overlap(pool)
    problems += [f"{p['a']} and {p['b']} score {p['sim']} >= {DISSIMILARITY_BOUND}" for p in over]
    if len(pool.bases) != POOL_SIZE:
        problems.append(f"{len(pool.bases)} bases, expected {POOL_SIZE}")
    if full and not problems:
        problems += assembly_problems(pool)
    para = paraphrase_scores(pool)
    return {
        "language": pool.language,
        "bases": len(pool.bases),
        "domains": dict(Counter(b.domain for b in pool.bases)),
        "problems": problems,
        "close_pairs": pairs,
        "paraphrase_sim": para,
    }


def paraphrase_scores(pool: Pool) -> dict[str, Any]:
    """Similarity of each base to its authored paraphrase and meaning change (informational)."""
    import numpy as np

    from tts_cache.match.semantic import _unit, load_model

    lang = pool.language
    enc = load_model()
    base = _unit(enc.encode([canonicalise(b.text, lang) for b in pool.bases]))
    para = _unit(enc.encode([canonicalise(b.paraphrase, lang) for b in pool.bases]))
    mean = _unit(enc.encode([canonicalise(b.meaning_change, lang) for b in pool.bases]))
    ps = (base * para).sum(1)
    ms = (base * mean).sum(1)
    return {
        "paraphrase": {b.id: round(float(x), 4) for b, x in zip(pool.bases, ps, strict=True)},
        "meaning_change": {b.id: round(float(x), 4) for b, x in zip(pool.bases, ms, strict=True)},
        "paraphrase_at_floor": int((ps >= DISSIMILARITY_BOUND).sum()),
        "paraphrase_mean": round(float(np.mean(ps)), 4),
    }


def select(pool: Pool, size: int = POOL_SIZE) -> Pool:
    """Drop the most conflicted bases until no pair reaches the bound and ``size`` remain.

    Drops keep the domains balanced.
    """
    import numpy as np

    s = similarity_matrix(pool)
    alive = np.ones(len(pool.bases), bool)
    domains = [b.domain for b in pool.bases]

    def drop_one(candidates: Any) -> None:
        counts = Counter(d for d, a in zip(domains, alive, strict=True) if a)
        idx = [int(i) for i in np.where(candidates)[0]]
        # most conflicts first, then the most crowded domain, then the highest worst pair
        conf = ((s >= DISSIMILARITY_BOUND) & alive[None, :]).sum(1)
        worst = np.where(alive[None, :], s, 0).max(1)
        i = max(idx, key=lambda i: (conf[i], counts[domains[i]], worst[i]))
        alive[i] = False

    while True:
        conflicted = alive & (((s >= DISSIMILARITY_BOUND) & alive[None, :]).sum(1) > 0)
        if not conflicted.any():
            break
        drop_one(conflicted)
    while alive.sum() > size:
        drop_one(alive)
    return Pool(pool.language, tuple(b for b, a in zip(pool.bases, alive, strict=True) if a))


def write_pool(pool: Pool, path: Path) -> None:
    import yaml

    data = {
        "language": pool.language,
        "bases": [
            {
                "id": b.id,
                "domain": b.domain,
                "text": b.text,
                "slots": [{"span": s.span, "kind": s.kind, "alts": list(s.alts)} for s in b.slots],
                "paraphrase": b.paraphrase,
                "meaning_change": b.meaning_change,
            }
            for b in pool.bases
        ],
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")


def write_dissimilarity_report(langs: list[str]) -> Path:
    out = {}
    for lang in langs:
        if pool_path(lang).exists():
            out[lang] = close_pairs(load_pool(pool_path(lang)))
    path = CORPUS_DIR / "dissimilarity.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


MANIFEST = CORPUS_DIR / "assembled.json"
MANIFEST_SEEDS = range(1, 11)


def assembled_manifest(langs: list[str] | None = None) -> dict[str, Any]:
    """SHA-256 and label counts of every corpus the run assembles (2.1 freezes them).

    The corpora are built deterministically from the pools, the code and the seeds,
    so their digests freeze them without committing the JSONL itself: the labelled
    mix at r = 0.25 and 0.5, the exact-only mix at r = 0.5 and the r = 0 control,
    seeds 1 to 10, in every language with a pool.
    """
    import hashlib

    from tts_cache.corpus import control

    out: dict[str, Any] = {}
    for lang in langs or [lang for lang in POOL_LANGUAGES if pool_path(lang).exists()]:
        pool = load_pool(pool_path(lang))
        for seed in MANIFEST_SEEDS:
            builds = {
                f"labelled_0.25_{seed}": labelled(pool, 200, 0.25, 0.10, seed),
                f"labelled_0.5_{seed}": labelled(pool, 200, 0.5, 0.10, seed),
                f"exact_0.5_{seed}": labelled(pool, 200, 0.5, 0.10, seed, mix="exact"),
                f"control_0.0_{seed}": control(pool, seed),
            }
            for name, corpus in builds.items():
                text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in corpus.events())
                out[f"{lang}_{name}"] = {
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "counts": {k: v for k, v in corpus.counts().items() if v},
                }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tts_cache.testplan.pools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("lang", choices=POOL_LANGUAGES)
    c.add_argument("--path")
    c.add_argument("--select", type=int, help="drop conflicted bases down to this many and rewrite")
    c.add_argument("--quick", action="store_true", help="skip the assembly check")
    sub.add_parser("report")
    sub.add_parser("manifest", help="write harness/corpus/assembled.json")
    args = ap.parse_args(argv)
    if args.cmd == "manifest":
        MANIFEST.write_text(json.dumps(assembled_manifest(), indent=1, ensure_ascii=False), encoding="utf-8")
        print(MANIFEST)
        return 0
    if args.cmd == "report":
        print(write_dissimilarity_report(list(POOL_LANGUAGES)))
        return 0
    path = Path(args.path) if args.path else pool_path(args.lang)
    pool = load_pool(path)
    if args.select:
        before = len(pool.bases)
        pool = select(pool, args.select)
        write_pool(pool, path)
        print(f"selected {len(pool.bases)} of {before} bases; rewrote {path}")
    res = check(pool, full=not args.quick)
    print(f"{res['language']}: {res['bases']} bases, domains {res['domains']}")
    print(
        f"paraphrase similarity mean {res['paraphrase_sim']['paraphrase_mean']}, "
        f"{res['paraphrase_sim']['paraphrase_at_floor']} at or above {DISSIMILARITY_BOUND}"
    )
    n_close = len(res["close_pairs"])
    print(f"pairs in [{REPORT_FROM}, {DISSIMILARITY_BOUND}) or above: {n_close}; top 5:")
    for p in res["close_pairs"][:5]:
        print(f"  {p['sim']}  {p['a']}: {p['a_text']}  |  {p['b']}: {p['b_text']}")
    for p in res["problems"]:
        print("PROBLEM:", p)
    print("OK" if not res["problems"] else f"{len(res['problems'])} problems")
    return 0 if not res["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
