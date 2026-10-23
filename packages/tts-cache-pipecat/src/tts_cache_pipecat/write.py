"""``TTSCacheWrite``: downstream of the TTS service, captures each miss's audio (SOLUTION.md 11.3, 11.6).

A capture opens on the miss's ``AggregatedTextFrame`` (the service has set its
context id), is confirmed or rejected by the service's ``on_tts_request``
event in either arrival order, collects the audio frames tagged with its
context id, and closes on its stop frame, the next context's start frame, or
the end of the LLM response. Only a clean, confirmed clip of plausible
duration reaches ``cache.capture``. An interruption drops every capture not
yet handed over, so a truncated clip never enters the store. Behind a
fallback service, a clip with any audio not marked as the primary's is not
stored.

On a hit's stop frame the write side emits the sentence's ``TTSTextFrame``
just before forwarding the stop frame, so the assistant context records the
words that were spoken, in order with the audio.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from pipecat.frames.frames import (
    AggregatedTextFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.utils.text.base_text_aggregator import TextType

from tts_cache import TTSCache
from tts_cache.checks import pcm_duration_ms, plausible_duration
from tts_cache.metrics import FailOpen
from tts_cache_pipecat._bridge import bridge_for
from tts_cache_pipecat.compat import PRIMARY_AUDIO, capture_mode_for, is_fallback_service

META = "tts_cache"


@dataclass
class _Capture:
    context_id: str
    key: str
    predicted: str
    audio: bytearray = field(default_factory=bytearray)
    sample_rate: int | None = None
    channels: int | None = None
    prepared: str | None = None
    confirmed: bool = False
    rejected: bool = False
    clean: bool = True
    closed: bool = False
    fallback: bool = False


class TTSCacheWrite(FrameProcessor):
    def __init__(self, cache: TTSCache, *, tts: TTSService, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cache = cache
        self._tts = tts
        self._fail_open = FailOpen(cache.metrics)
        self._captures: dict[str, _Capture] = {}
        self._requests: OrderedDict[str, str] = OrderedDict()
        self._latest_started: str | None = None
        self._hit_append: dict[str, bool] = {}
        self._mode = "off"
        self._attested = False
        try:
            self._mode = capture_mode_for(tts)
            self._attested = is_fallback_service(tts)
            tts.add_event_handler("on_tts_request", self._on_tts_request)
        except Exception as e:
            self._fail_open("write.init", e)
            self._mode = "off"

    @property
    def mode(self) -> str:
        return self._mode

    # The service's request event (11.3).

    async def _on_tts_request(self, tts: TTSService, context_id: str, text: str) -> None:
        try:
            cap = self._captures.get(context_id)
            if cap is not None:
                self._check(cap, text)
                return
            self._requests[context_id] = text
            while len(self._requests) > 256:
                self._requests.popitem(last=False)
        except Exception as e:
            self._fail_open("write.on_tts_request", e)

    def _check(self, cap: _Capture, prepared: str) -> None:
        cap.prepared = prepared
        if prepared == cap.predicted:
            cap.confirmed = True
            return
        cap.rejected = True
        self._cache.report_key_mismatch(cap.predicted, prepared, context_id=cap.context_id)

    # Frames.

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        before: Frame | None = None
        if direction == FrameDirection.DOWNSTREAM:
            try:
                before = await self._observe(frame)
            except Exception as e:
                self._fail_open("write.process_frame", e)
        if before is not None:
            await self.push_frame(before, direction)
        await self.push_frame(frame, direction)

    async def _observe(self, frame: Frame) -> Frame | None:
        """Track ``frame``; returns a frame to push just before it (a hit's transcript frame)."""
        if isinstance(frame, TTSTextFrame):
            return None
        if isinstance(frame, AggregatedTextFrame):
            self._open(frame)
        elif isinstance(frame, TTSStartedFrame):
            for cap in self._captures.values():
                if cap.context_id != frame.context_id:
                    cap.closed = True
            self._latest_started = frame.context_id
            meta = frame.metadata.get(META)
            if meta and meta.get("hit") and frame.context_id:
                self._hit_append[frame.context_id] = frame.append_to_context
        elif isinstance(frame, TTSAudioRawFrame):
            cap = self._captures.get(frame.context_id or "")
            if cap is not None:
                self._append(cap, frame)
        elif isinstance(frame, TTSStoppedFrame):
            meta = frame.metadata.get(META)
            if meta and meta.get("hit"):
                return self._transcript(frame, meta)
            cap = self._captures.pop(frame.context_id or "", None)
            if cap is not None:
                await self._finish(cap)
        elif isinstance(frame, LLMFullResponseEndFrame):
            for context_id in list(self._captures):
                await self._finish(self._captures.pop(context_id))
        elif isinstance(frame, EndFrame):
            # Shutdown hook (4.2a): give queued captures up to 2 s to reach the store.
            for context_id in list(self._captures):
                await self._finish(self._captures.pop(context_id))
            await self._cache.drain(2.0)
        elif isinstance(frame, InterruptionFrame):
            for cap in self._captures.values():
                self._event("capture_dropped", cap, reason="interruption")
            self._captures.clear()
            self._requests.clear()
            self._hit_append.clear()
        return None

    def _open(self, frame: AggregatedTextFrame) -> None:
        if self._mode != "exact" or not frame.context_id:
            return
        meta = frame.metadata.get(META)
        if meta is not None:
            if (
                meta.get("hit")
                or not meta.get("store")
                or not meta.get("key")
                or meta.get("predicted") is None
            ):
                return
            cap = _Capture(frame.context_id, meta["key"], meta["predicted"])
        else:
            # A TTSSpeakFrame miss: the service built this frame from the forwarded utterance.
            speak = bridge_for(self._tts).speak
            pending = next((p for p in speak if frame.will_be_spoken and p.filtered == frame.text), None)
            if pending is None:
                return
            speak.remove(pending)
            cap = _Capture(frame.context_id, pending.key, pending.predicted)
        self._captures[frame.context_id] = cap
        prepared = self._requests.pop(frame.context_id, None)
        if prepared is not None:
            self._check(cap, prepared)

    def _append(self, cap: _Capture, frame: TTSAudioRawFrame) -> None:
        # Audio arriving after a later context has started cannot be trusted to
        # belong to this sentence (a service may tag audio with the playing context).
        if cap.closed or (self._latest_started is not None and self._latest_started != cap.context_id):
            cap.clean = False
            return
        if self._attested and not frame.metadata.get(PRIMARY_AUDIO):
            # A fallback provider spoke this, in a voice the scope does not name.
            cap.fallback = True
            return
        if cap.sample_rate is None:
            cap.sample_rate, cap.channels = frame.sample_rate, frame.num_channels
        elif (cap.sample_rate, cap.channels) != (frame.sample_rate, frame.num_channels):
            cap.clean = False
            return
        cap.audio.extend(frame.audio)

    def _transcript(self, frame: TTSStoppedFrame, meta: dict[str, Any]) -> TTSTextFrame | None:
        text = meta.get("spoken_text")
        if not text:
            return None
        t = TTSTextFrame(text, TextType.SENTENCE, context_id=frame.context_id)
        t.will_be_spoken = True
        t.append_to_context = self._hit_append.pop(frame.context_id or "", True)
        t.interruptible = frame.interruptible
        return t

    def _event(self, name: str, cap: _Capture, **fields: Any) -> None:
        try:
            self._cache.metrics.event(name, key=cap.key, chars=len(cap.predicted.strip()), **fields)
        except Exception:
            pass

    async def _finish(self, cap: _Capture) -> None:
        if cap.rejected:
            return  # counted as key_mismatch when it was rejected
        if not cap.confirmed:
            self._event("reject", cap, reason="unconfirmed")
            return
        if cap.fallback:
            self._event("reject", cap, reason="fallback")
            return
        if not cap.clean:
            self._event("reject", cap, reason="unclean")
            return
        if not cap.audio or cap.sample_rate is None or cap.channels is None:
            self._event("reject", cap, reason="no_audio")
            return
        duration = pcm_duration_ms(bytes(cap.audio), cap.sample_rate, cap.channels)
        if not plausible_duration(cap.predicted, duration):
            self._event("reject", cap, reason="duration", duration_ms=round(duration))
            return
        await self._cache.capture(
            cap.predicted,
            bytes(cap.audio),
            cap.sample_rate,
            cap.channels,
            tags=bridge_for(self._tts).tags,
            key=cap.key,
        )
