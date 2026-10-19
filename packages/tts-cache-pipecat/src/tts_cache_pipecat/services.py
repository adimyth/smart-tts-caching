"""Websocket TTS services that finish each sentence's server-side context (SOLUTION.md 11.8).

Each subclass runs the stock ``run_tts`` and then finishes the sentence's
context, so with ``reuse_context_id_within_turn=False`` every sentence's audio
is tagged with its own context id. Constructor signatures are the stock ones.
Lifted from ``harness/attribution.py``, where all three pass the attribution
test in English, Hindi and Tamil, clean and interrupted.
"""

from collections.abc import AsyncGenerator

from pipecat.frames.frames import Frame
from pipecat.services.elevenlabs.dialogue.tts import (
    ElevenLabsDialogueTTSService as _StockElevenLabsDialogueTTSService,
)
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService as _StockElevenLabsTTSService
from pipecat.services.sarvam.tts import SarvamTTSService as _StockSarvamTTSService


class ElevenLabsTTSService(_StockElevenLabsTTSService):
    """ElevenLabs multi-context websocket: flush, then close, each sentence's context."""

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        async for frame in super().run_tts(text, context_id):
            yield frame
        await self.flush_audio(context_id=context_id)
        await self._close_context(context_id)


class ElevenLabsDialogueTTSService(_StockElevenLabsDialogueTTSService):
    """ElevenLabs Text-to-Dialogue websocket: close each sentence's context, which generates it."""

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        async for frame in super().run_tts(text, context_id):
            yield frame
        await self._close_context(context_id)


class SarvamTTSService(_StockSarvamTTSService):
    """Sarvam websocket: flush after each sentence; the server answers each flush in order."""

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        async for frame in super().run_tts(text, context_id):
            yield frame
        await self.flush_audio(context_id=context_id)
