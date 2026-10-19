"""Level C, semantic paraphrase."""

import time
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
import yaml
from helpers import SR, scope, tone_pcm

from tts_cache import TTSCache, build_key, canonicalise
from tts_cache.match import Matchers
from tts_cache.match.judge import JUDGE_DEADLINE_MS, JUDGE_THRESHOLD
from tts_cache.match.semantic import (
    DEFAULT_MODEL,
    ROUTES,
    STATIC_MODEL,
    SemanticMatcher,
    acceptance,
    load_model,
)
from tts_cache.metrics import LogSink
from tts_cache.policy import AllOf, LevelC, MinChars, Probation
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.tiered import TieredStore

FIXTURES = Path(__file__).parent / "fixtures" / "semantic"
LANGS = tuple(sorted(p.stem for p in FIXTURES.glob("*.yaml")))


def load(lang: str) -> list[dict]:
    return yaml.safe_load((FIXTURES / f"{lang}.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", LANGS)
def test_fixtures_have_the_required_shape(lang: str) -> None:
    pairs = load(lang)
    assert len(pairs) == 200 and len({p["a"] for p in pairs}) == 200
    assert Counter(p["label"] for p in pairs) == {"paraphrase": 100, "near_miss": 100}
    assert Counter(p["slot"] for p in pairs if p["label"] == "near_miss") == {
        "name": 25,
        "number": 25,
        "date": 25,
        "amount": 25,
    }
    for p in pairs:
        if p["label"] == "near_miss":
            a, b = p["a"].split(), p["b"].split()
            diff = [i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y]
            assert len(a) == len(b) and diff and diff[-1] - diff[0] == len(diff) - 1, p


@pytest.fixture(scope="module")
def model():
    return load_model()


@pytest.mark.slow
@pytest.mark.parametrize("lang", LANGS)
def test_acceptance_check_matches_the_routing_map(lang: str, model) -> None:
    """Section 5's check per language; ROUTES must record its outcome."""
    result = acceptance(lang, load(lang), model)
    print(
        f"{lang}: threshold {result.threshold:.4f} (max near-miss {result.near_miss_max:.4f}), "
        f"paraphrases accepted {result.paraphrase_share:.0%}, "
        f"near-misses accepted {result.near_miss_accepted}, "
        f"mean similarity paraphrase {result.paraphrase_mean} near-miss {result.near_miss_mean}, "
        f"{'PASS' if result.passed else 'FAIL'}"
    )
    assert result.near_miss_accepted == 0
    assert (ROUTES[lang] is not None) == result.passed


@pytest.mark.slow
@pytest.mark.parametrize("lang", LANGS)
def test_static_model_fails_the_check_even_with_the_vetoes(lang: str) -> None:
    """Why the routing map points at the ONNX encoder: the static model's paraphrase recall is too low."""
    result = acceptance(lang, load(lang), load_model(STATIC_MODEL))
    assert result.near_miss_accepted == 0 and not result.passed


@pytest.mark.slow
@pytest.mark.parametrize(("name", "budget_ms"), [(STATIC_MODEL, 2.0), (DEFAULT_MODEL, 6.0)])
def test_embed_and_probe_within_budget(name: str, budget_ms: float) -> None:
    sc = scope()
    load_model(name)
    matcher = SemanticMatcher({"en": name}, threshold=0.0)
    pairs = [p for lang in LANGS for p in load(lang)]
    stored = [p["b"] for p in pairs] + [
        f"Hot set sentence number {i} for the semantic index." for i in range(400)
    ]
    for s in stored:
        c = canonicalise(s, "en")
        matcher.add(sc, build_key(sc, c), c, s)
    queries = [canonicalise(p["a"], "en") for p in pairs]
    for q in queries[:20]:
        matcher.probe(sc, q)  # warm up the tokenizer
    times = []
    for q in queries:
        t = time.perf_counter()
        matcher.probe(sc, q)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    p99 = times[int(0.99 * len(times))]
    p50 = times[len(times) // 2]
    print(f"{name}: embed + probe over {len(stored)} entries: p50 {p50:.3f} ms, p99 {p99:.3f} ms")
    assert p99 < budget_ms


# Serving, with a stub encoder: sentences of one meaning share a vector.

MEANINGS = {
    "your order will arrive on monday": 0,
    "your order is going to reach you on monday": 0,
    "your order will arrive on tuesday": 0,  # the encoder is fooled; the slot veto is not
    "thanks for calling acme": 1,
}


class StubEncoder:
    def encode(self, sentences):
        out = []
        for s in sentences:
            v = np.zeros(16, dtype=np.float32)
            v[MEANINGS.get(s, 2 + hash(s) % 14)] = 1.0
            out.append(v)
        return np.stack(out)


async def make(policy, routes=None, judge=None) -> tuple[TTSCache, LogSink]:
    sc = scope()
    memory = MemoryStore()
    semantic = SemanticMatcher(
        routes if routes is not None else {"en": "stub"}, threshold=0.9, encoder=StubEncoder(), judge=judge
    )
    sink = LogSink(log=None)
    cache = TTSCache(
        sc,
        TieredStore(memory, MemoryStore()),
        policy=policy,
        matcher=Matchers(semantic=semantic),
        metrics=sink,
    )
    stored = "Your order will arrive on Monday."
    await cache.capture(stored, tone_pcm(stored), SR)
    await cache.drain(10)
    memory.load_warm(sc, [memory.peek(build_key(sc, canonicalise(stored, "en")))])  # type: ignore[list-item]
    semantic.refresh(sc, memory)
    return cache, sink


async def test_level_c_hit_carries_the_stored_text() -> None:
    cache, sink = await make(AllOf(Probation(), MinChars(), LevelC()))
    hit = await cache.lookup("Your order is going to reach you on Monday.")
    assert hit is not None and hit.level == "C"
    assert hit.spoken_text == "Your order will arrive on Monday."
    assert sink.counts["hit"] == 1
    await cache.close()


async def test_level_c_is_off_without_the_policy_and_for_unrouted_languages() -> None:
    cache, _ = await make(AllOf(Probation(), MinChars()))
    assert await cache.lookup("Your order is going to reach you on Monday.") is None
    await cache.close()
    cache, _ = await make(AllOf(Probation(), MinChars(), LevelC()), routes={"en": None})
    assert await cache.lookup("Your order is going to reach you on Monday.") is None
    await cache.close()


def test_default_routes_follow_the_acceptance_check() -> None:
    assert set(ROUTES) == {"en", "hi", "bn", "ta", "te", "kn", "ml", "mr", "gu", "pa", "or", "as"}
    assert {lang for lang, m in ROUTES.items() if m == DEFAULT_MODEL} == set(ROUTES)
    assert SemanticMatcher().model_for("hi-IN") == DEFAULT_MODEL
    assert SemanticMatcher().model_for("or-IN") == DEFAULT_MODEL
    assert SemanticMatcher().model_for("fr") is None and SemanticMatcher().model_for("ne") is None


# The judge, with a stub: the real model is tested in test_judge.py.

STORED = "Your order will arrive on Monday."
ASKED = "Your order is going to reach you on Monday."


class StubJudge:
    def __init__(self, value: float = 1.0, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def score(self, stored: str, asked: str) -> float:
        self.calls.append((stored, asked))
        if self.error is not None:
            raise self.error
        return self.value


async def test_level_c_serves_only_what_the_judge_accepts() -> None:
    judge = StubJudge(JUDGE_THRESHOLD)
    cache, sink = await make(AllOf(Probation(), MinChars(), LevelC()), judge=judge)
    hit = await cache.lookup(ASKED)
    assert hit is not None and hit.level == "C" and hit.spoken_text == STORED
    assert judge.calls == [(STORED, ASKED)]
    judge.value = JUDGE_THRESHOLD - 0.01
    assert await cache.lookup(ASKED) is None
    assert cache.matcher.semantic.judged == 1  # type: ignore[attr-defined]
    assert sink.counts["hit"] == 1 and sink.counts["fail_open"] == 0
    await cache.close()


async def test_the_judge_sees_only_candidates_the_vetoes_let_through() -> None:
    judge = StubJudge()
    cache, _ = await make(AllOf(Probation(), MinChars(), LevelC()), judge=judge)
    assert await cache.lookup("Your order will arrive on Tuesday.") is None  # a changed day: the veto refuses
    assert await cache.lookup("Thanks for calling Acme.") is None  # another meaning: below the threshold
    assert judge.calls == []
    await cache.close()


async def test_a_failing_judge_fails_open_to_a_miss() -> None:
    cache, sink = await make(
        AllOf(Probation(), MinChars(), LevelC()), judge=StubJudge(error=RuntimeError("boom"))
    )
    assert await cache.lookup(ASKED) is None
    assert sink.counts["fail_open"] == 1
    await cache.close()


async def test_a_judge_extends_the_default_lookup_deadline() -> None:
    plain, _ = await make(AllOf(Probation(), MinChars(), LevelC()))
    judged, _ = await make(AllOf(Probation(), MinChars(), LevelC()), judge=StubJudge())
    assert judged.deadline_s - plain.deadline_s == pytest.approx(JUDGE_DEADLINE_MS / 1000)
    await plain.close()
    await judged.close()
