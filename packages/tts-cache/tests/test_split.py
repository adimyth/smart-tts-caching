from pathlib import Path

import pytest
import yaml

from tts_cache.split import SentenceSplitter, match_endofsentence, split_text

FIXTURES = Path(__file__).parent / "fixtures" / "split"
LANGS = sorted(p.stem for p in FIXTURES.glob("*.yaml"))
CASES = [(lang, c) for lang in LANGS for c in yaml.safe_load((FIXTURES / f"{lang}.yaml").read_text())]


@pytest.mark.parametrize(("lang", "case"), CASES, ids=[c["text"][:30] for _, c in CASES])
def test_fixture_all_at_once(lang: str, case: dict) -> None:
    assert split_text(case["text"], lang) == case["sentences"]


@pytest.mark.parametrize(("lang", "case"), CASES, ids=[c["text"][:30] for _, c in CASES])
def test_fixture_token_by_token(lang: str, case: dict) -> None:
    s = SentenceSplitter(lang)
    out: list[str] = []
    for tok in case["text"].split(" "):
        out += s.feed(tok + " ")
    rest = s.flush()
    if rest and rest.strip():
        out.append(rest.strip())
    assert out == case["sentences"]


def test_required_overrides() -> None:
    assert split_text("डॉ. शर्मा", "hi") == ["डॉ. शर्मा"]
    assert split_text("Acme Inc. Thanks", "en") == ["Acme Inc.", "Thanks"]


def test_lookahead_emits_at_first_character() -> None:
    s = SentenceSplitter("en")
    assert s.feed("Hello.") == []
    assert s.feed(" ") == []
    assert s.feed("N") == ["Hello."]
    assert s.pending == " N"


def test_abbreviation_waits_for_word_end() -> None:
    s = SentenceSplitter("en")
    assert s.feed("Call Dr. Rao now. ") == []
    assert s.feed("B") == ["Call Dr. Rao now."]


def test_flush_and_reset() -> None:
    s = SentenceSplitter("en")
    s.feed("Half a sent")
    assert s.flush() == "Half a sent"
    assert s.flush() is None
    s.feed("More")
    s.reset()
    assert s.pending == ""


def test_match_endofsentence_unambiguous_terminator() -> None:
    assert match_endofsentence("नमस्ते", language="hi") == 0
    assert match_endofsentence("नमस्ते।", language="hi") == len("नमस्ते।")
