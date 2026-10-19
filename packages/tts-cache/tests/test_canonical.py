from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from tts_cache.canonical import SCHEMA_VERSION, canonicalise, rule_names, rules_hash, script_of

FIXTURES = Path(__file__).parent / "fixtures" / "canonical"
LANGS = tuple(sorted(p.stem for p in FIXTURES.glob("*.yaml")))


def load(lang: str) -> list[dict[str, str]]:
    return yaml.safe_load((FIXTURES / f"{lang}.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", LANGS)
def test_fixture_count(lang: str) -> None:
    assert len(load(lang)) >= 30


@pytest.mark.parametrize(
    ("lang", "case"),
    [(lang, c) for lang in LANGS for c in load(lang)],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_fixture(lang: str, case: dict[str, str]) -> None:
    assert canonicalise(case["in"], lang) == case["out"]


# Bump SCHEMA_VERSION together with this hash whenever a rule changes: the new
# version moves every key to a new scope, so stale entries stop matching.
RULES_HASH = {1: "898014c541fe938d"}


def test_schema_version_tracks_rules() -> None:
    assert RULES_HASH.get(SCHEMA_VERSION) == rules_hash(), (
        f"the canonicalisation rules changed (hash {rules_hash()}); "
        "bump SCHEMA_VERSION and record the new hash"
    )


def test_rule_order_follows_section_6() -> None:
    assert rule_names() == [
        "nfc",
        "indic_nukta",
        "indic_joiners",
        "zero_width",
        "ascii_digits",
        "terminators",
        "quotes_dashes",
        "emoji",
        "markdown",
        "whitespace",
        "trailing_period",
        "lowercase",
    ]


def test_rules_toggle_individually() -> None:
    assert canonicalise("Hello there.", "en", disabled={"lowercase"}) == "Hello there"
    assert canonicalise("Hello there.", "en", disabled={"trailing_period"}) == "hello there."
    assert canonicalise("नमस्ते।", "hi", disabled={"terminators"}) == "नमस्ते।"


def test_indic_rules_run_only_for_indic_languages() -> None:
    assert script_of("hi-IN") == "indic" and script_of("ta") == "indic" and script_of("en-US") == "latin"
    assert canonicalise("a‍b", "en") == "a‍b"
    assert canonicalise("a‍b", "hi") == "ab"


TEXT = st.text(
    alphabet=st.sampled_from(
        [
            *"abcXYZ .,!?;:-_*`#[]()'\"\n\t",
            *["\u201c", "\u201d", "\u2019", "\u2014", "\u2013", "\u2026", "\u0964", "\u0965", "\u3002"],
            *["\U0001f389", "\u2705", "\u00a0", "\u200b", "\u200c", "\u200d"],
            *["\u0915", "\u094d", "\u0937", "\u091c", "\u093c", "\u095b", "\u093e", "\u0902", "\u0901"],
            *["\u0967", "\u0be7", "\u0664", "\uff11", "e", "\u0301", "\u00e9", "\u0ba4", "\u0bc6", "\u0bbe"],
        ]
    ),
    max_size=40,
)


@settings(max_examples=600, deadline=None)
@given(TEXT, st.sampled_from(LANGS))
def test_idempotent(text: str, lang: str) -> None:
    once = canonicalise(text, lang)
    assert canonicalise(once, lang) == once
