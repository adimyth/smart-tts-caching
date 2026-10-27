"""A framework-free, sentence-level cache for text-to-speech audio."""

from tts_cache.cache import Audio, Hit, Lookup, TTSCache
from tts_cache.canonical import SCHEMA_VERSION, canonicalise
from tts_cache.key import build_key
from tts_cache.match import ExactMatcher, Match, Matcher
from tts_cache.metrics import LogSink, MetricsSink
from tts_cache.policy import DEFAULT_POLICY, LEVEL_C_POLICY, CachePolicy
from tts_cache.scope import Scope, scope_hash
from tts_cache.split import SentenceSplitter
from tts_cache.store import CacheStore, Entry
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.redis import RedisStore
from tts_cache.stores.sqlite import SQLiteStore
from tts_cache.stores.tiered import TieredStore
from tts_cache.verify import NoopVerifier, Verifier

__version__ = "0.2.0"

__all__ = [
    "DEFAULT_POLICY",
    "LEVEL_C_POLICY",
    "SCHEMA_VERSION",
    "Audio",
    "CachePolicy",
    "CacheStore",
    "Entry",
    "ExactMatcher",
    "Hit",
    "LogSink",
    "Lookup",
    "Match",
    "Matcher",
    "MemoryStore",
    "MetricsSink",
    "NoopVerifier",
    "RedisStore",
    "SQLiteStore",
    "Scope",
    "SentenceSplitter",
    "TTSCache",
    "TieredStore",
    "Verifier",
    "__version__",
    "build_key",
    "canonicalise",
    "scope_hash",
]
