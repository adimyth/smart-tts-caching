"""A headless caller: joins the agent over WebRTC, plays the call's lines, records the agent, saves the events.

The same thing the caller page does, without a browser, so a whole call can run from a terminal
and leave behind the agent's audio and every event for the listening and transcription checks.

    .venv/bin/python demo/caller_cli.py en-1 --out harness/out/demo
    .venv/bin/python demo/caller_cli.py en-2 --out harness/out/demo --pause-redis-before 5

Writes ``<out>/<call id>.agent.wav`` (what the caller heard) and ``<out>/<call id>.events.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import sys
import time
from pathlib import Path

import av
import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRecorder
from aiortc.mediastreams import AudioStreamTrack

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from script import CALLS_BY_ID  # noqa: E402

RATE = 48_000
FRAME = RATE * 20 // 1000  # 20 ms


class QueuedAudioTrack(AudioStreamTrack):
    """Silence, except for the PCM that was queued: the caller's lines, played when asked."""

    def __init__(self) -> None:
        super().__init__()
        self.pcm = bytearray()
        self.playing_until = 0.0

    def enqueue(self, pcm: bytes) -> float:
        self.pcm.extend(pcm)
        seconds = len(pcm) / 2 / RATE
        self.playing_until = time.time() + seconds
        return seconds

    async def recv(self):  # type: ignore[no-untyped-def]
        if hasattr(self, "_timestamp"):
            self._timestamp += FRAME
            wait = self._start + (self._timestamp / RATE) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        else:
            self._start = time.time()
            self._timestamp = 0
        chunk = bytes(self.pcm[: FRAME * 2])
        del self.pcm[: FRAME * 2]
        chunk = chunk + bytes(FRAME * 2 - len(chunk))
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        frame.planes[0].update(chunk)
        frame.pts = self._timestamp
        frame.sample_rate = RATE
        frame.time_base = fractions.Fraction(1, RATE)
        return frame


def decode_mp3(path: Path) -> bytes:
    out = bytearray()
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(audio=0):
            for f in resampler.resample(frame):
                out.extend(bytes(f.planes[0])[: f.samples * 2])
        for f in resampler.resample(None):
            out.extend(bytes(f.planes[0])[: f.samples * 2])
    return bytes(out)


async def events(base: str, call_id: str, queue: asyncio.Queue[dict]) -> None:
    async with httpx.AsyncClient(timeout=None) as client, client.stream("GET", f"{base}/api/events") as r:
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev.get("type") == "hello" or (ev.get("call") and ev["call"] != call_id):
                continue
            await queue.put(ev)


async def run(call_id: str, base: str, out: Path, pause_before: int | None, gap: float) -> int:
    call = CALLS_BY_ID[call_id]
    out.mkdir(parents=True, exist_ok=True)
    lines = [
        decode_mp3(HERE / "static" / "caller" / call_id / f"{i}.mp3")
        for i in range(1, len(call.caller_lines) + 1)
    ]

    queue: asyncio.Queue[dict] = asyncio.Queue()
    listener = asyncio.create_task(events(base, call_id, queue))
    await asyncio.sleep(0.3)

    pc = RTCPeerConnection()
    track = QueuedAudioTrack()
    pc.addTrack(track)
    recorder = MediaRecorder(str(out / f"{call_id}.agent.wav"))

    @pc.on("track")
    async def on_track(t):  # type: ignore[no-untyped-def]
        recorder.addTrack(t)
        await recorder.start()

    await pc.setLocalDescription(await pc.createOffer())
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{base}/api/offer",
            json={"sdp": pc.localDescription.sdp, "type": "offer", "request_data": {"call": call_id}},
        )
        r.raise_for_status()
        answer = r.json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    saved = []
    spoke = 0
    bot_stopped_after_last = False

    def show(ev: dict) -> None:
        t = ev["type"]
        if t == "sentence":
            tag = (
                f"HIT {ev['level']}"
                + (f" ({ev['diff_class']})" if ev.get("diff_class") not in (None, "exact") else "")
                if ev["hit"]
                else "MISS"
            )
            ms = f"{ev['first_audio_ms']!s:>8} ms" if ev.get("first_in_turn") else f"{'(queued)':>11}"
            print(f"  {tag:<18} {ms}  {ev['text']}")
        elif t in ("ignored", "answering_after_silence"):
            print(
                f"  [{t}] "
                + json.dumps(
                    {k: v for k, v in ev.items() if k not in ("t", "type", "call")}, ensure_ascii=False
                )
            )
        elif t == "waiting":
            print(f"  [waiting for the rest of the line: coverage {ev['coverage']}]")
        elif t == "sentence_text":
            print(f"  {'':<18} {'':>8}     ↳ {ev['text']}")
        elif t == "user":
            print(f"caller> {ev['text']}")
        elif t in ("warm_start", "provider_request", "redis_paused", "connected", "call_ended"):
            print(
                f"  [{t}] "
                + json.dumps(
                    {k: v for k, v in ev.items() if k not in ("t", "type", "call")}, ensure_ascii=False
                )
            )

    deadline = time.time() + 240
    try:
        while time.time() < deadline:
            try:
                ev = await asyncio.wait_for(queue.get(), 60)
            except TimeoutError:
                print("no event for 60 s, giving up")
                break
            saved.append(ev)
            show(ev)
            if ev["type"] == "turn_done":
                if spoke >= len(lines):
                    bot_stopped_after_last = True
                    break
                await asyncio.sleep(gap)
                if pause_before is not None and spoke + 1 == pause_before:
                    async with httpx.AsyncClient() as client:
                        await client.post(f"{base}/api/redis/pause", json={"ms": 5000})
                    await asyncio.sleep(0.3)
                seconds = track.enqueue(lines[spoke])
                spoke += 1
                print(f"caller plays line {spoke} ({seconds:.1f} s): {call.caller_lines[spoke - 1]}")
            elif ev["type"] == "call_ended":
                break
    finally:
        await asyncio.sleep(1.5)
        await recorder.stop()
        await pc.close()
        listener.cancel()
        with open(out / f"{call_id}.events.jsonl", "w", encoding="utf-8") as f:
            for ev in saved:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sentences = [e for e in saved if e["type"] == "sentence"]
    hits = sum(1 for e in sentences if e["hit"])
    requests = sum(1 for e in saved if e["type"] == "provider_request")
    print(
        f"\n{call_id}: {len(sentences)} sentences, {hits} from the cache, {requests} provider requests"
        + ("" if bot_stopped_after_last else " (ended early)")
    )
    return 0 if bot_stopped_after_last else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("call", choices=sorted(CALLS_BY_ID))
    ap.add_argument("--base", default="http://127.0.0.1:7860")
    ap.add_argument("--out", type=Path, default=Path("harness/out/demo"))
    ap.add_argument(
        "--pause-redis-before", type=int, help="pause Redis for 5 s before this caller line (1-based)"
    )
    ap.add_argument(
        "--gap",
        type=float,
        default=0.9,
        help="seconds of silence after the agent stops before the caller speaks",
    )
    a = ap.parse_args()
    return asyncio.run(run(a.call, a.base, a.out, a.pause_redis_before, a.gap))


if __name__ == "__main__":
    sys.exit(main())
