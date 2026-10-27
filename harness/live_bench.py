"""The live runs of TEST_PLAN.md section 11, the preflight of 2.3, and the rehearsal's fake mode (2.2).

It generalises ``cache_roundtrip.py`` from one turn to a corpus of sessions.
Each session is a fresh pipeline ``[TTSCacheRead, service, TTSCacheWrite,
Probe]`` with a fresh cache that warm-starts from its run's own Redis
database; an OFF run's pipeline is ``[SplitOnly, service, Probe]``, which
splits with the cache's own splitter so OFF and ON send identical sentences.
Each turn streams word by word, 20 ms apart. REST services close each
sentence's audio context when the response ends (11.2), ON runs go session by
session and OFF runs send their sessions in parallel, all under one adjustable
concurrency limit per provider.

Subcommands:

    plan       the runs and the budget from the frozen corpora (no provider call)
    preflight  the seven items of 2.3, written to preflight.json (live unless --fake)
    run        every run once, with retries, checkpoint and resume, and full capture (2.4)
    netcheck   TCP connect time to each provider host, 20 times (2.3, item 7)

    python harness/live_bench.py run --out harness/out/testplan/<id>/live --fake --faults
    python harness/live_bench.py run --out harness/out/testplan/<id>/live --max-chars N
    python harness/live_bench.py run --out ... --resume
"""

import argparse
import asyncio
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    AggregatedTextFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.utils.text.base_text_aggregator import TextType  # noqa: E402

from tts_cache import MemoryStore, SQLiteStore, TieredStore, TTSCache, codec  # noqa: E402
from tts_cache.bench import config_policy  # noqa: E402
from tts_cache.corpus import LabelledCorpus, LabelledItem  # noqa: E402
from tts_cache.metrics import LogSink  # noqa: E402
from tts_cache.split import SentenceSplitter  # noqa: E402
from tts_cache.stores.redis import RedisStore  # noqa: E402
from tts_cache.testplan.cells import (  # noqa: E402
    ELEVENLABS_CODE,
    PROVIDER,
    REST,
    SARVAM_CODE,
    SEED,
    LiveRun,
    cells,
    core_plan_runs,
    live_runs,
    usd,
)
from tts_cache.testplan.offline import corpus_for  # noqa: E402
from tts_cache_pipecat import TTSCacheRead, TTSCacheWrite, capture_mode_for, scope_from_service  # noqa: E402
from tts_cache_pipecat.compat import SERVICES, register_service  # noqa: E402

SR = 24_000
TOKEN_GAP_S = 0.02
STALL_S = 15.0
REST_STOP_TIMEOUT_S = STALL_S + 5.0
SESSION_RETRIES = 2
ADMIN_RETRIES = 5
ADMIN_TIMEOUT_S = 30.0
RETRIES = 2
BACKOFF_S = (1.0, 4.0)
BACKOFF_429_S = (5.0, 15.0)
CONNECT_WAIT_S = 0.6
LIVE_REDIS_PORT = 6600
LIVE_REDIS_DBS = 256
DEFAULT_CONCURRENCY = 8
HOSTS = {"openai": "api.openai.com", "elevenlabs": "api.elevenlabs.io", "sarvam": "api.sarvam.ai"}
PIPECAT_SHA = "1d1385b6d"
# A Pipecat context id inside an error message ("TTS context <uuid> completed with no audio").
_CONTEXT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def now() -> float:
    return time.monotonic()


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Services. REST classes close each sentence's audio context when its response
# ends (11.2), as Pipecat's own Sarvam websocket service does on its "final"
# event; OFF and ON runs use the same classes.
# ---------------------------------------------------------------------------


class RestClose:
    """Mixin: append a stop frame and remove the audio context as soon as the response ends."""

    _rest_ctx: str | None = None

    async def run_tts(self, text: str, context_id: str):  # type: ignore[no-untyped-def]
        self._rest_ctx = context_id
        async for frame in super().run_tts(text, context_id):  # type: ignore[misc]
            yield frame

    async def _push_tts_frames(self, src_frame: Any, *args: Any, **kwargs: Any) -> None:
        self._rest_ctx = None
        await super()._push_tts_frames(src_frame, *args, **kwargs)  # type: ignore[misc]
        cid = self._rest_ctx
        if cid and self.audio_context_available(cid):  # type: ignore[attr-defined]
            await self.append_to_audio_context(cid, TTSStoppedFrame(context_id=cid))  # type: ignore[attr-defined]
            await self.remove_audio_context(cid)  # type: ignore[attr-defined]


_REST_CLASSES: dict[type, type] = {}


def rest_close(cls: type) -> type:
    """The REST-close subclass of ``cls``, registered with the adapter like its parent."""
    if cls not in _REST_CLASSES:
        sub = type(f"RestClose{cls.__name__}", (RestClose, cls), {})
        spec = SERVICES.get(cls)
        register_service(sub, spec.service if spec else "fake", spec.settings if spec else (), capture_mode="exact")
        _REST_CLASSES[cls] = sub
    return _REST_CLASSES[cls]


@dataclass
class RequestLog:
    """Provider request ids and headers: aiohttp traces, httpx hooks, or the fake's own headers."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    # Written through as each request happens, so a harness crash loses none of them (2.4).
    path: Path | None = None

    def add(self, source: str, status: int | None, url: str, headers: Any) -> None:
        keep = {k: v for k, v in dict(headers or {}).items() if k.lower() not in ("set-cookie", "authorization")}
        rec = {"t": now(), "wall": time.time(), "source": source, "status": status, "url": url, "headers": keep}
        self.entries.append(rec)
        self.write(rec)

    def write(self, rec: dict[str, Any]) -> None:
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def aiohttp_trace(self) -> aiohttp.TraceConfig:
        tc = aiohttp.TraceConfig()

        async def on_end(_session: Any, _ctx: Any, params: Any) -> None:
            self.add("aiohttp", params.response.status, str(params.url), params.response.headers)

        tc.on_request_end.append(on_end)
        return tc

    def httpx_client(self) -> Any:
        import httpx

        async def hook(response: Any) -> None:
            self.add("httpx", response.status_code, str(response.request.url), response.headers)

        return httpx.AsyncClient(event_hooks={"response": [hook]}, timeout=httpx.Timeout(60.0, connect=10.0))


def sarvam_voice() -> str:
    return os.environ.get("SARVAM_TTS_SPEAKER") or os.environ.get("SARVAM_TTS_SPEKAER", "shubh")


def sarvam_model() -> str:
    return os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")


def build_service(
    name: str,
    lang: str,
    session: aiohttp.ClientSession,
    reqlog: RequestLog,
    *,
    fake: bool = False,
    run_id: str = "",
    faults: Any = None,
    seed: int = 1,
) -> Any:
    """The service of a cell, with one context per sentence, REST classes closing on response end."""
    common: dict[str, Any] = dict(sample_rate=SR, reuse_context_id_within_turn=False)
    if fake or name in REST:
        # REST classes close on response end. Pipecat's 3 s default would end a context
        # before a slow first byte (OpenAI p50 is 1.8 s), and the stall check must fire first.
        common["stop_frame_timeout_s"] = REST_STOP_TIMEOUT_S
    if fake:
        from fake_provider import FakeTTSService

        # A websocket service pauses after each turn until the bot stops speaking (Pipecat's
        # pause_frame_processing); the fake of a websocket cell does the same.
        return rest_close(FakeTTSService)(
            cell=name, lang=lang, run=run_id, faults=faults, seed=seed, pause_frame_processing=name not in REST, **common
        )
    el_key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_TTS_KEY", "")
    el_voice = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    if name == "openai_http":
        from pipecat.services.openai.tts import OpenAITTSService

        cls = rest_close(OpenAITTSService)
        return cls(
            api_key=os.environ["OPENAI_API_KEY"],
            http_client=reqlog.httpx_client(),
            settings=OpenAITTSService.Settings(model="gpt-4o-mini-tts", voice="alloy"),
            **common,
        )
    if name == "elevenlabs_http":
        from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService

        cls = rest_close(ElevenLabsHttpTTSService)
        return cls(
            api_key=el_key,
            aiohttp_session=session,
            settings=ElevenLabsHttpTTSService.Settings(
                model="eleven_flash_v2_5", voice=el_voice, language=ELEVENLABS_CODE[lang]
            ),
            **common,
        )
    if name == "elevenlabs_ws_fc":
        from tts_cache_pipecat.services import ElevenLabsTTSService

        return ElevenLabsTTSService(
            api_key=el_key,
            settings=ElevenLabsTTSService.Settings(
                model="eleven_flash_v2_5", voice=el_voice, language=ELEVENLABS_CODE[lang]
            ),
            **common,
        )
    if name == "elevenlabs_dialogue_ws_c":
        from tts_cache_pipecat.services import ElevenLabsDialogueTTSService

        return ElevenLabsDialogueTTSService(
            api_key=el_key,
            settings=ElevenLabsDialogueTTSService.Settings(
                model=os.environ.get("ELEVENLABS_DIALOGUE_MODEL", "eleven_v4_turbo"), voice=el_voice
            ),
            **common,
        )
    if name == "sarvam_http":
        from pipecat.services.sarvam.tts import SarvamHttpTTSService

        cls = rest_close(SarvamHttpTTSService)
        return cls(
            api_key=os.environ["SARVAM_API_KEY"],
            aiohttp_session=session,
            settings=SarvamHttpTTSService.Settings(model=sarvam_model(), voice=sarvam_voice(), language=SARVAM_CODE[lang]),
            **common,
        )
    if name == "sarvam_ws_flush":
        from tts_cache_pipecat.services import SarvamTTSService

        return SarvamTTSService(
            api_key=os.environ["SARVAM_API_KEY"],
            settings=SarvamTTSService.Settings(model=sarvam_model(), voice=sarvam_voice(), language=SARVAM_CODE[lang]),
            **common,
        )
    raise SystemExit(f"unknown service {name}")


# ---------------------------------------------------------------------------
# Pipeline processors.
# ---------------------------------------------------------------------------


class SplitOnly(FrameProcessor):
    """The OFF run's front: the cache's own splitter and nothing else, so OFF sends the sentences ON sends."""

    def __init__(self, language: str) -> None:
        super().__init__(name="split-only")
        self._splitter = SentenceSplitter(language)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TextFrame) and not isinstance(
            frame, AggregatedTextFrame
        ):
            for s in self._splitter.feed(frame.text):
                await self.push_frame(AggregatedTextFrame(s, TextType.SENTENCE, raw_text=s))
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            rest = self._splitter.flush()
            if rest and rest.strip():
                await self.push_frame(AggregatedTextFrame(rest, TextType.SENTENCE, raw_text=rest))
        if isinstance(frame, InterruptionFrame):
            self._splitter.reset()
        await self.push_frame(frame, direction)


@dataclass
class Ctx:
    context_id: str
    seq: int
    t_started: float | None = None
    t_first_audio: float | None = None
    t_last_audio: float | None = None
    t_stopped: float | None = None
    hit: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    text: str | None = None
    audio: bytearray = field(default_factory=bytearray)
    sample_rate: int = SR
    texts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Probe(FrameProcessor):
    """The last processor: every TTS frame per context, and a log of every frame.

    It also stands in for the output transport's speaking signals. Websocket services
    pause after each turn until BotStoppedSpeakingFrame comes upstream; the probe sends
    BotStartedSpeakingFrame at a turn's first audio and the session sends
    BotStoppedSpeakingFrame once the turn's contexts have all stopped (playback is instant).
    """

    def __init__(self, frame_log: list[list[Any]]) -> None:
        super().__init__(name="probe")
        self.ctxs: dict[str, Ctx] = {}
        self.order: list[str] = []
        self.frame_log = frame_log
        self.changed = asyncio.Event()
        self.speaking = False

    async def bot_stopped(self) -> None:
        if self.speaking:
            self.speaking = False
            await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    def ctx(self, cid: str) -> Ctx:
        if cid not in self.ctxs:
            self.ctxs[cid] = Ctx(cid, len(self.order))
            self.order.append(cid)
        return self.ctxs[cid]

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        t = now()
        cid = getattr(frame, "context_id", None)
        size = len(frame.audio) if isinstance(frame, TTSAudioRawFrame) else None
        self.frame_log.append([round(t, 6), type(frame).__name__, cid, size, direction.name[0]])
        if direction == FrameDirection.DOWNSTREAM and cid:
            meta = (getattr(frame, "metadata", None) or {}).get("tts_cache") or {}
            if isinstance(frame, AggregatedTextFrame) and not isinstance(frame, TTSTextFrame):
                c = self.ctx(cid)
                c.text = frame.text
                if meta:
                    c.meta.update({k: v for k, v in meta.items() if k in ("key", "predicted", "store")})
            elif isinstance(frame, TTSStartedFrame):
                c = self.ctx(cid)
                c.t_started = c.t_started or t
                if meta.get("hit"):
                    c.hit = True
                    c.meta.update(meta)
            elif isinstance(frame, TTSAudioRawFrame):
                c = self.ctx(cid)
                if frame.audio and not self.speaking:
                    self.speaking = True
                    await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
                if frame.audio:
                    c.t_first_audio = c.t_first_audio or t
                    c.t_last_audio = t
                    c.audio.extend(frame.audio)
                    c.sample_rate = frame.sample_rate
            elif isinstance(frame, TTSTextFrame):
                self.ctx(cid).texts.append(frame.text)
            elif isinstance(frame, TTSStoppedFrame):
                c = self.ctx(cid)
                c.t_stopped = c.t_stopped or t
                if meta.get("hit"):
                    c.hit = True
                    c.meta.update(meta)
                self.changed.set()
        await self.push_frame(frame, direction)


class RecordingSink(LogSink):
    """The cache's metrics: counted, and every event kept in order for the run's files."""

    def __init__(self) -> None:
        super().__init__(log=None)
        self.events: list[dict[str, Any]] = []

    def event(self, name: str, **fields: Any) -> None:
        super().event(name, **fields)
        self.events.append({"t": now(), "event": name, **{k: v for k, v in fields.items() if _plain(v)}})


def _plain(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), dict, list))


# ---------------------------------------------------------------------------
# Concurrency, Redis and the fault hooks.
# ---------------------------------------------------------------------------


class Limiter:
    """A FIFO semaphore whose capacity can be lowered while running (429 backoff, 2.4)."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.active = 0
        self._cond = asyncio.Condition()
        self.lowered: list[dict[str, Any]] = []

    async def __aenter__(self) -> "Limiter":
        async with self._cond:
            await self._cond.wait_for(lambda: self.active < self.capacity)
            self.active += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    def lower(self, reason: str) -> None:
        if self.capacity > 1:
            self.capacity -= 1
        self.lowered.append({"wall": time.time(), "capacity": self.capacity, "reason": reason})


class FairSlots:
    """Concurrency slots for one cell's runs (11.2: all runs of a cell go at the same time).

    A free slot goes to the waiting run that holds the fewest, earliest arrival first,
    so every ON run of the cell gets its one slot at once and the OFF runs' parallel
    sessions fill the rest, taking over the slots ON runs give up when they finish.
    The capacity is the provider's limit, read live, so a 429 lowers it here too.
    """

    def __init__(self, provider: Limiter) -> None:
        self.provider = provider
        self.active: dict[str, int] = defaultdict(int)
        self.total = 0
        self.waiting: dict[str, deque[asyncio.Future[None]]] = defaultdict(deque)
        self.order: list[str] = []

    def _dispatch(self) -> None:
        while self.total < self.provider.capacity:
            ready = [rid for rid in self.order if self.waiting[rid]]
            if not ready:
                return
            rid = min(ready, key=lambda r: (self.active[r], self.order.index(r)))
            fut = self.waiting[rid].popleft()
            if fut.done():
                continue
            self.active[rid] += 1
            self.total += 1
            fut.set_result(None)

    def _release(self, rid: str) -> None:
        self.active[rid] -= 1
        self.total -= 1
        self._dispatch()

    @contextlib.asynccontextmanager
    async def slot(self, rid: str) -> AsyncIterator[None]:
        if rid not in self.order:
            self.order.append(rid)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiting[rid].append(fut)
        self._dispatch()
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self._release(rid)
            elif fut in self.waiting[rid]:
                self.waiting[rid].remove(fut)
            raise
        try:
            yield
        finally:
            self._release(rid)


def port_lock(port: int) -> Any:
    """An exclusive lock for the live Redis on ``port``, held until the process exits; None if taken."""
    f = open(Path(tempfile.gettempdir()) / f"tts_cache_testplan_live_{port}.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def redis_running(port: int) -> bool:
    out = subprocess.run(["redis-cli", "-p", str(port), "ping"], capture_output=True, text=True)
    return out.stdout.strip() == "PONG"


def start_live_redis(port: int) -> None:
    if port == 6379:
        raise SystemExit("port 6379 is the shared Redis; never touch it")
    if redis_running(port):
        return
    subprocess.run(
        [
            "redis-server", "--port", str(port), "--databases", str(LIVE_REDIS_DBS), "--save", "",
            "--appendonly", "no", "--maxmemory", "4gb", "--maxmemory-policy", "allkeys-lfu", "--daemonize", "yes",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    for _ in range(100):
        if redis_running(port):
            return
        time.sleep(0.05)
    raise RuntimeError(f"redis on {port} did not start")


def stop_redis(port: int) -> None:
    if port == 6379:
        raise SystemExit("port 6379 is the shared Redis; never touch it")
    subprocess.run(["redis-cli", "-p", str(port), "shutdown", "nosave"], capture_output=True)


# ---------------------------------------------------------------------------
# The run: sessions, turns, retries, checkpoint.
# ---------------------------------------------------------------------------


@dataclass
class Options:
    out: Path
    fake: bool = False
    port: int = LIVE_REDIS_PORT
    concurrency: dict[str, int] = field(default_factory=dict)
    faults: Any = None
    crash_after_turns: int | None = None
    kill_redis_after_turns: int | None = None
    pause_redis_after_turns: int | None = None
    pause_redis_ms: int = 5000
    quiet: bool = True


class Harness:
    def __init__(self, runs: list[LiveRun], opts: Options) -> None:
        self.runs = runs
        self.opts = opts
        self.out = opts.out
        self.limiters = {p: Limiter(opts.concurrency.get(p, DEFAULT_CONCURRENCY)) for p in set(PROVIDER.values())}
        self.db_of = {run.id: i for i, run in enumerate(sorted(runs, key=lambda r: r.id))}
        self.turns_done = 0
        self.events_path = self.out / "events.jsonl"
        self.redis_fault_at: float | None = None
        self._fault_lock = asyncio.Lock()

    # Files.

    def event(self, kind: str, **fields: Any) -> None:
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"wall": time.time(), "t": now(), "event": kind, **fields}, ensure_ascii=False) + "\n")

    def rows_path(self, run: LiveRun) -> Path:
        return self.out / "rows" / f"{run.id}.jsonl"

    def journal_path(self, run: LiveRun) -> Path:
        return self.out / "journal" / f"{run.id}.jsonl"

    def done_rows(self, run: LiveRun) -> dict[int, dict[str, Any]]:
        p = self.rows_path(run)
        if not p.exists():
            return {}
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return {r["index"]: r for r in rows}

    def journal_entries(self, run: LiveRun) -> list[dict[str, Any]]:
        p = self.journal_path(run)
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def journal(self, run: LiveRun) -> set[int]:
        return {x["index"] for x in self.journal_entries(run)}

    def redis_url(self, run: LiveRun) -> str:
        return f"redis://localhost:{self.opts.port}/{self.db_of[run.id]}"

    # Top level.

    async def run_all(self) -> list[dict[str, Any]]:
        for d in ("rows", "journal", "frames", "audio", "requests", "metrics", "exports"):
            (self.out / d).mkdir(parents=True, exist_ok=True)
        start_live_redis(self.opts.port)
        self.event("start", runs=len(self.runs), fake=self.opts.fake)
        # Providers run side by side; within a provider, one cell at a time, all its runs together.
        cells: dict[str, dict[tuple[str, str], list[LiveRun]]] = defaultdict(dict)
        for run in self.runs:
            cells[run.provider].setdefault((run.lang, run.service), []).append(run)

        async def provider_chain(prov: str) -> list[tuple[LiveRun, Any]]:
            out: list[tuple[LiveRun, Any]] = []
            for (lang, service), runs in cells[prov].items():
                slots = FairSlots(self.limiters[prov])
                self.event("cell_start", provider=prov, cell=f"{lang}/{service}", runs=[r.id for r in runs])
                res = await asyncio.gather(*(self.run_one(r, slots) for r in runs), return_exceptions=True)
                out += list(zip(runs, res, strict=True))
                self.event("cell_end", provider=prov, cell=f"{lang}/{service}")
            return out

        chains = await asyncio.gather(*(provider_chain(p) for p in cells))
        pairs = [x for chain in chains for x in chain]
        summaries = []
        for run, res in pairs:
            if isinstance(res, BaseException):
                self.event("run_error", run=run.id, error=f"{type(res).__name__}: {res}")
                summaries.append({"run": run.id, "error": f"{type(res).__name__}: {res}"})
            else:
                summaries.append(res)
        with open(self.out / "runs.jsonl", "a", encoding="utf-8") as f:
            for s in summaries:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        self.event("end", limiters={p: lim.lowered for p, lim in self.limiters.items()})
        return summaries

    async def run_one(self, run: LiveRun, slots: FairSlots) -> dict[str, Any]:
        corpus = corpus_for(run.lang, run.r, SEED)
        done = self.done_rows(run)
        sent = self.journal(run)
        lost = sorted(sent - set(done))
        if lost:
            # Sent before a crash and never completed: never sent again (2.4).
            for idx in lost:
                it = corpus.items[idx]
                self.write_row(run, self.lost_row(run, it))
            self.event("resume_lost", run=run.id, indices=lost)
            done = self.done_rows(run)
        if done:
            self.event("resume", run=run.id, done=len(done))
        sessions = corpus.sessions
        if run.config != "OFF":
            flushed = bool(done)
            for s_items in sessions:
                remaining = [[it for it in turn if it.index not in done] for turn in s_items]
                # Resumed: part of this session ran before a crash, so its fresh cache warm-starts
                # with that part's captures, which the offline session would not have seen yet.
                partial = any(it.index in done for turn in s_items for it in turn)
                if any(remaining):
                    async with slots.slot(run.id):
                        if not flushed:
                            # Inside the limiter, so at most a few runs open admin connections at once.
                            await self.admin(run, "flush", lambda st: st.client.flushdb())
                            flushed = True
                        await self.guarded_session(run, corpus, s_items, resumed=partial)
            await self.export(run)
        else:

            async def one(s_items: list[list[LabelledItem]]) -> None:
                remaining = [[it for it in turn if it.index not in done] for turn in s_items]
                partial = any(it.index in done for turn in s_items for it in turn)
                if any(remaining):
                    async with slots.slot(run.id):
                        await self.guarded_session(run, corpus, s_items, resumed=partial)

            await asyncio.gather(*(one(s) for s in sessions))
        rows = list(self.done_rows(run).values())
        return self.summarise(run, rows)

    async def guarded_session(
        self, run: LiveRun, corpus: LabelledCorpus, s_items: list[list[LabelledItem]], *, resumed: bool
    ) -> None:
        """A session that raises is recorded and retried from its unsent sentences, twice (2.4)."""
        for attempt in range(SESSION_RETRIES + 1):
            done = set(self.done_rows(run)) | self.journal(run)
            remaining = [[it for it in turn if it.index not in done] for turn in s_items]
            if not any(remaining):
                return
            partial = resumed or attempt > 0
            try:
                await self.run_session(run, corpus, remaining, resumed=partial)
                return
            except Exception as e:  # noqa: BLE001 - a harness error must not end the run (2.4)
                import traceback

                self.event(
                    "session_error",
                    run=run.id,
                    attempt=attempt,
                    error=f"{type(e).__name__}: {e}",
                    traceback=traceback.format_exc()[-2000:],
                )
                # Sent but never completed: recorded as lost and never sent again, as after a crash.
                rows, sent = self.done_rows(run), self.journal(run)
                lost = [it for turn in s_items for it in turn if it.index in sent and it.index not in rows]
                for it in lost:
                    self.write_row(run, self.lost_row(run, it))
                if lost:
                    self.event("session_lost", run=run.id, indices=[it.index for it in lost])
                await asyncio.sleep(2.0 * (attempt + 1))
        self.event("session_abandoned", run=run.id, first_index=s_items[0][0].index if s_items and s_items[0] else None)

    async def admin(self, run: LiveRun, what: str, fn: Any) -> Any:
        """A harness operation on the run's database, with a generous timeout and retries."""
        for attempt in range(ADMIN_RETRIES):
            store = RedisStore(self.redis_url(run), socket_timeout=ADMIN_TIMEOUT_S)
            try:
                return await fn(store)
            except Exception as e:  # noqa: BLE001
                self.event("admin_retry", run=run.id, what=what, attempt=attempt, error=repr(e))
                await asyncio.sleep(2.0 * (attempt + 1))
            finally:
                await store.close()
        raise RuntimeError(f"{what} failed {ADMIN_RETRIES} times for {run.id}")

    async def export(self, run: LiveRun) -> None:
        async def do(store: RedisStore) -> None:
            n = await store.export(self.out / "exports" / f"{run.id}.jsonl")
            info = await store.client.info("memory")
            self.event("export", run=run.id, entries=n, used_memory=info.get("used_memory"))

        try:
            await self.admin(run, "export", do)
        except Exception as e:  # noqa: BLE001 - a failed export is recorded, the run stands
            self.event("export_error", run=run.id, error=repr(e))

    # One session: a fresh pipeline and cache; turns in order; failed sentences retried.

    async def run_session(
        self, run: LiveRun, corpus: LabelledCorpus, turns: list[list[LabelledItem]], *, resumed: bool
    ) -> None:
        sess = await Session.open(self, run, corpus)
        try:
            partial = resumed and bool(turns) and bool(turns[0]) and not turns[0][0].first_in_turn
            for ti, items in enumerate(turns):
                if not items:
                    continue
                disrupted = partial and ti == 0
                results = await sess.turn(items)
                if results.get("stalled"):
                    # The session's pipeline is wedged behind the stalled request: rebuild it.
                    await sess.close()
                    sess = await Session.open(self, run, corpus)
                await self.finish_turn(
                    run,
                    sess,
                    items,
                    results,
                    disrupted=disrupted or bool(results.get("stalled")),
                    resumed=resumed,
                )
                if sess.cache is not None:
                    await sess.cache.drain(10.0)
                await self.after_turn(not (ti == len(turns) - 1))
        finally:
            await sess.close()

    async def finish_turn(
        self,
        run: LiveRun,
        sess: "Session",
        items: list[LabelledItem],
        res: dict[str, Any],
        *,
        disrupted: bool,
        resumed: bool = False,
    ) -> None:
        """Rows for a turn; failed sentences retried alone, twice, with backoff (2.4)."""
        for it in items:
            outcome = res["by_index"].get(it.index)
            attempts = [outcome] if outcome is not None else []
            failed = outcome is None or not outcome["ok"]
            retry = 0
            while failed and retry < RETRIES:
                rate_limited = any("429" in e for a in attempts if a for e in a.get("errors", []))
                if rate_limited:
                    self.limiters[run.provider].lower(f"429 in {run.id}")
                    self.event("rate_limited", run=run.id, index=it.index, capacity=self.limiters[run.provider].capacity)
                await asyncio.sleep((BACKOFF_429_S if rate_limited else BACKOFF_S)[retry])
                retry += 1
                again = await sess.turn([it])
                if again.get("stalled"):
                    await sess.close()
                    new = await Session.open(self, run, sess.corpus)
                    sess.adopt(new)
                got = again["by_index"].get(it.index)
                attempts.append(got)
                failed = got is None or not got["ok"]
            final = attempts[-1] if attempts else None
            row = self.row(run, it, final, attempts, disrupted=disrupted or retry > 0, res=res)
            row["provider_failed"] = failed
            row["resumed_session"] = resumed
            self.write_row(run, row)
            if final is not None and final.get("ok") and not final["hit"] and final.get("audio"):
                await asyncio.to_thread(self.save_audio, run, it.index, final["audio"], final["sample_rate"])

    async def after_turn(self, mid_session: bool) -> None:
        self.turns_done += 1
        o = self.opts
        if o.pause_redis_after_turns is not None and self.turns_done == o.pause_redis_after_turns:
            # CLIENT PAUSE returns at once; the server holds every client's commands until it ends.
            self.event("fault_redis_pause", port=o.port, ms=o.pause_redis_ms)
            self.redis_fault_at = self.redis_fault_at or now()
            await asyncio.to_thread(
                subprocess.run,
                ["redis-cli", "-p", str(o.port), "client", "pause", str(o.pause_redis_ms), "all"],
                capture_output=True,
            )
        if o.kill_redis_after_turns is not None and self.turns_done == o.kill_redis_after_turns:
            async with self._fault_lock:
                self.event("fault_redis_kill", port=o.port)
                self.redis_fault_at = now()
                stop_redis(o.port)
                await asyncio.sleep(3.0)
                start_live_redis(o.port)
                self.event("fault_redis_restart", port=o.port)
        if o.crash_after_turns is not None and mid_session and self.turns_done >= o.crash_after_turns:
            self.event("fault_crash", turns_done=self.turns_done)
            os._exit(75)

    # Rows.

    def row(
        self,
        run: LiveRun,
        it: LabelledItem,
        final: dict[str, Any] | None,
        attempts: list[dict[str, Any] | None],
        *,
        disrupted: bool,
        res: dict[str, Any],
    ) -> dict[str, Any]:
        f = final or {}
        audio = f.get("audio") or b""
        sr = f.get("sample_rate") or SR
        row = {
            "run": run.id, "lang": run.lang, "service": run.service, "provider": run.provider,
            "config": run.config, "r": run.r, "kind": run.kind,
            "session": it.session, "turn": it.turn, "index": it.index, "first_in_turn": it.first_in_turn,
            "label": it.label, "base": it.base, "text": it.text, "chars": len(it.text.strip()),
            "hit": bool(f.get("hit")), "level": f.get("level"), "tier": f.get("tier"),
            "diff_class": f.get("diff_class"), "key": f.get("key"), "spoken_text": f.get("spoken_text"),
            "requested_text": f.get("requested_text"), "context_id": f.get("context_id"),
            "t_release": f.get("t_release"), "t_request": f.get("t_request"), "t_started": f.get("t_started"),
            "t_first_audio": f.get("t_first_audio"), "t_last_audio": f.get("t_last_audio"),
            "t_stopped": f.get("t_stopped"), "wall": time.time(),
            "ttfa_ms": f.get("ttfa_ms"), "audio_ms": round(len(audio) / 2 / sr * 1000, 1) if audio else 0.0,
            "bytes": len(audio), "digest": digest(audio) if audio else None,
            "transcript": f.get("texts"), "transcript_ok": f.get("transcript_ok"),
            "attempts": len([a for a in attempts if a is not None and a.get("requested")]),
            "attempt_log": [{k: v for k, v in (a or {}).items() if k not in ("audio",)} for a in attempts],
            "retried": len(attempts) > 1, "turn_disrupted": disrupted,
            "errors": [e for a in attempts if a for e in a.get("errors", [])],
            "redis_fault": self.redis_fault_at is not None, "mismatch": f.get("mismatch"),
            "lookup_ms": f.get("lookup_ms"), "miss_reason": f.get("miss_reason"),
        }  # fmt: skip
        return row

    def lost_row(self, run: LiveRun, it: LabelledItem) -> dict[str, Any]:
        row = self.row(run, it, None, [], disrupted=True, res={})
        # Billed but never completed: its requests are in the journal and count for V1.
        attempts = sum(1 for x in self.journal_entries(run) if x["index"] == it.index and x["kind"] == "request")
        row.update(lost_in_crash=True, provider_failed=False, attempts=attempts)
        return row

    def write_row(self, run: LiveRun, row: dict[str, Any]) -> None:
        with open(self.rows_path(run), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def journal_sent(self, run: LiveRun, index: int, kind: str) -> None:
        with open(self.journal_path(run), "a", encoding="utf-8") as f:
            f.write(json.dumps({"index": index, "kind": kind, "wall": time.time()}) + "\n")
            f.flush()

    def save_audio(self, run: LiveRun, index: int, pcm: bytes, sr: int) -> None:
        d = self.out / "audio" / run.id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{index:04d}.flac").write_bytes(codec.encode(pcm, sr, 1, "flac"))

    def summarise(self, run: LiveRun, rows: list[dict[str, Any]]) -> dict[str, Any]:
        hits = [r for r in rows if r["hit"]]
        return {
            "run": run.id, "lang": run.lang, "service": run.service, "config": run.config, "r": run.r,
            "kind": run.kind, "sentences": len(rows), "hits": len(hits),
            "billed_chars": sum(r["chars"] * r["attempts"] for r in rows if not r["hit"]),
            "requests": sum(r["attempts"] for r in rows),
            "provider_failed": sum(1 for r in rows if r.get("provider_failed")),
            "retried": sum(1 for r in rows if r["retried"]),
            "lost_in_crash": sum(1 for r in rows if r.get("lost_in_crash")),
            "mismatch": sum(1 for r in rows if r.get("mismatch")),
        }  # fmt: skip


class Session:
    """One session's pipeline: service, cache (ON) or splitter (OFF), probe, and turn execution."""

    def __init__(self, h: Harness, run: LiveRun, corpus: LabelledCorpus) -> None:
        self.h = h
        self.run = run
        self.corpus = corpus
        self.reqlog = RequestLog(path=h.out / "requests" / f"{run.id}.jsonl")
        self.frame_log: list[list[Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.cache: TTSCache | None = None
        self.sink = RecordingSink()
        self.http: aiohttp.ClientSession | None = None
        self.task: PipelineTask | None = None
        self.runner_task: asyncio.Task[Any] | None = None
        self.tts: Any = None
        self.probe: Probe | None = None
        self.closed = False
        self.turn_items: list[LabelledItem] = []
        self.journaled: set[int] = set()

    @classmethod
    async def open(cls, h: Harness, run: LiveRun, corpus: LabelledCorpus) -> "Session":
        s = cls(h, run, corpus)
        try:
            await s._open()
        except BaseException:
            await s.close()
            raise
        return s

    def adopt(self, other: "Session") -> None:
        self.__dict__.update(other.__dict__)

    async def _open(self) -> None:
        run, h = self.run, self.h
        self.http = aiohttp.ClientSession(trace_configs=[self.reqlog.aiohttp_trace()])
        self.tts = build_service(
            run.service, run.lang, self.http, self.reqlog, fake=h.opts.fake, run_id=run.id, faults=h.opts.faults
        )

        async def on_tts_request(_tts: Any, context_id: str, text: str) -> None:
            # Journal the request the moment it is made: a sentence sent before a crash is
            # never sent again (2.4). The first unjournaled sentence of the turn with this
            # text is the one sent; a retry matches a sentence already journaled.
            match = [it for it in self.turn_items if it.text.strip() == text.strip()]
            fresh = [it for it in match if it.index not in self.journaled]
            item = (fresh or match or [None])[0]
            rec: dict[str, Any] = {"t": now(), "context_id": context_id, "text": text, "index": None}
            if item is not None:
                self.journaled.add(item.index)
                h.journal_sent(run, item.index, "request")
                rec["index"] = item.index
            self.requests.append(rec)
            self.reqlog.write({"on_tts_request": rec})

        self.tts.add_event_handler("on_tts_request", on_tts_request)
        self.probe = Probe(self.frame_log)
        if run.config == "OFF":
            procs: list[Any] = [SplitOnly(f"{run.lang}-IN"), self.tts, self.probe]
        else:
            policy, matcher = config_policy(run.config)
            store = TieredStore(MemoryStore(warm_start_bytes=4 << 20), RedisStore(h.redis_url(run)), deadline_ms=20)
            self.cache = TTSCache(
                scope_from_service(self.tts, language=f"{run.lang}-IN"),
                store,
                policy=policy,
                matcher=matcher,
                metrics=self.sink,
            )
            await self.cache.start()
            procs = [TTSCacheRead(self.cache, tts=self.tts), self.tts, TTSCacheWrite(self.cache, tts=self.tts), self.probe]
            if capture_mode_for(self.tts) != "exact":
                h.event("capture_mode", run=run.id, mode=capture_mode_for(self.tts))
        self.task = PipelineTask(
            Pipeline(procs), params=PipelineParams(audio_out_sample_rate=SR), cancel_on_idle_timeout=False
        )

        @self.task.event_handler("on_pipeline_error")
        async def on_error(_task: Any, frame: ErrorFrame) -> None:
            rec = {"t": now(), "error": str(frame.error)}
            self.errors.append(rec)
            self.reqlog.write({"error": rec})

        self.runner_task = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.sleep(CONNECT_WAIT_S)  # the connection opens before the first turn, outside L1

    async def turn(self, items: list[LabelledItem]) -> dict[str, Any]:
        """Stream one turn word by word and wait for every sentence's stop frame, or a stall."""
        assert self.task is not None and self.probe is not None
        run, probe = self.run, self.probe
        self.turn_items = list(items)
        start_seq = len(probe.order)
        start_req = len(self.requests)
        start_err = len(self.errors)
        start_ev = len(self.sink.events)
        t_rel: list[float] = []
        await self.task.queue_frame(LLMFullResponseStartFrame())
        for i, it in enumerate(items):
            for j, tok in enumerate(it.text.split(" ")):
                if j == 0 and i > 0:
                    t_rel.append(now())
                await self.task.queue_frame(LLMTextFrame(tok + " "))
                await asyncio.sleep(TOKEN_GAP_S)
        t_rel.append(now())
        await self.task.queue_frame(LLMFullResponseEndFrame())
        stalled = False
        last_progress = now()
        last_state: tuple[int, int, int] = (-1, -1, -1)
        while True:
            ctxs = [probe.ctxs[c] for c in probe.order[start_seq:]]
            stopped = sum(1 for c in ctxs if c.t_stopped is not None)
            if len(ctxs) >= len(items) and stopped >= len(items):
                break
            state = (len(ctxs), stopped, sum(len(c.audio) for c in ctxs))
            if state != last_state:
                last_state, last_progress = state, now()
            # Stall: a request with no audio, or nothing happening at all, for STALL_S (2.4).
            open_ = [c for c in ctxs if c.t_stopped is None]
            waiting = open_[0] if open_ else None
            if waiting is not None and waiting.t_first_audio is None and now() - (waiting.t_started or last_progress) > STALL_S:
                stalled = True
            elif now() - last_progress > STALL_S:
                stalled = True
            if stalled:
                self.h.event("stall", run=run.id, indices=[it.index for it in items], contexts=len(ctxs), stopped=stopped)
                break
            probe.changed.clear()
            try:
                await asyncio.wait_for(probe.changed.wait(), timeout=0.05)
            except TimeoutError:
                pass
        if stalled:
            await self.task.queue_frame(InterruptionFrame())
            await asyncio.sleep(0.2)
        await probe.bot_stopped()
        ctxs = [probe.ctxs[c] for c in probe.order[start_seq:]]
        reqs = self.requests[start_req:]
        errs = self.errors[start_err:]
        events = self.sink.events[start_ev:]
        by_index: dict[int, dict[str, Any]] = {}
        req_by_ctx = {r["context_id"]: r for r in reqs}
        for k, it in enumerate(items):
            c = ctxs[k] if k < len(ctxs) else None
            if c is None:
                continue
            req = req_by_ctx.get(c.context_id)
            ok = c.t_stopped is not None and (c.hit or bool(c.audio))
            # Whether a sentence failed is decided by its audio, not by errors nearby: Pipecat
            # reports a provider error after the context ends, by which time the next request may
            # be out. An error that names a context belongs to it; one that names none annotates
            # a context in its window only if that context produced no audio.
            t_end = c.t_stopped or now()
            failed_here = not ok
            mine = [
                e["error"]
                for e in errs
                if (c.context_id in e["error"])
                or (
                    failed_here
                    and not _CONTEXT_ID.search(e["error"])
                    and (c.t_started or 0) - 0.05 <= e["t"] <= t_end + 0.5
                )
            ]
            ev = next((e for e in events if e["event"] == "hit" and e.get("key") == c.meta.get("key")), None) if c.hit else None
            miss_ev = next((e for e in events if e["event"] == "miss" and e.get("key") == c.meta.get("key")), None)
            spoken = c.meta.get("spoken_text")
            texts = list(c.texts)
            transcript_ok = (texts == [spoken]) if c.hit else None
            mismatch = None
            if not c.hit and c.text is not None and c.text.strip() != it.text.strip():
                mismatch = {"expected": it.text, "context_text": c.text}
            if c.hit and spoken is not None and run.config != "OFF" and c.meta.get("level") == "A" and spoken.strip() != it.text.strip():
                mismatch = {"expected": it.text, "spoken": spoken}
            by_index[it.index] = {
                "ok": ok and not any(c.context_id in e for e in mine),
                "hit": c.hit,
                "requested": req is not None,
                "requested_text": req["text"] if req else None,
                "context_id": c.context_id,
                "level": c.meta.get("level"),
                "key": c.meta.get("key"),
                "spoken_text": spoken,
                "tier": (ev or {}).get("tier"),
                "diff_class": (ev or {}).get("diff_class"),
                "lookup_ms": (ev or miss_ev or {}).get("lookup_ms"),
                "miss_reason": (miss_ev or {}).get("reason"),
                "t_release": t_rel[k] if k < len(t_rel) else None,
                "t_request": req["t"] if req else None,
                "t_started": c.t_started,
                "t_first_audio": c.t_first_audio,
                "t_last_audio": c.t_last_audio,
                "t_stopped": c.t_stopped,
                "ttfa_ms": round((c.t_first_audio - t_rel[k]) * 1000, 2)
                if c.t_first_audio is not None and k < len(t_rel)
                else None,
                "audio": bytes(c.audio),
                "sample_rate": c.sample_rate,
                "texts": texts,
                "transcript_ok": transcript_ok,
                "errors": mine,
                "mismatch": mismatch,
            }
        return {"by_index": by_index, "stalled": stalled}

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        run, h = self.run, self.h
        try:
            if self.cache is not None:
                await self.cache.drain(10.0)
                await self.cache.close(timeout=10.0)
        except Exception as e:
            h.event("cache_close_error", run=run.id, error=repr(e))
        if self.task is not None:
            await self.task.cancel()
        if self.runner_task is not None:
            try:
                await asyncio.wait_for(self.runner_task, timeout=10)
            except Exception as e:
                h.event("runner_close", run=run.id, error=repr(e))
        if self.http is not None:
            await self.http.close()
        fake_log = getattr(self.tts, "last_headers", None)
        if fake_log:
            self.reqlog.add("fake", 200, "fake://", fake_log)
        tag = f"{run.id}.jsonl"
        with gzip.open(h.out / "frames" / f"{tag}.gz", "at", encoding="utf-8") as f:
            for rec in self.frame_log:
                f.write(json.dumps(rec) + "\n")
        with open(h.out / "metrics" / tag, "a", encoding="utf-8") as f:
            for rec in self.sink.events:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# Budget (11.2, 11.4): from the frozen corpora and the offline misses.
# ---------------------------------------------------------------------------


def budget(runs: list[LiveRun], offline: Path | None) -> dict[str, Any]:
    per: dict[str, dict[str, float]] = defaultdict(lambda: {"worst": 0, "expected": 0, "runs": 0, "sentences": 0})
    missing = []
    for run in runs:
        corpus = corpus_for(run.lang, run.r, SEED)
        chars = [len(it.text.strip()) for it in corpus.items]
        p = per[run.provider]
        p["runs"] += 1
        p["sentences"] += len(chars)
        p["worst"] += sum(chars)
        if run.config == "OFF":
            p["expected"] += sum(chars)
            continue
        rows_file = offline / "rows" / f"{run.lang}_{run.config}_{run.r}_labelled_{SEED}_serverless.jsonl" if offline else None
        if rows_file is not None and rows_file.exists():
            rows = [json.loads(x) for x in rows_file.read_text(encoding="utf-8").splitlines() if x.strip()]
            p["expected"] += sum(r["chars"] for r in rows if not r["hit"])
        else:
            missing.append(run.id)
            p["expected"] += sum(chars)
    out: dict[str, Any] = {"providers": {}, "missing_offline": missing}
    for prov, p in sorted(per.items()):
        out["providers"][prov] = {
            **{k: int(v) for k, v in p.items()},
            "worst_usd": round(usd(prov, p["worst"]), 2),
            "expected_usd": round(usd(prov, p["expected"]), 2),
            # Every sentence retried twice and failing: the pathological ceiling, never expected.
            "retry_ceiling_chars": int(3 * p["worst"]),
        }
    out["worst"] = sum(int(p["worst"]) for p in per.values())
    out["expected"] = sum(int(p["expected"]) for p in per.values())
    out["worst_usd"] = round(sum(v["worst_usd"] for v in out["providers"].values()), 2)
    out["expected_usd"] = round(sum(v["expected_usd"] for v in out["providers"].values()), 2)
    return out


# ---------------------------------------------------------------------------
# Environment record and network baseline.
# ---------------------------------------------------------------------------


def environment(fake: bool) -> dict[str, Any]:
    """The run's environment: versions, SHAs and the non-secret settings (no key is ever written)."""
    from importlib.metadata import version

    def git(*a: str, cwd: Path = REPO) -> str:
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()

    secret = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    env_file = REPO / ".env"
    names = []
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                names.append(line.split("=", 1)[0].strip())
    settings = {n: ("<redacted>" if any(s in n for s in secret) else os.environ.get(n)) for n in names}
    return {
        "wall": time.time(),
        "fake": fake,
        "repo_sha": git("rev-parse", "HEAD"),
        "repo_dirty": bool(git("status", "--porcelain")),
        "pipecat_sha": git("rev-parse", "--short=9", "HEAD", cwd=Path.home() / "PersonalProjects" / "pipecat"),
        "pipecat_pinned": PIPECAT_SHA,
        "versions": {p: version(p) for p in ("pipecat-ai", "smart-tts-cache", "smart-tts-cache-pipecat", "onnxruntime", "redis", "aiohttp")},
        "python": sys.version,
        "platform": subprocess.run(["uname", "-a"], capture_output=True, text=True).stdout.strip(),
        "settings": settings,
        "sarvam_model": sarvam_model(),
        "sarvam_voice": sarvam_voice(),
        "openai_model": "gpt-4o-mini-tts",
        "elevenlabs_model": "eleven_flash_v2_5",
        "elevenlabs_dialogue_model": os.environ.get("ELEVENLABS_DIALOGUE_MODEL", "eleven_v4_turbo"),
        "stt_model": os.environ.get("STT_MODEL", "gpt-4o-mini-transcribe"),
    }


def netcheck(n: int = 20) -> dict[str, Any]:
    out = {}
    for prov, host in HOSTS.items():
        times = []
        errors = []
        for _ in range(n):
            t = time.perf_counter()
            try:
                with socket.create_connection((host, 443), timeout=5):
                    times.append((time.perf_counter() - t) * 1000)
            except OSError as e:
                errors.append(repr(e))
        out[prov] = {
            "host": host,
            "n": len(times),
            "p50_ms": round(statistics.median(times), 2) if times else None,
            "max_ms": round(max(times), 2) if times else None,
            "min_ms": round(min(times), 2) if times else None,
            "errors": errors,
        }
    return out


# ---------------------------------------------------------------------------
# Preflight (2.3).
# ---------------------------------------------------------------------------


def preflight_sentence(lang: str) -> str:
    """The pool base closest to 40 characters: deterministic, and real text of the language."""
    from tts_cache.corpus import load_pool
    from tts_cache.testplan.pools import pool_path

    bases = load_pool(pool_path(lang)).bases
    return min(bases, key=lambda b: (abs(len(b.text) - 40), b.id)).text


def probe_sentences(lang: str, n: int) -> list[str]:
    from tts_cache.corpus import load_pool
    from tts_cache.testplan.pools import pool_path

    bases = sorted(load_pool(pool_path(lang)).bases, key=lambda b: (abs(len(b.text) - 55), b.id))
    return [b.text for b in bases[:n]]


async def single(
    service: str,
    lang: str,
    sentences: list[str],
    *,
    fake: bool,
    cache_db: Path | None,
    codec_name: str = "flac",
) -> dict[str, Any]:
    """One turn through a fresh pipeline, with a SQLite-backed cache or none; returns per-sentence facts."""
    reqlog = RequestLog()
    frame_log: list[list[Any]] = []
    requests: list[dict[str, Any]] = []
    errors: list[str] = []
    async with aiohttp.ClientSession(trace_configs=[reqlog.aiohttp_trace()]) as http:
        tts = build_service(service, lang, http, reqlog, fake=fake, run_id=f"preflight-{service}-{lang}")

        async def on_req(_t: Any, cid: str, text: str) -> None:
            requests.append({"t": now(), "context_id": cid, "text": text})

        tts.add_event_handler("on_tts_request", on_req)
        probe = Probe(frame_log)
        cache = None
        if cache_db is not None:
            cache = TTSCache(
                scope_from_service(tts, language=f"{lang}-IN"),
                TieredStore(MemoryStore(), SQLiteStore(cache_db, sweep_interval_s=0)),
                metrics=LogSink(log=None),
                codec=codec_name,  # type: ignore[arg-type]
            )
            await cache.start()
            procs: list[Any] = [TTSCacheRead(cache, tts=tts), tts, TTSCacheWrite(cache, tts=tts), probe]
        else:
            procs = [SplitOnly(f"{lang}-IN"), tts, probe]
        task = PipelineTask(Pipeline(procs), params=PipelineParams(audio_out_sample_rate=SR), cancel_on_idle_timeout=False)

        @task.event_handler("on_pipeline_error")
        async def on_error(_task: Any, frame: ErrorFrame) -> None:
            errors.append(str(frame.error))

        runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
        await asyncio.sleep(CONNECT_WAIT_S)
        t_rel: list[float] = []
        await task.queue_frame(LLMFullResponseStartFrame())
        for i, s in enumerate(sentences):
            for j, tok in enumerate(s.split(" ")):
                if j == 0 and i > 0:
                    t_rel.append(now())
                await task.queue_frame(LLMTextFrame(tok + " "))
                await asyncio.sleep(TOKEN_GAP_S)
        t_rel.append(now())
        await task.queue_frame(LLMFullResponseEndFrame())
        t0 = now()
        while now() - t0 < 40:
            if sum(1 for c in probe.ctxs.values() if c.t_stopped is not None) >= len(sentences):
                break
            await asyncio.sleep(0.05)
        if cache is not None:
            await cache.drain(10.0)
            await cache.close(timeout=10.0)
        await task.cancel()
        try:
            await asyncio.wait_for(runner, timeout=10)
        except Exception:
            pass
    ctxs = [probe.ctxs[c] for c in probe.order]
    return {
        "requests": [r["text"] for r in requests],
        "errors": errors,
        "headers": reqlog.entries[-3:],
        "contexts": [
            {
                "hit": c.hit,
                "bytes": len(c.audio),
                "digest": digest(bytes(c.audio)) if c.audio else None,
                "ttfa_ms": round((c.t_first_audio - t_rel[i]) * 1000, 1) if c.t_first_audio and i < len(t_rel) else None,
                "t_first_audio": c.t_first_audio,
                "t_last_audio": c.t_last_audio,
                "t_stopped": c.t_stopped,
            }
            for i, c in enumerate(ctxs)
        ],
    }


async def preflight(out: Path, langs: list[str], *, fake: bool, concurrency: int, offline: Path | None) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "preflight_db"
    tmp.mkdir(exist_ok=True)
    res: dict[str, Any] = {"fake": fake, "wall": time.time(), "items": {}}
    chars = defaultdict(int)

    # Items 1 and 2: coverage, then the cache path, per cell.
    cov = {}
    for lang, service, kind in cells(langs):
        s = preflight_sentence(lang)
        db = tmp / f"{service}_{lang}.db"
        for p in tmp.glob(f"{service}_{lang}.db*"):
            p.unlink()
        try:
            r1 = await single(service, lang, [s], fake=fake, cache_db=db)
            chars[PROVIDER[service]] += sum(len(x.strip()) for x in r1["requests"])
            r2 = await single(service, lang, [s], fake=fake, cache_db=db)
            c1 = r1["contexts"][0] if r1["contexts"] else {}
            c2 = r2["contexts"][0] if r2["contexts"] else {}
            ok1 = bool(c1.get("bytes")) and not r1["errors"]
            ok2 = not r2["requests"] and bool(c2.get("hit")) and c2.get("digest") == c1.get("digest")
            cov[f"{lang}/{service}"] = {
                "kind": kind,
                "sentence": s,
                "coverage": "pass" if ok1 else "fail",
                "cache_path": "pass" if ok2 else "fail",
                "status": "ok" if ok1 and ok2 else "n/a",
                "error": None if ok1 else (r1["errors"] or ["no audio"]),
                "run1": r1,
                "run2": r2,
            }
        except Exception as e:
            cov[f"{lang}/{service}"] = {"kind": kind, "status": "n/a", "error": [f"{type(e).__name__}: {e}"]}
    res["items"]["1_2_coverage_and_cache_path"] = cov

    # Item 3: REST close, two back-to-back sentences on each REST class.
    rest = {}
    for service in sorted(REST):
        lang = "en"
        two = probe_sentences(lang, 2)
        try:
            r = await single(service, lang, two, fake=fake, cache_db=None)
            chars[PROVIDER[service]] += sum(len(x.strip()) for x in r["requests"])
            c = r["contexts"]
            gap = (c[1]["t_first_audio"] - c[0]["t_last_audio"]) * 1000 if len(c) == 2 and c[1]["t_first_audio"] and c[0]["t_last_audio"] else None
            stop_lag = (c[0]["t_stopped"] - c[0]["t_last_audio"]) * 1000 if c and c[0]["t_stopped"] and c[0]["t_last_audio"] else None
            rest[service] = {
                "gap_ms": round(gap, 1) if gap is not None else None,
                "first_stop_after_last_audio_ms": round(stop_lag, 1) if stop_lag is not None else None,
                "pass": stop_lag is not None and stop_lag < 1000,
                "contexts": c,
            }
        except Exception as e:
            rest[service] = {"pass": False, "error": f"{type(e).__name__}: {e}"}
    res["items"]["3_rest_close"] = rest

    # Item 4: concurrency probe, 20 sentences per provider at 1 and at the chosen concurrency.
    conc = {}
    for prov, (service, lang) in {"openai": ("openai_http", "en"), "elevenlabs": ("elevenlabs_ws_fc", "en"), "sarvam": ("sarvam_ws_flush", "hi")}.items():
        if not any(PROVIDER[s] == prov for _, s, _ in cells(langs)):
            continue
        sents = probe_sentences(lang, 20)
        chosen = concurrency
        trials = []
        for level in (1, chosen, max(1, chosen // 2)):
            if trials and level == trials[-1]["concurrency"]:
                break
            sem = asyncio.Semaphore(level)

            async def one(s: str, sem: asyncio.Semaphore = sem, service: str = service, lang: str = lang) -> dict[str, Any]:
                async with sem:
                    return await single(service, lang, [s], fake=fake, cache_db=None)

            got = await asyncio.gather(*(one(s) for s in sents))
            chars[prov] += sum(len(x.strip()) for g in got for x in g["requests"])
            ttfa = [g["contexts"][0]["ttfa_ms"] for g in got if g["contexts"] and g["contexts"][0]["ttfa_ms"] is not None]
            trials.append({"concurrency": level, "n": len(ttfa), "p50_ms": round(statistics.median(ttfa), 1) if ttfa else None, "errors": sum(len(g["errors"]) for g in got)})
            if level == 1:
                continue
            base = trials[0]["p50_ms"]
            p50 = trials[-1]["p50_ms"]
            if base and p50 and p50 <= base * 1.10:
                chosen = level
                break
            chosen = max(1, level // 2)
        conc[prov] = {"service": service, "lang": lang, "trials": trials, "chosen": chosen}
    res["items"]["4_concurrency"] = conc

    # Item 5: quota.
    quota: dict[str, Any] = {"openai": "owner confirms", "sarvam": "owner confirms"}
    if fake:
        quota["elevenlabs"] = "fake"
    else:
        try:
            key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_TTS_KEY", "")
            async with aiohttp.ClientSession() as http:
                async with http.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key}) as resp:
                    body = await resp.json()
                    quota["elevenlabs"] = {
                        "status": resp.status,
                        "character_count": body.get("character_count"),
                        "character_limit": body.get("character_limit"),
                        "tier": body.get("tier"),
                        "next_reset": body.get("next_character_count_reset_unix"),
                    }
        except Exception as e:
            quota["elevenlabs"] = {"error": repr(e)}
    res["items"]["5_quota"] = quota

    # Item 6: disk and Redis.
    free = shutil.disk_usage(REPO).free
    start_live_redis(LIVE_REDIS_PORT)
    res["items"]["6_disk_redis"] = {
        "free_gb": round(free / 1e9, 1),
        "disk_pass": free >= 10e9,
        "redis_port": LIVE_REDIS_PORT,
        "redis_up": redis_running(LIVE_REDIS_PORT),
        "redis_databases": LIVE_REDIS_DBS,
    }

    # Item 7: network baseline.
    res["items"]["7_network"] = netcheck() if not fake else {"fake": True}
    res["chars"] = dict(chars)
    res["chars_total"] = sum(chars.values())
    res["budget"] = budget(live_runs(langs), offline)
    res["concurrency"] = {p: v["chosen"] for p, v in conc.items()}
    res["dropped_cells"] = [k for k, v in cov.items() if v["status"] != "ok"]
    (out / "preflight.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return res


# ---------------------------------------------------------------------------
# Command line.
# ---------------------------------------------------------------------------


def select_runs(langs: list[str], drop_cells: list[str], plan: str = "full") -> list[LiveRun]:
    drop = set(drop_cells)
    runs = {
        "full": live_runs(langs),
        "core": core_plan_runs(langs),
        "core-fault": core_plan_runs(langs, fault=True),
    }[plan]
    return [r for r in runs if f"{r.lang}/{r.service}" not in drop]


def rehearsal_faults(runs: list[LiveRun]) -> Any:
    """One of each sentence fault of 2.2, each on the first new sentence after turn 0 of a different run."""
    from fake_provider import FaultPlan

    plan = FaultPlan()
    chosen = {}
    picks = [("error", "openai_http", "A+B+C"), ("empty", "openai_http", "OFF"), ("429", "sarvam_ws_flush", "A"), ("stall", "sarvam_ws_flush", "A+B")]
    for fault, service, config in picks:
        run = next((r for r in runs if r.service == service and r.config == config and r.id not in chosen.values()), None)
        if run is None:
            continue
        corpus = corpus_for(run.lang, run.r, SEED)
        it = next(it for it in corpus.items if it.turn >= 1 and it.label == "new")
        plan.add(run.id, it.text, fault, attempt=1)
        chosen[fault] = run.id
    return plan, chosen


# ---------------------------------------------------------------------------
# U8, misconfiguration (section 10): each documented constraint wired wrongly,
# one at a time, against the fake service.
# ---------------------------------------------------------------------------

U8_SCENARIOS = ("correct", "text_filters", "reuse_context_id", "no_write", "wrong_voice", "sample_rate")
U8_SENTENCES = 80
U8_SIGNALS = ("fail_open", "reject", "skip", "capture_dropped")


async def _u8_session(
    scenario: str, corpus: LabelledCorpus, items: list[LabelledItem], store: Any, *, phase: int = 1
) -> dict[str, Any]:
    from dataclasses import replace as dc_replace

    from fake_provider import FakeTTSService
    from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

    kw: dict[str, Any] = {"sample_rate": SR, "reuse_context_id_within_turn": scenario == "reuse_context_id"}
    if scenario == "text_filters":
        kw["text_filters"] = [MarkdownTextFilter()]
    tts = rest_close(FakeTTSService)(cell="elevenlabs_dialogue_ws_c", lang="en", run=f"u8-{scenario}", speed=50.0, **kw)
    tts._settings.voice = "fake-voice-2" if (scenario == "wrong_voice" and phase == 2) else "fake-voice"
    scope = scope_from_service(tts, language="en-IN")
    if scenario == "wrong_voice" and phase == 1:
        scope = dc_replace(scope, voice="fake-voice-2")
    if scenario == "sample_rate":
        scope = dc_replace(scope, sample_rate=16_000)
    policy, matcher = config_policy("A+B+C")
    sink = RecordingSink()
    cache = TTSCache(scope, store, policy=policy, matcher=matcher, metrics=sink)
    await cache.start()
    frame_log: list[list[Any]] = []
    probe = Probe(frame_log)
    procs: list[Any] = [TTSCacheRead(cache, tts=tts), tts]
    if scenario != "no_write":
        procs.append(TTSCacheWrite(cache, tts=tts))
    procs.append(probe)
    task = PipelineTask(Pipeline(procs), params=PipelineParams(audio_out_sample_rate=SR), cancel_on_idle_timeout=False)
    runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    warnings: list[str] = []
    sink_id = logger.add(lambda m: warnings.append(str(m).strip()), level="WARNING")
    await asyncio.sleep(0.3)
    by_turn: dict[int, list[LabelledItem]] = defaultdict(list)
    for it in items:
        by_turn[it.turn].append(it)
    pairs: list[tuple[LabelledItem, Any]] = []
    try:
        for turn in by_turn.values():
            start = len(probe.order)
            await task.queue_frame(LLMFullResponseStartFrame())
            for it in turn:
                for tok in it.text.split(" "):
                    await task.queue_frame(LLMTextFrame(tok + " "))
                    await asyncio.sleep(0.002)
            await task.queue_frame(LLMFullResponseEndFrame())
            deadline = now() + 8.0
            while now() < deadline:
                ctxs = [probe.ctxs[c] for c in probe.order[start:]]
                if len(ctxs) >= len(turn) and all(c.t_stopped is not None for c in ctxs):
                    break
                await asyncio.sleep(0.02)
            pairs += list(zip(turn, [probe.ctxs[c] for c in probe.order[start:]], strict=False))
            await cache.drain(5.0)
    finally:
        logger.remove(sink_id)
        await cache.drain(5.0)
        await cache.close(timeout=5.0, close_store=False)
        await task.cancel()
        try:
            await asyncio.wait_for(runner, timeout=10)
        except Exception:
            pass
    return {
        "pairs": pairs,
        "contexts": len(probe.order),
        "events": sink.events,
        "warnings": warnings,
        "ms_per_char": tts.ms_per_char,
    }


def _u8_wrong(c: Any, item: LabelledItem, corpus: LabelledCorpus, ms_per_char: float, voice_wrong: bool) -> str | None:
    """Why a served clip is wrong, or None: another base's text, a label A+B+C may not serve, foreign audio."""
    from tts_cache.bench import synthetic_pcm
    from tts_cache.testplan.oracle import ALLOWED

    if voice_wrong:
        return "other_voice"
    spoken = (c.meta.get("spoken_text") or "").strip()
    same_base = {x.text.strip() for x in corpus.items if x.base == item.base}
    if spoken and spoken not in same_base:
        return "other_base"
    if item.label not in ALLOWED["A+B+C"]:
        return f"label_{item.label}"
    if c.sample_rate and c.sample_rate != SR:
        return "sample_rate"
    want = len(synthetic_pcm(spoken or item.text, SR, ms_per_char))
    if want and abs(len(c.audio) - want) / want > 0.15:
        return "audio_length"
    return None


async def u8(out: Path) -> dict[str, Any]:
    corpus = corpus_for("en", 0.5, SEED)
    items = corpus.items[:U8_SENTENCES]
    res: dict[str, Any] = {}
    for scenario in U8_SCENARIOS:
        with tempfile_dir() as tmp:
            shared = SQLiteStore(tmp / "u8.db")
            store = TieredStore(MemoryStore(), shared, deadline_ms=20)
            sessions = [await _u8_session(scenario, corpus, items, store)]
            if scenario == "wrong_voice":
                # The next deployment speaks with the voice the first one's scope named.
                sessions.append(await _u8_session(scenario, corpus, items, store, phase=2))
            await shared.close()
        wrong: list[dict[str, Any]] = []
        hits = served = chars = 0
        signals: dict[str, int] = defaultdict(int)
        warnings: list[str] = []
        for si, s in enumerate(sessions):
            for it, c in s["pairs"]:
                chars += len(it.text.strip())
                if c.hit:
                    hits += 1
                    served += len(it.text.strip())
                    why = _u8_wrong(c, it, corpus, s["ms_per_char"], scenario == "wrong_voice" and si == 1)
                    if why:
                        wrong.append({"session": si, "text": it.text, "spoken": c.meta.get("spoken_text"), "why": why})
            for e in s["events"]:
                if e["event"] in U8_SIGNALS:
                    signals[f"{e['event']}:{e.get('reason') or e.get('where') or ''}"] += 1
            warnings += s["warnings"]
        res[scenario] = {
            "contexts": sum(s["contexts"] for s in sessions),
            "sentences": sum(len(s["pairs"]) for s in sessions),
            "hits": hits,
            "char_hit_rate": served / chars if chars else 0.0,
            "wrong_serves": wrong[:20],
            "wrong_count": len(wrong),
            "signals": dict(signals),
            "warnings": warnings[:10],
            "named": bool(signals) or bool(warnings),
        }
    # Section 10: for every misconfiguration, no clip may be served wrongly. A scope built for the
    # wrong voice serves the first voice's audio to the next deployment, which the cache cannot
    # see; it is a wrong serve all the same, so it counts against the pass.
    res["pass"] = all(v["wrong_count"] == 0 for v in res.values() if isinstance(v, dict))
    res["failing"] = [k for k, v in res.items() if isinstance(v, dict) and v["wrong_count"]]
    res["note"] = "wrong_voice serves the first voice's audio by construction; the row records whether anything names it"
    d = out / "usability"
    d.mkdir(parents=True, exist_ok=True)
    (d / "u8.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return res


def tempfile_dir() -> Any:
    import tempfile

    class _Dir:
        def __enter__(self) -> Path:
            self._t = tempfile.TemporaryDirectory()
            return Path(self._t.name)

        def __exit__(self, *exc: Any) -> None:
            self._t.cleanup()

    return _Dir()


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "preflight", "run"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, required=name != "plan")
        p.add_argument("--langs", default=",".join(["en", "hi", "bn", "as", "pa", "gu", "mr", "or", "ta", "te", "kn", "ml"]))
        p.add_argument("--offline", type=Path, help="the offline matrix directory, for expected characters")
        p.add_argument("--fake", action="store_true", help="FakeTTSService in place of every provider (rehearsal)")
        p.add_argument("--drop", default="", help="comma list of lang/service cells dropped at preflight")
        p.add_argument(
            "--plan", choices=("full", "core", "core-fault"), default="full", help="the full plan or docs/evaluation/PLAN.md"
        )
    run_p = sub.choices["run"]
    run_p.add_argument("--max-chars", type=int, help="refuse to start when the worst case exceeds this")
    run_p.add_argument("--concurrency", default="", help="provider=n,... (default 8 each)")
    run_p.add_argument("--resume", action="store_true")
    run_p.add_argument("--faults", action="store_true", help="inject the sentence faults of 2.2 (with --fake)")
    run_p.add_argument("--crash-after-turns", type=int)
    run_p.add_argument("--kill-redis-after-turns", type=int)
    run_p.add_argument("--pause-redis-after-turns", type=int, help="CLIENT PAUSE the live Redis for 5 s")
    run_p.add_argument("--only", default="", help="comma list of run ids (debugging)")
    sub.choices["preflight"].add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    sub.add_parser("netcheck").add_argument("--out", type=Path)
    sub.add_parser("u8").add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "u8":
        logger.remove()
        logger.add(sys.stderr, level="ERROR")
        res = asyncio.run(u8(args.out))
        print(json.dumps({k: (v if not isinstance(v, dict) else {x: v[x] for x in ("hits", "wrong_count", "named", "signals")}) for k, v in res.items()}, indent=1))
        return 0
    if args.cmd == "netcheck":
        res = netcheck()
        print(json.dumps(res, indent=1))
        if args.out:
            args.out.write_text(json.dumps({"wall": time.time(), "network": res}, indent=1))
        return 0
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    langs = [x for x in args.langs.split(",") if x]
    drop = [x for x in args.drop.split(",") if x]
    runs = select_runs(langs, drop, args.plan)
    if args.cmd == "plan":
        b = budget(runs, args.offline)
        print(json.dumps({"runs": len(runs), "cells": len({(r.lang, r.service) for r in runs}), "budget": b}, indent=1))
        return 0
    if args.cmd == "preflight":
        if not args.fake:
            print("preflight makes live provider calls (about 10,000 characters)", file=sys.stderr)
        res = asyncio.run(preflight(args.out, langs, fake=args.fake, concurrency=args.concurrency, offline=args.offline))
        print(json.dumps({k: v for k, v in res.items() if k != "items"}, indent=1, default=str))
        return 0
    # run
    if args.only:
        only = set(args.only.split(","))
        runs = [r for r in runs if r.id in only]
    b = budget(runs, args.offline)
    if not args.fake:
        if args.max_chars is None:
            print("a live run needs --max-chars (the budget guard, 11.2)", file=sys.stderr)
            return 2
        if b["worst"] > args.max_chars:
            print(f"refusing to start: worst case {b['worst']} characters exceeds --max-chars {args.max_chars}", file=sys.stderr)
            return 3
    args.out.mkdir(parents=True, exist_ok=True)
    # One harness per live Redis: a second run on the same port would share databases by number
    # and its fault injections would hit the first (the offline matrix takes the same kind of lock).
    lock = port_lock(LIVE_REDIS_PORT)
    if lock is None:
        print(f"another live harness holds the Redis on port {LIVE_REDIS_PORT}", file=sys.stderr)
        return 5
    if not args.resume and (args.out / "rows").exists() and any((args.out / "rows").iterdir()):
        print(f"{args.out} already holds rows; pass --resume to continue that run", file=sys.stderr)
        return 4
    conc = {k: int(v) for k, v in (x.split("=") for x in args.concurrency.split(",") if x)}
    faults = None
    if args.faults:
        faults, chosen = rehearsal_faults(runs)
        faults.log = args.out / "faults_injected.jsonl"
        (args.out / "faults.json").write_text(json.dumps(chosen, indent=1))
    opts = Options(
        out=args.out,
        fake=args.fake,
        concurrency=conc,
        faults=faults,
        crash_after_turns=args.crash_after_turns if not args.resume else None,
        kill_redis_after_turns=args.kill_redis_after_turns if not args.resume else None,
        pause_redis_after_turns=args.pause_redis_after_turns if not args.resume else None,
    )
    env_path = args.out / ("env.json" if not args.resume else f"env.resume.{int(time.time())}.json")
    env_path.write_text(json.dumps({**environment(args.fake), "budget": b, "runs": [asdict(r) for r in runs]}, indent=1, default=str))
    t0 = time.time()
    summaries = asyncio.run(Harness(runs, opts).run_all())
    if faults is not None:
        (args.out / "faults_injected.json").write_text(json.dumps(faults.injected, indent=1, ensure_ascii=False))
    print(json.dumps({"runs": len(summaries), "errors": sum(1 for s in summaries if "error" in s), "wall_s": round(time.time() - t0, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
