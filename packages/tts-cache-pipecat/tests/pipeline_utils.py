"""Run turns through [TTSCacheRead, tts, TTSCacheWrite] on Pipecat's test runner and read the result."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from pipecat.frames.frames import (
    AggregatedTextFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests.utils import run_test

from tts_cache import TTSCache
from tts_cache_pipecat import TTSCacheRead, TTSCacheWrite

SR = 24_000


def turn(*sentences: str) -> list[Frame]:
    """An LLM turn streamed word by word."""
    frames: list[Frame] = [LLMFullResponseStartFrame()]
    for s in sentences:
        for word in s.split(" "):
            frames.append(LLMTextFrame(word + " "))
    frames.append(LLMFullResponseEndFrame())
    return frames


@dataclass
class Spoken:
    """One context's frames, in the order they reached the end of the pipeline."""

    context_id: str
    audio: bytearray = field(default_factory=bytearray)
    texts: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    hit: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(bytes(self.audio)).hexdigest()[:16]


def contexts(frames: Sequence[Frame]) -> list[Spoken]:
    out: dict[str, Spoken] = {}
    for f in frames:
        cid = getattr(f, "context_id", None)
        if not cid or not isinstance(f, (TTSStartedFrame, TTSAudioRawFrame, TTSTextFrame, TTSStoppedFrame)):
            continue
        s = out.setdefault(cid, Spoken(cid))
        if isinstance(f, TTSStartedFrame):
            s.events.append("start")
            s.hit = bool(f.metadata.get("tts_cache", {}).get("hit"))
        elif isinstance(f, TTSAudioRawFrame):
            if not s.events or s.events[-1] != "audio":
                s.events.append("audio")
            s.audio.extend(f.audio)
        elif isinstance(f, TTSTextFrame):
            s.events.append("text")
            s.texts.append(f.text)
        else:
            s.events.append("stop")
    return list(out.values())


def stream_signature(frames: Sequence[Frame]) -> list[tuple[str, str]]:
    """Frames as the host sees them, without random context ids or cache metadata."""
    sig: list[tuple[str, str]] = []
    audio = bytearray()
    for f in frames:
        if isinstance(f, TTSAudioRawFrame):
            audio.extend(f.audio)
            continue
        if audio:
            sig.append(("audio", hashlib.sha256(bytes(audio)).hexdigest()[:12]))
            audio = bytearray()
        if isinstance(f, (TTSTextFrame, AggregatedTextFrame)):
            sig.append((type(f).__name__, f.text))
        elif isinstance(
            f, (TTSStartedFrame, TTSStoppedFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame)
        ):
            sig.append((type(f).__name__, ""))
    if audio:
        sig.append(("audio", hashlib.sha256(bytes(audio)).hexdigest()[:12]))
    return sig


async def run_frames(processor: FrameProcessor, frames: Sequence[Frame]) -> list[Frame]:
    down, _ = await run_test(
        processor,
        frames_to_send=frames,
        pipeline_params=PipelineParams(audio_out_sample_rate=SR),
    )
    return list(down)


async def run_cached(
    cache: TTSCache, tts, frames: Sequence[Frame], *, filters=(), read=None, write=None
) -> list[Frame]:
    read = read or TTSCacheRead(cache, tts=tts, text_filters=list(filters))
    write = write or TTSCacheWrite(cache, tts=tts)
    return await run_frames(Pipeline([read, tts, write]), frames)


async def settle(cache: TTSCache) -> None:
    if cache._queue is not None:
        await cache._queue.join()
