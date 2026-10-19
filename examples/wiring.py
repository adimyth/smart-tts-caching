"""The wiring of SOLUTION.md section 11.1, run against the fake TTS service.

The cache, policy, processors and pipeline are built with the same calls as
section 11.1. Three substitutions make it runnable without a provider or a
transport: the fake service from the adapter's tests stands in for
``SarvamTTSService(...)``, ``SQLiteStore`` stands in for the Redis store (the
one-process alternative 11.1 names), and Pipecat's test runner plays the
transport, STT and LLM by streaming a scripted LLM turn into the pipeline.
The service is constructed first because the scope is read from it.

Run: .venv/bin/python examples/wiring.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "tts-cache-pipecat" / "tests"))
sys.path.insert(0, str(HERE.parent / "packages" / "tts-cache" / "tests"))

from fake_tts import FakeTTSService  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter  # noqa: E402
from pipeline_utils import contexts, run_frames, turn  # noqa: E402

from tts_cache import MemoryStore, SQLiteStore, TieredStore, TTSCache  # noqa: E402
from tts_cache.policy import AllOf, MinChars, Probation, SkipIfMatches  # noqa: E402
from tts_cache_pipecat import TTSCacheRead, TTSCacheWrite, scope_from_service  # noqa: E402

PII_PATTERNS = [r"\b\d{6,}\b"]


async def expand_currency(text: str, text_type: str) -> str:
    return text.replace("₹", "rupees ")


async def main() -> dict[str, int]:
    db = Path(tempfile.mkdtemp()) / "tts-cache.db"
    requests = []
    for _ in range(2):
        # Any TTSService. Construct it with reuse_context_id_within_turn=False and without text_filters;
        # text_transforms stay on the service. Section 11.3 explains both.
        tts = FakeTTSService(reuse_context_id_within_turn=False, text_transforms=[("*", expand_currency)])

        cache = TTSCache(
            scope=scope_from_service(tts, language="en-US"),
            store=TieredStore(MemoryStore(warm_start_bytes=4 << 20), SQLiteStore(db), deadline_ms=20),
            policy=AllOf(Probation("48h", "30d"), MinChars(3, 400), SkipIfMatches(PII_PATTERNS)),
        )
        await cache.start()

        pipeline = Pipeline(
            [
                TTSCacheRead(cache, tts=tts, text_filters=[MarkdownTextFilter()], tags={"tenant": "acme"}),
                tts,
                TTSCacheWrite(cache, tts=tts),  # registers an on_tts_request handler on tts
            ]
        )
        frames = await run_frames(pipeline, turn("**Welcome** to Acme.", "Your balance is ₹500."))
        spoken = contexts(frames)
        assert [s.texts for s in spoken] == [["Welcome to Acme."], ["Your balance is ₹500."]]
        requests.append(len(tts.synthesized))
        await cache.close()
    return {"first_run_provider_requests": requests[0], "second_run_provider_requests": requests[1]}


if __name__ == "__main__":
    print(asyncio.run(main()))
