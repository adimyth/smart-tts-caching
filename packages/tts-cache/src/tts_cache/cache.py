"""The ``TTSCache`` facade: the public API, the hot path and the ingest queue (SOLUTION.md 2, 4.1, 8).

Every public method is a fail-open boundary: an exception inside the cache is
caught here, counted as a ``fail_open`` event, logged at most once a minute
per site, and the call behaves as a miss (``lookup``) or as a dropped capture
(``capture``). Only the provider's own errors reach the caller.
"""

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeVar

from tts_cache import codec as codecs
from tts_cache.canonical import canonicalise
from tts_cache.checks import is_silent, pcm_duration_ms, plausible_duration
from tts_cache.codec import Codec, PcmStream
from tts_cache.key import build_key
from tts_cache.match import ExactMatcher, Matcher
from tts_cache.metrics import FailOpen, LogSink, MetricsSink
from tts_cache.policy import DEFAULT_POLICY, DEFAULT_TTL_S, LEVELS, CachePolicy, Clip, Level, SentenceCtx
from tts_cache.scope import Scope, scope_hash
from tts_cache.split import SentenceSplitter
from tts_cache.store import CacheStore, Entry, TouchMeta
from tts_cache.stores.memory import MemoryStore
from tts_cache.stores.tiered import TieredStore
from tts_cache.verify import NoopVerifier, Verifier

T = TypeVar("T")
SERVABLE_B = frozenset({"punctuation", "filler"})


@dataclass
class Hit:
    """Stored audio for a sentence. ``pcm_stream()`` yields 20 ms chunks as they decode."""

    stream: PcmStream
    sample_rate: int
    channels: int
    spoken_text: str
    level: Level
    key: str
    meta: dict[str, Any] = field(default_factory=dict)

    def pcm_stream(self, chunk_ms: int = codecs.CHUNK_MS) -> Iterator[bytes]:
        return self.stream.chunks(chunk_ms)

    def apcm_stream(self, chunk_ms: int = codecs.CHUNK_MS) -> AsyncIterator[bytes]:
        """``pcm_stream`` for an event loop: never blocks it while the clip decodes."""
        return self.stream.achunks(chunk_ms)

    @property
    def pcm(self) -> bytes:
        """The whole clip, waiting for the decode to finish."""
        return self.stream.result()


@dataclass
class Audio:
    pcm: bytes
    sample_rate: int
    channels: int = 1
    cached: bool = False
    level: Level | None = None


@dataclass
class Lookup:
    """What a lookup decided: the key the sentence stores under, the hit if any, and whether to capture."""

    key: str | None
    canonical: str | None
    hit: Hit | None = None
    store: bool = False
    reason: str | None = None


@dataclass
class _Capture:
    text: str
    pcm: bytes
    sample_rate: int
    channels: int
    tags: Mapping[str, str]
    key: str | None
    source: Literal["llm", "speak"]


ProviderFn = Callable[[str], Awaitable["Audio | tuple[bytes, int]"]]
ProviderStreamFn = Callable[[str], AsyncIterator[bytes]]


def default_matcher(*, level_c: bool = False, judge: bool = False) -> Matcher:
    """Levels A and B when the ``[similarity]`` extra is installed, otherwise Level A alone.

    ``level_c`` adds the Level C matcher, which serves only under a policy that
    allows Level C (``LEVEL_C_POLICY``); ``judge`` adds it with the meaning
    check of :mod:`tts_cache.match.judge`, loading that model now.
    """
    try:
        from tts_cache.match import Matchers
        from tts_cache.match.near import NearDuplicateMatcher
        from tts_cache.match.semantic import SemanticMatcher
    except ImportError:
        return ExactMatcher()
    semantic = None
    if judge:
        from tts_cache.match.judge import load_judge

        semantic = SemanticMatcher(judge=load_judge())
    elif level_c:
        semantic = SemanticMatcher()
    return Matchers(near=NearDuplicateMatcher(), semantic=semantic)


class TTSCache:
    def __init__(
        self,
        scope: Scope,
        store: CacheStore,
        policy: CachePolicy | Any = DEFAULT_POLICY,
        matcher: Matcher | None = None,
        metrics: MetricsSink | None = None,
        verifier: Verifier | None = None,
        warm_start_bytes: int = 4 << 20,
        *,
        codec: Codec = "opus",
        deadline_ms: float | None = None,
        queue_size: int = 256,
        warm_start_timeout_s: float = 2.0,
        warm_texts: int = 2000,
        prefetch_min_chars: int = 8,
    ) -> None:
        self._scope = scope
        self.store = store
        self.policy = policy
        self.matcher: Matcher = matcher or default_matcher()
        self.metrics: MetricsSink = metrics or LogSink()
        self.verifier: Verifier = verifier or NoopVerifier()
        self.warm_start_bytes = warm_start_bytes
        self.codec: Codec = codec
        if deadline_ms is None:
            # The tiered store bounds its shared tier itself; the outer deadline adds room for the decode.
            deadline_ms = store.deadline_s * 1000 + 5 if isinstance(store, TieredStore) else 20.0
            # A Level C judge runs inside the lookup and brings its own budget.
            deadline_ms += float(getattr(self.matcher, "extra_deadline_ms", 0.0))
        self.deadline_s = deadline_ms / 1000.0
        self.queue_size = queue_size
        self.warm_start_timeout_s = warm_start_timeout_s
        self.warm_texts = warm_texts
        self.prefetch_min_chars = prefetch_min_chars
        self._fail_open = FailOpen(self.metrics)
        if isinstance(store, TieredStore):
            store.bind_metrics(self.metrics)
        self._breakers: set[str] = set()
        self._queue: asyncio.Queue[_Capture | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._own_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # Scope and breaker.

    @property
    def scope(self) -> Scope:
        return self._scope

    async def set_scope(self, scope: Scope) -> None:
        """Switch voice identity (a settings update); reads the new scope's generation. Never raises."""
        try:
            async with asyncio.timeout(self.deadline_s):
                gen = await self.store.generation(scope)
            self._scope = scope.with_generation(gen)
        except Exception as e:
            self._scope = scope
            self._fail_open("set_scope", e)

    def trip_breaker(self, reason: str, **fields: Any) -> None:
        """Stop serving hits for the current scope until the process restarts."""
        sh = scope_hash(self._scope)
        if sh not in self._breakers:
            self._breakers.add(sh)
            self._event("breaker", reason=reason, scope=sh, **fields)

    @property
    def breaker_tripped(self) -> bool:
        return scope_hash(self._scope) in self._breakers

    def report_key_mismatch(self, predicted: str, prepared: str, **fields: Any) -> None:
        """The host sent different text than the adapter keyed: reject, count, trip the breaker."""
        self._event("reject", reason="key_mismatch", predicted=predicted, prepared=prepared, **fields)
        self.trip_breaker("key_mismatch")

    def _event(self, name: str, **fields: Any) -> None:
        try:
            self.metrics.event(name, **fields)
        except Exception:  # metrics must not break the host
            pass

    # Lifecycle.

    async def start(self) -> None:
        """Start the ingest task, read the scope's generation, and warm the process memory. Never raises."""
        try:
            self._ensure_worker()
            start = getattr(self.store, "start", None)
            if start is not None:
                await start()
            async with asyncio.timeout(self.warm_start_timeout_s):
                gen = await self.store.generation(self._scope)
            self._scope = self._scope.with_generation(gen)
        except Exception as e:
            self._fail_open("start", e)
        await self._warm_start()

    async def _warm_start(self) -> None:
        """Load the hot set's clips (within ``warm_start_bytes``) and texts into process memory (4.2a)."""
        if not isinstance(self.store, TieredStore) or (self.warm_start_bytes <= 0 and self.warm_texts <= 0):
            return
        t0 = time.perf_counter()
        shared = self.store.shared
        try:
            async with asyncio.timeout(self.warm_start_timeout_s):
                warm = getattr(shared, "warm", None)
                if warm is not None:
                    entries, pairs = await warm(self._scope, self.warm_start_bytes, self.warm_texts)
                else:
                    entries = await shared.hot(self._scope, self.warm_start_bytes)
                    hot_texts = getattr(shared, "hot_texts", None)
                    pairs = (
                        await hot_texts(self._scope, self.warm_texts) if hot_texts and self.warm_texts else []
                    )
            memory: MemoryStore = self.store.memory
            loaded = memory.load_warm(self._scope, entries)
            texts = memory.add_warm_texts(self._scope, pairs)
            refresh = getattr(self.matcher, "refresh", None)
            if refresh is not None:
                await asyncio.to_thread(refresh, self._scope, memory)
            self._event(
                "warm_start",
                entries=len(entries),
                bytes=loaded,
                texts=len(entries) + texts,
                ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        except Exception as e:  # a failed warm start is a cold cache
            self._event("warm_start_failed", error=type(e).__name__)
            self._fail_open("warm_start", e)

    async def drain(self, timeout: float = 2.0) -> bool:  # noqa: ASYNC109 - mirrors close()
        """Wait up to ``timeout`` seconds for queued captures to be written; True if the queue emptied."""
        try:
            if self._queue is None or self._worker is None or self._worker.done():
                return True
            async with asyncio.timeout(timeout):
                await self._queue.join()
            return True
        except TimeoutError:
            return False
        except Exception as e:
            self._fail_open("drain", e)
            return False

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._loop = asyncio.get_running_loop()
            self._queue = asyncio.Queue(maxsize=self.queue_size)
            self._worker = asyncio.create_task(self._drain())

    async def close(self, timeout: float = 2.0, *, close_store: bool = True) -> None:  # noqa: ASYNC109 - fixed interface
        """Drain the capture queue for up to ``timeout`` seconds; drop and count what remains."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._queue is not None and self._worker is not None and not self._worker.done():
                try:
                    async with asyncio.timeout(timeout):
                        await self._queue.join()
                except TimeoutError:
                    self._event("capture_dropped", reason="shutdown", count=self._queue.qsize())
                self._worker.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._worker
            for t in list(self._background):
                t.cancel()
            if close_store:
                # A store that hangs on close must not hold up the host's shutdown.
                async with asyncio.timeout(timeout):
                    await self.store.close()
        except Exception as e:
            self._fail_open("close", e)

    # Lookup.

    def _ctx(
        self,
        text: str,
        canonical: str,
        tags: Mapping[str, str] | None,
        source: Literal["llm", "speak"] = "llm",
        turn_index: int | None = None,
        from_end: int | None = None,
    ) -> SentenceCtx:
        return SentenceCtx(
            text=text,
            canonical=canonical,
            language=self._scope.language,
            scope=self._scope,
            tags=dict(tags or {}),
            source=source,
            turn_index=turn_index,
            from_end=from_end,
        )

    async def lookup(self, text: str, *, tags: Mapping[str, str] | None = None) -> Hit | None:
        return (await self.probe(text, tags=tags)).hit

    async def probe(
        self,
        text: str,
        *,
        tags: Mapping[str, str] | None = None,
        source: Literal["llm", "speak"] = "llm",
        turn_index: int | None = None,
        from_end: int | None = None,
    ) -> Lookup:
        """``lookup`` plus the key a miss should be captured under. Never raises."""
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(self.deadline_s):
                return await self._probe(text, tags, source, turn_index, from_end, t0)
        except TimeoutError:
            self._event("deadline", chars=len(text.strip()), lookup_ms=_ms(t0))
            self._event("miss", chars=len(text.strip()), lookup_ms=_ms(t0), reason="deadline")
            return Lookup(None, None, reason="deadline")
        except Exception as e:
            self._fail_open("lookup", e)
            self._event("miss", chars=len(text.strip()), lookup_ms=_ms(t0), reason="fail_open")
            return Lookup(None, None, reason="fail_open")

    async def _probe(
        self,
        text: str,
        tags: Mapping[str, str] | None,
        source: Literal["llm", "speak"],
        turn_index: int | None,
        from_end: int | None,
        t0: float,
    ) -> Lookup:
        scope = self._scope
        canonical = canonicalise(text, scope.language)
        s = self._ctx(text, canonical, tags, source, turn_index, from_end)
        chars = s.chars
        namespace = _maybe(self.policy, "key_namespace", s)
        key = build_key(scope, canonical, namespace=namespace if isinstance(namespace, str) else None)
        if scope_hash(scope) in self._breakers:
            self._event("skip", reason="breaker", chars=chars, key=key)
            self._event("miss", reason="breaker", chars=chars, key=key, lookup_ms=_ms(t0))
            return Lookup(key, canonical, reason="breaker")
        if not self.policy.should_lookup(s):
            self._event("skip", reason="policy", chars=chars, key=key)
            self._event("miss", reason="policy", chars=chars, key=key, lookup_ms=_ms(t0))
            return Lookup(key, canonical, store=True, reason="policy")
        levels = self.policy.allowed_levels(s)
        if levels is None:
            levels = {"A"}
        ns = namespace if isinstance(namespace, str) else None
        handles = getattr(self.matcher, "handles", None)
        for level in LEVELS:
            allowed = level in levels
            # Level B is probed whenever a near matcher exists, so the diff
            # histogram covers every sentence, served or not (5).
            probe_b = level == "B" and handles is not None and handles("B")
            if not allowed and not probe_b:
                continue
            match = await self.matcher.find(scope, canonical, {level}, namespace=ns, text=text)
            if match is None:
                continue
            if level == "B":
                servable = (
                    allowed and match.diff_class in SERVABLE_B and match.diff_class in self._b_classes(s)
                )
                self._event(
                    "near",
                    diff_class=match.diff_class,
                    chars=chars,
                    key=match.key,
                    distance=match.score,
                    served=servable,
                )
                if not servable:
                    continue
            entry = await self.store.get(match.key)
            if entry is None or entry.expired():
                continue
            if entry.sample_rate != scope.sample_rate or entry.channels != scope.channels:
                self._event("skip", reason="format", chars=chars, key=match.key)
                continue
            if _maybe(self.policy, "serves", s) is False:
                self._event("shadow_hit", chars=chars, key=match.key, level=level, lookup_ms=_ms(t0))
                self._event("miss", reason="shadow", chars=chars, key=key, lookup_ms=_ms(t0))
                return Lookup(key, canonical, reason="shadow")
            hit = Hit(
                stream=entry.decoded(),
                sample_rate=entry.sample_rate,
                channels=entry.channels,
                spoken_text=text if level == "A" else entry.text,
                level=level,
                key=match.key,
                meta={"tier": entry.tier, "duration_ms": entry.duration_ms, "diff_class": match.diff_class},
            )
            self._event(
                "hit",
                key=match.key,
                level=level,
                tier=entry.tier,
                chars=chars,
                duration_ms=entry.duration_ms,
                lookup_ms=_ms(t0),
                language=scope.language,
                tags=s.tags,
                diff_class=match.diff_class,
            )
            self._after_hit(s, entry)
            # A hit at Level B or C is stored under its own key; this sentence's key stays empty.
            return Lookup(key, canonical, hit=hit, store=False)
        self._event("miss", key=key, chars=chars, lookup_ms=_ms(t0), language=scope.language, tags=s.tags)
        return Lookup(key, canonical, store=True)

    def _b_classes(self, s: SentenceCtx) -> frozenset[str]:
        got = _maybe(self.policy, "level_b_classes", s)
        return got if isinstance(got, frozenset) else frozenset()

    def _after_hit(self, s: SentenceCtx, entry: Entry) -> None:
        try:
            self.policy.on_hit(s, entry)
        except Exception as e:
            self._fail_open("policy.on_hit", e)
        now = time.time()
        promoted = replace(entry.without_pcm(), hit_count=entry.hit_count + 1)
        ttl = self.policy.ttl_for(s, promoted) or DEFAULT_TTL_S
        self._spawn(self.store.touch(entry.key, TouchMeta(hit_at=now, expires_at=now + ttl)), "touch")

    def _spawn(self, coro: Coroutine[Any, Any, Any], site: str) -> None:
        async def guarded() -> None:
            try:
                await coro
            except Exception as e:
                self._fail_open(site, e)

        task = asyncio.ensure_future(guarded())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # Prefix prefetch (5): move the shared-tier read of a hot sentence earlier in time.

    def prefetch(self, prefix: str) -> str | None:
        """Start fetching the one hot-set clip whose text starts with ``prefix``; never raises.

        Returns the parked key, or None when zero or several hot-set texts match
        or the prefix is still too short. Nothing plays because of a prefix:
        the clip is only moved into process memory (and its decode started),
        and the sentence's own lookup serves it only if its key is this key.
        """
        try:
            if not isinstance(self.store, TieredStore) or scope_hash(self._scope) in self._breakers:
                return None
            canonical = canonicalise(prefix, self._scope.language)
            if len(canonical) < self.prefetch_min_chars:
                return None
            found = self.store.memory.prefix_candidates(self._scope, canonical, limit=2)
            if len(found) != 1:
                return None
            key = found[0]
            entry = self.store.memory.peek(key)
            if entry is not None:
                entry.decoded()
            else:
                self._spawn(self._prefetch(key), "prefetch")
            self._event("prefetch", key=key, in_memory=entry is not None)
            return key
        except Exception as e:
            self._fail_open("prefetch", e)
            return None

    async def _prefetch(self, key: str) -> None:
        assert isinstance(self.store, TieredStore)
        await self.store.prefetch(key)

    # Capture and ingest.

    async def capture(
        self,
        text: str,
        pcm: bytes,
        sample_rate: int,
        channels: int = 1,
        *,
        tags: Mapping[str, str] | None = None,
        key: str | None = None,
        source: Literal["llm", "speak"] = "llm",
    ) -> None:
        """Enqueue a clip the provider just synthesised for ``text``. Returns at once; never raises."""
        try:
            if self._closed:
                self._event("capture_dropped", reason="closed")
                return
            self._ensure_worker()
            assert self._queue is not None
            try:
                self._queue.put_nowait(
                    _Capture(text, bytes(pcm), sample_rate, channels, dict(tags or {}), key, source)
                )
            except asyncio.QueueFull:
                self._event("reject", reason="queue_full", chars=len(text.strip()))
        except Exception as e:
            self._fail_open("capture", e)

    async def _drain(self) -> None:
        assert self._queue is not None
        queue = self._queue
        while True:
            c = await queue.get()
            try:
                if c is not None:
                    await self._ingest(c)
            except Exception as e:
                self._event("reject", reason="error", chars=len(c.text.strip()) if c else 0)
                self._fail_open("ingest", e)
            finally:
                queue.task_done()

    def _reject(self, reason: str, c: _Capture, key: str | None, **fields: Any) -> None:
        self._event("reject", reason=reason, key=key, chars=len(c.text.strip()), **fields)

    async def _ingest(self, c: _Capture) -> None:
        scope = self._scope
        canonical = canonicalise(c.text, scope.language)
        s = self._ctx(c.text, canonical, c.tags, c.source)
        namespace = _maybe(self.policy, "key_namespace", s)
        key = build_key(scope, canonical, namespace=namespace if isinstance(namespace, str) else None)
        if scope_hash(scope) in self._breakers:
            return self._reject("breaker", c, key)
        if c.key is not None and c.key != key:
            return self._reject("key_changed", c, key)
        if c.sample_rate != scope.sample_rate or c.channels != scope.channels:
            return self._reject("format", c, key)
        duration_ms = pcm_duration_ms(c.pcm, c.sample_rate, c.channels)
        if not plausible_duration(c.text, duration_ms):
            return self._reject("duration", c, key, duration_ms=round(duration_ms))
        if is_silent(c.pcm):
            return self._reject("silence", c, key)
        clip = Clip(round(duration_ms), c.sample_rate, c.channels, len(c.pcm))
        if not self.policy.should_store(s, clip):
            return self._reject("policy", c, key)
        recent = getattr(self.store, "recently_stored", None)
        if recent is not None and recent(key):
            self._event("store_coalesced", key=key, chars=s.chars)
            return
        verdict = await self.verifier.verify(c.text, c.pcm, c.sample_rate, c.channels, scope.language)
        if not verdict.ok:
            return self._reject("verify", c, key, score=verdict.score)
        audio = await asyncio.to_thread(codecs.encode, c.pcm, c.sample_rate, c.channels, self.codec)
        # A stored clip must decode to the clip that played, so a hit is never truncated.
        check = await asyncio.to_thread(codecs.decode, audio, self.codec, c.sample_rate, c.channels)
        if len(check) != len(c.pcm):
            return self._reject("codec", c, key)
        now = time.time()
        ttl = self.policy.ttl_for(s, None) or DEFAULT_TTL_S
        entry = Entry(
            key=key,
            audio=audio,
            codec=self.codec,
            sample_rate=c.sample_rate,
            channels=c.channels,
            duration_ms=round(duration_ms),
            text=c.text,
            canonical_text=canonical,
            language=scope.language,
            tags=c.tags,
            created_at=now,
            expires_at=now + ttl,
            verify_score=verdict.score,
            pcm=PcmStream.from_pcm(c.pcm, c.sample_rate, c.channels),
        )
        await self.store.put(key, entry)
        self._event(
            "store", key=key, chars=s.chars, bytes=len(audio), duration_ms=round(duration_ms), ttl_s=ttl
        )

    # Conveniences built on lookup and capture.

    async def synthesize(
        self, text: str, provider_fn: ProviderFn, *, tags: Mapping[str, str] | None = None
    ) -> Audio:
        """Lookup; on a miss call ``provider_fn(text)``, capture its audio and return it."""
        hit = await self.lookup(text, tags=tags)
        if hit is not None:
            try:
                pcm = hit.pcm if hit.stream.done else await asyncio.to_thread(hit.stream.result)
                return Audio(pcm, hit.sample_rate, hit.channels, cached=True, level=hit.level)
            except Exception as e:  # a clip that fails to decode is a miss
                self._fail_open("synthesize.decode", e)
        result = await provider_fn(text)
        audio = result if isinstance(result, Audio) else Audio(result[0], result[1])
        await self.capture(text, audio.pcm, audio.sample_rate, audio.channels, tags=tags)
        return audio

    async def stream(
        self,
        tokens: AsyncIterator[str],
        provider_stream_fn: ProviderStreamFn,
        *,
        tags: Mapping[str, str] | None = None,
    ) -> AsyncIterator[tuple[str, AsyncIterator[bytes]]]:
        """Split ``tokens`` into sentences and yield each with its audio, cached or synthesised."""
        splitter = SentenceSplitter(self._scope.language)
        async for token in tokens:
            for sentence in splitter.feed(token):
                yield sentence, await self._sentence_audio(sentence, provider_stream_fn, tags)
        rest = splitter.flush()
        if rest and rest.strip():
            yield rest, await self._sentence_audio(rest, provider_stream_fn, tags)

    async def _sentence_audio(
        self, sentence: str, provider_stream_fn: ProviderStreamFn, tags: Mapping[str, str] | None
    ) -> AsyncIterator[bytes]:
        found = await self.probe(sentence, tags=tags)
        if found.hit is not None:
            return _iterate(found.hit.pcm_stream())
        return self._capturing(sentence, provider_stream_fn(sentence), found, tags)

    async def _capturing(
        self, sentence: str, chunks: AsyncIterator[bytes], found: Lookup, tags: Mapping[str, str] | None
    ) -> AsyncIterator[bytes]:
        collected = bytearray()
        async for chunk in chunks:
            collected.extend(chunk)
            yield chunk
        # Only a clip consumed to the end is whole enough to store.
        if found.store:
            await self.capture(
                sentence,
                bytes(collected),
                self._scope.sample_rate,
                self._scope.channels,
                tags=tags,
                key=found.key,
            )

    # Sync wrappers for hosts without an event loop.

    def _sync_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None and loop.is_running():
            return loop
        if self._own_loop is None:
            ready = threading.Event()
            own = asyncio.new_event_loop()
            self._own_loop = own

            def run() -> None:
                asyncio.set_event_loop(own)
                own.call_soon(ready.set)
                own.run_forever()

            threading.Thread(target=run, name="tts-cache-sync", daemon=True).start()
            ready.wait()
            asyncio.run_coroutine_threadsafe(self.start(), own).result(self.warm_start_timeout_s + 5)
        return self._own_loop

    def _run_sync(self, coro: Coroutine[Any, Any, T], timeout: float) -> T:
        loop = self._sync_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            coro.close()
            raise RuntimeError("the sync API cannot be called from the cache's own event loop")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def lookup_sync(
        self, text: str, *, tags: Mapping[str, str] | None = None, timeout: float = 1.0
    ) -> Hit | None:
        try:
            return self._run_sync(self.lookup(text, tags=tags), timeout)
        except Exception as e:
            self._fail_open("lookup_sync", e)
            return None

    def capture_sync(
        self,
        text: str,
        pcm: bytes,
        sample_rate: int,
        channels: int = 1,
        *,
        tags: Mapping[str, str] | None = None,
        timeout: float = 1.0,
    ) -> None:
        try:
            self._run_sync(self.capture(text, pcm, sample_rate, channels, tags=tags), timeout)
        except Exception as e:
            self._fail_open("capture_sync", e)

    def close_sync(self, timeout: float = 2.0) -> None:
        try:
            self._run_sync(self.close(timeout), timeout + 1)
        except Exception as e:
            self._fail_open("close_sync", e)
        if self._own_loop is not None:
            self._own_loop.call_soon_threadsafe(self._own_loop.stop)
            self._own_loop = None


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


def _maybe(policy: object, name: str, *args: object) -> object:
    fn = getattr(policy, name, None)
    return fn(*args) if fn is not None else None


async def _iterate(chunks: Iterator[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
