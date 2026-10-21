"""Level B, near-duplicate matching."""

import random
import time
from pathlib import Path

import pytest
import yaml
from helpers import SR, scope, tone_pcm

from tts_cache import TTSCache, build_key, canonicalise
from tts_cache.match import Matchers
from tts_cache.match.lexicon import FILLER_PHRASES, FILLERS
from tts_cache.match.near import NearDuplicateMatcher, classify, tokens
from tts_cache.metrics import LogSink
from tts_cache.policy import AllOf, LevelB, MinChars, Probation
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.tiered import TieredStore

FIXTURES = Path(__file__).parent / "fixtures"
LANGS = tuple(sorted(p.stem for p in (FIXTURES / "near").glob("*.yaml")))
CASES = [
    (lang, c) for lang in LANGS for c in yaml.safe_load((FIXTURES / "near" / f"{lang}.yaml").read_text())
]


@pytest.mark.parametrize("lang", LANGS)
def test_fixture_counts_cover_every_class(lang: str) -> None:
    cases = [c for code, c in CASES if code == lang]
    assert len(cases) >= 40
    assert {c["class"] for c in cases} == {"punctuation", "filler", "question", "slot", "other"}


@pytest.mark.parametrize(("lang", "case"), CASES, ids=[f"{lang}:{c['a'][:24]}" for lang, c in CASES])
def test_fixture_class(lang: str, case: dict) -> None:
    a, b = canonicalise(case["a"], lang), canonicalise(case["b"], lang)
    assert classify(a, b, lang, a_text=case["a"], b_text=case["b"]) == case["class"]


@pytest.mark.parametrize("lang", LANGS)
def test_filler_lists_cover_the_fixtures(lang: str) -> None:
    f = yaml.safe_load((FIXTURES / "filler" / f"{lang}.yaml").read_text())
    assert set(f["words"]) <= FILLERS[lang]
    assert set(f["phrases"]) <= FILLER_PHRASES[lang]


def test_tokens_keep_indic_vowel_signs() -> None:
    assert tokens("ம்ம், புரிந்தது.") == ["ம்ம்", "புரிந்தது"]
    assert tokens("मैं ठीक हूँ।") == ["मैं", "ठीक", "हूँ"]


# The slot-never-served property.

TEMPLATES = {
    "en": [
        ("Thanks, {}.", "name"),
        ("Hi {}, how can I help you today?", "name"),
        ("Your order {} has been shipped.", "number"),
        ("Your OTP is {}.", "number"),
        ("Your appointment is on {} morning.", "date"),
        ("Please call us back on {}.", "date"),
        ("Your refund of {} has been processed.", "amount"),
        ("The total due this month is {}.", "amount"),
    ],
    "hi": [
        ("धन्यवाद, {} जी।", "name"),
        ("आपका ऑर्डर नंबर {} है।", "number"),
        ("आपकी अपॉइंटमेंट {} को है।", "date"),
        ("आपका {} का रिफंड आ गया है।", "amount"),
    ],
    "ta": [
        ("நன்றி, {}.", "name"),
        ("உங்கள் ஆர்டர் எண் {}.", "number"),
        ("உங்கள் சந்திப்பு {} அன்று.", "date"),
        ("உங்கள் {} திருப்பி அனுப்பப்பட்டது.", "amount"),
    ],
}
VALUES = {
    "name": {
        "en": ["Alex", "Sam", "Priya", "Rahul", "Maria", "John", "Kavya", "Arjun", "Emma", "Ravi"],
        "hi": ["प्रिया", "नेहा", "राहुल", "अमित", "सीमा", "रवि", "गीता", "मोहन"],
        "ta": ["குமார்", "ரமேஷ்", "பிரியா", "கணேஷ்", "மீனா", "சுரேஷ்", "ரவி", "கீதா"],
    },
    "date": {
        "en": ["Monday", "Tuesday", "Friday", "12 May", "3 June", "the 21st", "Sunday"],
        "hi": ["सोमवार", "मंगलवार", "शुक्रवार", "12 मई", "3 जून", "रविवार"],
        "ta": ["திங்கள்கிழமை", "செவ்வாய்க்கிழமை", "வெள்ளிக்கிழமை", "மே 12", "ஜூன் 3"],
    },
}


def slot_value(rng: random.Random, kind: str, lang: str) -> str:
    if kind == "number":
        return str(rng.randint(1000, 99999))
    if kind == "amount":
        return {
            "en": f"${rng.randint(5, 900)}",
            "hi": f"₹{rng.randint(5, 9000)}",
            "ta": f"₹{rng.randint(5, 9000)}",
        }[lang]
    return rng.choice(VALUES[kind][lang])


def variations(n: int = 1000, seed: int = 3) -> list[tuple[str, str, str]]:
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        lang = rng.choice(sorted(TEMPLATES))
        template, kind = rng.choice(TEMPLATES[lang])
        x, y = slot_value(rng, kind, lang), slot_value(rng, kind, lang)
        if x != y:
            out.append((lang, template.format(x), template.format(y)))
    return out


@pytest.mark.parametrize(
    "classes", [{"punctuation"}, {"filler"}, {"punctuation", "filler"}, {"slot", "other"}]
)
async def test_slot_variations_are_never_served(classes: set[str]) -> None:
    pairs = variations()
    by_lang: dict[str, list[tuple[str, str]]] = {}
    for lang, stored, query in pairs:
        by_lang.setdefault(lang, []).append((stored, query))
    served = probed = 0
    for lang, items in by_lang.items():
        sc = scope(language=lang)
        memory = MemoryStore(warm_start_bytes=1 << 40)
        near = NearDuplicateMatcher()
        sink = LogSink(log=None)
        cache = TTSCache(
            sc,
            TieredStore(memory, MemoryStore()),
            policy=AllOf(Probation(), MinChars(), LevelB(classes)),
            matcher=Matchers(near=near),
            metrics=sink,
        )
        for stored, _ in items:
            await cache.capture(stored, tone_pcm(stored), SR)
        await cache.drain(60)
        entries = [e for e in [memory.peek(build_key(sc, canonicalise(s, lang))) for s, _ in items] if e]
        memory.load_warm(sc, entries)
        near.refresh(sc, memory)
        for _, query in items:
            hit = await cache.lookup(query)
            if hit is not None and hit.level == "B":
                served += 1
        probed += sink.counts["near"]
        await cache.close()
    assert served == 0
    assert probed > 500  # the near matcher found the stored variant and classified it


async def test_probe_under_1_ms_p99_over_1000_entries() -> None:
    from tts_cache.corpus import generate

    sc = scope()
    near = NearDuplicateMatcher()
    corpus = generate(1000, 0.0, seed=8)
    stored = corpus.sentences
    for s in stored:
        c = canonicalise(s, "en")
        near.add(sc, build_key(sc, c), c, s)
    assert len(near) == 1000
    rng = random.Random(1)
    queries = []
    for s in rng.sample(stored, 500):
        queries.append("Okay, " + s)  # filler
        queries.append(s.replace(" ", ", ", 1))  # punctuation
    queries += [f"Completely unrelated sentence number {i} for the index." for i in range(200)]
    times, found = [], 0
    for q in queries:
        c = canonicalise(q, "en")
        t = time.perf_counter()
        r = near.probe(sc, c, q)
        times.append((time.perf_counter() - t) * 1000)
        found += r is not None
    times.sort()
    p99 = times[int(0.99 * len(times))]
    print(f"probe p50 {times[len(times) // 2]:.3f} ms p99 {p99:.3f} ms, found {found} of {len(queries)}")
    assert p99 < 1.0
    assert found >= 900


async def test_level_b_serves_only_allowed_classes_and_reports_the_histogram() -> None:
    sc = scope()
    memory = MemoryStore()
    near = NearDuplicateMatcher()
    sink = LogSink(log=None)
    cache = TTSCache(
        sc,
        TieredStore(memory, MemoryStore()),
        policy=AllOf(Probation(), MinChars(), LevelB({"punctuation"})),
        matcher=Matchers(near=near),
        metrics=sink,
    )
    stored = [
        "Sure, let me check that for you.",
        "Thanks for calling, Alex.",
        "Your refund has been processed.",
    ]
    for s in stored:
        await cache.capture(s, tone_pcm(s), SR)
    await cache.drain(10)
    memory.load_warm(sc, [memory.peek(build_key(sc, canonicalise(s, "en"))) for s in stored])  # type: ignore[misc]
    near.refresh(sc, memory)
    punct = await cache.lookup("Sure let me check that for you.")
    assert punct is not None and punct.level == "B" and punct.spoken_text == stored[0]
    assert await cache.lookup("Okay, your refund has been processed.") is None  # filler: not allowed here
    assert await cache.lookup("Thanks for calling, Sam.") is None  # slot: never
    assert sink.counts["near"] == 3 and sink.counts["hit"] == 1
    await cache.close()


@pytest.mark.parametrize(
    ("stored", "query"),
    [
        ("Your order is confirmed.", "Your order is confirmed?"),
        ("Your order is confirmed?", "Your order is confirmed."),
        ("Is that right?", "Is that right!"),
        ("Your order is confirmed.", "Okay, your order is confirmed?"),
        ("आपका ऑर्डर कन्फर्म हो गया है।", "आपका ऑर्डर कन्फर्म हो गया है?"),
    ],
)
async def test_a_question_is_never_served_as_a_statement(stored: str, query: str) -> None:
    lang = "hi" if any("\u0900" <= ch <= "\u097f" for ch in stored) else "en"
    sc = scope(language=lang)
    memory = MemoryStore()
    near = NearDuplicateMatcher()
    sink = LogSink(log=None)
    cache = TTSCache(
        sc,
        TieredStore(memory, MemoryStore()),
        policy=AllOf(Probation(), MinChars(), LevelB({"punctuation", "filler"})),
        matcher=Matchers(near=near),
        metrics=sink,
    )
    await cache.capture(stored, tone_pcm(stored), SR)
    await cache.drain(10)
    memory.load_warm(sc, [memory.peek(build_key(sc, canonicalise(stored, lang)))])  # type: ignore[misc]
    near.refresh(sc, memory)
    assert await cache.lookup(query) is None
    assert sink.counts["near"] == 1  # the stored sentence was found and refused, not missed
    await cache.close()


async def test_bench_reports_what_level_b_punctuation_buys() -> None:
    from tts_cache.bench import level_b_comparison

    off, on = await level_b_comparison(0.5, n=120, variants=0.5)
    assert off["mode"] == "level_b_off" and on["mode"] == "level_b_on"
    # Both runs probe Level B, so both carry the diff histogram; only "on" serves.
    assert off["near_histogram"].get("punctuation", 0) > 0 and off["near_histogram"].get("filler", 0) > 0
    assert sum(off["level_b_hits"].values()) == 0
    assert on["level_b_hits"]["punctuation"] > 0
    assert on["level_b_hits"]["filler"] == on["level_b_hits"]["slot"] == on["level_b_hits"]["other"] == 0
    assert on["hit_rate"] > off["hit_rate"] + 0.02 and on["billed_chars"] < off["billed_chars"]
