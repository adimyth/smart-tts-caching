"""Rehearse the demo offline: does every agent sentence of a second call land on its expected outcome?

The first call's agent sentences are captured into a fresh cache with stand-in audio, under the
demo's policy and matcher. Each sentence of the second call is then probed and the actual level
or miss reason is printed beside the expected one. Run it before every recording, and after any
change to the script or the matcher.

    .venv/bin/python demo/rehearse.py            # both languages, Level C with the judge
    .venv/bin/python demo/rehearse.py en --no-judge
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "packages" / "tts-cache" / "tests"))

from helpers import tone_pcm  # noqa: E402
from script import CALLS, LANGUAGE, Expect, filling_call  # noqa: E402

from tts_cache import LEVEL_C_POLICY, MemoryStore, SQLiteStore, TieredStore, TTSCache  # noqa: E402
from tts_cache.cache import default_matcher  # noqa: E402
from tts_cache.policy import AllOf, SkipIfMatches  # noqa: E402
from tts_cache.scope import Scope  # noqa: E402

SR = 24_000
SKIP_PATTERNS = [r"\d{4,}"]


def demo_policy():  # type: ignore[no-untyped-def]
    return AllOf(LEVEL_C_POLICY, SkipIfMatches(SKIP_PATTERNS))


async def rehearse(language: str, *, judge: bool) -> int:
    scope = Scope(
        service="rehearsal",
        model="demo",
        voice="meera",
        language=f"{language}-IN",
        sample_rate=SR,
        channels=1,
    )
    db = Path(tempfile.mkdtemp()) / "rehearsal.db"
    cache = TTSCache(
        scope=scope,
        store=TieredStore(MemoryStore(), SQLiteStore(db), deadline_ms=2000),
        policy=demo_policy(),
        matcher=default_matcher(level_c=True, judge=judge),
    )
    await cache.start()

    first = filling_call(language)
    for turn in first.agent_turns:
        for s in turn.sentences:
            await cache.capture(s, tone_pcm(s, SR), SR, 1)
    await cache.drain(10.0)
    # The second call runs in a fresh process, whose warm start builds the Level B and C
    # indexes from the shared store. A new cache over the same database does the same here.
    await cache.close(close_store=False)
    cache = TTSCache(
        scope=scope,
        store=TieredStore(MemoryStore(), SQLiteStore(db), deadline_ms=2000),
        policy=demo_policy(),
        matcher=default_matcher(level_c=True, judge=judge),
    )
    await cache.start()

    failures = 0
    print(
        f"\n== {language}: cache filled with {sum(len(t.sentences) for t in first.agent_turns)} sentences of {first.id}"
    )
    for call in CALLS:
        if call.language != language or call.fills:
            continue
        print(f"\n-- {call.id} ({call.caller})")
        print(f"{'expected':<10} {'actual':<16} sentence")
        for turn in call.agent_turns:
            for s, e in zip(turn.sentences, turn.expect, strict=True):
                result = await cache.probe(s)
                if result.hit is not None:
                    actual = f"hit {result.hit.level}"
                    diff = result.hit.meta.get("diff_class")
                    if diff and diff != "exact":
                        actual += f" ({diff})"
                else:
                    actual = f"miss ({result.reason or 'not stored'})"
                ok = _matches(e, result)
                failures += 0 if ok else 1
                expected = f"{e.outcome} {e.level or ''}".strip() if e else "-"
                flag = "" if ok else "   <-- DIFFERS"
                print(f"{expected:<10} {actual:<16} {s}{flag}")
    await cache.close()
    return failures


def _matches(e: Expect | None, result) -> bool:  # type: ignore[no-untyped-def]
    if e is None:
        return True
    if e.outcome == "hit":
        return result.hit is not None and result.hit.level == e.level
    return result.hit is None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("languages", nargs="*", default=[LANGUAGE])
    ap.add_argument("--no-judge", action="store_true", help="Level C without the entailment judge")
    a = ap.parse_args()
    failures = 0
    for lang in a.languages:
        failures += await rehearse(lang, judge=not a.no_judge)
    print(
        f"\n{failures} sentence(s) differ from the script's expectation"
        if failures
        else "\nevery sentence as expected"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
