"""Record the launch video: four calls through the caller page, title cards, narration, one MP4.

The page is driven by Playwright in Chrome and recorded with Playwright's screen capture, which
has no sound. The page records the call's own audio mix (the caller's lines and the agent) at the
same time, and marks the same instant on both with a white flash and a 1 kHz beep, so the two
are aligned afterwards to within a frame. Cards between the calls are rendered from
``cards/card.html``; the intro and outro are narrated with ElevenLabs (or macOS ``say``).

    uv run --no-project --with playwright --with opencv-python-headless --with numpy --with httpx \\
        python demo/video/record_video.py --out harness/out/demo/video

The agent is started by this script in scripted mode with an empty cache (``--no-agent`` to use
one already running). Needs Redis on port 6380, ffmpeg, and the keys in ``.env``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))

BASE = "http://127.0.0.1:7860"
CALLS = [("call-1", None), ("call-2", 5)]
W, H, FPS = 1920, 1080, 30

NARRATION = {
    "intro": (
        "A voice agent repeats itself. The greeting, the disclosure, the confirmation: the same sentences, "
        "in the same voice, on every call, and every time the provider is paid to synthesise them again. "
        "Smart TTS Caching plays what the agent has already said, in milliseconds, and sends only new sentences "
        "to the provider. Here are two calls to a hospital desk. The first fills the cache. Watch the second."
    ),
    "outro": (
        "Two processors around a stock Pipecat TTS service, and a framework-free core. "
        "The code, the evaluation and this demo are on GitHub. Try it."
    ),
}
NARRATOR_VOICE_FALLBACK = "21m00Tcm4TlvDq8ikWAM"  # when .env has no ELEVENLABS_VOICE_ID
NARRATOR_MODEL = "eleven_v4"
CARD_SECONDS = 4.5


def load_env() -> None:
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sh(*args: str) -> None:
    subprocess.run(list(args), check=True)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return float("nan")


# ---------------------------------------------------------------------------
# The agent.
# ---------------------------------------------------------------------------


def start_agent(log: Path, lang: str) -> subprocess.Popen:  # type: ignore[type-arg]
    p = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "demo/agent.py"), "--mode", "scripted", "--flush"],
        stdout=open(log, "w"),
        stderr=subprocess.STDOUT,
        cwd=ROOT,
        env={**os.environ, "DEMO_LANG": lang},
    )
    for _ in range(90):
        try:
            if httpx.get(f"{BASE}/api/config", timeout=2).status_code == 200:
                return p
        except Exception:
            pass
        time.sleep(1)
    p.kill()
    raise SystemExit("the agent did not come up; see " + str(log))


# ---------------------------------------------------------------------------
# Recording the calls.
# ---------------------------------------------------------------------------


class BadTake(Exception):
    pass


def record_calls(out: Path, attempts: int = 3) -> dict[str, dict[str, Path]]:
    """Record every call; a take in which a sentence produced no audio (a provider stall) is redone from an empty cache."""
    for attempt in range(1, attempts + 1):
        try:
            return _record_calls_once(out)
        except BadTake as e:
            print(f"take {attempt} failed: {e}; flushing the cache and recording again")
            httpx.post(f"{BASE}/api/redis/flush", timeout=10)
            time.sleep(2)
    raise SystemExit("no clean take")


def _record_calls_once(out: Path) -> dict[str, dict[str, Path]]:
    from playwright.sync_api import sync_playwright

    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Path]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        for call_id, pause in CALLS:
            ctx = browser.new_context(
                viewport={"width": W, "height": H},
                device_scale_factor=1,
                accept_downloads=True,
                record_video_dir=str(raw),
                record_video_size={"width": W, "height": H},
                color_scheme="dark",
            )
            page = ctx.new_page()
            url = f"{BASE}/?stage=1&call={call_id}" + (f"&pause={pause}" if pause else "")
            page.goto(url, wait_until="networkidle")
            page.wait_for_function("() => document.querySelectorAll('#callSelect option').length > 0")
            time.sleep(1.0)
            print(f"{call_id}: recording")
            page.evaluate("() => window.__demo.start()")
            try:
                page.wait_for_function("() => window.__demo.state === 'done'", timeout=240_000)
            except Exception as e:
                browser.close()
                raise BadTake(f"{call_id} did not finish: {type(e).__name__}") from None
            no_audio = page.evaluate("() => window.__demo.noAudio")
            if no_audio:
                browser.close()
                raise BadTake(f"{call_id}: {no_audio} sentence(s) produced no audio")
            time.sleep(5.0)  # the end-of-call summary stays on screen
            with page.expect_download() as dl:
                page.evaluate("() => window.__demo.stop()")
            streamed = raw / f"{call_id}.audio.stream.webm"
            dl.value.save_as(str(streamed))
            audio = raw / f"{call_id}.audio.webm"
            # MediaRecorder writes a streaming WebM with no duration; a remux fills the header in.
            sh("ffmpeg", "-v", "error", "-y", "-i", str(streamed), "-c", "copy", str(audio))
            video_src = Path(page.video.path())  # type: ignore[union-attr]
            page.close()
            ctx.close()
            video = raw / f"{call_id}.video.webm"
            video_src.rename(video)
            files[call_id] = {"video": video, "audio": audio}
            print(f"{call_id}: video {duration(video):.1f} s, audio {duration(audio):.1f} s")
        browser.close()
    return files


# ---------------------------------------------------------------------------
# Aligning the screen recording with the audio, by the flash and the beep.
# ---------------------------------------------------------------------------


def flash_time(video: Path) -> float:
    import cv2

    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or FPS
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            raise SystemExit(f"no flash found in {video}")
        if frame.mean() > 170:
            cap.release()
            return i / fps
        i += 1


def beep_time(audio: Path) -> float:
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(audio), "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
        capture_output=True,
        check=True,
    ).stdout
    x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32767
    win = 48  # 1 ms
    env = np.abs(x[: len(x) // win * win]).reshape(-1, win).max(axis=1)
    loud = np.nonzero(env > 0.25)[0]
    if not len(loud):
        raise SystemExit(f"no beep found in {audio}")
    return loud[0] * win / 48000


def mux_call(call_id: str, f: dict[str, Path], out: Path) -> Path:
    tv, ta = flash_time(f["video"]), beep_time(f["audio"])
    start_v, start_a = tv + 0.15, ta + 0.15  # cut just past the marker so the flash is not in the video
    mp4 = out / f"{call_id}.mp4"
    print(f"{call_id}: flash at {tv:.3f} s, beep at {ta:.3f} s")
    sh(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{start_v:.3f}",
        "-i",
        str(f["video"]),
        "-ss",
        f"{start_a:.3f}",
        "-i",
        str(f["audio"]),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        f"scale={W}:{H},fps={FPS},format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        str(mp4),
    )
    return mp4


# ---------------------------------------------------------------------------
# Cards and narration.
# ---------------------------------------------------------------------------


def narrate(kind: str, out: Path) -> Path | None:
    text = NARRATION[kind]
    mp3 = out / f"{kind}.narration.mp3"
    if mp3.exists():
        return mp3
    key = os.environ.get("ELEVENLABS_TTS_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    if key:
        r = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ.get('ELEVENLABS_VOICE_ID', NARRATOR_VOICE_FALLBACK)}",
            headers={"xi-api-key": key},
            json={"text": text, "model_id": NARRATOR_MODEL},
            timeout=120,
        )
        if r.status_code == 200:
            mp3.write_bytes(r.content)
            print(
                f"{kind}: narrated with ElevenLabs {NARRATOR_MODEL} ({len(text)} characters, cost header {r.headers.get('character-cost')})"
            )
            return mp3
        print(f"{kind}: ElevenLabs refused ({r.status_code}); using the macOS voice")
    aiff = out / f"{kind}.narration.aiff"
    sh("say", "-o", str(aiff), text)
    sh("ffmpeg", "-v", "error", "-y", "-i", str(aiff), str(mp3))
    return mp3


def card_png(kind: str, out: Path, page) -> Path:  # type: ignore[no-untyped-def]
    png = out / f"card-{kind}.png"
    page.goto((HERE / "cards" / "card.html").as_uri() + f"?kind={kind}")
    page.wait_for_timeout(300)
    page.screenshot(path=str(png))
    return png


def card_mp4(kind: str, png: Path, narration: Path | None, out: Path) -> Path:
    mp4 = out / f"card-{kind}.mp4"
    if narration is not None:
        secs = duration(narration) / 1.12 + 0.9
        sh(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(FPS),
            "-i",
            str(png),
            "-i",
            str(narration),
            "-t",
            f"{secs:.2f}",
            "-vf",
            f"scale={W}:{H},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-af",
            "adelay=300|300,atempo=1.12,apad",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            str(mp4),
        )
    else:
        sh(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(FPS),
            "-i",
            str(png),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            f"{CARD_SECONDS:.2f}",
            "-vf",
            f"scale={W}:{H},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(mp4),
        )
    return mp4


def render_cards(out: Path) -> dict[str, Path]:
    from playwright.sync_api import sync_playwright

    cards: dict[str, Path] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        for kind in ["intro", "call-call-1", "call-call-2", "outro"]:
            png = card_png(kind, out, page)
            narration = narrate(kind, out) if kind == "intro" else None
            cards[kind] = card_mp4(kind, png, narration, out)
        browser.close()
    return cards


# ---------------------------------------------------------------------------
# Composing.
# ---------------------------------------------------------------------------


def concat(parts: list[Path], final: Path) -> None:
    args = ["ffmpeg", "-v", "error", "-y"]
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    chain = (
        "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
        + f"concat=n={n}:v=1:a=1[v][a0];[a0]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    )
    args += [
        "-filter_complex",
        chain,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(final),
    ]
    sh(*args)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", choices=("hi", "en"), default="hi", help="the edition to record")
    ap.add_argument("--out", type=Path, help="default harness/out/demo/video-<lang>")
    ap.add_argument("--no-agent", action="store_true", help="use an agent already running on port 7860")
    ap.add_argument("--skip-calls", action="store_true", help="reuse the recordings in <out>/raw")
    ap.add_argument(
        "--only-compose", action="store_true", help="reuse <out>/*.mp4 and only rebuild the final file"
    )
    a = ap.parse_args()
    load_env()
    out: Path = a.out or ROOT / f"harness/out/demo/video-{a.lang}"
    out.mkdir(parents=True, exist_ok=True)
    os.environ["DEMO_LANG"] = a.lang

    if not a.only_compose:
        agent = None
        if not a.skip_calls:
            if not a.no_agent:
                agent = start_agent(out / "agent.log", a.lang)
            try:
                files = record_calls(out)
            finally:
                if agent is not None:
                    agent.terminate()
            (out / "raw" / "files.json").write_text(
                json.dumps({k: {kk: str(v) for kk, v in d.items()} for k, d in files.items()})
            )
        else:
            files = {
                k: {kk: Path(v) for kk, v in d.items()}
                for k, d in json.loads((out / "raw" / "files.json").read_text()).items()
            }
        for call_id, f in files.items():
            mux_call(call_id, f, out)
        render_cards(out)

    parts = [out / "card-intro.mp4"]
    for call_id, _ in CALLS:
        parts += [out / f"card-call-{call_id}.mp4", out / f"{call_id}.mp4"]
    parts.append(out / "card-outro.mp4")
    final = out / "launch.mp4"
    concat(parts, final)
    print(f"\n{final}: {duration(final):.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
