"""Shape test for every frozen pool in harness/corpus (TEST_PLAN.md 6.3)."""

import pytest

from tts_cache.corpus import POOL_LANGUAGES, load_pool, validate_pool
from tts_cache.testplan.pools import (
    DISSIMILARITY_BOUND,
    POOL_SIZE,
    assembly_problems,
    close_pairs,
    fixture_overlap,
    pool_path,
)

PRESENT = [lang for lang in POOL_LANGUAGES if pool_path(lang).exists()]


def test_some_pool_exists() -> None:
    if not PRESENT:
        pytest.skip("no pool written yet")


@pytest.mark.parametrize("lang", PRESENT)
def test_pool_shape(lang: str) -> None:
    pool = load_pool(pool_path(lang))
    assert pool.language == lang
    assert len(pool.bases) == POOL_SIZE
    assert validate_pool(pool) == []
    assert fixture_overlap(pool) == []
    assert assembly_problems(pool) == []


@pytest.mark.parametrize("lang", PRESENT)
def test_pool_dissimilarity(lang: str) -> None:
    pool = load_pool(pool_path(lang))
    too_close = close_pairs(pool, lo=DISSIMILARITY_BOUND)
    assert too_close == [], too_close[:5]


def test_assembled_corpora_match_the_frozen_manifest() -> None:
    # 2.1 freezes the assembled corpora; they follow from the pools, the code and the seeds,
    # so any change to any of those shows up here as a changed digest.
    import json

    from tts_cache.testplan.pools import MANIFEST, assembled_manifest

    if not MANIFEST.exists():
        pytest.skip("no manifest written yet")
    frozen = json.loads(MANIFEST.read_text(encoding="utf-8"))
    current = assembled_manifest([lang for lang in POOL_LANGUAGES if pool_path(lang).exists()])
    changed = sorted(k for k in frozen.keys() | current.keys() if frozen.get(k) != current.get(k))
    assert changed == [], changed[:10]
