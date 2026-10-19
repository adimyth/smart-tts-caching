import pytest
from helpers import scope
from store_contract import make_entry

from tts_cache.policy import (
    DEFAULT_POLICY,
    DEFAULT_TTL_S,
    AllOf,
    AlwaysCache,
    AnyOf,
    ByTag,
    CachePolicy,
    Clip,
    MinChars,
    Namespace,
    Probation,
    RateLimit,
    SentenceCtx,
    Shadow,
    SkipIfMatches,
    SpeakFramesOnly,
    TurnPosition,
    parse_duration,
)

CLIP = Clip(1000, 24000, 1, 48000)


def ctx(text: str = "Hello there.", **over: object) -> SentenceCtx:
    fields: dict[str, object] = {
        "text": text,
        "canonical": text.lower().rstrip("."),
        "language": "en",
        "scope": scope(),
    }
    fields.update(over)
    return SentenceCtx(**fields)  # type: ignore[arg-type]


def test_parse_duration() -> None:
    assert parse_duration("48h") == 48 * 3600
    assert parse_duration("30d") == 30 * 86400
    assert parse_duration("15m") == 900 and parse_duration("90s") == 90 and parse_duration(5) == 5
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_default_policy() -> None:
    assert isinstance(DEFAULT_POLICY, CachePolicy)
    s = ctx()
    assert DEFAULT_POLICY.should_lookup(s) and DEFAULT_POLICY.should_store(s, CLIP)
    assert DEFAULT_POLICY.ttl_for(s, None) == 48 * 3600
    hit = make_entry("Hello there.", hit_count=1)
    assert DEFAULT_POLICY.ttl_for(s, hit) == 30 * 86400
    assert DEFAULT_POLICY.allowed_levels(s) == {"A", "B", "C"}
    assert DEFAULT_POLICY.level_b_classes(s) == {"punctuation", "filler"}
    assert not DEFAULT_POLICY.should_lookup(ctx("Hi", canonical="hi"))
    assert not DEFAULT_POLICY.should_lookup(ctx("x" * 401, canonical="x" * 401))


def test_probation_promotes_on_first_hit() -> None:
    p = Probation("1h", "2d")
    assert p.ttl_for(ctx(), None) == 3600
    assert p.ttl_for(ctx(), make_entry("a b c.", hit_count=0)) == 3600
    assert p.ttl_for(ctx(), make_entry("a b c.", hit_count=1)) == 2 * 86400


def test_always_and_shadow() -> None:
    assert AlwaysCache("1d").ttl_for(ctx(), None) == 86400
    sh = Shadow(AlwaysCache("1d"))
    assert sh.should_lookup(ctx()) and sh.should_store(ctx(), CLIP) and not sh.serves(ctx())
    assert not AllOf(sh, MinChars()).serves(ctx())


def test_skip_if_matches_blocks_store_only() -> None:
    p = SkipIfMatches([r"\d{4,}", "card"])
    assert p.should_lookup(ctx("Your code is 123456."))
    assert not p.should_store(ctx("Your code is 123456."), CLIP)
    assert not p.should_store(ctx("Your card is blocked."), CLIP)
    assert p.should_store(ctx("Thanks for calling."), CLIP)


def test_turn_position_and_speak_frames() -> None:
    p = TurnPosition(1, 1)
    assert p.should_lookup(ctx(turn_index=0)) and p.should_lookup(ctx(turn_index=3, from_end=0))
    assert not p.should_lookup(ctx(turn_index=2, from_end=2)) and not p.should_lookup(ctx())
    s = SpeakFramesOnly()
    assert s.should_lookup(ctx(source="speak")) and not s.should_store(ctx(source="llm"), CLIP)


def test_namespace_and_composition() -> None:
    p = AllOf(Namespace("tenant"), MinChars())
    assert p.key_namespace(ctx(tags={"tenant": "acme"})) == "tenant=acme"
    assert AllOf(MinChars()).key_namespace(ctx()) is None


def test_rate_limit() -> None:
    p = RateLimit(2)
    assert [p.should_store(ctx(), CLIP) for _ in range(3)] == [True, True, False]


def test_all_any_bytag() -> None:
    long_only = MinChars(20, 400)
    assert not AllOf(MinChars(), long_only).should_lookup(ctx())
    assert AnyOf(MinChars(), long_only).should_lookup(ctx())
    assert AllOf(Probation("1h", "1d"), AlwaysCache("10m")).ttl_for(ctx(), None) == 600
    assert AnyOf(Probation("1h", "1d"), AlwaysCache("10m")).ttl_for(ctx(), None) == 3600
    assert AllOf(MinChars()).ttl_for(ctx(), None) == DEFAULT_TTL_S
    router = ByTag("flow", {"billing": SkipIfMatches(["."])}, default=AlwaysCache())
    assert not router.should_store(ctx(tags={"flow": "billing"}), CLIP)
    assert router.should_store(ctx(tags={"flow": "other"}), CLIP)
    assert router.should_store(ctx(), CLIP)


def test_level_grants() -> None:
    class Grant(MinChars):
        def allowed_levels(self, s):  # type: ignore[override]
            return {"A", "B"}

    class Deny(MinChars):
        def allowed_levels(self, s):  # type: ignore[override]
            return set()

    assert AllOf(Probation(), Grant()).allowed_levels(ctx()) == {"A", "B"}
    assert AllOf(Probation(), Deny()).allowed_levels(ctx()) == set()
    assert AllOf(MinChars()).allowed_levels(ctx()) == {"A"}
