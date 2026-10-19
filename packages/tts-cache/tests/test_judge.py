"""Level C's judge: an NLI cross-encoder reads the stored and the requested sentence together."""

import time
from collections import Counter
from pathlib import Path

import pytest
import yaml

from tts_cache import canonicalise
from tts_cache.match.judge import JUDGE_DEADLINE_MS, JUDGE_THRESHOLD, calibrate, load_judge
from tts_cache.match.slots import slots, span_veto

FIXTURES = Path(__file__).parent / "fixtures"
SERVES = FIXTURES / "judge" / "level_c_serves.yaml"
LANGS = ("en", "hi", "bn", "as", "pa", "gu", "mr", "or", "ta", "te", "kn", "ml")


def serves() -> list[dict]:
    return yaml.safe_load(SERVES.read_text(encoding="utf-8"))


def semantic_pairs() -> list[dict]:
    return [
        {**p, "lang": lang}
        for lang in LANGS
        for p in yaml.safe_load((FIXTURES / "semantic" / f"{lang}.yaml").read_text(encoding="utf-8"))
    ]


def test_serves_fixture_has_the_test_plan_shape() -> None:
    pairs = serves()
    assert len(pairs) == 1228 and len({(p["lang"], p["a"], p["b"]) for p in pairs}) == 1228
    assert Counter(p["label"] for p in pairs) == {"paraphrase": 1135, "changed": 93}
    assert {p["lang"] for p in pairs} == set(LANGS)


class TableJudge:
    def __init__(self, table: dict[tuple[str, str], float]) -> None:
        self.table = table

    def score(self, stored: str, asked: str) -> float:
        return self.table[(stored, asked)]


def test_calibrate_picks_the_lowest_threshold_that_refuses_every_other_pair() -> None:
    pairs = [
        {"a": "a1", "b": "b1", "label": "paraphrase"},
        {"a": "a2", "b": "b2", "label": "paraphrase"},
        {"a": "a3", "b": "b3", "label": "near_miss"},
        {"a": "a4", "b": "b4", "label": "changed"},
    ]
    judge = TableJudge({("a1", "b1"): 0.9, ("a2", "b2"): 0.4, ("a3", "b3"): 0.5, ("a4", "b4"): 0.2})
    c = calibrate(pairs, judge)
    assert c.threshold == pytest.approx(0.5, abs=1e-5) and c.refused_max == 0.5
    assert c.paraphrase_share == 0.5 and c.pairs == 4


@pytest.fixture(scope="module")
def judge():
    return load_judge()


@pytest.mark.slow
def test_judge_refuses_every_changed_meaning_level_c_served(judge) -> None:
    """The 93 wrong serves of the test plan, with a margin; about half the paraphrases still serve."""
    pairs = serves()
    c = calibrate(pairs, judge)
    print(f"highest changed pair {c.refused_max:.3f}; paraphrases kept at {JUDGE_THRESHOLD}: ", end="")
    scores = judge.scores([(p["a"], p["b"]) for p in pairs])
    para = [s for s, p in zip(scores, pairs, strict=True) if p["label"] == "paraphrase"]
    kept = sum(s >= JUDGE_THRESHOLD for s in para) / len(para)
    print(f"{kept:.1%}")
    assert c.refused_max <= JUDGE_THRESHOLD - 0.05
    assert kept >= 0.45


@pytest.mark.slow
def test_judge_alone_misses_changed_digits_so_it_runs_after_the_vetoes(judge) -> None:
    """Changed amounts and ids can score as entailed; the slot veto refuses every one of them."""
    pairs = semantic_pairs()
    near = [p for p in pairs if p["label"] == "near_miss"]
    scores = judge.scores([(p["a"], p["b"]) for p in near])
    through = [p for s, p in zip(scores, near, strict=True) if s >= JUDGE_THRESHOLD]
    assert through, "the judge now refuses every one-slot near-miss; revisit the module docstring"
    for p in through:
        a, b = canonicalise(p["a"], p["lang"]), canonicalise(p["b"], p["lang"])
        assert slots(a, p["lang"], p["a"]) != slots(b, p["lang"], p["b"]) or span_veto(a, b, p["lang"]), p


@pytest.mark.slow
def test_judge_within_its_deadline_budget(judge) -> None:
    pairs = serves()[:220]
    for p in pairs[:20]:
        judge.score(p["a"], p["b"])
    times = []
    for p in pairs[20:]:
        t = time.perf_counter()
        judge.score(p["a"], p["b"])
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    p50, p99 = times[len(times) // 2], times[int(0.99 * len(times))]
    print(f"judge, both directions: p50 {p50:.1f} ms, p99 {p99:.1f} ms")
    assert p99 < JUDGE_DEADLINE_MS
