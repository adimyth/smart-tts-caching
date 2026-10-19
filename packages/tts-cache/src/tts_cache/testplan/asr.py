"""ASR on every served clip of the live runs (TEST_PLAN.md 7.2), from saved data only.

Each distinct entry a live ON run served is decoded from that run's store
export (``live/exports/<run>.jsonl``) and transcribed once; every hit of it is
then scored by word error rate against both the text stored with the clip and
the text the sentence requested. The model is ``STT_MODEL`` (default
``gpt-4o-mini-transcribe``) and the normalisation is ``harness/transcribe.py``'s,
so the numbers compare with the attribution runs of TEST_REPORT.md.

    python -m tts_cache.testplan.asr <run dir> [--limit N] [--dry-run]

``--dry-run`` counts the clips and their audio seconds, for the cost estimate,
without calling the provider. Results go to ``<run dir>/asr/asr.jsonl``, one
line per hit, and transcripts are cached in ``asr/transcripts.jsonl`` so an
interrupted run resumes without paying twice.
"""

import argparse
import base64
import io
import json
import os
import sys
import unicodedata
import wave
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-4o-mini-transcribe"
# Indic transcripts often come back in a neighbouring script or with sandhi spellings
# (TEST_REPORT.md, attribution); a looser bound than English, as harness/transcribe.py uses.
WER_OK = {"en": 0.2}
WER_OK_DEFAULT = 0.4


def norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s.lower())
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith(("P", "S")))
    return " ".join(s.split())


def wer(ref: str, hyp: str) -> float:
    r, h = norm(ref).split(), norm(hyp).split()
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    return d[len(r)][len(h)] / max(1, len(r))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def served(live: Path) -> list[dict[str, Any]]:
    """Every hit of every live ON run, with the exported entry it names (None when missing)."""
    out = []
    for rows_path in sorted((live / "rows").glob("*.jsonl")):
        rows = _jsonl(rows_path)
        hits = [r for r in rows if r.get("hit")]
        if not hits:
            continue
        entries = {
            rec["entry"]["key"]: rec["entry"]
            for rec in _jsonl(live / "exports" / f"{rows_path.stem}.jsonl")
            if "entry" in rec
        }
        for r in hits:
            out.append({"row": r, "entry": entries.get(r.get("key") or "")})
    return out


def run(run_dir: Path, *, limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    from tts_cache import codec

    live = run_dir / "live"
    out_dir = run_dir / "asr"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "transcripts.jsonl"
    transcripts = {t["key"]: t for t in _jsonl(cache_path)}
    items = served(live)
    distinct: dict[str, dict[str, Any]] = {}
    for it in items:
        e = it["entry"]
        if e is not None:
            distinct.setdefault(e["key"], e)
    seconds = sum(e.get("duration_ms", 0) for e in distinct.values()) / 1000
    if dry_run:
        return {"hits": len(items), "clips": len(distinct), "audio_s": round(seconds, 1)}
    model = os.environ.get("STT_MODEL", DEFAULT_MODEL)
    client = None
    todo = [k for k in distinct if k not in transcripts]
    if limit is not None:
        todo = todo[:limit]
    if todo:
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    with open(cache_path, "a", encoding="utf-8") as cache:
        for key in todo:
            e = distinct[key]
            pcm = codec.decode(base64.b64decode(e["audio"]), e["codec"], e["sample_rate"], e["channels"])
            lang = str(e.get("language") or "en").split("-")[0]
            f = io.BytesIO(_wav(pcm, e["sample_rate"], e["channels"]))
            f.name = "clip.wav"
            assert client is not None
            tr = client.audio.transcriptions.create(model=model, file=f, language=lang)
            rec = {"key": key, "text": tr.text, "model": model, "language": lang}
            transcripts[key] = rec
            cache.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cache.flush()
    rows = []
    for it in items:
        r, e = it["row"], it["entry"]
        t = transcripts.get(e["key"]) if e is not None else None
        lang = r["lang"]
        rec: dict[str, Any] = {
            "run": r["run"],
            "lang": lang,
            "config": r["config"],
            "index": r["index"],
            "label": r["label"],
            "level": r.get("level"),
            "key": r.get("key"),
            "requested": r["text"],
            "stored": None if e is None else e.get("text"),
            "heard": None if t is None else t["text"],
        }
        if t is not None and e is not None:
            rec["wer_stored"] = round(wer(e.get("text") or "", t["text"]), 4)
            rec["wer_requested"] = round(wer(r["text"], t["text"]), 4)
            rec["ok"] = rec["wer_stored"] <= WER_OK.get(lang, WER_OK_DEFAULT)
        rows.append(rec)
    (out_dir / "asr.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8"
    )
    return {
        "hits": len(rows),
        "clips": len(distinct),
        "transcribed": sum(1 for k in distinct if k in transcripts),
        "audio_s": round(seconds, 1),
        "model": model,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tts_cache.testplan.asr", description=__doc__)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[5] / ".env")
    except ImportError:
        pass
    print(json.dumps(run(a.run_dir, limit=a.limit, dry_run=a.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
