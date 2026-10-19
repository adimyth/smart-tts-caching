"""A deterministic in-process TTS service for pipeline tests.

``run_tts`` yields audio derived from the text it receives (a tone whose pitch
comes from the text's hash, 60 ms per character), in chunks, after a
configurable delay. It behaves like Pipecat's REST services: the base class
pushes the start frame, the transcript frame after the audio, and the stop
frame when the context goes quiet.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from helpers import tone_pcm
from pipecat.frames.frames import Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from tts_cache_pipecat.compat import register_service

SR = 24_000


class FakeTTSService(TTSService):
    def __init__(
        self,
        *,
        delay: float = 0.005,
        chunk_ms: int = 40,
        chunk_delay: float = 0.0,
        sample_rate: int | None = SR,
        voice: str = "fake-voice",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("reuse_context_id_within_turn", False)
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            stop_frame_timeout_s=0.05,
            sample_rate=sample_rate,
            settings=TTSSettings(model="fake-1", voice=voice, language=None),
            **kwargs,
        )
        self.delay = delay
        self.chunk_ms = chunk_ms
        self.chunk_delay = chunk_delay
        self.synthesized: list[str] = []

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        self.synthesized.append(text)
        await asyncio.sleep(self.delay)
        pcm = tone_pcm(text, self.sample_rate)
        step = self.sample_rate * self.chunk_ms // 1000 * 2
        for i in range(0, len(pcm), step):
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            yield TTSAudioRawFrame(pcm[i : i + step], self.sample_rate, 1, context_id=context_id)


register_service(FakeTTSService, "fake", (), capture_mode="exact")
