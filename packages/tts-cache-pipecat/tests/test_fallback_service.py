"""A fallback service: the scope is its primary's, and only the primary's audio is stored."""

from collections.abc import AsyncGenerator
from typing import Any, Literal

import pytest
from fake_tts import SR, FakeTTSService
from helpers import tone_pcm
from pipecat.frames.frames import Frame, TTSAudioRawFrame, TTSSpeakFrame, TTSUpdateSettingsFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipeline_utils import contexts, run_cached, settle

from tts_cache import MemoryStore, TTSCache
from tts_cache.metrics import LogSink
from tts_cache_pipecat import TTSCacheRead, mark_primary_audio, register_fallback_service, scope_from_service

TEXT = "Welcome to Acme, how can I help?"


class FakeFallbackTTSService(TTSService):
    """Speaks with its primary's voice, or a second voice when the primary is down.

    ``mark`` says which audio frames it marks as the primary's: all of them
    (correct), none (a service that forgot), or only the first (a primary that
    failed mid-sentence and was finished by the fallback).
    """

    def __init__(
        self, *, primary_down: bool = False, mark: Literal["all", "none", "first"] = "all", **kwargs: Any
    ) -> None:
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            stop_frame_timeout_s=0.05,
            sample_rate=SR,
            settings=TTSSettings(model="wrapper", voice="wrapper", language=None),
            **kwargs,
        )
        self.primary = FakeTTSService(voice="primary-voice")
        self.primary_down = primary_down
        self.mark = mark
        self.synthesized: list[tuple[str, str]] = []

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        source = "fallback" if self.primary_down else "primary"
        self.synthesized.append((source, text))
        pcm = tone_pcm(text if source == "primary" else f"{text} (fallback)", SR)
        step = SR * 40 // 1000 * 2
        for n, i in enumerate(range(0, len(pcm), step)):
            frame = TTSAudioRawFrame(pcm[i : i + step], SR, 1, context_id=context_id)
            if source == "primary" and (self.mark == "all" or (self.mark == "first" and n == 0)):
                mark_primary_audio(frame)
            yield frame


register_fallback_service(FakeFallbackTTSService, primary=lambda tts: tts.primary)


def make(tts: FakeFallbackTTSService) -> tuple[TTSCache, LogSink]:
    sink = LogSink(log=None)
    return TTSCache(scope_from_service(tts, language="en-US"), MemoryStore(), metrics=sink), sink


def test_scope_is_the_primarys() -> None:
    scope = scope_from_service(FakeFallbackTTSService(), language="en-US")
    assert (scope.service, scope.model, scope.voice, scope.sample_rate) == (
        "fake",
        "fake-1",
        "primary-voice",
        SR,
    )


@pytest.mark.parametrize("primary_down", [False, True])
async def test_primary_audio_is_stored_and_served(primary_down: bool) -> None:
    tts = FakeFallbackTTSService()
    cache, sink = make(tts)
    await run_cached(cache, tts, [TTSSpeakFrame(TEXT)])
    await settle(cache)
    assert sink.counts["store"] == 1
    # A hit plays the primary's voice whether or not the primary is up.
    again = FakeFallbackTTSService(primary_down=primary_down)
    spoken = contexts(await run_cached(cache, again, [TTSSpeakFrame(TEXT)]))
    assert again.synthesized == [] and spoken[0].hit


@pytest.mark.parametrize(
    "kwargs",
    [{"primary_down": True}, {"mark": "none"}, {"mark": "first"}],
    ids=["fallback", "unmarked", "partial"],
)
async def test_audio_not_marked_as_the_primarys_is_never_stored(kwargs: dict[str, Any]) -> None:
    tts = FakeFallbackTTSService(**kwargs)
    cache, sink = make(tts)
    await run_cached(cache, tts, [TTSSpeakFrame(TEXT)])
    await settle(cache)
    assert sink.counts["store"] == 0 and sink.counts["reject:fallback"] == 1
    healthy = FakeFallbackTTSService()
    spoken = contexts(await run_cached(cache, healthy, [TTSSpeakFrame(TEXT)]))
    assert not spoken[0].hit and healthy.synthesized == [("primary", TEXT)]


async def test_settings_update_stops_serving() -> None:
    tts = FakeFallbackTTSService()
    cache, _ = make(tts)
    read = TTSCacheRead(cache, tts=tts)
    assert read.mode == "exact"
    await run_cached(cache, tts, [TTSUpdateSettingsFrame(delta=TTSSettings(voice="other"))], read=read)
    assert read.mode == "off"
