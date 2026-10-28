"""Try the cache on a live call: your own system prompt, your microphone, the bot on the other end.

    .venv/bin/python demo/try.py                         # the demo persona, Hinglish
    .venv/bin/python demo/try.py --prompt my_prompt.txt  # your own agent
    .venv/bin/python demo/try.py --lang en --no-level-c  # English voice, Levels A and B only
    .venv/bin/python demo/try.py --mode scripted         # the launch video's calls, no microphone
    .venv/bin/python demo/try.py --mode pure             # the LLM with the persona alone

It checks the keys and Redis, starts the agent, waits until it is up, and opens the call page in
your browser with the microphone on. The demo persona runs in hybrid mode: the LLM answers the
caller, and says the hospital's standard lines (the greeting, the confirmations, the closing) word
for word, as a production agent's script does. In pure mode the LLM rewords them on every call,
so few sentences repeat. Your own prompt runs in pure mode. Talk to the bot, hang up, press Connect again and say
the same things: the second call opens on a warm cache, and every sentence the bot repeats
shows a HIT badge with its time to first audio. "Pause Redis 5 s" shows the cache failing open.

Needs SARVAM_API_KEY, and OPENAI_API_KEY outside scripted mode, in ``.env`` or the environment,
and Redis on port 6380 (``make redis-up``). ``demo/quickstart.sh`` sets all of that up first.
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
import redis
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", type=Path, help="a text file with the system prompt for your agent")
    ap.add_argument(
        "--lang", choices=("hi", "en"), default="hi", help="the bot's voice and the cache's language"
    )
    ap.add_argument("--llm", default="gpt-5.6-luna")
    ap.add_argument(
        "--mode",
        choices=("pure", "hybrid", "scripted"),
        help="hybrid for the demo persona, pure with --prompt (the default for each)",
    )
    ap.add_argument("--redis", default="redis://localhost:6380/9")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument(
        "--no-level-c",
        action="store_true",
        help="skip the Level C models (about 1.6 GB on the first run); Levels A and B only",
    )
    ap.add_argument("--keep-cache", action="store_true", help="do not empty the cache at start")
    ap.add_argument("--no-open", action="store_true", help="print the link without opening a browser")
    a = ap.parse_args()
    # Hybrid pins the persona's lines; your own prompt replaces the persona, so it runs pure.
    a.mode = a.mode or ("pure" if a.prompt else "hybrid")

    load_dotenv(ROOT / ".env")
    need = ["SARVAM_API_KEY"] + ([] if a.mode == "scripted" else ["OPENAI_API_KEY"])
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        print(f"missing {' and '.join(missing)}; add it to .env or the environment", file=sys.stderr)
        return 1
    try:
        redis.from_url(a.redis, socket_connect_timeout=1).ping()
    except Exception:
        print(f"Redis is not answering at {a.redis}; run `make redis-up` first", file=sys.stderr)
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
    if not a.no_level_c:
        print("Level C is on: the first run downloads about 1.6 GB of models (--no-level-c skips them)")
    agent = subprocess.Popen(cmd, cwd=ROOT, env=env)
    # The scripted calls play the caller's recorded lines; the LLM modes take your microphone.
    url = f"http://127.0.0.1:{a.port}/" + ("" if a.mode == "scripted" else "?talk=1")
    try:
        # The agent loads the matcher before it serves, so wait as long as it is still alive.
        waited = 0
        while not _serving(a.port):
            if agent.poll() is not None:
                print("the agent exited; see its output above", file=sys.stderr)
                return 1
            time.sleep(1)
            waited += 1
            if waited % 30 == 0:
                print(f"still loading ({waited} s) ...")
        print(f"\nopen {url}\n")
        if a.mode == "scripted":
            print("Pick a call and press Connect. Run call 1, then call 2 to see the cache serve.")
        else:
            print(
                "Press Connect and talk. Hang up, Connect again, and repeat yourself to see the cache serve."
            )
        print("Ctrl-C stops the agent.")
        if not a.no_open:
            webbrowser.open(url)
        agent.wait()
    except KeyboardInterrupt:
        agent.terminate()
    return 0


def _serving(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/config", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    sys.exit(main())
