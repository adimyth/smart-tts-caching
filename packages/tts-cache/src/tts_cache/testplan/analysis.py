"""Everything the report states, computed from the saved files of one run directory (TEST_PLAN.md 2.5).

Layout of ``<run dir>`` (``harness/out/testplan/<run id>/``):

- ``offline/``: the matrix of section 8 (``runs.jsonl``, ``rows/``);
- ``sizes/capacity``, ``sizes/pressure``, ``sizes/memory``, ``sizes/population``: section 9;
- ``usability/``: section 10;
- ``live/``: section 11 (``rows/``, ``exports/``, ``requests/``, ``events.jsonl``);
- ``preflight/preflight.json``: 2.3;
- ``asr/asr.jsonl``: 7.2.

``python -m tts_cache.testplan.analysis <run dir>`` writes ``<run dir>/analysis.json``.
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

from tts_cache.testplan.cells import (
    CORE,
    PROVIDER,
    REDIS_USD_PER_GB_MONTH,
    SEED,
    SPREAD,
    USD_PER_MCHARS,
)
from tts_cache.testplan.oracle import OUTSIDE_LIST
from tts_cache.testplan.stats import bootstrap, dist, pct

LABELS = (
    "new",
    "exact",
    "surface",
    "punct_inner",
    "punct_end",
    "filler",
    "paraphrase",
    "slot_change",
    "meaning_change",
    "punct_question",
)
CRITICAL_LABELS = ("new", "slot_change", "meaning_change")
CONFIGS = ("OFF", "A", "A+B", "A+B+C")
LEVEL_CONFIG = {"A": "A", "B": "A+B", "C": "A+B+C"}
GATES_MS = {"A": 1.0, "B": 1.0, "C": 6.0}
VOLUMES = (10_000, 100_000, 1_000_000)
PROBATION_DAYS = 2.0
DEFAULT_POPULATION = 2_000  # warm_texts default: the population "at the default size"
V4_EXCEPTIONS = ("capture_reject", "provider_failed")


def jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def latest(runs: list[dict[str, Any]], key: str = "id") -> list[dict[str, Any]]:
    """The last non-error record per id (a resumed or repeated run keeps its latest result)."""
    by: dict[str, dict[str, Any]] = {}
    for r in runs:
        if "error" in r and r.get(key) in by:
            continue
        by[r.get(key, "")] = r
    return list(by.values())


# ---------------------------------------------------------------------------
# Offline (sections 7 and 8).
# ---------------------------------------------------------------------------


def _rows_files(off: Path) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    for p in sorted((off / "rows").glob("*.jsonl")):
        yield p.stem, jsonl(p)


def offline(off: Path) -> dict[str, Any]:
    runs = [r for r in latest(jsonl(off / "runs.jsonl")) if "error" not in r]
    errors = [r for r in jsonl(off / "runs.jsonl") if "error" in r]
    ids = {r["id"] for r in runs}
    by_cell: dict[tuple[str, str, float, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_cell[(r["lang"], r["config"], r["r"], r["mix"], r["profile"])].append(r)

    # Per-sentence aggregates.
    label_n: Counter[tuple[str, str, float, str]] = Counter()
    label_hit: Counter[tuple[str, str, float, str]] = Counter()
    label_vis: Counter[tuple[str, str, float, str]] = Counter()
    label_vis_hit: Counter[tuple[str, str, float, str]] = Counter()
    wrong_by: Counter[tuple[str, str, float, str]] = Counter()
    flagged: Counter[tuple[str, str, float]] = Counter()
    critical: list[dict[str, Any]] = []
    unexplained: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []
    why: Counter[tuple[str, str, str]] = Counter()
    l4: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    l5: dict[tuple[str, str], list[float]] = defaultdict(list)
    sess: dict[tuple[str, str, float], list[dict[str, Any]]] = defaultdict(list)
    turn_first_hit: dict[tuple[str, str, float], list[int]] = defaultdict(list)
    exp_chars: Counter[tuple[str, str, float]] = Counter()
    exp_served: Counter[tuple[str, str, float]] = Counter()
    seq: dict[tuple[str, str, float, int], list[dict[str, Any]]] = {}
    for rid, rows in _rows_files(off):
        if rid not in ids or not rows:
            continue
        r0 = rows[0]
        lang, config, r, mix, profile, seed = (
            r0[k] for k in ("lang", "config", "r", "mix", "profile", "seed")
        )
        if r == 0.0:
            if config != "OFF":
                l4[(lang, config, profile)] += [x["lookup_ms"] + x["capture_ms"] for x in rows]
                # The control holds only new sentences: any serve is critical.
                for x in rows:
                    if x["hit"]:
                        critical.append(
                            {
                                "run": rid,
                                "index": x["index"],
                                "label": "new",
                                "base": x["base"],
                                "level": x.get("level"),
                                "served_from": x.get("served_from"),
                                "spoken_text": x.get("spoken_text"),
                                "config": config,
                                "lang": lang,
                            }
                        )
            continue
        if mix != "labelled" or profile != "serverless":
            continue
        seq[(lang, config, r, seed)] = [
            {
                k: x.get(k)
                for k in (
                    "index",
                    "hit",
                    "level",
                    "lookup_ms",
                    "first_chunk_ms",
                    "chars",
                    "first_in_turn",
                    "turn",
                    "session",
                    "label",
                    "wrong",
                    "why",
                    "reject",
                )
            }
            for x in rows
        ]
        for x in rows:
            if x.get("expected"):
                exp_chars[(lang, config, r)] += x["chars"]
                if x["hit"] and not x["wrong"]:
                    exp_served[(lang, config, r)] += x["chars"]
            k = (lang, config, r, x["label"])
            label_n[k] += 1
            if x["hit"] and not x["wrong"]:
                label_hit[k] += 1
            if x.get("visible"):
                label_vis[k] += 1
                if x["hit"] and not x["wrong"]:
                    label_vis_hit[k] += 1
            if x["hit"] and x["wrong"]:
                wrong_by[k] += 1
            if x.get("flagged"):
                flagged[(lang, config, r)] += 1
            if x.get("critical"):
                critical.append(
                    {
                        "run": rid,
                        "index": x["index"],
                        "label": x["label"],
                        "base": x["base"],
                        "level": x.get("level"),
                        "served_from": x.get("served_from"),
                        "spoken_text": x.get("spoken_text"),
                        "config": config,
                        "lang": lang,
                    }
                )
            if "why" in x:
                why[(lang, config, x["why"].split(":", 1)[0])] += 1
                if x["why"].startswith("unexplained"):
                    unexplained.append(
                        {"run": rid, "index": x["index"], "label": x["label"], "why": x["why"]}
                    )
                if x["why"].split(":", 1)[0] in OUTSIDE_LIST:
                    outside.append({"run": rid, "index": x["index"], "label": x["label"], "why": x["why"]})
            if x["hit"] and x.get("tier"):
                l5[(lang, x["tier"])].append(x["lookup_ms"] + x["first_chunk_ms"])
        # Sessions stand in for calls (7.4).
        per_s: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for x in rows:
            per_s[x["session"]].append(x)
        for s_rows in per_s.values():
            chars = sum(x["chars"] for x in s_rows)
            served = sum(x["chars"] for x in s_rows if x["hit"] and not x["wrong"])
            firsts = [x for x in s_rows if x["first_in_turn"]]
            sess[(lang, config, r)].append(
                {
                    "chars": chars,
                    "served": served,
                    "rate": served / chars if chars else 0.0,
                    "zero": served == 0,
                    "all_turns_cached": bool(firsts) and all(x["hit"] and not x["wrong"] for x in firsts),
                }
            )
        turn_first_hit[(lang, config, r)] += [
            int(x["hit"] and not x["wrong"]) for x in rows if x["first_in_turn"]
        ]

    cells: dict[str, Any] = {}
    for (lang, config, r, mix, profile), rs in sorted(by_cell.items()):
        key = f"{lang}|{config}|{r}|{mix}|{profile}"
        rs = sorted(rs, key=lambda x: x["seed"])
        cells[key] = {
            "lang": lang,
            "config": config,
            "r": r,
            "mix": mix,
            "profile": profile,
            "seeds": [x["seed"] for x in rs],
            "char_hit_rate": bootstrap([x["char_hit_rate"] for x in rs]),
            "sentence_hit_rate": bootstrap([x["sentence_hit_rate"] for x in rs]),
            "capture_eff_servable": bootstrap(
                [x["capture_eff_servable"] for x in rs if x.get("capture_eff_servable") is not None]
            ),
            "capture_eff_expected": bootstrap(
                [x["capture_eff_expected"] for x in rs if x.get("capture_eff_expected") is not None]
            ),
            "capture_expected_pooled": (exp_served[(lang, config, r)] / exp_chars[(lang, config, r)])
            if exp_chars[(lang, config, r)]
            else None,
            "billed_chars": sum(x["billed_chars"] for x in rs),
            "chars": sum(x["chars"] for x in rs),
            "served_chars": sum(x["served_chars"] for x in rs),
            "wrong": sum(x["wrong"] for x in rs),
            "critical": sum(x["critical"] for x in rs),
            "flagged": sum(x["flagged"] for x in rs),
            "unexplained": sum(x["unexplained"] for x in rs),
            "by_level": dict(sum((Counter(x["by_level"]) for x in rs), Counter())),
            "stored_bytes": statistics.fmean([x["stored_bytes"] for x in rs]) if rs else 0,
            "stored": statistics.fmean([x["stored"] for x in rs]) if rs else 0,
            "v1": all(x["v1"] for x in rs),
            "v10": all(x["v10"] for x in rs),
            "wall_s": sum(x["wall_s"] for x in rs),
            "warm_start_ms": dist([s["start_ms"] for x in rs for s in x.get("sessions", [])]),
        }

    # Increments, per seed then pooled (7.1).
    deltas: dict[str, Any] = {}
    for lang in sorted({c["lang"] for c in cells.values()}):
        for r in (0.25, 0.5):

            def rates(config: str, lang: str = lang, r: float = r) -> dict[int, float]:
                return {
                    x["seed"]: x["char_hit_rate"]
                    for x in by_cell.get((lang, config, r, "labelled", "serverless"), [])
                }

            a, ab, abc = rates("A"), rates("A+B"), rates("A+B+C")
            deltas[f"{lang}|{r}"] = {
                "B": bootstrap([ab[s] - a[s] for s in sorted(set(a) & set(ab))]),
                "C": bootstrap([abc[s] - ab[s] for s in sorted(set(ab) & set(abc))]),
            }

    # V7, monotonicity per seed.
    v7 = []
    for (lang, config, r, mix, profile), rs in by_cell.items():
        if config != "A" or mix != "labelled" or r == 0.0:
            continue
        for x in rs:
            hits = {
                c: next(
                    (
                        y["hits"]
                        for y in by_cell.get((lang, c, r, mix, profile), [])
                        if y["seed"] == x["seed"]
                    ),
                    None,
                )
                for c in ("A", "A+B", "A+B+C")
            }
            a, ab, abc = hits["A"], hits["A+B"], hits["A+B+C"]
            if a is not None and ab is not None and abc is not None and not (a <= ab <= abc):
                v7.append({"lang": lang, "r": r, "seed": x["seed"], "profile": profile, "hits": hits})

    recall = {}
    for (lang, config, r, label), n in label_n.items():
        recall[f"{lang}|{config}|{r}|{label}"] = {
            "n": n,
            "served": label_hit[(lang, config, r, label)],
            "recall": label_hit[(lang, config, r, label)] / n if n else None,
            "visible": label_vis[(lang, config, r, label)],
            "recall_visible": label_vis_hit[(lang, config, r, label)] / label_vis[(lang, config, r, label)]
            if label_vis[(lang, config, r, label)]
            else None,
            "wrong": wrong_by[(lang, config, r, label)],
        }
    sessions = {}
    for (lang, config, r), ss in sess.items():
        rates_ = [s["rate"] for s in ss]
        sessions[f"{lang}|{config}|{r}"] = {
            "n": len(ss),
            "rate_p10": pct(rates_, 0.10),
            "rate_p50": pct(rates_, 0.5),
            "rate_p90": pct(rates_, 0.9),
            "served_chars_p10": pct([s["served"] for s in ss], 0.1),
            "served_chars_p50": pct([s["served"] for s in ss], 0.5),
            "served_chars_p90": pct([s["served"] for s in ss], 0.9),
            "chars_mean": statistics.fmean([s["chars"] for s in ss]) if ss else None,
            "zero_hit_share": sum(s["zero"] for s in ss) / len(ss) if ss else None,
            "all_turns_cached_share": sum(s["all_turns_cached"] for s in ss) / len(ss) if ss else None,
            "turn_start_hit_share": statistics.fmean(turn_first_hit[(lang, config, r)])
            if turn_first_hit[(lang, config, r)]
            else None,
            "turns": len(turn_first_hit[(lang, config, r)]),
            "hist": _hist(rates_, 10),
        }
    overhead = {
        f"{lang}|{config}|{profile}": dist(v, (0.5, 0.99, 0.999)) | {"mean": round(statistics.fmean(v), 4)}
        for (lang, config, profile), v in l4.items()
        if v
    }
    hitpath = {f"{lang}|{tier}": dist(v, (0.5, 0.99, 0.999)) for (lang, tier), v in l5.items() if v}
    return {
        "cells": cells,
        "deltas": deltas,
        "recall": recall,
        "sessions": sessions,
        "l4": overhead,
        "l5": hitpath,
        "why": {f"{a}|{b}|{c}": n for (a, b, c), n in why.items()},
        "critical": critical,
        "unexplained": unexplained,
        "outside_list": outside,
        "v7": v7,
        "errors": errors,
        "flagged": {f"{a}|{b}|{c}": n for (a, b, c), n in flagged.items()},
        "seed1": {f"{a}|{b}|{c}": v for (a, b, c, s), v in seq.items() if s == SEED},
        "seqs": {f"{a}|{b}|{c}|{s}": v for (a, b, c, s), v in seq.items()},
        "runs": len(runs),
    }


def _hist(xs: list[float], bins: int) -> list[int]:
    out = [0] * bins
    for x in xs:
        out[min(bins - 1, int(x * bins))] += 1
    return out


# ---------------------------------------------------------------------------
# Sizes (section 9).
# ---------------------------------------------------------------------------


def sizes(d: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cap = jsonl(d / "capacity" / "capacity.jsonl")
    if cap:
        groups: dict[tuple[str, str, float, float], list[dict[str, Any]]] = defaultdict(list)
        for r in cap:
            if "error" not in r:
                groups[(r["arm"], r["lang"], r["s"], r["frac"])].append(r)
        curves: dict[str, Any] = {}
        for (arm, lang, s, frac), rs in sorted(groups.items()):
            k = f"{arm}|{lang}|{s}"
            curves.setdefault(k, []).append(
                {
                    "frac": frac,
                    "char_hit_rate": statistics.fmean(x["char_hit_rate"] for x in rs),
                    "seeds": len(rs),
                    "evicted": statistics.fmean(x.get("evicted_keys", 0) for x in rs),
                    "peak_bytes": statistics.fmean(x.get("peak_bytes", 0) for x in rs),
                    "fail_open": sum(x.get("fail_open", 0) for x in rs),
                }
            )
        knees = {}
        for k, pts in curves.items():
            full = next((p["char_hit_rate"] for p in pts if p["frac"] >= 1.0), None)
            ok = [p["frac"] for p in pts if full and p["char_hit_rate"] >= 0.9 * full]
            knees[k] = min(ok) if ok else None
        out["capacity"] = {"curves": curves, "knee": knees, "errors": [r for r in cap if "error" in r]}
        cal = d / "capacity" / "calibration.json"
        if cal.exists():
            out["capacity"]["calibration"] = json.loads(cal.read_text())
    press = jsonl(d / "pressure" / "pressure_full" / "runs.jsonl") + jsonl(
        d / "pressure" / "pressure_capped" / "runs.jsonl"
    )
    if press:
        g: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in press:
            if "error" in r:
                continue
            frac = r.get("tag", "").removeprefix("cap") or "1.0"
            g[(r["lang"], r["config"], frac)].append(r)
        out["pressure"] = {
            f"{lang}|{config}|{frac}": {
                "char_hit_rate": bootstrap([x["char_hit_rate"] for x in rs]),
                "wrong": sum(x["wrong"] for x in rs),
                "critical": sum(x["critical"] for x in rs),
                "unexplained": sum(x["unexplained"] for x in rs),
                "evicted_misses": sum(x["why"].get("evicted", 0) for x in rs),
                "seeds": len(rs),
            }
            for (lang, config, frac), rs in sorted(g.items())
        }
        out["pressure_errors"] = [r for r in press if "error" in r]
    mem = jsonl(d / "memory" / "memory.jsonl")
    if mem:
        g2: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
        for r in mem:
            if "error" not in r:
                g2[(r["lang"], r["warm_start_bytes"], r["warm_texts"])].append(r)
        out["memory"] = {f"{a}|{b}|{c}": _memory_cell(rs) for (a, b, c), rs in sorted(g2.items())}
        out["memory_errors"] = [r for r in mem if "error" in r]
    pop = jsonl(d / "population" / "population.jsonl")
    if pop:
        out["population"] = {f"{r['lang']}|{r['n']}": r for r in pop if "error" not in r}
        out["population_errors"] = [r for r in pop if "error" in r]
    return out


def _memory_cell(rs: list[dict[str, Any]]) -> dict[str, Any]:
    sessions = [s for r in rs for s in r.get("sessions", [])]

    def mean(key: str, src: list[dict[str, Any]]) -> float | None:
        v = [x[key] for x in src if x.get(key) is not None]
        return round(statistics.fmean(v), 3) if v else None

    tiers: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    l5: dict[str, list[float]] = defaultdict(list)
    miss: list[float] = []
    for r in rs:
        tiers.update(r.get("tiers", {}))
        levels.update(r.get("levels", {}))
        for tier, v in (r.get("l5_p50") or {}).items():
            l5[tier].append(v)
        miss += r.get("miss_lookup_ms") or []
    return {
        "seeds": len(rs),
        "start_ms": mean("start_ms", sessions),
        "fetch_ms": mean("fetch_ms", sessions),
        "load_ms": mean("load_ms", sessions),
        "index_ms": mean("index_ms", sessions),
        "rss_mb": round(statistics.fmean([s["rss"] for s in sessions if s.get("rss")]) / 2**20, 1)
        if any(s.get("rss") for s in sessions)
        else None,
        "tiers": dict(tiers),
        "levels": dict(levels),
        "char_hit_rate": mean("char_hit_rate", rs),
        "first_turn_hits": mean("first_turn_hits", rs),
        "turns": mean("turns", rs),
        "first_turn_hit_ms": dist(
            [x["ms"] for r in rs for x in r.get("first_turn", []) if x["hit"]], (0.5, 0.95)
        ),
        "l5_p50": {t: round(statistics.median(v), 3) for t, v in l5.items()},
        "miss_lookup_ms": dist(miss, (0.5, 0.99)),
    }


# ---------------------------------------------------------------------------
# Live (section 11).
# ---------------------------------------------------------------------------


def live(d: Path, off: dict[str, Any]) -> dict[str, Any]:
    from tts_cache.testplan.offline import corpus_for
    from tts_cache.testplan.oracle import annotate

    if not (d / "rows").exists():
        return {}
    events = jsonl(d / "events.jsonl")
    fault_runs = set()
    redis_fault_wall = next((e["wall"] for e in events if e["event"] == "fault_redis_kill"), None)
    faults_file = d / "faults.json"
    injected = json.loads(faults_file.read_text()) if faults_file.exists() else {}
    runs: dict[str, list[dict[str, Any]]] = {}
    for p in sorted((d / "rows").glob("*.jsonl")):
        rows = sorted(jsonl(p), key=lambda x: x["index"])
        runs[p.stem] = rows
    v1, v2, v4, attribution, transcript = [], [], [], [], []
    rel: dict[str, dict[str, int]] = defaultdict(Counter)  # type: ignore[arg-type]
    l1: dict[str, list[float]] = defaultdict(list)
    l1_by_window: dict[str, list[tuple[float, float]]] = defaultdict(list)
    hit_ttfa: dict[str, list[float]] = defaultdict(list)
    l3: dict[str, list[float]] = defaultdict(list)
    ms_per_char: dict[str, list[float]] = defaultdict(list)
    turn_ttfa: dict[str, list[float]] = defaultdict(list)
    all_ttfa: dict[str, list[float]] = defaultdict(list)
    summaries = {}
    critical = []
    for rid, rows in runs.items():
        if not rows:
            continue
        r0 = rows[0]
        lang, service, config, r = r0["lang"], r0["service"], r0["config"], r0["r"]
        cell = f"{lang}|{service}"
        corpus = corpus_for(lang, r, SEED)
        complete = len(rows) == len(corpus.items)
        # Reliability (2.4).
        for x in rows:
            rel[cell]["sentences"] += 1
            rel[cell]["retried"] += int(x["retried"])
            rel[cell]["provider_failed"] += int(bool(x.get("provider_failed")))
            rel[cell]["lost_in_crash"] += int(bool(x.get("lost_in_crash")))
            rel[cell]["attempts"] += x["attempts"]
            rel[cell]["errors"] += len(x["errors"])
        # Latency rows: no retry, no disruption, no failure.
        clean = [
            x
            for x in rows
            if not x["retried"]
            and not x["turn_disrupted"]
            and not x.get("provider_failed")
            and not x.get("lost_in_crash")
            and x.get("ttfa_ms") is not None
        ]
        for x in clean:
            if x["first_in_turn"]:
                l1[f"{cell}|{config}|{r}"].append(x["ttfa_ms"])
                l1_by_window[f"{cell}|{config}"].append((x["wall"], x["ttfa_ms"]))
            if x["hit"]:
                hit_ttfa[f"{cell}|{config}"].append(x["ttfa_ms"])
            if config == "OFF":
                all_ttfa[cell].append(x["ttfa_ms"])
                if x["first_in_turn"]:
                    turn_ttfa[cell].append(x["ttfa_ms"])
                if x["bytes"] and x["chars"]:
                    ms_per_char[cell].append(x["audio_ms"] / x["chars"])
        by_turn: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for x in rows:
            by_turn[x["turn"]].append(x)
        for t_rows in by_turn.values():
            if any(
                x["retried"]
                or x["turn_disrupted"]
                or x.get("provider_failed")
                or x.get("t_first_audio") is None
                for x in t_rows
            ):
                continue
            stall = 0.0
            for a, b in pairwise(t_rows):
                stall += max(0.0, (b["t_first_audio"] - (a["t_first_audio"] + a["audio_ms"] / 1000)) * 1000)
            l3[f"{cell}|{config}|{r}"].append(stall)
        # V1 live: provider requests = misses + retries, and billed text = the missed sentences.
        reqs = [
            json.loads(x)["on_tts_request"]
            for x in _lines(d / "requests" / f"{rid}.jsonl")
            if '"on_tts_request"' in x
        ]
        # A sentence lost in a crash was still requested; its journaled attempts count as billed.
        misses = [x for x in rows if not x["hit"]]
        expected_requests = sum(x["attempts"] for x in misses)
        req_texts = Counter(q["text"].strip() for q in reqs)
        miss_texts = Counter()
        for x in misses:
            miss_texts[x["text"].strip()] += x["attempts"]
        ok1 = len(reqs) == expected_requests and req_texts == miss_texts
        v1.append(
            {
                "run": rid,
                "requests": len(reqs),
                "expected": expected_requests,
                "texts_match": req_texts == miss_texts,
                "pass": ok1,
            }
        )
        if config == "OFF":
            summaries[rid] = _live_summary(rows, complete)
            continue
        # V2 live: the oracle over the live rows.
        ann = [dict(x, served_key=x["key"] if x["hit"] else None, chars=x["chars"]) for x in rows]
        if complete:
            ann = annotate(corpus, ann, config)
            for x in ann:
                if x["critical"]:
                    critical.append(
                        {
                            "run": rid,
                            "index": x["index"],
                            "label": x["label"],
                            "level": x["level"],
                            "spoken_text": x.get("spoken_text"),
                            "text": x["text"],
                        }
                    )
            v2.append(
                {
                    "run": rid,
                    "wrong": sum(1 for x in ann if x["wrong"]),
                    "critical": sum(1 for x in ann if x["critical"]),
                    "wrong_by_label": dict(Counter(x["label"] for x in ann if x["wrong"])),
                }
            )
        summaries[rid] = _live_summary(ann, complete)
        # V4 live = offline, sentence by sentence (seed 1).
        seq = off.get("seed1", {}).get(f"{lang}|{config}|{r}")
        if seq is not None:
            diffs = []
            # A killed Redis loses the stores of the runs already under way; runs started after
            # it begin from an empty database anyway and are checked in full.
            hit_by_kill = redis_fault_wall is not None and any(y["wall"] < redis_fault_wall for y in rows)
            for x, o in zip(rows, seq, strict=False):
                if bool(x["hit"]) == bool(o["hit"]):
                    continue
                reason = None
                # A provider error on the first attempt, retried or not, is the provider failing.
                if x.get("provider_failed") or (x.get("retried") and x.get("errors")):
                    reason = "provider_failed"
                elif x.get("lost_in_crash"):
                    reason = "lost_in_crash"
                elif hit_by_kill and x["wall"] >= (redis_fault_wall or 0):
                    reason = "redis_fault_injected"
                elif x.get("resumed_session"):
                    reason = "resumed_session"
                elif _source_rejected(x, rows):
                    reason = "capture_reject"
                elif _source_failed(x, rows, corpus):
                    reason = "provider_failed"
                diffs.append(
                    {
                        "index": x["index"],
                        "label": x["label"],
                        "live_hit": x["hit"],
                        "offline_hit": o["hit"],
                        "explained_by": reason,
                        "turn_disrupted": x["turn_disrupted"],
                    }
                )
            v4.append(
                {
                    "run": rid,
                    "mismatches": len(diffs),
                    "unexplained": [y for y in diffs if y["explained_by"] is None],
                    "explained": [y for y in diffs if y["explained_by"] is not None],
                    # V4's own exceptions are capture rejects and provider failures; a mismatch
                    # explained by an injected fault, a crash or a resume is reported, not passed.
                    "outside_v4_exceptions": [
                        y for y in diffs if y["explained_by"] not in (None, *V4_EXCEPTIONS)
                    ],
                    "pass": all(y["explained_by"] for y in diffs),
                }
            )
        # Transcript truth and attribution (7.2).
        bad_t = [
            x["index"]
            for x in rows
            if x["hit"] and x["level"] in ("B", "C") and x.get("transcript_ok") is False
        ]
        transcript.append(
            {
                "run": rid,
                "bc_hits": sum(1 for x in rows if x["hit"] and x["level"] in ("B", "C")),
                "mismatched": bad_t,
            }
        )
        attribution.append(_attribution(d, rid, rows))
    windows = {}
    for k, pts in l1_by_window.items():
        if not pts:
            continue
        t0 = min(t for t, _ in pts)
        w: dict[int, list[float]] = defaultdict(list)
        for t, v in pts:
            w[int((t - t0) // 600)].append(v)
        windows[k] = {str(i): {"p50": pct(v, 0.5), "n": len(v)} for i, v in sorted(w.items())}
    return {
        "runs": summaries,
        "v1": v1,
        "v2": v2,
        "v4": v4,
        "transcript": transcript,
        "attribution": attribution,
        "critical": critical,
        "reliability": {k: dict(v) for k, v in rel.items()},
        "l1": {k: dist(v) for k, v in l1.items()},
        "l1_values": {k: v for k, v in l1.items()},
        "hit_ttfa": {k: dist(v) for k, v in hit_ttfa.items()},
        "l3": {k: dist(v) for k, v in l3.items()},
        "turn_ttfa": {k: v for k, v in turn_ttfa.items()},
        "all_ttfa": {k: v for k, v in all_ttfa.items()},
        "ms_per_char": {k: statistics.median(v) for k, v in ms_per_char.items() if v},
        "windows": windows,
        "events": [e for e in events if e["event"] not in ("export",)],
        "faults": injected,
        "fault_runs": sorted(fault_runs),
        "exports": [e for e in events if e["event"] == "export"],
    }


def _lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _source_rejected(x: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    """A live miss whose earlier copy was never stored: no earlier miss of its base kept audio."""
    earlier = [y for y in rows if y["index"] < x["index"] and y["base"] == x["base"] and not y["hit"]]
    return bool(earlier) and all(not y.get("bytes") for y in earlier)


def _source_failed(x: dict[str, Any], rows: list[dict[str, Any]], corpus: Any) -> bool:
    earlier = [y for y in rows if y["index"] < x["index"] and y["base"] == x["base"]]
    return any(y.get("provider_failed") or y.get("lost_in_crash") or y.get("retried") for y in earlier)


def _live_summary(rows: list[dict[str, Any]], complete: bool) -> dict[str, Any]:
    chars = sum(x["chars"] for x in rows)
    served = sum(x["chars"] for x in rows if x["hit"] and not x.get("wrong"))
    return {
        "complete": complete,
        "sentences": len(rows),
        "hits": sum(1 for x in rows if x["hit"]),
        "char_hit_rate": served / chars if chars else 0.0,
        "billed_chars": sum(x["chars"] * x["attempts"] for x in rows if not x["hit"]),
        "provider_failed": sum(1 for x in rows if x.get("provider_failed")),
        "retried": sum(1 for x in rows if x["retried"]),
    }


def _attribution(d: Path, rid: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every hit's audio digest equals the stored entry it names (7.2).

    A hit in the process tier plays the PCM the provider returned, which the
    ingest worker keeps beside the Opus clip; any other hit plays the decoded
    clip. So a hit matches the decoded export or the miss that captured its key.
    """
    import base64
    import hashlib

    from tts_cache import codec

    path = d / "exports" / f"{rid}.jsonl"
    if not path.exists():
        return {"run": rid, "checked": 0, "mismatched": [], "note": "no export"}
    entries = {}
    for rec in jsonl(path):
        e = rec.get("entry")
        if e:
            entries[e["key"]] = e
    checked, bad, missing = 0, [], []
    original = 0
    cache: dict[str, str] = {}
    captured: dict[str, str] = {}
    for x in sorted(rows, key=lambda x: x["index"]):
        if not x["hit"] and x.get("key") and x.get("digest"):
            captured.setdefault(x["key"], x["digest"])
        if not x["hit"] or not x.get("digest"):
            continue
        e = entries.get(x["key"])
        if e is None:
            missing.append(x["index"])
            continue
        if x["key"] not in cache:
            pcm = codec.decode(base64.b64decode(e["audio"]), e["codec"], e["sample_rate"], e["channels"])
            cache[x["key"]] = hashlib.sha256(pcm).hexdigest()[:16]
        checked += 1
        if cache[x["key"]] == x["digest"]:
            continue
        if captured.get(x["key"]) == x["digest"]:
            original += 1
            continue
        bad.append(x["index"])
    return {
        "run": rid,
        "checked": checked,
        "matched_original_pcm": original,
        "mismatched": bad,
        "missing_entry": missing,
    }


# ---------------------------------------------------------------------------
# Composition (11.3).
# ---------------------------------------------------------------------------


def compose(off: dict[str, Any], lv: dict[str, Any], reps: int = 5) -> dict[str, Any]:
    """Composed L1 and L3 per live cell, configuration and r (11.3).

    Each offline seed's hit sequence replays with a hit costing its measured
    hit path (lookup plus first decoded chunk) and a miss costing its measured
    lookup plus a provider time to first audio resampled from the cell's OFF
    runs: turn starts from the turn-start distribution, later sentences from
    all sentences. Releases are one 20 ms token apart per word, as live. The
    interval is over seeds; the check is against the live L1 of the same cell.
    """
    import random

    from tts_cache.testplan.offline import corpus_for

    out: dict[str, Any] = {}
    turn_ttfa = lv.get("turn_ttfa", {})
    all_ttfa = lv.get("all_ttfa", {})
    mpc = lv.get("ms_per_char", {})
    seqs = off.get("seqs", {})
    words: dict[tuple[str, float, int], list[int]] = {}
    for cell, starts in sorted(turn_ttfa.items()):
        if not starts:
            continue
        lang = cell.split("|")[0]
        later = all_ttfa.get(cell) or starts
        ms_char = mpc.get(cell, 70.0)
        per: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
            lambda: {"l1": [], "l3": [], "p50": []}
        )
        for key, seq in sorted(seqs.items()):
            k_lang, config, r_s, seed_s = key.split("|")
            if k_lang != lang:
                continue
            r, seed = float(r_s), int(seed_s)
            if (lang, r, seed) not in words:
                words[(lang, r, seed)] = [len(it.text.split(" ")) for it in corpus_for(lang, r, seed).items]
            wc = words[(lang, r, seed)]
            rng = random.Random(f"{cell}|{key}")
            by_turn: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for x in seq:
                by_turn[x["turn"]].append(x)
            seed_l1 = []
            for _ in range(reps):
                for t_rows in by_turn.values():
                    rel = 0.0
                    first: list[tuple[float, float]] = []
                    for i, x in enumerate(t_rows):
                        if i:
                            rel += wc[x["index"]] * 20.0
                        if x["hit"]:
                            cost = x["lookup_ms"] + (x["first_chunk_ms"] or 0.0)
                        else:
                            cost = x["lookup_ms"] + rng.choice(starts if i == 0 else later)
                        first.append((rel + cost, x["chars"] * ms_char))
                    seed_l1.append(first[0][0])
                    per[(config, r_s)]["l1"].append(first[0][0])
                    stall = sum(max(0.0, fb - (fa + da)) for (fa, da), (fb, _) in pairwise(first))
                    per[(config, r_s)]["l3"].append(stall)
            per[(config, r_s)]["p50"].append(pct(seed_l1, 0.5) or 0.0)
        offs = [(lv.get("l1") or {}).get(f"{cell}|OFF|{r}") for r in (0.25, 0.5)]
        p50s = [o["p50"] for o in offs if o and o.get("p50") is not None]
        tol = abs(p50s[0] - p50s[1]) if len(p50s) == 2 else None
        for (config, r_s), v in per.items():
            live_l1 = (lv.get("l1") or {}).get(f"{cell}|{config}|{r_s}")
            comp_p50 = pct(v["l1"], 0.5)
            live_p50 = (live_l1 or {}).get("p50")
            out[f"{cell}|{config}|{r_s}"] = {
                "l1": dist(v["l1"], (0.5, 0.9, 0.95)),
                "l1_p50_seeds": bootstrap(v["p50"]),
                "l3": dist(v["l3"], (0.5, 0.95)),
                "live_l1": live_l1,
                "off_spread_ms": tol,
                "agrees": None
                if (live_p50 is None or comp_p50 is None or tol is None)
                else abs(live_p50 - comp_p50) <= max(tol, 1.0),
            }
        # OFF composed: every sentence a miss, for the waiting-removed arithmetic of 13.2.
        for r_s in ("0.25", "0.5"):
            rng = random.Random(f"{cell}|OFF|{r_s}")
            vals = [rng.choice(starts) for _ in range(2000)]
            out[f"{cell}|OFF|{r_s}"] = {
                "l1": dist(vals, (0.5, 0.9, 0.95)),
                "live_l1": (lv.get("l1") or {}).get(f"{cell}|OFF|{r_s}"),
            }
    return out


# ---------------------------------------------------------------------------
# V-checks, verdicts (13.1) and the impact model (13.2).
# ---------------------------------------------------------------------------


def gates(off: dict[str, Any], sz: dict[str, Any], lang: str, n: int) -> dict[str, Any]:
    """V6 (n = 1,000) and V9 (n = 2,000) for one language."""
    a = off["l4"].get(f"{lang}|A|serverless") or {}
    pop = (sz.get("population") or {}).get(f"{lang}|{n}") or {}
    b99 = (pop.get("b_probe_ms") or {}).get("p99")
    c99 = (pop.get("c_probe_ms") or {}).get("p99")
    return {
        "A_mean_ms": a.get("mean"),
        "A": None if a.get("mean") is None else a["mean"] < GATES_MS["A"],
        "B_p99_ms": b99,
        "B": None if b99 is None else b99 < GATES_MS["B"],
        "C_p99_ms": c99,
        "C": None if c99 is None else c99 < GATES_MS["C"],
        "n": n,
    }


def verdicts(off: dict[str, Any], sz: dict[str, Any], lv: dict[str, Any], langs: list[str]) -> dict[str, Any]:
    out = {}
    pop = sz.get("population") or {}
    live_crit: Counter[tuple[str, str]] = Counter()
    for c in lv.get("critical", []):
        live_crit[(c["run"].split("_")[0], c["run"].split("_")[-2])] += 1
    for lang in langs:
        g6 = gates(off, sz, lang, 1_000)
        res: dict[str, Any] = {"v6": g6}
        for level, config in LEVEL_CONFIG.items():
            # A serve of a critical label is always a wrong serve; the r = 0 control counts too.
            offline_crit = sum(
                rec["wrong"]
                for key, rec in off["recall"].items()
                if key.startswith(f"{lang}|{config}|") and key.split("|")[3] in CRITICAL_LABELS
            ) + sum(
                1
                for c in off.get("critical", [])
                if c.get("lang") == lang and c.get("config") == config and c["run"].split("_")[2] == "0.0"
            )
            pop_crit = (
                (pop.get(f"{lang}|{DEFAULT_POPULATION}") or {}).get("critical", 0) if level != "A" else 0
            )
            lc = live_crit[(lang, config)]
            safe = offline_crit == 0 and pop_crit == 0 and lc == 0
            entry: dict[str, Any] = {
                "safe": safe,
                "offline_critical": offline_crit,
                "population_critical": pop_crit,
                "live_critical": lc,
            }
            if level == "A":
                eff = [
                    off["cells"].get(f"{lang}|A|{r}|labelled|serverless", {}).get("capture_expected_pooled")
                    for r in (0.25, 0.5)
                ]
                eff_v = [e for e in eff if e is not None]
                entry["capture_eff_expected"] = min(eff_v) if eff_v else None
                entry["working"] = safe and bool(eff_v) and min(eff_v) >= 0.99 and bool(g6["A"])
            else:
                delta = off["deltas"].get(f"{lang}|0.25", {}).get(level, {})
                entry["delta_r025"] = delta
                entry["worth"] = safe and (delta.get("lo") or 0) > 0 and bool(g6[level])
            res[level] = entry
        rec = "A"
        if res["B"]["worth"]:
            rec = "A+B"
            if res["C"]["worth"]:
                rec = "A+B+C"
        res["recommended"] = rec
        res["punct_question_served"] = sum(
            v["wrong"]
            for k, v in off["recall"].items()
            if k.startswith(f"{lang}|") and k.endswith("|punct_question")
        )
        out[lang] = res
    return out


def impact(
    off: dict[str, Any], sz: dict[str, Any], comp: dict[str, Any], lv: dict[str, Any], langs: list[str]
) -> dict[str, Any]:
    """Derived (13.2): monthly saving, waiting removed, Redis cost and break-even, per provider."""
    out: dict[str, Any] = {}
    knees = (sz.get("capacity") or {}).get("knee") or {}
    knee = knees.get("redis|en|1.0") or 1.0
    for lang in langs:
        for config in ("A", "A+B", "A+B+C"):
            for r in (0.25, 0.5):
                cell = off["cells"].get(f"{lang}|{config}|{r}|labelled|serverless")
                ses = off["sessions"].get(f"{lang}|{config}|{r}")
                if not cell or not ses:
                    continue
                rate = cell["char_hit_rate"]
                chars_per_call = ses["chars_mean"] or 0
                miss_chars_per_call = chars_per_call * (1 - (rate["mean"] or 0))
                bytes_per_char = (
                    (cell["stored_bytes"] / max(1, cell["billed_chars"] / max(1, len(cell["seeds"]))))
                    if cell["billed_chars"]
                    else 265
                )
                for provider in sorted(set(PROVIDER.values())):
                    price = USD_PER_MCHARS[provider] / 1e6
                    for vol in VOLUMES:
                        total = vol * chars_per_call
                        saved = {
                            k: (None if rate[k] is None else total * rate[k]) for k in ("mean", "lo", "hi")
                        }
                        # Probation keeps a singleton 48 h (BASE_POLICY): the store holds about two
                        # days of misses, plus the promoted repeats, which the hits already bound.
                        stored_gb = (
                            vol * PROBATION_DAYS / 30 * miss_chars_per_call * bytes_per_char * knee / 1e9
                        )
                        redis_usd = stored_gb * REDIS_USD_PER_GB_MONTH
                        money = {k: (None if v is None else v * price) for k, v in saved.items()}
                        out[f"{lang}|{config}|{r}|{provider}|{vol}"] = {
                            "chars_saved": saved,
                            "usd_saved": money,
                            "redis_gb": round(stored_gb, 3),
                            "redis_usd": round(redis_usd, 2),
                            "net_usd": None if money["mean"] is None else round(money["mean"] - redis_usd, 2),
                            "break_even_rate": (redis_usd / (total * price)) if total else None,
                            "turn_start_share": ses["turn_start_hit_share"],
                        }
    # Waiting removed per hit turn and per call, from composed L1 against OFF.
    wait = {}
    for key, v in comp.items():
        cell_lang, service, config, r = key.split("|")
        if config == "OFF":
            continue
        base = comp.get(f"{cell_lang}|{service}|OFF|{r}") or {}
        ses = off["sessions"].get(f"{cell_lang}|{config}|{r}") or {}
        b50, c50 = (base.get("l1") or {}).get("p50"), (v.get("l1") or {}).get("p50")
        b90, c90 = (base.get("l1") or {}).get("p90"), (v.get("l1") or {}).get("p90")
        turns_per_call = (ses.get("turns") or 0) / max(1, ses.get("n") or 1)
        wait[key] = {
            "per_turn_p50_ms": None if b50 is None or c50 is None else round(b50 - c50, 1),
            "per_turn_p90_ms": None if b90 is None or c90 is None else round(b90 - c90, 1),
            "per_call_p50_ms": None if b50 is None or c50 is None else round((b50 - c50) * turns_per_call, 1),
            "turns_per_call": round(turns_per_call, 2),
        }
    return {
        "money": out,
        "wait": wait,
        "knee_fraction": knee,
        "redis_usd_per_gb_month": REDIS_USD_PER_GB_MONTH,
    }


DEFAULT_KNOBS = {"warm_start_bytes": 4 << 20, "warm_texts": 2_000}


def u2(off: dict[str, Any], sz: dict[str, Any], langs: list[str]) -> dict[str, Any]:
    """U2: the default configuration's saving as a share of the best found, and the knobs worth changing."""
    out: dict[str, Any] = {}
    for lang in langs:
        per_r = {}
        for r in (0.25, 0.5):
            rates = {
                c: (off["cells"].get(f"{lang}|{c}|{r}|labelled|serverless") or {})
                .get("char_hit_rate", {})
                .get("mean")
                for c in ("A", "A+B", "A+B+C")
            }
            have = {c: v for c, v in rates.items() if v is not None}
            if not have:
                continue
            best = max(have, key=lambda c: have[c])
            default = have.get("A+B+C")
            per_r[str(r)] = {
                "rates": have,
                "best": best,
                "default_share_of_best": None if default is None or not have[best] else default / have[best],
            }
        mem = {k: v for k, v in (sz.get("memory") or {}).items() if k.startswith(f"{lang}|")}
        knobs = []
        default_key = f"{lang}|{DEFAULT_KNOBS['warm_start_bytes']}|{DEFAULT_KNOBS['warm_texts']}"
        base = (mem.get(default_key) or {}).get("char_hit_rate")
        if base is not None:
            for k, v in mem.items():
                rate = v.get("char_hit_rate")
                if rate is not None and rate - base > 0.01:
                    _, wsb, wt = k.split("|")
                    knobs.append(
                        {
                            "warm_start_bytes": int(wsb),
                            "warm_texts": int(wt),
                            "gain_points": round((rate - base) * 100, 2),
                        }
                    )
        out[lang] = {"configs": per_r, "default_memory_rate": base, "better_knobs": knobs}
    return out


def vchecks(
    off: dict[str, Any], sz: dict[str, Any], lv: dict[str, Any], us: dict[str, Any], langs: list[str]
) -> dict[str, Any]:
    v: dict[str, Any] = {}
    cells = off.get("cells", {})
    v["V1"] = {
        "offline": all(c["v1"] for c in cells.values()) if cells else None,
        "offline_fail": [k for k, c in cells.items() if not c["v1"]],
        "live": all(x["pass"] for x in lv.get("v1", [])) if lv.get("v1") else None,
        "live_fail": [x for x in lv.get("v1", []) if not x["pass"]],
    }
    v["V2"] = {
        "offline_critical": off.get("critical", []),
        "live_critical": lv.get("critical", []),
        "pass": not off.get("critical") and not lv.get("critical"),
        "punct_question_served": sum(
            r["wrong"] for k, r in off.get("recall", {}).items() if k.endswith("|punct_question")
        ),
    }
    # A reason must come from V3's closed list; a named cause outside it fails V3 as surely as none.
    v["V3"] = {
        "unexplained": off.get("unexplained", []),
        "outside_list": off.get("outside_list", []),
        "pass": not off.get("unexplained") and not off.get("outside_list"),
    }
    v["V4"] = {
        "runs": lv.get("v4", []),
        "pass": all(x["pass"] for x in lv.get("v4", [])) if lv.get("v4") else None,
    }
    u4 = us.get("u4") or {}
    v5 = us.get("v5") or {}
    v["V5"] = v5 or {"pass": None}
    g6 = {lang: gates(off, sz, lang, 1_000) for lang in langs}
    v["V6"] = {
        "per_language": g6,
        "pass": all(all(g[k] is not False for k in ("A", "B", "C")) for g in g6.values()),
    }
    v["V7"] = {"violations": off.get("v7", []), "pass": not off.get("v7")}
    press = sz.get("pressure") or {}
    cap = sz.get("capacity") or {}
    v["V8"] = {
        "pressure_critical": {k: p["critical"] for k, p in press.items() if p["critical"]},
        "capacity_errors": len(cap.get("errors", [])) + len(sz.get("pressure_errors", [])),
        "pass": (not any(p["critical"] for p in press.values()))
        and not cap.get("errors")
        and not sz.get("pressure_errors")
        if (press or cap)
        else None,
    }
    g9 = {lang: gates(off, sz, lang, 2_000) for lang in langs}
    v["V9"] = {
        "per_language": g9,
        "pass": all(all(g[k] is not False for k in ("A", "B", "C")) for g in g9.values()),
    }
    u6 = us.get("u6") or {}
    v["V10"] = {
        "offline": all(c["v10"] for c in cells.values()) if cells else None,
        "u6": u6.get("pass"),
        "pass": (all(c["v10"] for c in cells.values()) if cells else True) and u6.get("pass") is not False,
    }
    v["U4"] = u4
    return v


def analyse(run_dir: Path) -> dict[str, Any]:
    from tts_cache.corpus import POOL_LANGUAGES
    from tts_cache.testplan.pools import pool_path

    langs = [lang for lang in POOL_LANGUAGES if pool_path(lang).exists()]
    off = (
        offline(run_dir / "offline")
        if (run_dir / "offline").exists()
        else {"cells": {}, "recall": {}, "sessions": {}, "l4": {}, "l5": {}, "deltas": {}}
    )
    sz = sizes(run_dir / "sizes") if (run_dir / "sizes").exists() else {}
    lv = live(run_dir / "live", off) if (run_dir / "live").exists() else {}
    us = {}
    for p in sorted((run_dir / "usability").glob("*.json")) if (run_dir / "usability").exists() else []:
        us[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    us["u2"] = u2(off, sz, langs)
    comp = compose(off, lv) if lv else {}
    ver = verdicts(off, sz, lv, langs)
    imp = impact(off, sz, comp, lv, langs)
    vc = vchecks(off, sz, lv, us, langs)
    pre_p = run_dir / "preflight" / "preflight.json"
    preflight = json.loads(pre_p.read_text()) if pre_p.exists() else None
    asr = jsonl(run_dir / "asr" / "asr.jsonl")
    meta = {}
    for name in ("freeze.json", "timings.json"):
        p = run_dir / name
        if p.exists():
            meta[name.removesuffix(".json")] = json.loads(p.read_text())
    return {
        "run_dir": str(run_dir),
        "langs": langs,
        "cells_live": {"core": CORE, "spread": SPREAD},
        "offline": {k: v for k, v in off.items() if k != "seed1"},
        "sizes": sz,
        "live": {k: v for k, v in lv.items() if k not in ("l1_values", "turn_ttfa", "all_ttfa")},
        "l1_values": lv.get("l1_values", {}),
        "usability": us,
        "composed": comp,
        "verdicts": ver,
        "impact": imp,
        "vchecks": vc,
        "preflight": preflight,
        "asr": asr,
        "meta": meta,
    }


def main(argv: list[str] | None = None) -> int:
    run_dir = Path((argv or sys.argv[1:])[0])
    res = analyse(run_dir)
    (run_dir / "analysis.json").write_text(
        json.dumps(res, ensure_ascii=False, default=str, indent=None), encoding="utf-8"
    )
    print(run_dir / "analysis.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
