"""The Redis tests run only against a dedicated instance, never the shared one on 6379."""

import os
from urllib.parse import urlparse

import pytest

SHARED_PORT = 6379


def test_redis_url() -> str:
    url = os.environ.get("TTS_CACHE_REDIS_URL")
    if not url:
        pytest.skip("TTS_CACHE_REDIS_URL not set")
    if (urlparse(url).port or SHARED_PORT) == SHARED_PORT:
        pytest.fail("refusing to run destructive Redis tests against port 6379, the shared instance")
    return url


test_redis_url.__test__ = False  # type: ignore[attr-defined]
