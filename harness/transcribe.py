"""Transcribe each captured clip and compare with the sentence it was keyed to.

Usage: python harness/transcribe.py [service ...]   (defaults to every out/*/clean)
"""

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


import unicodedata


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


import argparse

ap = argparse.ArgumentParser()
ap.add_argument("services", nargs="*")
ap.add_argument("--mode", default="clean_en", help="out/<service>/<mode> directory, e.g. clean_hi")
args = ap.parse_args()
lang = args.mode.split("_")[1] if "_" in args.mode else "en"
services = args.services or sorted(p.parent.parent.name for p in (ROOT / "out").glob(f"*/{args.mode}/report.json"))
overall = []
for svc in services:
    d = ROOT / "out" / svc / args.mode
    rep = json.loads((d / "report.json").read_text())
    wavs = sorted(d.glob("*.wav"))
    print(f"\n=== {svc}")
    for i, row in enumerate(rep["sentences"]):
        wav = next((w for w in wavs if w.name.startswith(f"{i:02d}_")), None)
        if not wav or not row["text"]:
            print(f"  {i}: no clip")
            continue
        with open(wav, "rb") as f:
            tr = client.audio.transcriptions.create(model=os.environ.get("STT_MODEL", "gpt-4o-mini-transcribe"), file=f, language=lang)
        e = wer(row["text"], tr.text)
        overall.append(e)
        flag = "OK " if e <= (0.2 if lang == "en" else 0.4) else "BAD"
        print(f"  {flag} wer={e:.2f}  keyed={row['text']!r}\n                 heard={tr.text!r}")
print(f"\nclips: {len(overall)}  mean WER: {sum(overall)/max(1,len(overall)):.3f}  worst: {max(overall) if overall else 0:.2f}")
