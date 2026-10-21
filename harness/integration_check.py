"""TEST_PLAN_CORE.md 4.6: the README wiring on a stock Pipecat pipeline with a live service and Redis.

A bot without a transport: the LLM's output is streamed into the pipeline as
text frames, as an LLM service would push them. The same sentence is spoken
in two turns; the second must make no provider request.

    python harness/integration_check.py en   # ElevenLabs websocket
    python harness/integration_check.py hi   # Sarvam websocket
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from tts_cache import DEFAULT_POLICY, MemoryStore, RedisStore, TieredStore, TTSCache
from tts_cache_pipecat import ElevenLabsTTSService, SarvamTTSService, TTSCacheRead, TTSCacheWrite, scope_from_service

REDIS_URL = os.environ.get("INTEGRATION_REDIS_URL", "redis://localhost:6380/7")
SENTENCE = {
    "en": "Thank you for calling, how can I help you today?",
    "hi": "कॉल करने के लिए धन्यवाद, मैं आपकी क्या मदद कर सकता हूँ?",
}


class AudioCounter(FrameProcessor):
    """Stands in for transport.output(): counts the audio that would play, and says when the bot speaks.

    Websocket TTS services pause after each turn until the output transport sends
    BotStoppedSpeakingFrame upstream; without it the second turn never reaches the service.
    Playback here is instant: the bot stops speaking when its last open context stops.
    """

    def __init__(self) -> None:
        super().__init__()
        self.frames = 0
        self.open: set[str] = set()
        self.speaking = False

    async def process_frame(self, frame, direction: FrameDirection) -> None:  # type: ignore[no-untyped-def]
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, TTSStartedFrame):
            self.open.add(frame.context_id or "")
        elif isinstance(frame, TTSAudioRawFrame):
            self.frames += 1
            if not self.speaking:
                self.speaking = True
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        elif isinstance(frame, TTSStoppedFrame):
            self.open.discard(frame.context_id or "")
            if self.speaking and not self.open:
                self.speaking = False
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)


def make_tts(lang: str):  # type: ignore[no-untyped-def]
    # One context id per sentence, and no text_filters on the service: TTSCacheRead runs them.
    if lang == "en":
        return ElevenLabsTTSService(
            api_key=os.environ["ELEVENLABS_TTS_KEY"],
            settings=ElevenLabsTTSService.Settings(
                model="eleven_flash_v2_5", voice=os.environ["ELEVENLABS_VOICE_ID"], language="en"
            ),
            reuse_context_id_within_turn=False,
        )
    return SarvamTTSService(
        api_key=os.environ["SARVAM_API_KEY"],
        settings=SarvamTTSService.Settings(
            model=os.environ["SARVAM_TTS_MODEL"], voice=os.environ["SARVAM_TTS_SPEKAER"], language="hi-IN"
        ),
        reuse_context_id_within_turn=False,
    )


async def main(lang: str) -> dict[str, object]:
    tts = make_tts(lang)
    requests: list[str] = []

    @tts.event_handler("on_tts_request")
    async def on_tts_request(_tts, _context_id: str, text: str) -> None:  # type: ignore[no-untyped-def]
        requests.append(text)

    cache = TTSCache(
        scope=scope_from_service(tts, language=f"{lang}-IN"),
        store=TieredStore(MemoryStore(warm_start_bytes=4 << 20), RedisStore(REDIS_URL), deadline_ms=20),
        policy=DEFAULT_POLICY,
    )
    await cache.start()
    out = AudioCounter()
    pipeline = Pipeline(
        [TTSCacheRead(cache, tts=tts, text_filters=[MarkdownTextFilter()]), tts, TTSCacheWrite(cache, tts=tts), out]
    )
    task = PipelineTask(pipeline, params=PipelineParams(audio_out_sample_rate=24_000))
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(1.0)
    turns = []
    for _ in range(2):
        before_req, before_audio = len(requests), out.frames
        t0 = time.monotonic()
        await task.queue_frame(LLMFullResponseStartFrame())
        for word in SENTENCE[lang].split(" "):
            await task.queue_frame(LLMTextFrame(word + " "))
            await asyncio.sleep(0.02)  # an LLM streams tokens; the harness uses the same 20 ms gap
        await task.queue_frame(LLMFullResponseEndFrame())
        while out.frames == before_audio and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.01)
        await asyncio.sleep(3.0)  # let the turn play out and the capture land
        await cache.drain(5.0)
        turns.append({"provider_requests": len(requests) - before_req, "audio_frames": out.frames - before_audio})
    await task.cancel()
    await runner
    await cache.close()
    return {"lang": lang, "turns": turns, "pass": turns[0]["provider_requests"] == 1 and turns[1]["provider_requests"] == 0 and turns[1]["audio_frames"] > 0}


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    print(json.dumps(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "en")), ensure_ascii=False))
