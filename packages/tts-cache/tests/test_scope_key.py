import dataclasses
import re

import pytest
from helpers import scope
from hypothesis import given
from hypothesis import strategies as st

from tts_cache.canonical import canonicalise
from tts_cache.key import build_key, generation_key, hot_key, meta_key, split_key, tag_key
from tts_cache.scope import canonical_json, scope_base_hash, scope_hash


def test_key_format() -> None:
    k = build_key(scope(), "hello")
    assert re.fullmatch(r"tts:[0-9a-f]{16}:[0-9a-f]{24}", k)
    s, d = split_key(k)
    assert meta_key(k) == f"tts:meta:{s}:{d}"
    assert tag_key("tenant", "acme") == "tts:tag:tenant=acme"
    assert hot_key(scope()) == f"tts:hot:en:{scope_hash(scope())}"
    assert generation_key(scope()).startswith("tts:gen:")


def test_canonical_json_normalises() -> None:
    assert canonical_json({"b": 1.0, "a": [2.50, True]}) == '{"a":[2.5,true],"b":1}'
    assert scope_hash(scope(settings={"speed": 1})) == scope_hash(scope(settings={"speed": 1.0}))
    assert canonical_json({"x": 0.1 + 0.2}) == canonical_json({"x": 0.3})


FIELD_CHANGES = {
    "service": "other",
    "model": "m2",
    "voice": "v2",
    "language": "hi-IN",
    "sample_rate": 16_000,
    "channels": 2,
    "settings": {"speed": 1.1},
    "schema_version": 99,
    "generation": 7,
}


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(scope())])
def test_key_differs_when_any_scope_field_differs(field: str) -> None:
    changed = dataclasses.replace(scope(), **{field: FIELD_CHANGES[field]})
    assert build_key(changed, "hello") != build_key(scope(), "hello")


def test_generation_is_outside_the_base_hash() -> None:
    assert scope_base_hash(scope(generation=3)) == scope_base_hash(scope())
    assert scope_hash(scope(generation=3)) != scope_hash(scope())


def test_namespace_folds_into_key() -> None:
    assert build_key(scope(), "x", namespace="tenant=a") != build_key(scope(), "x", namespace="tenant=b")
    assert build_key(scope(), "x", namespace=None) == build_key(scope(), "x")


WORDS = st.lists(
    st.sampled_from(["hello", "thanks", "alex", "your", "code", "is", "1234", "नमस्ते", "धन्यवाद"]), min_size=1
)


@given(
    WORDS,
    st.lists(st.sampled_from([" ", "  ", "\t", " ", "\n"]), min_size=8, max_size=8),
    st.sampled_from(["", " "]),
)
def test_key_matches_across_whitespace(words: list[str], gaps: list[str], edge: str) -> None:
    plain = " ".join(words) + "."
    spaced = edge + "".join(w + gaps[i % len(gaps)] for i, w in enumerate(words)).rstrip() + "." + edge
    sc = scope()
    assert build_key(sc, canonicalise(plain, "en")) == build_key(sc, canonicalise(spaced, "en"))


NATIVE = {"0123456789": ["०१२३४५६७८९", "௦௧௨௩௪௫௬௭௮௯", "٠١٢٣٤٥٦٧٨٩", "０１２３４５６７８９", "০১২৩৪৫৬৭৮৯"]}


@given(
    st.text(alphabet="0123456789", min_size=1, max_size=8),
    st.sampled_from(NATIVE["0123456789"]),
    st.sampled_from(["en", "hi", "ta"]),
)
def test_key_matches_across_digit_scripts(digits: str, block: str, lang: str) -> None:
    native = digits.translate(str.maketrans("0123456789", block))
    sc = scope(language=lang)
    a = canonicalise(f"Your code is {digits}.", lang)
    b = canonicalise(f"Your code is {native}.", lang)
    assert build_key(sc, a) == build_key(sc, b)


@given(st.sampled_from(["café", "naïve", "ज़रूर", "फ़ोन", "கொடு", "ড়"]), st.sampled_from(["NFC", "NFD", "NFKC"]))
def test_key_matches_across_unicode_forms(word: str, form: str) -> None:
    import unicodedata

    sc = scope()
    a = canonicalise(f"{word} please.", "hi")
    b = canonicalise(unicodedata.normalize(form, f"{word} please."), "hi")
    assert build_key(sc, a) == build_key(sc, b)
