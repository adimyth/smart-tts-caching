"""A fake TTS provider for the rehearsal of TEST_PLAN.md 2.2: no network, no spend.

``FakeTTSService`` is a Pipecat ``TTSService`` that behaves like one live cell
of section 11.1. Its time to first byte is drawn around that cell's first
sentence in TEST_REPORT.md's live table, its audio is as long per character as the
cell's real clips were (``harness/out/roundtrip``), and it streams that audio
at four times real time in 40 ms chunks, as the providers do. Languages with
no live measurement take Hindi's numbers, the closest measured script.

Faults are injected per request from a shared :class:`FaultPlan`, keyed by run
and sentence text, so the harness can make the n-th attempt at a sentence
fail in a chosen way:

- ``error``: an ``ErrorFrame`` and no audio (a provider 5xx);
- ``empty``: a response with no audio (the OpenAI Hindi case of TEST_REPORT.md);
- ``429``: an ``ErrorFrame`` carrying status 429;
- ``stall``: no audio for ``stall_s`` seconds, longer than the harness's 15 s stall limit.

The harness crash and the Redis kill are injected by the harness itself.
"""

import asyncio
import json
import math
import random
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from tts_cache.bench import synthetic_pcm
from tts_cache_pipecat.compat import register_service

SR = 24_000
CHUNK_MS = 40
SPEED = 4.0  # audio delivered per second of wall clock, in seconds

# TEST_REPORT.md, "Live: time to first audio, provider vs cache hit": the first sentence of the turn, ms.
TTFB_MS: dict[tuple[str, str], float] = {
    ("elevenlabs_dialogue_ws_c", "en"): 131, ("elevenlabs_dialogue_ws_c", "hi"): 134,
    ("elevenlabs_dialogue_ws_c", "ta"): 133,
    ("sarvam_ws_flush", "en"): 172, ("sarvam_ws_flush", "hi"): 218, ("sarvam_ws_flush", "ta"): 201,
    ("elevenlabs_ws_fc", "en"): 285, ("elevenlabs_ws_fc", "hi"): 323, ("elevenlabs_ws_fc", "ta"): 310,
    ("elevenlabs_http", "en"): 344, ("elevenlabs_http", "hi"): 417, ("elevenlabs_http", "ta"): 548,
    ("sarvam_http", "en"): 976, ("sarvam_http", "hi"): 2438, ("sarvam_http", "ta"): 654,
    ("openai_http", "en"): 1844, ("openai_http", "hi"): 1727, ("openai_http", "ta"): 1718,
}  # fmt: skip
# Median audio per character of each cell's live clips (harness/out/roundtrip, run 1), ms.
MS_PER_CHAR: dict[tuple[str, str], float] = {
    ("elevenlabs_dialogue_ws_c", "en"): 58.4, ("elevenlabs_dialogue_ws_c", "hi"): 80.0,
    ("elevenlabs_dialogue_ws_c", "ta"): 69.4,
    ("elevenlabs_http", "en"): 64.1, ("elevenlabs_http", "hi"): 90.4, ("elevenlabs_http", "ta"): 74.3,
    ("elevenlabs_ws_fc", "en"): 62.6, ("elevenlabs_ws_fc", "hi"): 75.7, ("elevenlabs_ws_fc", "ta"): 77.0,
    ("openai_http", "en"): 73.0, ("openai_http", "hi"): 107.6, ("openai_http", "ta"): 86.9,
    ("sarvam_http", "en"): 35.4, ("sarvam_http", "hi"): 55.2, ("sarvam_http", "ta"): 49.3,
    ("sarvam_ws_flush", "en"): 43.3, ("sarvam_ws_flush", "hi"): 52.2, ("sarvam_ws_flush", "ta"): 51.8,
}  # fmt: skip
TTFB_SIGMA = 0.25
FAULTS = ("error", "empty", "429", "stall")


def cell_numbers(service: str, lang: str) -> tuple[float, float]:
    """Time to first byte and audio ms per character for a cell; Hindi's where none was measured."""
    key = (service, lang) if (service, lang) in TTFB_MS else (service, "hi")
    return TTFB_MS[key], MS_PER_CHAR[key]


@dataclass
class FaultPlan:
    """Faults by (run id, sentence text, attempt number from 1); every request is counted."""

    faults: dict[tuple[str, str, int], str] = field(default_factory=dict)
    attempts: dict[tuple[str, str], int] = field(default_factory=dict)
    injected: list[dict[str, Any]] = field(default_factory=list)
    # Each injection is appended here as it happens, so a harness crash cannot lose the record.
    log: Path | None = None

    def add(self, run: str, text: str, fault: str, attempt: int = 1) -> None:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}")
        self.faults[(run, text.strip(), attempt)] = fault

    def take(self, run: str, text: str) -> str | None:
        k = (run, text.strip())
        self.attempts[k] = self.attempts.get(k, 0) + 1
        fault = self.faults.get((run, text.strip(), self.attempts[k]))
        if fault:
            rec = {"run": run, "text": text.strip(), "attempt": self.attempts[k], "fault": fault}
            self.injected.append(rec)
            if self.log is not None:
                with open(self.log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({**rec, "wall": time.time()}, ensure_ascii=False) + "\n")
        return fault


class FakeTTSService(TTSService):
    """A REST-like provider standing in for one live cell; see the module docstring."""

    def __init__(
        self,
        *,
        cell: str,
        lang: str,
        run: str = "",
        faults: FaultPlan | None = None,
        seed: int = 1,
        stall_s: float = 20.0,
        speed: float = SPEED,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("reuse_context_id_within_turn", False)
        kwargs.setdefault("sample_rate", SR)
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(model=f"fake-{cell}", voice="fake-voice", language=None),
            **kwargs,
        )
        self.cell = cell
        self.lang = lang
        self.run = run
        self.faults = faults
        self.stall_s = stall_s
        self.speed = speed
        self.ttfb_ms, self.ms_per_char = cell_numbers(cell, lang)
        self._rng = random.Random(f"{cell}/{lang}/{run}/{seed}")
        self.requests = 0
        self.last_headers: dict[str, str] = {}

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        self.requests += 1
        self.last_headers = {"x-request-id": f"fake-{self.run}-{self.requests}", "x-fake-cell": self.cell}
        fault = self.faults.take(self.run, text) if self.faults is not None else None
        ttfb = self.ttfb_ms * math.exp(self._rng.gauss(0.0, TTFB_SIGMA)) / 1000
        if fault == "stall":
            await asyncio.sleep(self.stall_s)
        await asyncio.sleep(ttfb)
        if fault == "error":
            yield ErrorFrame(error="Error getting audio (status: 500, error: fake provider error)")
            return
        if fault == "429":
            yield ErrorFrame(error="Error getting audio (status: 429, error: Too Many Requests)")
            return
        if fault == "empty":
            return
        await self.start_tts_usage_metrics(text)
        pcm = synthetic_pcm(text, SR, self.ms_per_char)
        step = SR * CHUNK_MS // 1000 * 2
        for i in range(0, len(pcm), step):
            yield TTSAudioRawFrame(pcm[i : i + step], SR, 1, context_id=context_id)
            await asyncio.sleep(CHUNK_MS / 1000 / self.speed)


register_service(FakeTTSService, "fake", (), capture_mode="exact")
