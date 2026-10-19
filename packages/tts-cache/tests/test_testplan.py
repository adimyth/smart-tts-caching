"""The test plan's calculators: U5 and U6 summaries, U2, attribution, the impact model and the report."""

import base64
import hashlib
import json
from pathlib import Path

import numpy as np

from tts_cache import codec
from tts_cache.testplan import analysis, report, usability


def test_metrics_only_reads_the_log_alone() -> None:
    lines = [
        json.dumps({"event": "hit", "chars": 40, "key": "k1"}),
        json.dumps({"event": "miss", "chars": 60, "key": "k2"}),
        json.dumps({"event": "miss", "chars": 20, "reason": "deadline"}),
        json.dumps({"event": "reject", "reason": "duration"}),
        "not json",
        json.dumps({"event": "store", "bytes": 100}),
    ]
    m = usability.metrics_only(lines)
    assert (m["hits"], m["misses"], m["saved_chars"], m["chars"]) == (1, 2, 40, 120)
    assert m["char_hit_rate"] == 40 / 120
    assert m["rejects"] == {"duration": 1}


def _row(
    i: int, t: float, ms: float, hit: bool, tier: str | None = None, degraded: str | None = None
) -> dict:
    return {
        "index": i,
        "t": t,
        "lookup_ms": ms,
        "hit": hit,
        "tier": tier,
        "degraded": degraded,
        "chars": 10,
        "decode_ms": 1.0,
    }


def test_u5_summary_splits_the_fault_window() -> None:
    rows = [_row(0, 0.0, 1.0, True, "shared"), _row(1, 1.0, 1.0, False)]
    rows += [_row(2, 2.0, 21.0, False, degraded="deadline"), _row(3, 3.0, 5.0, False, degraded="fail_open")]
    rows += [_row(4, 5.0, 1.0, False, degraded="breaker"), _row(5, 6.0, 1.0, True, "shared")]
    s = usability._u5_summary("client_pause", rows, {"start": 1.5, "end": 4.0}, [])
    assert s["during_n"] == 2
    assert s["added_ms"]["max"] == 20.0
    assert s["hit_rate"] == {"before": 0.5, "during": 0.0, "after": 0.5}
    # Recovery: the first lookup after the window that reached a healthy store.
    assert s["recovery_s"] == 2.0
    assert s["pass"] and s["within_deadline"]
    assert not usability._u5_summary("kill", rows, {"start": 1.5, "end": 4.0}, ["probe: boom"])["pass"]


def test_u2_shares_and_better_knobs() -> None:
    off = {
        "cells": {
            f"en|{c}|0.25|labelled|serverless": {"char_hit_rate": {"mean": v}}
            for c, v in (("A", 0.2), ("A+B", 0.25), ("A+B+C", 0.24))
        }
    }
    sz = {
        "memory": {
            f"en|{4 << 20}|2000": {"char_hit_rate": 0.24},
            f"en|{16 << 20}|2000": {"char_hit_rate": 0.26},
        }
    }
    u = analysis.u2(off, sz, ["en"])["en"]
    assert u["configs"]["0.25"]["best"] == "A+B"
    assert abs(u["configs"]["0.25"]["default_share_of_best"] - 0.96) < 1e-9
    assert u["better_knobs"] == [{"warm_start_bytes": 16 << 20, "warm_texts": 2000, "gain_points": 2.0}]


def _digest(pcm: bytes) -> str:
    return hashlib.sha256(pcm).hexdigest()[:16]


def test_attribution_accepts_the_decoded_clip_or_the_captured_pcm(tmp_path: Path) -> None:
    sr = 24_000
    pcm = (np.sin(np.arange(sr) / 7.0) * 8000).astype(np.int16).tobytes()
    opus = codec.encode(pcm, sr, 1, "opus")
    decoded = codec.decode(opus, "opus", sr, 1)
    (tmp_path / "exports").mkdir()
    entry = {
        "key": "k",
        "audio": base64.b64encode(opus).decode(),
        "codec": "opus",
        "sample_rate": sr,
        "channels": 1,
    }
    (tmp_path / "exports" / "run.jsonl").write_text(json.dumps({"entry": entry}) + "\n")
    rows = [
        {"index": 0, "hit": False, "key": "k", "digest": _digest(pcm)},
        {"index": 1, "hit": True, "key": "k", "digest": _digest(pcm)},  # process tier: the captured PCM
        {"index": 2, "hit": True, "key": "k", "digest": _digest(decoded)},  # shared tier: the decoded clip
        {"index": 3, "hit": True, "key": "k", "digest": "0" * 16},
    ]
    a = analysis._attribution(tmp_path, "run", rows)
    assert a["checked"] == 3
    assert a["matched_original_pcm"] == 1
    assert a["mismatched"] == [3]


def test_impact_sizes_redis_for_two_days_of_misses() -> None:
    off = {
        "cells": {
            "en|A|0.25|labelled|serverless": {
                "char_hit_rate": {"mean": 0.25, "lo": 0.2, "hi": 0.3},
                "stored_bytes": 265_000,
                "billed_chars": 1_000,
                "seeds": [1],
            }
        },
        "sessions": {"en|A|0.25": {"chars_mean": 1_000, "turn_start_hit_share": 0.1}},
    }
    out = analysis.impact(off, {}, {}, {}, ["en"])
    x = out["money"]["en|A|0.25|openai|100000"]
    # 100,000 calls × 1,000 chars × 0.25 saved, at $16 per million characters.
    assert abs(x["usd_saved"]["mean"] - 400.0) < 1e-6
    # Two days of a month's misses: 100,000 × 2/30 × 750 chars × 265 bytes.
    assert abs(x["redis_gb"] - 100_000 * 2 / 30 * 750 * 265 / 1e9) < 1e-3


def test_report_renders_every_table_and_chart(tmp_path: Path) -> None:
    a = {
        "run_dir": "x",
        "langs": ["en"],
        "offline": {
            "cells": {
                f"en|{c}|{r}|labelled|serverless": {
                    "char_hit_rate": {"mean": 0.1 + r / 2, "lo": 0.1, "hi": 0.3, "n": 2},
                    "sentence_hit_rate": {"mean": 0.2, "lo": 0.1, "hi": 0.3, "n": 2},
                    "served_chars": 100,
                    "seeds": [1, 2],
                }
                for c in ("A", "A+B", "A+B+C")
                for r in (0.0, 0.25, 0.5)
            },
            "sessions": {
                "en|A+B+C|0.25": {
                    "n": 3,
                    "rate_p10": 0,
                    "rate_p50": 0.2,
                    "rate_p90": 0.4,
                    "zero_hit_share": 0.3,
                    "all_turns_cached_share": 0,
                    "turn_start_hit_share": 0.1,
                    "hist": [1, 2, 0, 0, 0, 0, 0, 0, 0, 0],
                }
            },
        },
        "sizes": {
            "capacity": {
                "curves": {
                    "redis|en|1.0": [
                        {"frac": 0.1, "char_hit_rate": 0.1, "evicted": 5, "fail_open": 0},
                        {"frac": 1.0, "char_hit_rate": 0.3, "evicted": 0, "fail_open": 0},
                    ]
                },
                "knee": {"redis|en|1.0": 0.5},
            },
            "population": {
                "en|1000": {"recall": {"paraphrase": 0.8}, "wrong": {}, "critical": 0, "probes": 10},
                "en|2000": {
                    "recall": {"paraphrase": 0.7},
                    "wrong": {"paraphrase": 1},
                    "critical": 0,
                    "probes": 10,
                },
            },
        },
        "live": {"l1": {"en|openai_http|OFF|0.25": {"n": 3, "p50": 900.0, "p95": 1500.0, "max": 1600.0}}},
        "l1_values": {
            "en|openai_http|OFF|0.25": [800.0, 900.0, 1500.0],
            "en|openai_http|A|0.25": [10.0, 900.0],
        },
        "usability": {},
        "vchecks": {"V1": {"pass": True, "offline": True, "live": None}},
        "verdicts": {
            "en": {"recommended": "A", "A": {"safe": True}, "B": {"safe": True}, "C": {"safe": False}}
        },
        "impact": {},
        "composed": {},
    }
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps(a))
    md = tmp_path / "TEST_REPORT.md"
    rep = report.Report(json.loads(p.read_text()), tmp_path / "test-report", "test-report")
    md.write_text(rep.render())
    (tmp_path / "test-report" / "index.json").write_text(json.dumps(rep.charts))
    assert report.check(md) == []
    svg = (tmp_path / "test-report" / "capacity.svg").read_text()
    assert svg.startswith("<svg") and "redis en 1.0" in svg


def test_table_and_interval_formatting() -> None:
    assert report.ci({"mean": 0.25, "lo": 0.2, "hi": 0.3, "n": 10}) == "25.0 [20.0, 30.0]"
    assert report.ci({"mean": 0.25, "lo": 0.25, "hi": 0.25, "n": 1}) == "25.0"
    assert report.ci(None) == "–"
    assert report.table(("a", "b"), []) == "_No data._\n"
    assert report.table(("a", "b"), [(1, "x")]).splitlines()[2] == "| 1 | x |"
