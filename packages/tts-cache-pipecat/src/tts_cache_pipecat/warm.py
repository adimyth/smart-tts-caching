"""Provider functions for ``tts-cache warm`` through the providers' REST classes (SOLUTION.md 11.8).

``rest_provider(scope)`` returns ``async (text) -> Audio`` that synthesises one
sentence through the scope's provider REST service, built with the scope's
model, voice and audio settings, in a one-frame Pipecat pipeline. The text is
sent as given: it is already the text the provider receives at runtime.

    tts-cache warm --from replay.json --scope scope.json --store redis://... \\
        --provider tts_cache_pipecat.warm:rest_provider
"""

import os
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from pipecat.frames.frames import Frame, TTSAudioRawFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import run_test

from tts_cache.cache import Audio
from tts_cache.scope import Scope


def build_rest_service(scope: Scope, session: aiohttp.ClientSession) -> TTSService:
    settings: dict[str, Any] = {"model": scope.model, "voice": scope.voice, **dict(scope.settings)}
    common = {"sample_rate": scope.sample_rate, "reuse_context_id_within_turn": False}
    if scope.service == "openai":
        from pipecat.services.openai.tts import OpenAITTSService

        settings.pop("language", None)
        return OpenAITTSService(
            api_key=os.environ["OPENAI_API_KEY"], settings=OpenAITTSService.Settings(**settings), **common
        )
    if scope.service == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService

        key = os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVENLABS_TTS_KEY"]
        return ElevenLabsHttpTTSService(
            api_key=key,
            aiohttp_session=session,
            settings=ElevenLabsHttpTTSService.Settings(**settings),
            **common,
        )
    if scope.service == "sarvam":
        from pipecat.services.sarvam.tts import SarvamHttpTTSService

        return SarvamHttpTTSService(
            api_key=os.environ["SARVAM_API_KEY"],
            aiohttp_session=session,
            settings=SarvamHttpTTSService.Settings(**settings),
            **common,
        )
    raise ValueError(f"no REST class for service {scope.service!r}")


class _Collect(FrameProcessor):
    def __init__(self) -> None:
        super().__init__(name="warm-collect")
        self.audio = bytearray()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self.audio.extend(frame.audio)
        await self.push_frame(frame, direction)


def rest_provider(
    scope: Scope,
    *,
    factory: Callable[[Scope, aiohttp.ClientSession], TTSService] = build_rest_service,
) -> Callable[[str], Awaitable[Audio]]:
    async def synthesize(text: str) -> Audio:
        async with aiohttp.ClientSession() as session:
            tts = factory(scope, session)
            collect = _Collect()
            await run_test(
                Pipeline([tts, collect]),
                frames_to_send=[TTSSpeakFrame(text, append_to_context=False)],
                pipeline_params=PipelineParams(audio_out_sample_rate=scope.sample_rate),
            )
        if not collect.audio:
            raise RuntimeError(f"no audio for {text!r}")
        return Audio(bytes(collect.audio), scope.sample_rate)

    return synthesize
