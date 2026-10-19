"""Live round trip through the cache: one turn, twice, through a real Pipecat TTS service.

Pipeline: [TTSCacheRead, service, TTSCacheWrite, probe] with a SQLiteStore,
the four-sentence turn of attribution.py streamed token by token. Run 1 starts
from an empty store and captures every sentence; run 2 builds a fresh service
and cache on the same file. Checks:

- run 2 makes zero provider requests (counted with ``on_tts_request``);
- run 2 plays byte-identical audio per sentence. The store holds FLAC here, the
  lossless codec option, so the replayed bytes can be compared with the live
  ones; each captured clip is also re-encoded to Opus offline and its decode
  checked for length and waveform correlation;
- the transcript frames are identical. For services that emit one
  ``TTSTextFrame`` per sentence they must match exactly. ElevenLabs services
  emit per-word frames with word timestamps, which a hit replaces with one
  sentence-level frame (SOLUTION.md 11.7); for those, run 2's frame must equal
  the keyed text and the run 1 words are reported next to it.

Usage:
    python harness/cache_roundtrip.py openai_http --lang en
    python harness/cache_roundtrip.py all --lang hi --quiet
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import numpy as np
from dotenv import load_dotenv
from loguru import logger

import attribution as A
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from tts_cache import SQLiteStore, TTSCache, codec
from tts_cache.metrics import LogSink
from tts_cache.policy import AlwaysCache
from tts_cache_pipecat import TTSCacheRead, TTSCacheWrite, capture_mode_for, scope_from_service

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "roundtrip"
SERVICES = [
    "openai_http",
    "elevenlabs_http",
    "elevenlabs_ws_fc",
    "elevenlabs_dialogue_ws_c",
    "sarvam_http",
    "sarvam_ws_flush",
]
WORD_TIMESTAMPS = {"elevenlabs_http", "elevenlabs_ws_fc", "elevenlabs_dialogue_ws_c"}
SR = 24000
WAIT_SECS = 40.0


@dataclass
class Ctx:
    context_id: str
    hit: bool = False
    audio: bytearray = field(default_factory=bytearray)
    texts: list[str] = field(default_factory=list)
    stopped: bool = False
    t_first_audio: float | None = None


class Probe(FrameProcessor):
    def __init__(self, n_expected: int):
        super().__init__(name="probe")
        self.ctxs: dict[str, Ctx] = {}
        self.order: list[str] = []
        self.errors: list[str] = []
        self.n_expected = n_expected
        self.done = asyncio.Event()

    def _ctx(self, cid: str) -> Ctx:
        if cid not in self.ctxs:
            self.ctxs[cid] = Ctx(cid)
            self.order.append(cid)
        return self.ctxs[cid]

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        cid = getattr(frame, "context_id", None)
        if isinstance(frame, TTSStartedFrame) and cid:
            self._ctx(cid).hit = bool(frame.metadata.get("tts_cache", {}).get("hit"))
        elif isinstance(frame, TTSAudioRawFrame) and cid:
            c = self._ctx(cid)
            if c.t_first_audio is None and frame.audio:
                c.t_first_audio = time.monotonic()
            c.audio.extend(frame.audio)
        elif isinstance(frame, TTSTextFrame) and cid:
            self._ctx(cid).texts.append(frame.text)
        elif isinstance(frame, TTSStoppedFrame) and cid:
            self._ctx(cid).stopped = True
            if sum(c.stopped for c in self.ctxs.values()) >= self.n_expected:
                self.done.set()
        elif isinstance(frame, ErrorFrame):
            self.errors.append(frame.error)
        await self.push_frame(frame, direction)


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


async def run_once(name: str, lang: str, db: Path) -> dict:
    A.LANG = lang
    sentences = A.SENTENCES_BY_LANG[lang]
    async with aiohttp.ClientSession() as session:
        tts = A.build_service(name, SR, session)
        sink = LogSink(log=None, keep=200)
        cache = TTSCache(
            scope_from_service(tts, language=f"{lang}-IN"),
            SQLiteStore(db),
            policy=AlwaysCache("1d"),
            metrics=sink,
            codec="flac",
        )
        await cache.start()
        requests: list[str] = []

        async def on_tts_request(_tts, context_id: str, text: str):
            requests.append(text)

        tts.add_event_handler("on_tts_request", on_tts_request)
        probe = Probe(len(sentences))
        read = TTSCacheRead(cache, tts=tts)
        write = TTSCacheWrite(cache, tts=tts)
        task = PipelineTask(
            Pipeline([read, tts, write, probe]),
            params=PipelineParams(audio_out_sample_rate=SR),
            cancel_on_idle_timeout=False,
        )
        runner = PipelineRunner(handle_sigint=False)
        run = asyncio.create_task(runner.run(task))
        t0 = time.monotonic()
        await asyncio.sleep(0.5)  # let a websocket service connect
        await task.queue_frame(LLMFullResponseStartFrame())
        # t_release[i]: when Pipecat's aggregator can release sentence i, which is
        # the moment the first token of sentence i+1 (or the end frame) is queued.
        t_release: list[float] = []
        for i, s in enumerate(sentences):
            for j, tok in enumerate(s.split(" ")):
                if j == 0 and i > 0:
                    t_release.append(time.monotonic())
                await task.queue_frame(TextFrame(tok + " "))
                await asyncio.sleep(A.TOKEN_GAP_SECS)
        t_release.append(time.monotonic())
        await task.queue_frame(LLMFullResponseEndFrame())
        try:
            await asyncio.wait_for(probe.done.wait(), timeout=WAIT_SECS)
            timed_out = False
        except TimeoutError:
            timed_out = True
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.3)
        await cache.close(timeout=10.0)
        await task.cancel()
        try:
            await asyncio.wait_for(run, timeout=10)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"runner did not finish cleanly: {e}")
    ctxs = [probe.ctxs[c] for c in probe.order]
    return {
        "mode": {"read": read.mode, "write": write.mode, "capture_mode": capture_mode_for(tts)},
        "requests": requests,
        "timed_out": timed_out,
        "elapsed_s": round(elapsed, 2),
        "errors": probe.errors,
        "events": dict(sink.counts),
        "contexts": [
            {
                "hit": c.hit,
                "bytes": len(c.audio),
                "digest": digest(bytes(c.audio)),
                "texts": c.texts,
                "stopped": c.stopped,
                "ttfa_ms": round((c.t_first_audio - t_release[i]) * 1000, 1)
                if c.t_first_audio is not None and i < len(t_release)
                else None,
            }
            for i, c in enumerate(ctxs)
        ],
        "audio": [bytes(c.audio) for c in ctxs],
    }


def opus_check(pcm: bytes) -> dict:
    if not pcm:
        return {"opus_bytes": 0, "same_length": False, "corr": 0.0, "note": "no audio"}
    data = codec.encode(pcm, SR, 1, "opus")
    out = codec.decode(data, "opus", SR, 1)
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    b = np.frombuffer(out, dtype="<i2").astype(np.float64)
    corr = float(np.corrcoef(a, b)[0, 1]) if len(a) == len(b) and len(a) else 0.0
    return {"opus_bytes": len(data), "same_length": len(out) == len(pcm), "corr": round(corr, 4)}


def norm(s: str) -> str:
    return " ".join(s.split())


async def roundtrip(name: str, lang: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    db = OUT / f"{name}_{lang}.db"
    for p in OUT.glob(f"{name}_{lang}.db*"):
        p.unlink()
    r1 = await run_once(name, lang, db)
    r2 = await run_once(name, lang, db)
    sentences = A.SENTENCES_BY_LANG[lang]
    notes: list[str] = []
    n = len(sentences)
    if r1["mode"]["capture_mode"] != "exact":
        notes.append(f"capture mode {r1['mode']['capture_mode']}")
    if len(r1["requests"]) != n:
        notes.append(f"run 1 made {len(r1['requests'])} provider requests, expected {n}")
    if r1["events"].get("store", 0) != n:
        rejects = {k: v for k, v in r1["events"].items() if k.startswith(("reject", "capture_dropped"))}
        notes.append(f"run 1 stored {r1['events'].get('store', 0)} of {n} clips {rejects}")
    if r2["requests"]:
        notes.append(f"run 2 made {len(r2['requests'])} provider requests: {r2['requests']}")
    c1, c2 = r1["contexts"], r2["contexts"]
    if len(c1) != n or len(c2) != n:
        notes.append(f"contexts: run 1 {len(c1)}, run 2 {len(c2)}, expected {n}")
    if not all(c["hit"] for c in c2):
        notes.append("run 2 did not serve every sentence from the cache")
    identical = [a == b for a, b in zip(r1["audio"], r2["audio"], strict=False)]
    if not (len(identical) == n and all(identical)):
        notes.append(f"audio identical per sentence: {identical}")
    transcript = []
    for i, (a, b) in enumerate(zip(c1, c2, strict=False)):
        keyed = sentences[i].strip()
        if name in WORD_TIMESTAMPS:
            ok = b["texts"] == [keyed]
            transcript.append({"run1_words": a["texts"], "run2": b["texts"], "run2_is_keyed_text": ok})
            if not ok:
                notes.append(f"sentence {i}: run 2 transcript {b['texts']} is not the keyed text {keyed!r}")
        else:
            ok = a["texts"] == b["texts"] == [keyed]
            transcript.append({"run1": a["texts"], "run2": b["texts"], "identical": ok})
            if not ok:
                notes.append(f"sentence {i}: transcripts differ: {a['texts']} vs {b['texts']}")
    for r, label in ((r1, "run 1"), (r2, "run 2")):
        if r["errors"]:
            notes.append(f"{label} errors: {r['errors']}")
        if r["timed_out"]:
            notes.append(f"{label} timed out waiting for stop frames")
        if r["events"].get("fail_open"):
            notes.append(f"{label} fail_open events: {r['events']['fail_open']}")
    report = {
        "service": name,
        "lang": lang,
        "verdict": "PASS" if not notes else "FAIL",
        "notes": notes,
        "run1": {k: v for k, v in r1.items() if k != "audio"},
        "run2": {k: v for k, v in r2.items() if k != "audio"},
        "audio_identical": identical,
        "transcript": transcript,
        "opus_offline": [opus_check(a) for a in r1["audio"]],
    }
    (OUT / f"{name}_{lang}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


async def main():
    load_dotenv(ROOT.parent / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("service", choices=[*SERVICES, "all"])
    ap.add_argument("--lang", choices=["en", "hi", "ta"], default="en")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.quiet:
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    A.TAP_SARVAM = False
    names = SERVICES if args.service == "all" else [args.service]
    summary = []
    for n in names:
        try:
            rep = await roundtrip(n, args.lang)
        except Exception as e:  # noqa: BLE001
            rep = {"service": n, "lang": args.lang, "verdict": "ERROR", "notes": [repr(e)]}
        summary.append({k: rep.get(k) for k in ("service", "lang", "verdict", "notes")})
        print(f"\n=== {n} [{args.lang}] -> {rep['verdict']}")
        for note in rep["notes"]:
            print(f"  ! {note}")
        if "run1" in rep:
            print(
                f"  run 1: {len(rep['run1']['requests'])} requests, {rep['run1']['events'].get('store', 0)} stored, {rep['run1']['elapsed_s']} s"
            )
            print(
                f"  run 2: {len(rep['run2']['requests'])} requests, hits {[c['hit'] for c in rep['run2']['contexts']]}, {rep['run2']['elapsed_s']} s"
            )
            print(
                f"  audio identical {rep['audio_identical']}; opus corr {[o['corr'] for o in rep['opus_offline']]}"
            )
    (OUT / f"summary_{args.lang}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
