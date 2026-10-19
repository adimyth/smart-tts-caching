"""``TTSCacheRead``: upstream of the TTS service; splits, keys, looks up and injects hits (11.3, 11.5).

The read side owns the text filters, splits the LLM's tokens into sentences
with the core splitter, predicts the text the provider will receive (filters,
then the service's own transforms and preparation), and keys on that
prediction. A hit is played as ``TTSStartedFrame``, 20 ms ``TTSAudioRawFrame``s
and ``TTSStoppedFrame``, which ride the service's serialization queue in
order; the stop frame carries the spoken text for the write side, which emits
the transcript frame. A miss goes to the service as an ``AggregatedTextFrame``
carrying the key and the prediction for the write side to check.

Every frame is handled inside a fail-open boundary: on any exception the
sentence goes to the service as a plain miss.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from pipecat.frames.frames import (
    AggregatedTextFrame,
    EagerTranscriptionFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMAssistantPushAggregationFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    MetricsFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData, TTSUsageMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.utils.text.base_text_aggregator import TextType
from pipecat.utils.text.base_text_filter import BaseTextFilter
from pipecat.utils.text.transforms.pronunciations import PronunciationTransform
from pipecat.utils.types import is_given

from tts_cache import TTSCache
from tts_cache.cache import Lookup
from tts_cache.metrics import FailOpen
from tts_cache.split import SentenceSplitter
from tts_cache_pipecat._bridge import PendingSpeak, bridge_for
from tts_cache_pipecat.compat import (
    UnsupportedService,
    capture_mode_for,
    scope_from_service,
    scope_from_settings,
    settings_after,
)

META = "tts_cache"
_TRANSCRIPTIONS = (TranscriptionFrame, InterimTranscriptionFrame, EagerTranscriptionFrame)


class TTSCacheRead(FrameProcessor):
    def __init__(
        self,
        cache: TTSCache,
        *,
        tts: TTSService,
        text_filters: Sequence[BaseTextFilter] = (),
        tags: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._cache = cache
        self._tts = tts
        self._filters = list(text_filters)
        self._tags = dict(tags or {})
        self._fail_open = FailOpen(cache.metrics)
        self._splitter = SentenceSplitter(cache.scope.language)
        self._turn_index = 0
        self._llm_response_started = False
        self._emitted = False
        self._parked: str | None = None
        self._mode = "off"
        try:
            self._mode = capture_mode_for(tts)
            bridge_for(tts).tags = self._tags
            if tts._text_filters:
                # The service would filter the read side's already-filtered text a second time.
                self._fail_open("read.init", ValueError("construct the TTS service without text_filters"))
                self._mode = "off"
            scope_from_service(
                tts, language=cache.scope.language, sample_rate=cache.scope.sample_rate or None
            )
        except UnsupportedService as e:
            self._fail_open("read.scope", e)
            self._mode = "off"
        except Exception as e:
            self._fail_open("read.init", e)
            self._mode = "off"

    @property
    def mode(self) -> str:
        return self._mode

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM or self._mode == "off":
            await self.push_frame(frame, direction)
            return
        try:
            consumed = await self._handle(frame)
        except Exception as e:
            self._fail_open("read.process_frame", e)
            consumed = False
        if not consumed:
            await self.push_frame(frame, direction)

    async def _handle(self, frame: Frame) -> bool:
        """Handle ``frame``; True when it was consumed and must not be forwarded."""
        if (
            isinstance(frame, (TextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame))
            and frame.skip_tts
        ):
            return False
        if isinstance(frame, StartFrame):
            await self._on_start(frame)
            return False
        if isinstance(frame, AggregatedTextFrame) or isinstance(frame, _TRANSCRIPTIONS):
            return False
        if isinstance(frame, TextFrame):
            for sentence in self._splitter.feed(frame.text):
                await self._sentence(sentence)
            self._maybe_prefetch()
            return True
        if isinstance(frame, LLMFullResponseStartFrame):
            self._turn_index = 0
            self._llm_response_started = True
            return False
        if isinstance(frame, (LLMFullResponseEndFrame, EndFrame)):
            rest = self._splitter.flush()
            if rest:
                await self._sentence(rest)
            self._llm_response_started = False
            return False
        if isinstance(frame, TTSSpeakFrame):
            return await self._speak(frame)
        if isinstance(frame, InterruptionFrame):
            self._parked = None
            self._splitter.reset()
            self._turn_index = 0
            self._llm_response_started = False
            for f in self._filters:
                await f.handle_interruption()
            return False
        if isinstance(frame, TTSUpdateSettingsFrame):
            if frame.service is None or frame.service is self._tts:
                await self._on_settings(frame)
            return False
        return False

    # Scope.

    async def _on_start(self, frame: StartFrame) -> None:
        if self._cache.scope.sample_rate:
            return
        try:
            scope = scope_from_service(
                self._tts, language=self._cache.scope.language, sample_rate=frame.audio_out_sample_rate
            )
            await self._cache.set_scope(scope)
        except Exception as e:
            self._fail_open("read.start", e)
            self._mode = "off"

    async def _on_settings(self, frame: TTSUpdateSettingsFrame) -> None:
        try:
            settings = settings_after(self._tts, frame.delta, frame.settings)
            rate = self._cache.scope.sample_rate or None
            scope = scope_from_settings(
                self._tts, settings, language=self._cache.scope.language, sample_rate=rate
            )
            await self._cache.set_scope(scope)
        except Exception as e:
            # A scope we cannot compute could serve the old voice: stop serving.
            self._fail_open("read.settings", e)
            self._mode = "off"

    def _maybe_prefetch(self) -> None:
        """At most one prefix prefetch per sentence, while it is still streaming (5)."""
        if self._parked is None and self._splitter.pending.strip():
            self._parked = self._cache.prefetch(self._splitter.pending)

    # Text prediction (11.3).

    async def _filter(self, sentence: str) -> str | None:
        """``filters(lstrip_newlines(sentence))``, or None when the service would drop the sentence."""
        text = sentence.lstrip("\n")
        if not text.strip():
            return None
        for f in self._filters:
            await f.reset_interruption()
            text = await f.filter(text)
        return text if text.strip() else None

    async def _predict(self, filtered: str, text_type: TextType | str) -> str | None:
        """The service's transforms and preparation applied to ``filtered``; None if a transform raises."""
        tts = self._tts
        transformed = filtered
        try:
            for transform_type, transform in tts._text_transforms:
                if transform_type == text_type or transform_type == "*":
                    if isinstance(transform, PronunciationTransform) and not tts.supports_pronunciations:
                        continue
                    transformed = await transform(transformed, text_type)
            return tts._prepare_text_for_tts(transformed)
        except Exception:
            # The service raises on the same input and reports it as it does today.
            return None

    def _skipped(self, text_type: TextType | str) -> bool:
        return text_type in self._tts._skip_text_types

    async def _sentence(self, sentence: str) -> None:
        self._emitted = False
        parked, self._parked = self._parked, None
        try:
            await self._sentence_inner(sentence, parked)
        except Exception as e:
            self._fail_open("read.sentence", e)
            # Degrade to a plain miss, unless this sentence already went out.
            if not self._emitted:
                await self.push_frame(AggregatedTextFrame(sentence, TextType.SENTENCE, raw_text=sentence))

    async def _sentence_inner(self, sentence: str, parked: str | None = None) -> None:
        if self._skipped(TextType.SENTENCE):
            await self.push_frame(AggregatedTextFrame(sentence, TextType.SENTENCE, raw_text=sentence))
            return
        filtered = await self._filter(sentence)
        if filtered is None:
            # Whitespace only, before or after the filters: the service drops it as it does today.
            await self.push_frame(AggregatedTextFrame(sentence, TextType.SENTENCE, raw_text=sentence))
            return
        predicted = await self._predict(filtered, TextType.SENTENCE)
        found = Lookup(None, None)
        if predicted is not None:
            found = await self._cache.probe(predicted, tags=self._tags, turn_index=self._turn_index)
        self._turn_index += 1
        if found.hit is not None and parked is not None and found.hit.key == parked:
            self._cache.metrics.event("prefetch_hit", key=parked)
        if found.hit is not None and await self._play(found, filtered):
            return
        miss = AggregatedTextFrame(filtered, TextType.SENTENCE, raw_text=sentence)
        miss.metadata[META] = {
            "hit": False,
            "key": found.key,
            "predicted": predicted,
            "store": bool(found.store and found.key and predicted is not None and self._mode == "exact"),
            "level": None,
        }
        await self.push_frame(miss)

    # Hits (11.5).

    async def _play(
        self,
        found: Lookup,
        filtered: str,
        *,
        append_to_context: bool = True,
        interruptible: bool = True,
        push_aggregation: bool = False,
    ) -> bool:
        """Push the hit's frames; False (nothing pushed) if its audio cannot be read."""
        hit = found.hit
        assert hit is not None
        chunks = hit.apcm_stream()
        try:
            first = await anext(chunks)
        except Exception as e:
            self._fail_open("read.decode", e)
            return False
        context_id = str(uuid.uuid4())
        spoken = filtered if hit.level == "A" else hit.spoken_text

        def mark(f: Frame) -> Frame:
            f.interruptible = interruptible
            return f

        started = TTSStartedFrame(context_id=context_id)
        started.append_to_context = append_to_context
        started.metadata[META] = {"hit": True, "key": hit.key, "level": hit.level}
        self._emitted = True
        await self.push_frame(mark(started))
        try:
            await self.push_frame(
                mark(TTSAudioRawFrame(first, hit.sample_rate, hit.channels, context_id=context_id))
            )
            async for chunk in chunks:
                await self.push_frame(
                    mark(TTSAudioRawFrame(chunk, hit.sample_rate, hit.channels, context_id=context_id))
                )
        except Exception as e:
            self._fail_open("read.play", e)
        stopped = TTSStoppedFrame(context_id=context_id)
        stopped.metadata[META] = {"hit": True, "key": hit.key, "level": hit.level, "spoken_text": spoken}
        await self.push_frame(mark(stopped))
        try:
            if push_aggregation:
                await self.push_frame(mark(LLMAssistantPushAggregationFrame()))
            await self.push_frame(self._hit_metrics())
        except Exception as e:
            self._fail_open("read.play", e)
        return True

    def _hit_metrics(self) -> MetricsFrame:
        model = self._tts._settings.model
        model_name = str(model) if is_given(model) and model else None
        return MetricsFrame(
            data=[
                TTFBMetricsData(processor=self._tts.name, model=model_name, value=0.0),
                TTSUsageMetricsData(processor=self._tts.name, model=model_name, value=0),
            ]
        )

    # Scripted utterances (11.5).

    async def _speak(self, frame: TTSSpeakFrame) -> bool:
        try:
            if self._skipped(frame.text_type):
                return False
            filtered = await self._filter(frame.text)
            if filtered is None:
                return False
            predicted = await self._predict(filtered, frame.text_type)
            if predicted is None:
                frame.text = filtered
                return False
            found = await self._cache.probe(predicted, tags=self._tags, source="speak")
            if found.hit is not None:
                played = await self._play(
                    found,
                    filtered,
                    append_to_context=frame.append_to_context,
                    interruptible=frame.interruptible,
                    push_aggregation=frame.append_to_context and not self._llm_response_started,
                )
                if played:
                    return True
            # The service runs no filters of its own, so it must receive the filtered text.
            frame.text = filtered
            if found.store and found.key and self._mode == "exact":
                bridge_for(self._tts).speak.append(PendingSpeak(filtered, found.key, predicted))
            return False
        except Exception as e:
            self._fail_open("read.speak", e)
            return False
