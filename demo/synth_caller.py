"""Pre-synthesise the caller's lines with Sarvam bulbul:v4-flash, one mp3 per line.

The caller page plays these into the call, so every take says the same words in the same voice.
Files land in ``demo/static/caller/<edition>/<call id>/<n>.mp3`` with a ``manifest.json`` beside them, and a
line that already has its file is skipped.

    .venv/bin/python demo/synth_caller.py            # every call
    .venv/bin/python demo/synth_caller.py call-2 --force
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from script import CALLS, CALLS_BY_ID, LANGUAGE, TTS_LANGUAGE_CODE  # noqa: E402

OUT = HERE / "static" / "caller" / LANGUAGE
MODEL = "bulbul:v4-flash"


def synth(client: httpx.Client, text: str, voice: str, path: Path) -> None:
    r = client.post(
        "https://api.sarvam.ai/text-to-speech",
        json={
            "text": text,
            "target_language_code": TTS_LANGUAGE_CODE,
            "speaker": voice,
            "model": MODEL,
            "pace": 1.0,
            "speech_sample_rate": 24000,
        },
        timeout=60,
    )
    r.raise_for_status()
    wav = base64.b64decode(r.json()["audios"][0])
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav)
        tmp = f.name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp, "-b:a", "128k", str(path)], check=True)
    os.unlink(tmp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("calls", nargs="*", default=[c.id for c in CALLS])
    ap.add_argument("--force", action="store_true", help="re-synthesise lines that already have a file")
    a = ap.parse_args()
    load_dotenv(HERE.parent / ".env")
    client = httpx.Client(headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]})
    for cid in a.calls:
        call = CALLS_BY_ID[cid]
        d = OUT / cid
        d.mkdir(parents=True, exist_ok=True)
        manifest = []
        for i, line in enumerate(call.caller_lines, start=1):
            path = d / f"{i}.mp3"
            if a.force or not path.exists():
                synth(client, line, call.voice, path)
                print(f"{cid} {i}: {line}")
            manifest.append({"n": i, "text": line, "file": f"{i}.mp3"})
        (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
