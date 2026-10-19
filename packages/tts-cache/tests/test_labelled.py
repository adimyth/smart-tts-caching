"""The labelled corpus of TEST_PLAN.md 6.4, on a programmatic English pool, and every frozen pool's shape."""

import math

import pytest

from tts_cache.canonical import canonicalise
from tts_cache.corpus import (
    DECOY_SHARES,
    REPEAT_SHARES,
    Base,
    Pool,
    Slot,
    control,
    labelled,
    read_log,
    validate_pool,
)

NAMES = [
    "Rahul",
    "Priya",
    "Arjun",
    "Meera",
    "Kabir",
    "Anita",
    "Vikram",
    "Sana",
    "Ravi",
    "Leela",
    "Omar",
    "Divya",
    "Farhan",
    "Nisha",
]
THINGS = ["order", "ticket", "parcel", "request", "booking", "refund", "claim", "account", "plan", "card"]


def make_pool(size: int = 140) -> Pool:
    bases = []
    for i in range(size):
        name, thing = NAMES[i % len(NAMES)], THINGS[i // len(NAMES) % len(THINGS)]
        num = str(1000 + 37 * i)
        bases.append(
            Base(
                id=f"en-{i:03d}",
                domain="support",
                text=f"Hello {name}, your {thing} {num} is ready for pickup today.",
                slots=[
                    Slot(name, "name", ["Zara", "Imran", "Gita"]),
                    Slot(num, "number", ["9001", "9002", "9003"]),
                ],
                paraphrase=f"{name}, the {thing} {num} can be collected today.",
                meaning_change=f"Hello {name}, your {thing} {num} is not ready for pickup today.",
            )
        )
    return Pool("en", bases)


POOL = make_pool()


def test_programmatic_pool_is_valid() -> None:
    assert validate_pool(POOL) == []


@pytest.mark.parametrize("r", [0.25, 0.5])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_label_shares_within_one_sentence(r: float, seed: int) -> None:
    c = labelled(POOL, n=200, r=r, d=0.10, seed=seed)
    counts = c.counts()
    assert len(c.items) == 200
    n_rep, n_dec = round(200 * r), 20
    for label, share in REPEAT_SHARES.items():
        assert abs(counts[label] - n_rep * share) <= 1, label
    for label, share in DECOY_SHARES.items():
        assert abs(counts[label] - n_dec * share) <= 1, label
    assert counts["new"] == 200 - n_rep - n_dec


def test_turns_and_sessions() -> None:
    c = labelled(POOL, seed=4)
    turns = c.sessions
    for si, session in enumerate(turns):
        if si < len(turns) - 1:
            assert 3 <= len(session) <= 6
        for turn in session:
            assert 1 <= len(turn) <= 4
            assert turn[0].first_in_turn and not any(it.first_in_turn for it in turn[1:])
    assert [it.index for it in c.items] == list(range(200))


def test_deterministic_per_seed() -> None:
    assert labelled(POOL, seed=7).items == labelled(POOL, seed=7).items
    assert labelled(POOL, seed=7).items != labelled(POOL, seed=8).items


def test_every_label_holds_against_earlier_sentences() -> None:
    c = labelled(POOL, r=0.5, seed=5)
    seen_base: set[str] = set()
    seen_canon: list[str] = []
    by_id = {b.id: b for b in POOL.bases}
    for it in c.items:
        canon = canonicalise(it.text, "en")
        base_canon = canonicalise(by_id[it.base].text, "en")
        if it.label == "new":
            assert it.base not in seen_base and canon not in seen_canon
        else:
            assert it.base in seen_base, "a repeat or decoy draws from introduced bases"
        if it.label in ("exact", "surface"):
            assert canon == base_canon
        elif it.label != "new":
            assert canon not in seen_canon, f"{it.label} variant {it.text!r} occurred before"
        if it.label == "exact":
            assert it.text == by_id[it.base].text
        seen_base.add(it.base)
        seen_canon.append(canon)


def test_exact_only_mix() -> None:
    c = labelled(POOL, r=0.5, seed=1, mix="exact")
    counts = c.counts()
    assert counts["exact"] == 100 and counts["new"] == 100
    assert sum(counts.values()) == 200


def test_control_is_every_base_once() -> None:
    c = control(POOL, seed=3)
    assert len(c.items) == len(POOL.bases)
    assert {it.label for it in c.items} == {"new"}
    assert sorted(it.base for it in c.items) == sorted(b.id for b in POOL.bases)


def test_too_small_a_pool_is_refused() -> None:
    with pytest.raises(ValueError):
        labelled(make_pool(100), n=200, r=0.25)


def test_events_are_a_replay_log(tmp_path) -> None:
    c = labelled(POOL, seed=2)
    path = tmp_path / "c.jsonl"
    c.write(path)
    events = read_log(path)
    assert [e.text for e in events] == [it.text for it in c.items]
    ts = [e.ts for e in events]
    assert ts == sorted(ts)
    assert len({e.session for e in events}) == len(c.sessions)
    assert not math.isnan(ts[0])
