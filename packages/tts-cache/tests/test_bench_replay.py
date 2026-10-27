import asyncio
import json
from collections import Counter

import pytest

from tts_cache import TTSCache, canonicalise
from tts_cache.bench import SimulatedProvider, bench_scope, run_core, save, sweep
from tts_cache.cli import main as cli
from tts_cache.corpus import Event, generate, read_log, write_log
from tts_cache.replay import report, simulate
from tts_cache.split import split_text
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.sqlite import SQLiteStore
from tts_cache.stores.tiered import TieredStore

DAY = 86400.0


@pytest.mark.parametrize("r", [0.0, 0.1, 0.25, 0.5, 0.75, 0.9])
def test_corpus_has_the_target_repetition_rate(r: float) -> None:
    c = generate(200, r, seed=3)
    sentences = c.sentences
    assert len(sentences) == 200 and c.repeats == round(200 * r)
    assert len(set(sentences)) == 200 - c.repeats
    # Lengths follow the sampled target to within one word, and never pass 160 before the terminator.
    assert all(8 <= len(s) <= 161 for s in sentences) and sum(len(s) >= 20 for s in sentences) > 190
    assert all(1 <= len(t) <= 5 for t in c.turns)
    # The splitter ends every generated sentence exactly where the generator does.
    assert [x for t in c.turns for x in split_text(" ".join(t), "en")] == sentences


def test_corpus_is_seeded_and_zipf_skewed() -> None:
    a, b = generate(400, 0.75, seed=9), generate(400, 0.75, seed=9)
    assert a.turns == b.turns
    counts = sorted(Counter(a.sentences).values(), reverse=True)
    assert counts[0] >= 5 * counts[len(counts) // 2]


@pytest.mark.parametrize("language", ["hi-IN", "ta-IN"])
def test_corpus_in_indic_languages(language: str) -> None:
    c = generate(60, 0.5, seed=2, language=language)
    assert [x for t in c.turns for x in split_text(" ".join(t), language)] == c.sentences


@pytest.mark.slow  # a wall-clock budget: a local check, deselected in CI
async def test_bench_adds_under_1_ms_per_sentence_at_r0(tmp_path) -> None:
    """Gate: single-process profile."""
    corpus = generate(200, 0.0, seed=1)
    run = await run_core(corpus, 0.0, cache_on=True, provider=SimulatedProvider(), workdir=tmp_path)
    s = run.summary()
    assert s["hits"] == 0 and s["stored"] == 200
    assert s["added_mean_ms"] < 1.0, s
    print({k: s[k] for k in ("added_mean_ms", "added_p50_ms", "added_p99_ms")})


@pytest.mark.parametrize("r", [0.25, 0.5])
async def test_replay_reproduces_the_bench_hit_rate(r: float, tmp_path) -> None:
    """Gate: replay over the bench's own log, within two points."""
    corpus = generate(200, r, seed=4)
    provider = SimulatedProvider()
    bench = (await run_core(corpus, r, cache_on=True, provider=provider, workdir=tmp_path)).summary()
    write_log(tmp_path / "log.jsonl", corpus.events())
    replay = report(read_log(tmp_path / "log.jsonl"), rules=False)
    assert abs(bench["hit_rate"] - replay["hit_rate"]) <= 0.02, (bench["hit_rate"], replay["hit_rate"])
    # Billed characters fall by the hit rate.
    assert (
        abs((1 - provider.chars / bench["chars"]) - bench["hit_rate"]) < 1e-4
    )  # the summary rounds to 4 places


async def test_sweep_outputs_table_and_plot(tmp_path) -> None:
    rows = await sweep((0.0, 0.5), n=40, log_dir=tmp_path / "logs")
    assert [(x["r"], x["cache"]) for x in rows] == [(0.0, "off"), (0.0, "on"), (0.5, "off"), (0.5, "on")]
    off, on = rows[2], rows[3]
    assert on["billed_chars"] < off["billed_chars"] and on["ttfa_p50_ms"] < off["ttfa_p50_ms"]
    assert off["hit_rate"] == 0 and on["hit_rate"] > 0.3
    save(rows, tmp_path / "out")
    assert (tmp_path / "out" / "bench.svg").read_text().startswith("<svg")
    assert json.loads((tmp_path / "out" / "bench.json").read_text())[3]["cache"] == "on"
    assert (tmp_path / "logs" / "bench-r0.5.jsonl").exists()


# Replay semantics on hand-made logs.


def ev(ts: float, text: str, session: str = "s", language: str = "en") -> Event:
    return Event(ts, session, text, language, "voice-a")


def test_probation_and_sliding_ttl() -> None:
    t = 1_792_000_000.0
    log = [
        ev(t, "Thanks for calling Acme today."),
        ev(t + 3 * DAY, "Thanks for calling Acme today."),  # past 48 h probation: miss, stored again
        ev(t + 3.5 * DAY, "Thanks for calling Acme today."),  # hit, promoted to 30 days
        ev(t + 20 * DAY, "Thanks for calling Acme today."),  # still alive
        ev(t + 60 * DAY, "Thanks for calling Acme today."),  # 40 days without a hit: expired
    ]
    r = simulate(log)
    assert r.hits == 2 and r.sentences == 5


def test_capture_is_not_visible_within_its_turn() -> None:
    r = simulate([ev(0, "Sure, let me check that. Sure, let me check that.")])
    assert r.hits == 0
    r = simulate([ev(0, "Sure, let me check that."), ev(30, "Sure, let me check that.")])
    assert r.hits == 1


def test_rule_increments_credit_the_rule_that_matched() -> None:
    log = [ev(i * 60, t, f"s{i}", "hi") for i, t in enumerate(["आपका कोड १२३ है।", "आपका कोड 123 है।"] * 3)]
    rep = report(log)
    # With the rule: 1 miss, 5 hits. Without it the two spellings miss once each: 4 hits. 1/6 more.
    assert abs(rep["rule_increments"]["ascii_digits"] - 1 / 6) < 1e-3
    assert rep["rule_increments"]["lowercase"] == 0


def test_report_fields() -> None:
    t = 1_792_000_000.0
    log = [
        ev(t + i * 600, f"Opening line for everyone. Item number {i % 4} today.", f"s{i}") for i in range(40)
    ]
    rep = report(log, rules=False)
    assert 0 < rep["hit_rate"] < 1 and rep["steady_state_hit_rate"] >= rep["hit_rate"]
    assert len(rep["timeline"]) == 10
    assert (
        rep["gap_first_to_second_s"]["count"] == 5 and rep["gap_first_to_second_s"]["histogram"]["<1h"] == 5
    )
    assert set(rep["warm_coverage"]) == {"2MB", "4MB", "8MB"} and rep["warm_coverage"]["4MB"] > 0.9
    assert rep["size"]["entries"] == 5
    assert rep["hot_set"]["voice-a"][0] == "Opening line for everyone."


def test_cli_replay_then_warm_fills_the_store(tmp_path, capsys) -> None:
    corpus = generate(80, 0.5, seed=6)
    write_log(tmp_path / "log.jsonl", corpus.events(scope="sim"))
    assert cli(["replay", "--log", str(tmp_path / "log.jsonl"), "--out", str(tmp_path / "replay.json")]) == 0
    assert "hit rate" in capsys.readouterr().out
    hot = json.loads((tmp_path / "replay.json").read_text())["hot_set"]["sim"]
    scope = bench_scope()
    (tmp_path / "scope.json").write_text(json.dumps(scope.as_dict()))
    db = tmp_path / "warm.db"
    args = ["warm", "--from", str(tmp_path / "replay.json"), "--scope", str(tmp_path / "scope.json")]
    args += ["--store", f"sqlite:{db}", "--provider", "tts_cache.bench:simulated_provider_factory"]
    assert cli(args) == 0
    assert f"'stored': {len(hot)}" in capsys.readouterr().out

    async def lookups() -> int:
        cache = TTSCache(scope, TieredStore(MemoryStore(), SQLiteStore(db)))
        await cache.start()
        hits = sum([await cache.lookup(t) is not None for t in hot])
        await cache.close()
        return hits

    assert asyncio.run(lookups()) == len(hot)
    assert canonicalise(hot[0], "en")
