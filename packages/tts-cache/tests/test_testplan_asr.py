"""ASR over saved live data (TEST_PLAN.md 7.2), with the transcription client stubbed out."""

import base64
import json
import sys
import types
from pathlib import Path

import pytest

from tts_cache import codec
from tts_cache.bench import synthetic_pcm
from tts_cache.testplan import asr


def test_wer_normalises_case_and_punctuation() -> None:
    assert asr.wer("Hello, there!", "hello there") == 0.0
    assert asr.wer("your refund is ready", "your refund is not ready") == pytest.approx(0.25)


def _run_dir(tmp: Path) -> Path:
    live = tmp / "live"
    (live / "rows").mkdir(parents=True)
    (live / "exports").mkdir()
    pcm = synthetic_pcm("Your refund is ready.", 24_000)
    entry = {
        "key": "k1",
        "audio": base64.b64encode(codec.encode(pcm, 24_000, 1, "flac")).decode(),
        "codec": "flac",
        "sample_rate": 24_000,
        "channels": 1,
        "duration_ms": 1000,
        "text": "Your refund is ready.",
        "language": "en-IN",
    }
    (live / "exports" / "en_x_A+B_0.5.jsonl").write_text(json.dumps({"entry": entry}) + "\n")
    common = {"run": "en_x_A+B_0.5", "lang": "en", "config": "A+B", "key": "k1"}
    rows = [
        {**common, "index": 0, "label": "new", "hit": False, "text": "Your refund is ready."},
        {
            **common,
            "index": 5,
            "label": "punct_inner",
            "hit": True,
            "level": "B",
            "text": "Your refund, is ready.",
        },
    ]
    (live / "rows" / "en_x_A+B_0.5.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp


def test_dry_run_counts_without_calling(tmp_path: Path) -> None:
    got = asr.run(_run_dir(tmp_path), dry_run=True)
    assert got == {"hits": 1, "clips": 1, "audio_s": 1.0}


def test_run_scores_against_stored_and_requested_text(tmp_path: Path, monkeypatch) -> None:
    calls = []

    class Transcriptions:
        def create(self, **kw):
            calls.append(kw)
            return types.SimpleNamespace(text="your refund is ready")

    class OpenAI:
        def __init__(self, **kw):
            self.audio = types.SimpleNamespace(transcriptions=Transcriptions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=OpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    d = _run_dir(tmp_path)
    got = asr.run(d)
    assert got["transcribed"] == 1 and len(calls) == 1 and calls[0]["language"] == "en"
    rec = json.loads((d / "asr" / "asr.jsonl").read_text().splitlines()[0])
    assert rec["wer_stored"] == 0.0 and rec["wer_requested"] == 0.0 and rec["ok"]
    # A second run reuses the cached transcript and pays nothing.
    asr.run(d)
    assert len(calls) == 1
