"""Try the cache on a live call: your own system prompt, your microphone, the bot on the other end.

    .venv/bin/python demo/try.py                         # the demo persona, Hinglish
    .venv/bin/python demo/try.py --prompt my_prompt.txt  # your own agent
    .venv/bin/python demo/try.py --lang en --no-level-c  # English voice, Levels A and B only

It checks Redis, starts the agent in pure LLM mode with your prompt, and opens the call page
in your browser with the microphone on. Talk to the bot, hang up, press Connect again and say
the same things: the second call opens on a warm cache, and every sentence the bot repeats
shows a HIT badge with its time to first audio. "Pause Redis 5 s" shows the cache failing open.

Needs ``.env`` with OPENAI_API_KEY and SARVAM_API_KEY, and Redis on port 6380 (``make redis-up``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", type=Path, help="a text file with the system prompt for your agent")
    ap.add_argument(
        "--lang", choices=("hi", "en"), default="hi", help="the bot's voice and the cache's language"
    )
    ap.add_argument("--llm", default="gpt-5.6-luna")
    ap.add_argument("--mode", choices=("pure", "hybrid", "scripted"), default="pure")
    ap.add_argument("--redis", default="redis://localhost:6380/9")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument(
        "--no-level-c", action="store_true", help="skip the 1.1 GB judge model; Levels A and B only"
    )
    ap.add_argument("--keep-cache", action="store_true", help="do not empty the cache at start")
    a = ap.parse_args()

    env_file = ROOT / ".env"
    if not env_file.exists():
        print("no .env found; it needs OPENAI_API_KEY and SARVAM_API_KEY", file=sys.stderr)
        return 1
    try:
        subprocess.run(
            ["redis-cli", "-p", str(a.redis.rsplit(":", 1)[1].split("/")[0]), "ping"],
            check=True,
            capture_output=True,
        )
    except Exception:
        print("Redis is not answering on that port; run `make redis-up` first", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        str(HERE / "agent.py"),
        "--mode",
        a.mode,
        "--llm",
        a.llm,
        "--redis",
        a.redis,
        "--port",
        str(a.port),
    ]
    if a.prompt:
        cmd += ["--system-prompt", str(a.prompt)]
    if a.no_level_c:
        cmd.append("--no-level-c")
    if not a.keep_cache:
        cmd.append("--flush")
    env = {**os.environ, "DEMO_LANG": a.lang}
    print("starting the agent" + (" with your prompt" if a.prompt else " with the demo persona") + " ...")
    agent = subprocess.Popen(cmd, cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{a.port}/?talk=1"
    try:
        for _ in range(120):
            if agent.poll() is not None:
                print("the agent exited; see its output above", file=sys.stderr)
                return 1
            try:
                if httpx.get(f"http://127.0.0.1:{a.port}/api/config", timeout=1).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        print(f"\nopen {url}\n")
        print("Press Connect and talk. Hang up, Connect again, and repeat yourself to see the cache serve.")
        print("Ctrl-C stops the agent.")
        webbrowser.open(url)
        agent.wait()
    except KeyboardInterrupt:
        agent.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
