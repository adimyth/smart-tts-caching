"""The write side's capture state machine, driven frame by frame."""

import asyncio

import pytest
from fake_tts import SR, FakeTTSService
from helpers import tone_pcm
from pipecat.frames.frames import (
    AggregatedTextFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.utils.text.base_text_aggregator import TextType

from tts_cache import MemoryStore, TTSCache
from tts_cache.metrics import LogSink
from tts_cache_pipecat import PipecatMetricsSink, TTSCacheWrite, scope_from_service

TEXT = "I can help you with your account today."


def setup() -> tuple[TTSCacheWrite, TTSCache, LogSink, FakeTTSService]:
    tts = FakeTTSService()
    sink = LogSink(log=None)
    cache = TTSCache(scope_from_service(tts, language="en"), MemoryStore(), metrics=sink)
    return TTSCacheWrite(cache, tts=tts), cache, sink, tts


def miss(
    ctx: str, text: str = TEXT, key: str = "tts:0123456789abcdef:0123456789abcdef01234567"
) -> AggregatedTextFrame:
    f = AggregatedTextFrame(text, TextType.SENTENCE)
    f.context_id = ctx
    f.metadata["tts_cache"] = {"hit": False, "key": key, "predicted": text, "store": True, "level": None}
    return f


async def play(w: TTSCacheWrite, ctx: str, text: str = TEXT, *, stop: bool = True) -> None:
    await w._observe(TTSStartedFrame(context_id=ctx))
    pcm = tone_pcm(text)
    for i in range(0, len(pcm), 1920):
        await w._observe(TTSAudioRawFrame(pcm[i : i + 1920], SR, 1, context_id=ctx))
    if stop:
        await w._observe(TTSStoppedFrame(context_id=ctx))


async def stored(cache: TTSCache, sink: LogSink) -> int:
    if cache._queue is not None:
        await cache._queue.join()
    return sink.counts["store"]


@pytest.mark.parametrize("event_first", [True, False])
async def test_request_event_in_either_order(event_first: bool) -> None:
    w, cache, sink, tts = setup()
    key = (await cache.probe(TEXT)).key
    if event_first:
        await w._on_tts_request(tts, "c1", TEXT)
    await w._observe(miss("c1", key=key))  # type: ignore[arg-type]
    if not event_first:
        await w._on_tts_request(tts, "c1", TEXT)
    await play(w, "c1")
    assert await stored(cache, sink) == 1


async def test_unconfirmed_capture_is_not_stored() -> None:
    w, cache, sink, _ = setup()
    await w._observe(miss("c1", key=(await cache.probe(TEXT)).key))  # type: ignore[arg-type]
    await play(w, "c1")
    assert await stored(cache, sink) == 0 and sink.counts["reject:unconfirmed"] == 1


async def test_mismatch_rejects_and_trips_breaker() -> None:
    w, cache, sink, tts = setup()
    await w._observe(miss("c1", key=(await cache.probe(TEXT)).key))  # type: ignore[arg-type]
    await w._on_tts_request(tts, "c1", TEXT + " extra")
    await play(w, "c1")
    assert await stored(cache, sink) == 0 and cache.breaker_tripped


async def test_audio_after_a_later_context_started_is_unclean() -> None:
    w, cache, sink, tts = setup()
    key = (await cache.probe(TEXT)).key
    await w._observe(miss("c1", key=key))  # type: ignore[arg-type]
    await w._on_tts_request(tts, "c1", TEXT)
    await play(w, "c1", stop=False)
    await w._observe(TTSStartedFrame(context_id="c2"))
    await w._observe(TTSAudioRawFrame(b"\x01\x00" * 480, SR, 1, context_id="c1"))
    await w._observe(TTSStoppedFrame(context_id="c1"))
    assert await stored(cache, sink) == 0 and sink.counts["reject:unclean"] == 1


async def test_next_start_closes_and_end_of_response_submits() -> None:
    w, cache, sink, tts = setup()
    key = (await cache.probe(TEXT)).key
    await w._observe(miss("c1", key=key))  # type: ignore[arg-type]
    await w._on_tts_request(tts, "c1", TEXT)
    await play(w, "c1", stop=False)
    await w._observe(TTSStartedFrame(context_id="c2"))
    await w._observe(LLMFullResponseEndFrame())
    assert await stored(cache, sink) == 1


async def test_implausible_duration_is_rejected() -> None:
    w, cache, sink, tts = setup()
    await w._observe(miss("c1", key=(await cache.probe(TEXT)).key))  # type: ignore[arg-type]
    await w._on_tts_request(tts, "c1", TEXT)
    await play(w, "c1", text="Hi")
    assert await stored(cache, sink) == 0 and sink.counts["reject:duration"] == 1


async def test_interruption_drops_open_captures() -> None:
    w, cache, sink, tts = setup()
    await w._observe(miss("c1", key=(await cache.probe(TEXT)).key))  # type: ignore[arg-type]
    await w._on_tts_request(tts, "c1", TEXT)
    await play(w, "c1", stop=False)
    await w._observe(InterruptionFrame())
    await w._observe(TTSStoppedFrame(context_id="c1"))
    assert await stored(cache, sink) == 0 and sink.counts["capture_dropped:interruption"] == 1


async def test_hit_stop_frame_gets_its_transcript_first() -> None:
    w, _, _, _ = setup()
    started = TTSStartedFrame(context_id="h1")
    started.append_to_context = False
    started.metadata["tts_cache"] = {"hit": True, "key": "k", "level": "A"}
    await w._observe(started)
    stop = TTSStoppedFrame(context_id="h1")
    stop.metadata["tts_cache"] = {"hit": True, "key": "k", "level": "A", "spoken_text": "Welcome."}
    before = await w._observe(stop)
    assert isinstance(before, TTSTextFrame) and before.text == "Welcome." and before.context_id == "h1"
    assert before.append_to_context is False


async def test_serve_only_never_captures() -> None:
    w, cache, sink, tts = setup()
    w._mode = "serve_only"
    await w._observe(miss("c1", key=(await cache.probe(TEXT)).key))  # type: ignore[arg-type]
    await w._on_tts_request(tts, "c1", TEXT)
    await play(w, "c1")
    assert await stored(cache, sink) == 0


async def test_pipecat_metrics_sink_pushes_frames() -> None:
    inner = LogSink(log=None)
    sink = PipecatMetricsSink(inner)
    pushed = []

    class P(FrameProcessor):
        async def push_frame(self, frame, direction=None):  # type: ignore[override]
            pushed.append(frame)

    sink.attach(P())
    sink.event("hit", chars=5, lookup_ms=0.4)
    sink.event("store", chars=5)
    await asyncio.sleep(0)
    assert inner.counts["hit"] == 1 and inner.counts["store"] == 1
    assert len(pushed) == 1 and pushed[0].data[0].processor == "TTSCache#hit"
