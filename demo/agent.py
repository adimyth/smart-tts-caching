"""The launch demo: a Pipecat voice agent with the cache, reached from a browser over WebRTC.

One process serves the caller page and runs one pipeline per call. A call is one WebRTC
connection from the page, which plays the caller's pre-synthesised lines into it (or your
microphone). Every cache event, every sentence's time to first audio and every provider request
is streamed back to the page as server-sent events, so the recording shows the call and the
cache side by side.

    .venv/bin/python demo/agent.py --mode scripted           # the agent says the script's turns
    .venv/bin/python demo/agent.py --mode hybrid             # a real LLM with the pinned lines
    .venv/bin/python demo/agent.py --mode pure               # a real LLM with the persona only

Then open http://localhost:7860, pick a call, and press Connect. Each call builds its own cache
instance over the shared Redis, as one container per session would, so the second call of a
language starts from a warm start and the Level B and C indexes it builds there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    AggregatedTextFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from script import (  # noqa: E402
    AGENT,
    AGENT_VOICE,
    CALLS,
    CALLS_BY_ID,
    LANGUAGE,
    TTS_LANGUAGE_CODE,
    Call,
    hybrid_prompt,
    opening_instruction,
    persona_prompt,
)

from tts_cache import LEVEL_C_POLICY, MemoryStore, RedisStore, TieredStore, TTSCache  # noqa: E402
from tts_cache.cache import default_matcher  # noqa: E402
from tts_cache.metrics import LogSink  # noqa: E402
from tts_cache.policy import DEFAULT_POLICY, AllOf, SkipIfMatches  # noqa: E402
from tts_cache_pipecat import (  # noqa: E402
    SarvamTTSService,
    TTSCacheRead,
    TTSCacheWrite,
    scope_from_service,
)

OUT_RATE = 24_000
TRACE = os.environ.get("DEMO_TRACE") == "1"
IN_RATE = 16_000
SKIP_PATTERNS = [r"\d{4,}"]  # patient IDs, phone numbers and the like are never stored


# ---------------------------------------------------------------------------
# Events to the page.
# ---------------------------------------------------------------------------


class Bus:
    """Fan-out of JSON events to every open server-sent-events stream."""

    def __init__(self) -> None:
        self.queues: set[asyncio.Queue[dict[str, Any]]] = set()

    def emit(self, type_: str, **fields: Any) -> None:
        rec = {"t": round(time.time(), 3), "type": type_, **fields}
        if type_ != "frame":
            logger.debug("demo event " + json.dumps(rec, ensure_ascii=False, default=str)[:300])
        for q in list(self.queues):
            q.put_nowait(rec)

    async def stream(self) -> AsyncIterator[str]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.queues.add(q)
        try:
            yield "data: " + json.dumps({"type": "hello"}) + "\n\n"
            while True:
                try:
                    rec = await asyncio.wait_for(q.get(), 15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: " + json.dumps(rec, ensure_ascii=False, default=str) + "\n\n"
        finally:
            self.queues.discard(q)


def _plain(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, type(None)))


class BusSink(LogSink):
    """The cache's metrics: counted, logged, and forwarded to the page."""

    SHOWN = {"hit", "miss", "store", "reject", "fail_open", "deadline", "skip", "shadow_hit", "prefetch"}

    def __init__(self, bus: Bus, call_id: str) -> None:
        super().__init__(log=None)
        self.bus = bus
        self.call_id = call_id

    def event(self, name: str, **fields: Any) -> None:
        super().event(name, **fields)
        if name in self.SHOWN:
            self.bus.emit(
                "cache", call=self.call_id, event=name, **{k: v for k, v in fields.items() if _plain(v)}
            )


# ---------------------------------------------------------------------------
# Processors.
# ---------------------------------------------------------------------------


class Tap(FrameProcessor):
    """After the STT: what the caller said, to the page."""

    def __init__(self, bus: Bus, call_id: str) -> None:
        super().__init__(name="tap")
        self.bus = bus
        self.call_id = call_id

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            self.bus.emit("user", call=self.call_id, text=frame.text)
        await self.push_frame(frame, direction)


@dataclass
class _Sentence:
    text: str = ""
    t_ready: float = 0.0
    t_first_audio: float = 0.0
    hit: bool = False
    level: str | None = None
    diff_class: str | None = None
    reported: bool = False
    first_in_turn: bool = False
    key: str | None = None
    spoken: str | None = None  # the stored sentence, when it differs from the requested one


@dataclass
class TurnState:
    """What the read side and the output side know about the current agent turn, shared by the two taps."""

    bus: Bus
    call_id: str
    # When the read side released each sentence: hits by their context id, misses by their text,
    # since the TTS service gives a miss a context id of its own.
    ready: dict[str, float] = field(default_factory=dict)
    response_open: bool = False
    released: int = 0  # sentences released in this turn
    stopped: int = 0  # sentence contexts closed in this turn
    speaking: bool = False
    turn: int = 0
    done_turn: int = -1
    first_text: str | None = None  # the turn's first sentence, when it is a miss
    first_cid: str | None = None  # the turn's first sentence, when it is a hit

    _pending: Any = None

    def maybe_done(self) -> None:
        """Emit turn_done once per turn: response closed, every sentence closed, playback stopped.

        The transport reports a stop at each sentence's TTSStoppedFrame and starts again at once
        while the rest of that sentence plays out, so a stop only counts when no start follows it
        within a moment.
        """
        if self.done_turn == self.turn or self.response_open or self.speaking or not self.released:
            return
        if self.stopped >= self.released and self._pending is None:
            self._pending = asyncio.create_task(self._confirm_done(self.turn))

    async def _confirm_done(self, turn: int) -> None:
        try:
            await asyncio.sleep(0.4)
            if not self.speaking and self.turn == turn and self.done_turn != turn:
                self.done_turn = turn
                self.bus.emit("turn_done", call=self.call_id)
        finally:
            self._pending = None


class ReadyTap(FrameProcessor):
    """Right after TTSCacheRead: when the LLM's turn opens and closes, and when each sentence is released.

    The TTS service holds the LLM's end-of-response frame until its own contexts have closed,
    and a turn served entirely from the cache gives it none, so the turn's shape is read here,
    before the service, not after it.
    """

    def __init__(self, state: TurnState) -> None:
        super().__init__(name="ready-tap")
        self.state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        st = self.state
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                st.turn += 1
                st.response_open = True
                st.released = 0
                st.stopped = 0
            elif isinstance(frame, LLMFullResponseEndFrame):
                st.response_open = False
                st.maybe_done()
            elif isinstance(frame, AggregatedTextFrame) and not isinstance(frame, TTSTextFrame):
                st.ready[frame.text] = time.perf_counter()
                st.released += 1
                if st.released == 1:
                    st.first_text = frame.text
            elif isinstance(frame, TTSStartedFrame) and frame.context_id:
                st.ready[frame.context_id] = time.perf_counter()
                st.released += 1
                if st.released == 1:
                    st.first_cid = frame.context_id
        await self.push_frame(frame, direction)


class Probe(FrameProcessor):
    """Before the output transport: each sentence's time from ready to first audio, and when a turn is over.

    The transport alone cannot say when a turn is over: it reports a stop after any 350 ms gap,
    including the gap before a slow sentence's audio arrives. So the end of a turn is the
    transport's stop once the LLM has closed the turn and every sentence's context has closed.
    """

    def __init__(self, state: TurnState) -> None:
        super().__init__(name="probe")
        self.state = state
        self.bus = state.bus
        self.call_id = state.call_id
        self.sentences: dict[str, _Sentence] = {}

    def _s(self, cid: str) -> _Sentence:
        return self.sentences.setdefault(cid, _Sentence())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        t = time.perf_counter()
        st = self.state
        if TRACE:
            self.bus.emit(
                "frame",
                call=self.call_id,
                name=type(frame).__name__,
                cid=getattr(frame, "context_id", None),
                dir=direction.name[0],
            )
        if isinstance(frame, BotStartedSpeakingFrame):
            st.speaking = True
            self.bus.emit("bot_started", call=self.call_id)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            st.speaking = False
            self.bus.emit("bot_stopped", call=self.call_id)
            st.maybe_done()
        cid = getattr(frame, "context_id", None)
        if direction == FrameDirection.DOWNSTREAM and cid:
            meta = (getattr(frame, "metadata", None) or {}).get("tts_cache") or {}
            if isinstance(frame, AggregatedTextFrame) and not isinstance(frame, TTSTextFrame):
                s = self._s(cid)
                s.text = frame.text
                s.t_ready = st.ready.get(frame.text, s.t_ready or t)
                s.first_in_turn = frame.text == st.first_text
            elif isinstance(frame, TTSTextFrame):
                s = self._s(cid)
                if not s.text:
                    s.text = frame.text
                    if s.reported:
                        self.bus.emit("sentence_text", call=self.call_id, cid=cid, text=s.text)
            elif isinstance(frame, TTSStartedFrame):
                s = self._s(cid)
                s.t_ready = st.ready.get(cid, s.t_ready or t)
                s.first_in_turn = s.first_in_turn or cid == st.first_cid
                if meta.get("hit"):
                    s.hit = True
                    s.level = meta.get("level")
                    s.diff_class = meta.get("diff_class")
                    s.key = meta.get("key")
            elif isinstance(frame, TTSAudioRawFrame) and frame.audio:
                s = self._s(cid)
                if not s.t_first_audio:
                    s.t_first_audio = t
                    self._report(cid, s)
            elif isinstance(frame, TTSStoppedFrame):
                s = self._s(cid)
                st.stopped += 1
                if meta.get("hit"):
                    s.hit = True
                    s.level = s.level or meta.get("level")
                    s.diff_class = s.diff_class or meta.get("diff_class")
                    # What the LLM asked to say; on a Level B or C hit the stored clip's text differs.
                    requested = meta.get("requested_text") or meta.get("spoken_text") or ""
                    spoken = meta.get("spoken_text") or ""
                    if spoken and spoken != requested:
                        s.spoken = spoken
                    if requested and requested != s.text:
                        s.text = requested
                        if s.reported:
                            self.bus.emit(
                                "sentence_text", call=self.call_id, cid=cid, text=s.text, spoken=s.spoken
                            )
                if not s.reported:
                    self._report(cid, s)
                st.maybe_done()
        await self.push_frame(frame, direction)

    def _report(self, cid: str, s: _Sentence) -> None:
        s.reported = True
        ms = round((s.t_first_audio - s.t_ready) * 1000, 1) if s.t_first_audio and s.t_ready else None
        self.bus.emit(
            "sentence",
            call=self.call_id,
            cid=cid,
            text=s.text,
            hit=s.hit,
            level=s.level,
            diff_class=s.diff_class,
            first_audio_ms=ms,
            first_in_turn=s.first_in_turn,
            key=s.key,
            spoken=s.spoken,
        )


_NUMBER_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "शून्य": "0",
    "एक": "1",
    "दो": "2",
    "तीन": "3",
    "चार": "4",
    "पाँच": "5",
    "पांच": "5",
    "छह": "6",
    "छः": "6",
    "सात": "7",
    "आठ": "8",
    "नौ": "9",
}


def _tokens(text: str) -> list[str]:
    """Words, lower-cased and stripped of punctuation, with number words and digit runs as single digits."""
    out: list[str] = []
    for w in text.lower().replace("'", "").split():
        w = w.strip('.,?!;:।"()')
        if not w:
            continue
        if w in _NUMBER_WORDS:
            out.append(_NUMBER_WORDS[w])
        elif any(ch.isdigit() for ch in w):
            out.extend(ch for ch in w if ch.isdigit())
        else:
            out.append(w)
    return out


def coverage(expected: str, heard: str) -> float:
    """The share of the expected line's tokens that were heard (a multiset overlap)."""
    exp = _tokens(expected)
    if not exp:
        return 1.0
    got = _tokens(heard)
    hit = 0
    for tok in exp:
        if tok in got:
            got.remove(tok)
            hit += 1
    return hit / len(exp)


class ScriptedLLM(FrameProcessor):
    """A stand-in for the LLM: answers each caller line with the next scripted turn, streamed as tokens.

    Smart Turn ends a caller's turn at any pause that sounds final, so one line may arrive as
    several user turns, and the tail of a line may arrive after the agent has begun to answer
    it. The turn is answered once what was heard covers most of the line the caller was going
    to say; a fragment that belongs to the previous line is ignored; and a line that never
    reaches the mark is answered after the caller has been silent for a while, as a person
    would. The timer runs only while the caller is silent.
    """

    COVERAGE = 0.75
    SILENCE_S = 2.0  # answer a partly heard line after this much silence
    SILENCE_UNHEARD_S = 5.0  # and a line with almost nothing heard after this much

    def __init__(self, call: Call, bus: Bus) -> None:
        super().__init__(name="scripted-llm")
        self.call = call
        self.turns = call.agent_turns
        self.i = 0
        self.bus = bus
        self.waiting = False
        self.last_c = 0.0
        self.user_speaking = False
        self._later: asyncio.Task[None] | None = None

    @staticmethod
    def _text(m: Any) -> tuple[str | None, str]:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        return role, content if isinstance(content, str) else ""

    def _heard(self, frame: LLMContextFrame) -> tuple[str, str]:
        """The user's words since the agent last spoke, and the latest of them."""
        words: list[str] = []
        for m in reversed(frame.context.messages):
            role, content = self._text(m)
            if role == "assistant":
                break
            if role == "user" and content:
                words.insert(0, content)
        return " ".join(words), words[-1] if words else ""

    def _cancel(self) -> None:
        if self._later is not None:
            self._later.cancel()
            self._later = None

    def _arm(self) -> None:
        self._cancel()
        if self.waiting and not self.user_speaking:
            delay = self.SILENCE_S if self.last_c >= 0.3 else self.SILENCE_UNHEARD_S
            self._later = asyncio.create_task(self._answer_later(delay))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
            self._cancel()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
            self._arm()
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            if getattr(frame, "speculation", False):
                return
            if self.i >= len(self.turns):
                return
            if self.i > 0:  # turn 0 is the greeting, before the caller has said anything
                expected = self.call.caller_lines[self.i - 1]
                heard, latest = self._heard(frame)
                c = coverage(expected, heard)
                previous = self.call.caller_lines[self.i - 2] if self.i >= 2 else ""
                if c <= self.last_c and previous and coverage(latest, previous) >= 0.5:
                    # The tail of the line already answered, not the start of the next one.
                    self.bus.emit("ignored", call=self.call.id, heard=latest)
                    return
                self.last_c = c
                if c < self.COVERAGE:
                    self.waiting = True
                    self.bus.emit("waiting", call=self.call.id, heard=heard, coverage=round(c, 2))
                    self._arm()
                    return
            await self._answer()
            return
        await self.push_frame(frame, direction)

    async def _answer_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._later = None
        self.bus.emit("answering_after_silence", call=self.call.id, coverage=round(self.last_c, 2))
        await self._answer()

    async def _answer(self) -> None:
        self._cancel()
        self.waiting = False
        self.last_c = 0.0
        turn = self.turns[self.i]
        self.i += 1
        await self.push_frame(LLMFullResponseStartFrame())
        for sentence in turn.sentences:
            for tok in sentence.split(" "):
                await self.push_frame(LLMTextFrame(tok + " "))
                await asyncio.sleep(0.02)  # an LLM streams tokens; the harness uses the same gap
        await self.push_frame(LLMFullResponseEndFrame())


# ---------------------------------------------------------------------------
# Services.
# ---------------------------------------------------------------------------


def make_tts(language: str) -> SarvamTTSService:
    # One context id per sentence, and no text_filters on the service: TTSCacheRead applies them.
    return SarvamTTSService(
        api_key=os.environ["SARVAM_API_KEY"],
        settings=SarvamTTSService.Settings(
            model="bulbul:v4-flash", voice=AGENT_VOICE, language=TTS_LANGUAGE_CODE
        ),
        reuse_context_id_within_turn=False,
    )


def make_stt(language: str) -> SarvamSTTService:
    # Code-mix mode: the caller speaks Hinglish, and the transcript keeps both scripts.
    return SarvamSTTService(
        api_key=os.environ["SARVAM_API_KEY"],
        sample_rate=IN_RATE,
        mode="codemix",
        settings=SarvamSTTService.Settings(model="saaras:v4"),
    )


@dataclass
class Config:
    mode: str
    llm: str
    redis_url: str
    level_c: bool
    matcher: Any
    bus: Bus
    system_prompt: str | None = None  # replaces the persona in the LLM modes


def policy_for(cfg: Config):  # type: ignore[no-untyped-def]
    base = LEVEL_C_POLICY if cfg.level_c else DEFAULT_POLICY
    return AllOf(base, SkipIfMatches(SKIP_PATTERNS))


# ---------------------------------------------------------------------------
# One call.
# ---------------------------------------------------------------------------


async def run_call(connection: SmallWebRTCConnection, call: Call, cfg: Config) -> None:
    bus = cfg.bus
    lang = call.language
    bus.emit("call_started", call=call.id, language=lang, caller=call.caller, mode=cfg.mode)
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=IN_RATE,
            audio_out_sample_rate=OUT_RATE,
        ),
    )
    tts = make_tts(lang)
    stt = make_stt(lang)

    cache = TTSCache(
        scope=scope_from_service(tts, language=f"{lang}-IN", sample_rate=OUT_RATE),
        store=TieredStore(MemoryStore(warm_start_bytes=4 << 20), RedisStore(cfg.redis_url), deadline_ms=20),
        policy=policy_for(cfg),
        matcher=cfg.matcher,
        metrics=BusSink(bus, call.id),
    )
    t0 = time.perf_counter()
    await cache.start()
    bus.emit("warm_start", call=call.id, ms=round((time.perf_counter() - t0) * 1000, 1))

    @tts.event_handler("on_tts_request")
    async def on_tts_request(_tts, _context_id: str, text: str) -> None:  # type: ignore[no-untyped-def]
        bus.emit("provider_request", call=call.id, text=text, chars=len(text))

    state = TurnState(bus, call.id)
    context = LLMContext()
    if cfg.mode == "scripted":
        llm: FrameProcessor = ScriptedLLM(call, bus)
    else:
        prompt = cfg.system_prompt or (hybrid_prompt(lang) if cfg.mode == "hybrid" else persona_prompt(lang))
        llm = OpenAILLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            settings=OpenAILLMService.Settings(model=cfg.llm, system_instruction=prompt),
        )
    barge_in = cfg.mode != "scripted"
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
            # Smart Turn v3 decides when the caller is done, so a pause mid-sentence does not end the turn
            # and a finished sentence ends it at once.
            # The scripted agent answers a caller line once it has heard all of it; a fragment that
            # arrives while it is speaking must not cut the turn. The LLM modes keep barge-in.
            user_turn_strategies=UserTurnStrategies(
                start=[
                    VADUserTurnStartStrategy(enable_interruptions=barge_in),
                    TranscriptionUserTurnStartStrategy(enable_interruptions=barge_in),
                ],
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
            ),
        ),
    )
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            Tap(bus, call.id),
            user_agg,
            llm,
            TTSCacheRead(cache, tts=tts, text_filters=[MarkdownTextFilter()]),
            ReadyTap(state),
            tts,
            TTSCacheWrite(cache, tts=tts),
            Probe(state),
            transport.output(),
            assistant_agg,
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(audio_out_sample_rate=OUT_RATE, enable_metrics=True),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client) -> None:  # type: ignore[no-untyped-def]
        bus.emit("connected", call=call.id)
        if cfg.mode != "scripted":
            opening = (
                "The caller has just connected. Greet them briefly."
                if cfg.system_prompt
                else opening_instruction(lang)
            )
            context.add_message({"role": "developer", "content": opening})
        await asyncio.sleep(1.0)  # let the TTS websocket settle before the first context
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client) -> None:  # type: ignore[no-untyped-def]
        bus.emit("disconnected", call=call.id)
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        await cache.close()
        bus.emit("call_ended", call=call.id)


# ---------------------------------------------------------------------------
# The web app.
# ---------------------------------------------------------------------------


def build_app(cfg: Config) -> FastAPI:
    handler = SmallWebRTCRequestHandler()
    tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await handler.close()

    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "mode": cfg.mode,
            "llm": cfg.llm if cfg.mode != "scripted" else None,
            "level_c": cfg.level_c,
            "language": LANGUAGE,
            "agent": AGENT,
            "custom_prompt": cfg.system_prompt is not None,
            "calls": [
                {
                    "id": c.id,
                    "language": c.language,
                    "caller": c.caller,
                    "fills": c.fills,
                    "lines": list(c.caller_lines),
                    "expect": {
                        s: {"outcome": e.outcome, "level": e.level, "short": e.short, "why": e.why}
                        for t in c.agent_turns
                        for s, e in zip(t.sentences, t.expect, strict=True)
                        if e is not None
                    },
                }
                for c in CALLS
            ],
        }

    @app.post("/api/offer")
    async def offer(request: Request) -> dict[str, str] | None:
        body = await request.json()
        req = SmallWebRTCRequest.from_dict(body)
        call_id = (req.request_data or {}).get("call", "en-1")
        call = CALLS_BY_ID[call_id]

        async def on_connection(connection: SmallWebRTCConnection) -> None:
            t = asyncio.create_task(run_call(connection, call, cfg))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

        return await handler.handle_web_request(request=req, webrtc_connection_callback=on_connection)

    @app.patch("/api/offer")
    async def ice(request: Request) -> dict[str, str]:
        body = await request.json()
        await handler.handle_patch_request(SmallWebRTCPatchRequest(**body))
        return {"ok": "true"}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(cfg.bus.stream(), media_type="text/event-stream")

    @app.post("/api/redis/pause")
    async def redis_pause(request: Request) -> dict[str, Any]:
        body = await request.json() if int(request.headers.get("content-length", "0") or 0) else {}
        ms = int(body.get("ms", 5000))
        r = aioredis.from_url(cfg.redis_url)
        try:
            await r.execute_command("CLIENT", "PAUSE", ms, "ALL")
        finally:
            await r.aclose()
        cfg.bus.emit("redis_paused", ms=ms)
        return {"paused_ms": ms}

    @app.post("/api/redis/flush")
    async def redis_flush() -> dict[str, Any]:
        r = aioredis.from_url(cfg.redis_url)
        try:
            await r.flushdb()
        finally:
            await r.aclose()
        cfg.bus.emit("redis_flushed")
        return {"flushed": True}

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("scripted", "hybrid", "pure"), default="scripted")
    ap.add_argument("--llm", default="gpt-5.6-luna")
    ap.add_argument("--redis", default="redis://localhost:6380/9")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--no-level-c", action="store_true", help="Levels A and B only (no judge model to load)")
    ap.add_argument("--flush", action="store_true", help="empty the Redis database at start")
    ap.add_argument("--system-prompt", type=Path, help="a file with your own system prompt (LLM modes)")
    a = ap.parse_args()
    load_dotenv(HERE.parent / ".env")
    system_prompt = a.system_prompt.read_text(encoding="utf-8") if a.system_prompt else None
    if system_prompt and a.mode == "scripted":
        a.mode = "pure"

    level_c = not a.no_level_c
    logger.info(
        "loading the matcher" + (" with the Level C judge (first run downloads the model)" if level_c else "")
    )
    matcher = default_matcher(level_c=level_c, judge=level_c)
    cfg = Config(
        mode=a.mode,
        llm=a.llm,
        redis_url=a.redis,
        level_c=level_c,
        matcher=matcher,
        bus=Bus(),
        system_prompt=system_prompt,
    )
    if a.flush:
        import redis

        redis.from_url(a.redis).flushdb()
        logger.info(f"flushed {a.redis}")

    import uvicorn

    logger.info(
        f"mode={a.mode} lang={LANGUAGE} llm={a.llm if a.mode != 'scripted' else '-'} level_c={level_c} redis={a.redis}"
    )
    uvicorn.run(build_app(cfg), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
