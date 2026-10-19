"""``tts-cache-pipecat bench``: the synthetic benchmark in the Pipecat host (SOLUTION.md 9.3).

The corpus of ``tts-cache bench`` runs turn by turn through a real pipeline,
``[TTSCacheRead, simulated service, TTSCacheWrite, probe]``, or through the
service alone with the cache off. The simulated service behaves like a REST
provider and sleeps its sampled time to first byte, so times are real. The
report adds what the core bench cannot see: the read side's own time per
sentence (filters, prediction, lookup and pushing the frames), which is the
adapter's overhead, and time to first audio measured at the pipeline's end.
"""

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import run_test

from tts_cache import MemoryStore, SQLiteStore, TieredStore, TTSCache
from tts_cache.bench import SR, synthetic_pcm
from tts_cache.corpus import Corpus, generate
from tts_cache.metrics import LogSink
from tts_cache_pipecat.compat import register_service, scope_from_service
from tts_cache_pipecat.read import TTSCacheRead
from tts_cache_pipecat.write import TTSCacheWrite


class SimulatedTTSService(TTSService):
    """A REST-like service: sleeps a lognormal time to first byte, then yields audio derived from the text."""

    def __init__(
        self, *, ttfb_median_ms: float = 250.0, ttfb_sigma: float = 0.5, seed: int = 7, **kwargs: Any
    ):
        kwargs.setdefault("reuse_context_id_within_turn", False)
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            # A REST context ends when no audio arrives for this long, so it must
            # outlast the slowest time to first byte (Pipecat's default is 3 s).
            stop_frame_timeout_s=max(0.1, 8 * ttfb_median_ms / 1000),
            sample_rate=SR,
            settings=TTSSettings(model="sim-1", voice="v1", language=None),
            **kwargs,
        )
        self.ttfb_median_ms = ttfb_median_ms
        self.ttfb_sigma = ttfb_sigma
        self._rng = random.Random(seed)
        self.chars = 0

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        self.chars += len(text.strip())
        await asyncio.sleep(self.ttfb_median_ms * math.exp(self._rng.gauss(0.0, self.ttfb_sigma)) / 1000)
        pcm = synthetic_pcm(text)
        step = SR * 40 // 1000 * 2
        for i in range(0, len(pcm), step):
            yield TTSAudioRawFrame(pcm[i : i + step], SR, 1, context_id=context_id)


register_service(SimulatedTTSService, "simulated", (), capture_mode="exact")


class _FirstAudio(FrameProcessor):
    def __init__(self) -> None:
        super().__init__(name="first-audio")
        self.t0 = 0.0
        self.contexts: dict[str, dict[str, Any]] = {}

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        cid = getattr(frame, "context_id", None)
        if isinstance(frame, TTSStartedFrame) and cid:
            self.contexts.setdefault(cid, {"hit": bool(frame.metadata.get("tts_cache", {}).get("hit"))})
        elif isinstance(frame, TTSAudioRawFrame) and cid:
            c = self.contexts.setdefault(cid, {"hit": False})
            c.setdefault("first_audio_ms", (time.perf_counter() - self.t0) * 1000)
        await self.push_frame(frame, direction)


def _turn_frames(turn: Sequence[str]) -> list[Frame]:
    frames: list[Frame] = [LLMFullResponseStartFrame()]
    for s in turn:
        frames += [LLMTextFrame(w + " ") for w in s.split(" ")]
    frames.append(LLMFullResponseEndFrame())
    return frames


def _q(xs: Sequence[float], q: float) -> float:
    if not xs:
        return 0.0
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


async def run_pipecat(
    corpus: Corpus, r: float, *, cache_on: bool, ttfb_median_ms: float, ttfb_sigma: float, workdir: Path
) -> dict[str, Any]:
    sink = LogSink(log=None)
    cache: TTSCache | None = None
    read_ms: list[float] = []
    read_hit_ms: list[float] = []
    turn_ttfa: list[float] = []
    contexts: list[dict[str, Any]] = []
    billed = 0
    rng_seed = 7
    for i, turn in enumerate(corpus.turns):
        tts = SimulatedTTSService(ttfb_median_ms=ttfb_median_ms, ttfb_sigma=ttfb_sigma, seed=rng_seed + i)
        probe = _FirstAudio()
        if cache_on:
            if cache is None:
                store = TieredStore(
                    MemoryStore(), SQLiteStore(workdir / f"pipecat-{r}.db", sweep_interval_s=0)
                )
                cache = TTSCache(scope_from_service(tts, language=corpus.language), store, metrics=sink)
                await cache.start()
            read = TTSCacheRead(cache, tts=tts)
            inner = read._sentence

            async def timed(sentence: str, inner=inner, read=read) -> None:
                t = time.perf_counter()
                await inner(sentence)
                # A hit's time is mostly its clip decoding into frames; a miss's is pure overhead.
                (read_hit_ms if read._emitted else read_ms).append((time.perf_counter() - t) * 1000)

            read._sentence = timed  # type: ignore[method-assign]
            pipeline = Pipeline([read, tts, TTSCacheWrite(cache, tts=tts), probe])
        else:
            pipeline = Pipeline([tts, probe])
        probe.t0 = time.perf_counter()
        await run_test(
            pipeline,
            frames_to_send=_turn_frames(turn),
            pipeline_params=PipelineParams(audio_out_sample_rate=SR),
        )
        billed += tts.chars
        ctxs = [c for c in probe.contexts.values() if "first_audio_ms" in c]
        contexts += ctxs
        if ctxs:
            turn_ttfa.append(min(c["first_audio_ms"] for c in ctxs))
        if cache is not None:
            await cache.drain(10.0)
    if cache is not None:
        await cache.close()
    chars = sum(len(s.strip()) for s in corpus.sentences)
    hit_ttfa = [c["first_audio_ms"] for c in contexts if c["hit"]]
    return {
        "host": "pipecat",
        "r": r,
        "cache": "on" if cache_on else "off",
        "sentences": len(corpus.sentences),
        "billed_chars": billed,
        "hit_rate": round(1 - billed / chars, 4) if chars else 0.0,
        "turn_ttfa_p50_ms": round(_q(turn_ttfa, 0.5), 2),
        "hit_turn_start_ttfa_p50_ms": round(_q(hit_ttfa, 0.5), 2),
        "read_side_p50_ms": round(_q(read_ms, 0.5), 4),
        "read_side_p99_ms": round(_q(read_ms, 0.99), 4),
        "read_side_mean_ms": round(statistics.fmean(read_ms), 4) if read_ms else 0.0,
        "read_side_hit_p50_ms": round(_q(read_hit_ms, 0.5), 4),
        "stored": sink.counts["store"],
    }


async def sweep(
    r_values: Sequence[float], *, n: int, seed: int, language: str, ttfb_median_ms: float, ttfb_sigma: float
) -> list[dict[str, Any]]:
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for r in r_values:
            corpus = generate(n, r, seed=seed, language=language)
            for on in (False, True):
                rows.append(
                    await run_pipecat(
                        corpus,
                        r,
                        cache_on=on,
                        ttfb_median_ms=ttfb_median_ms,
                        ttfb_sigma=ttfb_sigma,
                        workdir=Path(tmp),
                    )
                )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tts-cache-pipecat", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("bench", help="the synthetic benchmark through a real Pipecat pipeline")
    p.add_argument("--r", default="0,0.5")
    p.add_argument("-n", type=int, default=100)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--language", default="en-US")
    p.add_argument("--ttfb-median-ms", type=float, default=250.0)
    p.add_argument("--ttfb-sigma", type=float, default=0.5)
    p.add_argument("--out", help="write the rows as JSON here")
    args = ap.parse_args(argv)
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    rows = asyncio.run(
        sweep(
            tuple(float(x) for x in args.r.split(",")),
            n=args.n,
            seed=args.seed,
            language=args.language,
            ttfb_median_ms=args.ttfb_median_ms,
            ttfb_sigma=args.ttfb_sigma,
        )
    )
    cols = [
        "r",
        "cache",
        "billed_chars",
        "hit_rate",
        "turn_ttfa_p50_ms",
        "read_side_p50_ms",
        "read_side_p99_ms",
    ]
    cols.append("read_side_hit_p50_ms")
    print(" ".join(f"{c:>16}" for c in cols))
    for row in rows:
        print(" ".join(f"{row[c]!s:>16}" for c in cols))
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
