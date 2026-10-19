"""Cache key and the store key families of SOLUTION.md section 4.2c."""

import hashlib

from tts_cache.scope import Scope, scope_base_hash, scope_hash


def text_hash(canonical_text: str) -> str:
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()[:24]


def build_key(scope: Scope, canonical_text: str, *, namespace: str | None = None) -> str:
    """``tts:{scope_hash}:{sha256(canonical_text)[:24]}``."""
    return f"tts:{scope_hash(scope, namespace)}:{text_hash(canonical_text)}"


def split_key(key: str) -> tuple[str, str]:
    """``(scope_hash, text_hash)`` of an audio key."""
    prefix, scope, digest = key.split(":")
    if prefix != "tts" or len(scope) != 16 or len(digest) != 24:
        raise ValueError(f"not an audio key: {key!r}")
    return scope, digest


def meta_key(key: str) -> str:
    scope, digest = split_key(key)
    return f"tts:meta:{scope}:{digest}"


def tag_key(name: str, value: str) -> str:
    return f"tts:tag:{name}={value}"


def generation_key(scope: Scope) -> str:
    return f"tts:gen:{scope_base_hash(scope)}"


def hot_key(scope: Scope) -> str:
    return f"tts:hot:{scope.lang}:{scope_hash(scope)}"
