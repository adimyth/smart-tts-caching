"""The offline part of the test plan end to end: matrix, size sweeps, usability, analysis and report.

``python -m tts_cache.testplan --out harness/out/testplan/<run id> [--seeds 1-10] [--quick]``

Each part writes under ``<out>`` in the layout ``analysis`` reads, and
``<out>/timings.json`` records its wall time. A part that fails is recorded
and the next one runs.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from tts_cache.corpus import POOL_LANGUAGES

PARTS = ("offline", "capacity", "pressure", "memory", "population", "usability", "report")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tts_cache.testplan", description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--langs", default=",".join(POOL_LANGUAGES))
    ap.add_argument("--quick", action="store_true", help="small size sweeps, for a rehearsal")
    ap.add_argument("--only", default=",".join(PARTS))
    ap.add_argument("--no-installs", action="store_true", help="skip U3's fresh-venv install sizes")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    w = str(a.workers)
    quick = ["--quick"] if a.quick else []

    def sizes(sweep: str, *extra: str) -> list[str]:
        out = str(a.out / "sizes" / sweep)
        return [py, "-m", "tts_cache.testplan.sizes", sweep, "--out", out, "--workers", w, *extra, *quick]

    offline = ["--out", str(a.out / "offline"), "--seeds", a.seeds, "--workers", w, "--langs", a.langs]
    installs = ["--no-installs"] if a.no_installs else []
    steps = {
        "offline": [py, "-m", "tts_cache.testplan.offline", *offline, "--resume"],
        "capacity": sizes("capacity"),
        "pressure": sizes("pressure"),
        "memory": sizes("memory"),
        "population": sizes("population", "--langs", a.langs),
        "usability": [
            py,
            "-m",
            "tts_cache.testplan.usability",
            "all",
            "--out",
            str(a.out),
            "--langs",
            "en,hi,ta",
            *installs,
        ],
        "report": [py, "-m", "tts_cache.testplan.report", str(a.out)],
    }
    timings_path = a.out / "timings.json"
    timings = json.loads(timings_path.read_text()) if timings_path.exists() else {}
    failed = []
    for part in [p for p in PARTS if p in a.only.split(",")]:
        t0 = time.time()
        res = subprocess.run(steps[part], check=False)
        timings[part] = round(time.time() - t0, 1)
        timings_path.write_text(json.dumps(timings, indent=1))
        if res.returncode:
            failed.append(part)
        print(json.dumps({"part": part, "exit": res.returncode, "wall_s": timings[part]}), flush=True)
    print(json.dumps({"failed": failed, "total_s": round(sum(timings.values()), 1)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
