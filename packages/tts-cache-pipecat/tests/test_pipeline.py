import pytest
from fake_tts import FakeTTSService
from helpers import tone_pcm
from pipecat.frames.frames import (
    InterruptionFrame,
    LLMAssistantPushAggregationFrame,
    MetricsFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.services.settings import TTSSettings
from pipecat.tests.utils import SleepFrame
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter
from pipeline_utils import SR, contexts, run_cached, run_frames, settle, stream_signature, turn

from tts_cache import MemoryStore, TTSCache
from tts_cache.metrics import LogSink
from tts_cache_pipecat import TTSCacheRead, TTSCacheWrite, scope_from_service

S = [
    "Hello, and thank you for calling.",
    "I can help you with your account today.",
    "Could you tell me your registered mobile number?",
    "Take your time, there is no rush.",
]


def make(tts=None, store=None, **kw) -> tuple[TTSCache, FakeTTSService, LogSink]:
    tts = tts or FakeTTSService()
    sink = LogSink(log=None)
    cache = TTSCache(
        scope_from_service(tts, language="en-US"),
        store if store is not None else MemoryStore(),
        metrics=sink,
        **kw,
    )
    return cache, tts, sink


async def prime(cache: TTSCache, *texts: str) -> None:
    for t in texts:
        await cache.capture(t, tone_pcm(t, SR), SR)
    await settle(cache)


async def test_ordering_with_hits_at_two_and_four() -> None:
    cache, tts, sink = make()
    await prime(cache, S[1], S[3])
    frames = await run_cached(cache, tts, turn(*S))
    spoken = contexts(frames)
    assert [s.texts for s in spoken] == [[t] for t in S]
    assert [s.hit for s in spoken] == [False, True, False, True]
    for s, text in zip(spoken, S, strict=True):
        assert s.events == ["start", "audio", "text", "stop"], s.events
        assert bytes(s.audio) == tone_pcm(text, SR)
    # The fake provider received exactly the two misses.
    assert tts.synthesized == [S[0], S[2]]
    # One transcript frame per sentence reaches the assistant context, never two.
    appended = [f.text for f in frames if type(f).__name__ == "TTSTextFrame" and f.append_to_context]
    assert appended == S
    assert sum(isinstance(f, MetricsFrame) for f in frames) >= 2
    assert sink.counts["reject:key_mismatch"] == 0
    # The misses were captured: a second run makes no provider request.
    await settle(cache)
    assert sink.counts["store"] == 4
    tts2 = FakeTTSService()
    again = contexts(await run_cached(cache, tts2, turn(*S)))
    assert tts2.synthesized == [] and all(s.hit for s in again)
    assert [s.digest for s in again] == [s.digest for s in spoken]


async def expand_currency(text: str, text_type: str) -> str:
    return text.replace("$5", "five dollars")


async def test_text_prediction_with_filter_and_transform() -> None:
    cache, tts, sink = make(FakeTTSService(text_transforms=[("*", expand_currency)]))
    first = await run_cached(
        cache, tts, turn("**Thanks**, Alex.", "That costs $5."), filters=[MarkdownTextFilter()]
    )
    await settle(cache)
    assert tts.synthesized == ["Thanks, Alex.", "That costs five dollars."]
    assert sink.counts["reject:key_mismatch"] == 0 and sink.counts["store"] == 2
    assert [s.texts for s in contexts(first)] == [["Thanks, Alex."], ["That costs $5."]]
    tts2 = FakeTTSService(text_transforms=[("*", expand_currency)])
    second = contexts(
        await run_cached(cache, tts2, turn("Thanks, Alex.", "That costs $5."), filters=[MarkdownTextFilter()])
    )
    assert tts2.synthesized == [] and all(s.hit for s in second)
    # The transcript carries the filtered text, never the transform's output.
    assert [s.texts for s in second] == [["Thanks, Alex."], ["That costs $5."]]
    misses = [e for e in sink.counts if e.startswith("miss")]
    assert misses == ["miss"]


async def test_wrong_mirror_trips_the_breaker() -> None:
    cache, tts, sink = make()
    await prime(cache, S[1])
    read = TTSCacheRead(cache, tts=tts)
    real = read._predict

    async def drifted(filtered, text_type):
        predicted = await real(filtered, text_type)
        return None if predicted is None else predicted + " "

    read._predict = drifted  # type: ignore[method-assign]
    await run_cached(cache, tts, turn(S[0]), read=read)
    assert sink.counts["reject:key_mismatch"] == 1 and cache.breaker_tripped
    tts2 = FakeTTSService()
    frames = await run_cached(cache, tts2, turn(S[1]))
    assert tts2.synthesized == [S[1]]  # cached, but served as a miss
    assert not any(s.hit for s in contexts(frames))
    await settle(cache)
    assert sink.counts["store"] == 1  # only the primed clip


async def test_interruption_drops_the_open_capture() -> None:
    cache, tts, sink = make(FakeTTSService(chunk_delay=0.02))
    frames = [*turn(S[0], S[1])[:-1], SleepFrame(0.15), InterruptionFrame(), SleepFrame(0.2), turn()[-1]]
    await run_cached(cache, tts, frames)
    await settle(cache)
    assert sink.counts["store"] == 0
    assert sink.counts["capture_dropped:interruption"] >= 1
    assert len(cache.store) == 0  # type: ignore[arg-type]


async def test_speak_frame_miss_then_hit() -> None:
    cache, tts, sink = make()
    first = await run_cached(cache, tts, [TTSSpeakFrame("Welcome to Acme.")])
    await settle(cache)
    assert tts.synthesized == ["Welcome to Acme."] and sink.counts["store"] == 1
    assert [s.texts for s in contexts(first)] == [["Welcome to Acme."]]
    tts2 = FakeTTSService()
    second = await run_cached(cache, tts2, [TTSSpeakFrame("Welcome to Acme.")])
    assert tts2.synthesized == []
    spoken = contexts(second)
    assert spoken[0].hit and spoken[0].texts == ["Welcome to Acme."]
    assert any(isinstance(f, LLMAssistantPushAggregationFrame) for f in second)


async def test_settings_update_changes_scope() -> None:
    cache, tts, _ = make()
    await prime(cache, S[0])
    before = cache.scope
    frames = [TTSUpdateSettingsFrame(delta=TTSSettings(voice="other-voice")), *turn(S[0])]
    spoken = contexts(await run_cached(cache, tts, frames))
    assert cache.scope != before and cache.scope.voice == "other-voice"
    assert not spoken[0].hit and tts.synthesized == [S[0]]


async def test_sample_rate_from_start_frame() -> None:
    tts = FakeTTSService(sample_rate=None)
    cache, _, _ = make(tts)
    assert cache.scope.sample_rate == 0
    await run_cached(cache, tts, turn(S[0]))
    assert cache.scope.sample_rate == SR


async def test_prefix_prefetch_parks_the_hot_clip(tmp_path) -> None:
    from tts_cache import SQLiteStore, TieredStore

    db = tmp_path / "c.db"
    first, _, _ = make(store=SQLiteStore(db))
    await prime(first, S[1], S[3])
    await first.close()
    tts = FakeTTSService()
    sink = LogSink(log=None)
    # No clip fits the warm budget; the hot set's texts still load, so the read
    # side can fetch a clip while its sentence is still streaming.
    store = TieredStore(MemoryStore(warm_start_bytes=1), SQLiteStore(db), deadline_ms=50)
    cache = TTSCache(scope_from_service(tts, language="en-US"), store, metrics=sink, warm_start_bytes=1)
    await cache.start()
    assert len(store.memory) == 0 and sum(len(t) for t in store.memory.warm_texts.values()) == 2
    spoken = contexts(await run_cached(cache, tts, turn(*S)))
    assert [s.hit for s in spoken] == [False, True, False, True]
    assert sink.counts["prefetch"] == 2 and sink.counts["prefetch_hit"] == 2
    await cache.close()


# Fail-open: a broken cache leaves the frame stream identical to no cache.


async def baseline(sentences: list[str]) -> list[tuple[str, str]]:
    tts = FakeTTSService(text_filters=[MarkdownTextFilter()])
    return stream_signature(await run_frames(Pipeline([tts]), turn(*sentences)))


class BrokenStore(MemoryStore):
    async def get(self, key):
        raise ConnectionError("store down")

    async def put(self, key, entry):
        raise ConnectionError("store down")


@pytest.mark.parametrize("fault", ["store", "canonicaliser", "write_side", "read_side"])
async def test_fail_open_stream_matches_no_cache(fault: str, monkeypatch) -> None:
    expected = await baseline(S)
    cache, tts, sink = make(store=BrokenStore() if fault == "store" else None)

    def boom(*a, **k):
        raise RuntimeError(f"injected {fault}")

    async def aboom(*a, **k):
        raise RuntimeError(f"injected {fault}")

    write = TTSCacheWrite(cache, tts=tts)
    read = TTSCacheRead(cache, tts=tts, text_filters=[MarkdownTextFilter()])
    if fault == "canonicaliser":
        monkeypatch.setattr("tts_cache.cache.canonicalise", boom)
    elif fault == "write_side":
        monkeypatch.setattr(write, "_observe", aboom)
        monkeypatch.setattr(write, "_check", boom)
    elif fault == "read_side":
        monkeypatch.setattr(read, "_sentence_inner", aboom)
    got = stream_signature(await run_cached(cache, tts, turn(*S), read=read, write=write))
    assert got == expected
    assert sink.counts["fail_open"] >= 1


async def test_level_c_hit_writes_the_stored_text_to_the_transcript() -> None:
    import numpy as np

    from tts_cache import build_key, canonicalise
    from tts_cache.match import Matchers
    from tts_cache.match.semantic import SemanticMatcher
    from tts_cache.policy import AllOf, LevelC, MinChars, Probation
    from tts_cache.stores.tiered import TieredStore

    stored = "Your order will arrive on Monday."
    spoken_by_llm = "Your order is going to reach you on Monday."
    meanings = {canonicalise(stored, "en"): 0, canonicalise(spoken_by_llm, "en"): 0}

    class Stub:
        def encode(self, sentences):
            out = np.zeros((len(sentences), 8), dtype=np.float32)
            for i, s in enumerate(sentences):
                out[i, meanings.get(s, 1 + hash(s) % 7)] = 1.0
            return out

    tts = FakeTTSService()
    memory = MemoryStore()
    semantic = SemanticMatcher({"en": "stub"}, threshold=0.9, encoder=Stub())
    sink = LogSink(log=None)
    cache = TTSCache(
        scope_from_service(tts, language="en-US"),
        TieredStore(memory, MemoryStore()),
        policy=AllOf(Probation(), MinChars(), LevelC()),
        matcher=Matchers(semantic=semantic),
        metrics=sink,
    )
    await prime(cache, stored)
    memory.load_warm(cache.scope, [memory.peek(build_key(cache.scope, canonicalise(stored, "en")))])  # type: ignore[list-item]
    semantic.refresh(cache.scope, memory)
    frames = await run_cached(cache, tts, turn(spoken_by_llm, S[0]))
    spoken = contexts(frames)
    assert spoken[0].hit and spoken[0].texts == [stored]  # the words that were spoken
    meta = next(
        f.metadata["tts_cache"]
        for f in frames
        if type(f).__name__ == "TTSStoppedFrame" and f.metadata.get("tts_cache")
    )
    assert (meta["spoken_text"], meta["requested_text"]) == (stored, spoken_by_llm)
    assert bytes(spoken[0].audio) == tone_pcm(stored, SR)
    assert tts.synthesized == [S[0]]  # nothing for the Level C sentence
    appended = [f.text for f in frames if type(f).__name__ == "TTSTextFrame" and f.append_to_context]
    assert appended == [stored, S[0]]
