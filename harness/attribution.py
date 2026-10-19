"""Per-sentence audio attribution test for Pipecat TTS services.

Drives one TTS service with a scripted four-sentence turn, token by token, with
``reuse_context_id_within_turn=False`` so each sentence gets its own context id.
Records every frame the service emits and the ``on_tts_request`` event, then
reports, per sentence: the text the provider received, time to first audio,
time to the stop frame, bytes, duration, and whether any audio frame was
interleaved with another sentence's context. Writes one WAV per sentence.

Usage:
    python harness/attribution.py openai_http
    python harness/attribution.py elevenlabs_ws --interrupt
    python harness/attribution.py all
"""

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from pipecat.frames.frames import (
    AggregatedTextFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"

SENTENCES_BY_LANG = {
    "en": [
        "Hello, and thank you for calling.",
        "I can help you with your account today.",
        "Could you tell me your registered mobile number?",
        "Take your time, there is no rush.",
    ],
    "hi": [
        "नमस्ते, कॉल करने के लिए धन्यवाद।",
        "मैं आज आपके खाते में आपकी मदद कर सकता हूँ।",
        "क्या आप मुझे अपना पंजीकृत मोबाइल नंबर बता सकते हैं?",
        "कोई जल्दी नहीं है, आराम से बताइए।",
    ],
    "ta": [
        "வணக்கம், அழைத்ததற்கு நன்றி.",
        "இன்று உங்கள் கணக்கில் நான் உங்களுக்கு உதவ முடியும்.",
        "உங்கள் பதிவு செய்த மொபைல் எண்ணைச் சொல்ல முடியுமா?",
        "அவசரம் இல்லை, நிதானமாகச் சொல்லுங்கள்.",
    ],
}
# Long-turn corpus: many short sentences in quick succession, to exercise
# per-sentence context churn (ElevenLabs allows 5 open contexts per connection).
LONG_SENTENCES = [
    "Sure.", "Let me check that.", "One moment please.", "Thanks for waiting.",
    "I found your account.", "Your balance is fine.", "No payment is due.",
    "Is there anything else?", "Happy to help.", "Take care.", "Goodbye for now.",
    "Have a good day.", "Talk soon.", "Bye.",
]
SENTENCES = SENTENCES_BY_LANG["en"]
LANG = "en"
SARVAM_LANG = {"en": "en-IN", "hi": "hi-IN", "ta": "ta-IN"}
ELEVEN_LANG = {"en": "en", "hi": "hi", "ta": "ta"}
MS_PER_CHAR = {"en": (30, 120), "hi": (20, 300), "ta": (20, 300)}

# Overall wait for all stop frames. Longer than ElevenLabs' 20 s idle-context
# timeout so a context that only closes on timeout is visible as such.
WAIT_SECS = 30.0
TOKEN_GAP_SECS = 0.02


@dataclass
class Capture:
    context_id: str
    text_requested: str | None = None
    t_request: float | None = None
    t_started: float | None = None
    t_first_audio: float | None = None
    t_stop: float | None = None
    audio: bytearray = field(default_factory=bytearray)
    n_audio_frames: int = 0
    sample_rate: int | None = None
    channels: int | None = None
    tts_text: list[str] = field(default_factory=list)
    interleaved_after: set[str] = field(default_factory=set)


class Probe(FrameProcessor):
    """Sink that records every TTS-related frame with a timestamp."""

    def __init__(self, n_expected: int):
        super().__init__(name="probe")
        self.t0 = time.monotonic()
        self.captures: dict[str, Capture] = {}
        self.order: list[str] = []
        self.errors: list[str] = []
        self.started_order: list[str] = []
        self.first_audio_order: list[str] = []
        self.n_expected = n_expected
        self.done = asyncio.Event()
        self.t_interrupt: float | None = None
        self.last_audio_t: dict[str, float] = {}

    def _cap(self, cid: str) -> Capture:
        if cid not in self.captures:
            self.captures[cid] = Capture(context_id=cid)
            self.order.append(cid)
        return self.captures[cid]

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        t = self.now()
        if isinstance(frame, TTSStartedFrame) and frame.context_id:
            c = self._cap(frame.context_id)
            c.t_started = c.t_started or t
            self.started_order.append(frame.context_id)
        elif isinstance(frame, TTSAudioRawFrame):
            cid = frame.context_id or "<none>"
            c = self._cap(cid)
            if c.t_first_audio is None:
                c.t_first_audio = t
                self.first_audio_order.append(cid)
            # Interleaving: audio for this context arriving after a later
            # context has already produced audio.
            later = self.first_audio_order[self.first_audio_order.index(cid) + 1 :]
            c.interleaved_after.update(later)
            c.audio.extend(frame.audio)
            c.n_audio_frames += 1
            self.last_audio_t[cid] = t
            c.sample_rate = frame.sample_rate
            c.channels = frame.num_channels
        elif isinstance(frame, TTSTextFrame) and frame.context_id:
            self._cap(frame.context_id).tts_text.append(frame.text)
        elif isinstance(frame, TTSStoppedFrame) and frame.context_id:
            c = self._cap(frame.context_id)
            c.t_stop = c.t_stop or t
            if sum(1 for x in self.captures.values() if x.t_stop is not None) >= self.n_expected:
                self.done.set()
        elif isinstance(frame, ErrorFrame):
            self.errors.append(frame.error)
        await self.push_frame(frame, direction)


def build_service(name: str, sr: int, session: aiohttp.ClientSession):
    """Construct the named service with the per-sentence context setting."""
    common = dict(sample_rate=sr, reuse_context_id_within_turn=False)
    sarvam_lang = SARVAM_LANG[LANG]
    eleven_lang = ELEVEN_LANG[LANG]
    if name == "openai_http":
        from pipecat.services.openai.tts import OpenAITTSService

        return OpenAITTSService(
            api_key=os.environ["OPENAI_API_KEY"],
            settings=OpenAITTSService.Settings(model="gpt-4o-mini-tts", voice="alloy"),
            **common,
        )
    if name == "elevenlabs_http":
        from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService

        return ElevenLabsHttpTTSService(
            api_key=(os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVENLABS_TTS_KEY"]),
            aiohttp_session=session,
            settings=ElevenLabsHttpTTSService.Settings(
                model="eleven_flash_v2_5",
                voice=os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
                language=eleven_lang,
            ),
            **common,
        )
    if name == "elevenlabs_ws":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return ElevenLabsTTSService(
            api_key=(os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVENLABS_TTS_KEY"]),
            settings=ElevenLabsTTSService.Settings(
                model="eleven_flash_v2_5",
                voice=os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
            ),
            **common,
        )
    if name == "elevenlabs_dialogue_ws":
        from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService

        return ElevenLabsDialogueTTSService(
            api_key=(os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVENLABS_TTS_KEY"]),
            settings=ElevenLabsDialogueTTSService.Settings(
                model=os.environ.get("ELEVENLABS_DIALOGUE_MODEL", "eleven_v4_turbo"),
                voice=os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
            ),
            **common,
        )
    if name == "sarvam_http":
        from pipecat.services.sarvam.tts import SarvamHttpTTSService

        return SarvamHttpTTSService(
            api_key=os.environ["SARVAM_API_KEY"],
            aiohttp_session=session,
            settings=SarvamHttpTTSService.Settings(
                model=os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"), voice=os.environ.get("SARVAM_TTS_SPEAKER") or os.environ.get("SARVAM_TTS_SPEKAER", "shubh"), language=sarvam_lang
            ),
            **common,
        )
    if name == "sarvam_ws":
        from pipecat.services.sarvam.tts import SarvamTTSService

        return SarvamTTSService(
            api_key=os.environ["SARVAM_API_KEY"],
            settings=SarvamTTSService.Settings(model=os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"), voice=os.environ.get("SARVAM_TTS_SPEAKER") or os.environ.get("SARVAM_TTS_SPEKAER", "shubh"), language=sarvam_lang),
            **common,
        )
    el_key = os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVENLABS_TTS_KEY"]
    el_voice = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    if name == "elevenlabs_ws_fc":
        cls = _elevenlabs_ws_flush_close()
        return cls(api_key=el_key, settings=cls.Settings(model="eleven_flash_v2_5", voice=el_voice, language=eleven_lang), **common)
    if name == "elevenlabs_dialogue_ws_c":
        cls = _elevenlabs_dialogue_close()
        return cls(
            api_key=el_key,
            settings=cls.Settings(model=os.environ.get("ELEVENLABS_DIALOGUE_MODEL", "eleven_v4_turbo"), voice=el_voice),
            **common,
        )
    if name == "sarvam_ws_flush":
        cls = _sarvam_ws_flush()
        if TAP_SARVAM:
            cls.tap_log = []
        return cls(
            api_key=os.environ["SARVAM_API_KEY"],
            settings=cls.Settings(
                model=os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"),
                voice=os.environ.get("SARVAM_TTS_SPEAKER") or os.environ.get("SARVAM_TTS_SPEKAER", "shubh"),
                language=sarvam_lang,
            ),
            **common,
        )
    raise SystemExit(f"unknown service {name}")



# ---------------------------------------------------------------------------
# Per-sentence context handling for websocket services: the subclasses
# tts-cache-pipecat ships (tts_cache_pipecat.services). The Sarvam one is
# wrapped here only to log every server message for attribution analysis.
# ---------------------------------------------------------------------------

# cache_roundtrip.py sets this to False so it wires the shipped class itself.
TAP_SARVAM = True


def _elevenlabs_ws_flush_close():
    from tts_cache_pipecat.services import ElevenLabsTTSService

    return ElevenLabsTTSService


def _elevenlabs_dialogue_close():
    from tts_cache_pipecat.services import ElevenLabsDialogueTTSService

    return ElevenLabsDialogueTTSService


def _sarvam_ws_flush():
    from tts_cache_pipecat.services import SarvamTTSService

    if not TAP_SARVAM:
        return SarvamTTSService

    class SarvamWSPerSentence(SarvamTTSService):
        """The shipped class, plus a log of every server message (no change in behaviour)."""

        tap_log: list = []

        async def _receive_messages(self):
            outer = self
            if not hasattr(self, "_orig_get_ws"):
                self._orig_get_ws = self._get_websocket

            async def tap():
                async for m in outer._orig_get_ws():
                    try:
                        d = json.loads(m)
                        data = d.get("data", {}) or {}
                        outer.tap_log.append(
                            {
                                "t": round(time.monotonic(), 3),
                                "type": d.get("type"),
                                "event": data.get("event_type"),
                                "request_id": data.get("request_id"),
                                "audio_b64_len": len(data.get("audio", "")) if d.get("type") == "audio" else None,
                                "active_ctx": (self.get_active_audio_context_id() or "")[:8],
                            }
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    yield m

            self._get_websocket = lambda: tap()  # noqa: E731
            await super()._receive_messages()

    return SarvamWSPerSentence


ALL = [
    "openai_http",
    "elevenlabs_http",
    "elevenlabs_ws",
    "elevenlabs_dialogue_ws",
    "sarvam_http",
    "sarvam_ws",
    "elevenlabs_ws_fc",
    "elevenlabs_dialogue_ws_c",
    "sarvam_ws_flush",
]


async def run_one(name: str, *, interrupt: bool, sr: int, long: bool = False) -> dict:
    global SENTENCES
    SENTENCES = LONG_SENTENCES if long else SENTENCES_BY_LANG[LANG]
    mode = ("interrupt" if interrupt else "clean") + f"_{LANG}" + ("_long" if long else "")
    out_dir = OUT / name / mode
    out_dir.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        tts = build_service(name, sr, session)
        probe = Probe(n_expected=len(SENTENCES))
        requests: list[tuple[float, str, str]] = []

        @tts.event_handler("on_tts_request")
        async def on_tts_request(_tts, context_id: str, text: str):
            t = probe.now()
            requests.append((t, context_id, text))
            c = probe._cap(context_id)
            c.text_requested = text
            c.t_request = c.t_request or t

        pipeline = Pipeline([tts, probe])
        task = PipelineTask(
            pipeline,
            params=PipelineParams(audio_out_sample_rate=sr),
            cancel_on_idle_timeout=False,
        )
        runner = PipelineRunner(handle_sigint=False)
        run = asyncio.create_task(runner.run(task))

        async def feed():
            await asyncio.sleep(0.5)  # let the service connect
            await task.queue_frame(LLMFullResponseStartFrame())
            for i, s in enumerate(SENTENCES):
                for tok in s.split(" "):
                    await task.queue_frame(TextFrame(tok + " "))
                    await asyncio.sleep(TOKEN_GAP_SECS)
                if interrupt and i == 1:
                    # The LLM is still streaming when the user barges in: feed the
                    # first token of sentence 3 so the aggregator can confirm the
                    # end of sentence 2. Then wait for sentence 2's audio to start.
                    await task.queue_frame(TextFrame(SENTENCES[2].split(" ")[0] + " "))
                    for _ in range(600):
                        if len(probe.first_audio_order) >= 2:
                            break
                        await asyncio.sleep(0.01)
                    probe.t_interrupt = probe.now()
                    await task.queue_frame(InterruptionFrame())
                    probe.n_expected = 1
                    break
            await task.queue_frame(LLMFullResponseEndFrame())

        await feed()
        try:
            await asyncio.wait_for(probe.done.wait(), timeout=(WAIT_SECS * (2 if long else 1)) if not interrupt else 8.0)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
        t_end = probe.now()
        await task.cancel()
        try:
            await asyncio.wait_for(run, timeout=10)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"runner did not finish cleanly: {e}")

    rows = []
    for cid in probe.order:
        c = probe.captures[cid]
        n_bytes = len(c.audio)
        dur = n_bytes / ((c.sample_rate or sr) * 2 * (c.channels or 1)) if n_bytes else 0.0
        chars = len((c.text_requested or "").strip())
        ms_per_char = (dur * 1000 / chars) if chars else None
        if n_bytes:
            with wave.open(str(out_dir / f"{probe.order.index(cid):02d}_{cid[:8]}.wav"), "wb") as w:
                w.setnchannels(c.channels or 1)
                w.setsampwidth(2)
                w.setframerate(c.sample_rate or sr)
                w.writeframes(bytes(c.audio))
        rows.append(
            {
                "context_id": cid,
                "text": c.text_requested,
                "tts_text": "".join(c.tts_text) or None,
                "t_request": c.t_request,
                "ttfa_ms": round((c.t_first_audio - c.t_request) * 1000)
                if c.t_first_audio is not None and c.t_request is not None
                else None,
                "stop_after_request_ms": round((c.t_stop - c.t_request) * 1000)
                if c.t_stop is not None and c.t_request is not None
                else None,
                "stopped": c.t_stop is not None,
                "audio_frames": c.n_audio_frames,
                "bytes": n_bytes,
                "duration_s": round(dur, 2),
                "ms_per_char": round(ms_per_char) if ms_per_char else None,
                "interleaved_with_later": sorted(x[:8] for x in c.interleaved_after),
                "audio_after_interrupt_ms": round((probe.last_audio_t[cid] - probe.t_interrupt) * 1000)
                if probe.t_interrupt is not None and cid in probe.last_audio_t
                else None,
            }
        )

    verdict_notes = []
    n_ctx = len([r for r in rows if r["text"]])
    expected = 2 if interrupt else len(SENTENCES)
    if n_ctx != expected:
        verdict_notes.append(f"{n_ctx} contexts with text, expected {expected}")
    if interrupt:
        # Only the first sentence must complete; the second was cut. Audio for
        # any context arriving well after the interruption is a leak.
        rows_with_text = [r for r in rows if r["text"]]
        if rows_with_text and not rows_with_text[0]["stopped"]:
            verdict_notes.append("first sentence did not complete before interruption")
        for r in rows_with_text:
            if r["audio_after_interrupt_ms"] is not None and r["audio_after_interrupt_ms"] > 500:
                verdict_notes.append(f"audio leaked {r['audio_after_interrupt_ms']} ms after interruption on {r['context_id'][:8]}")
    if any(r["interleaved_with_later"] for r in rows):
        verdict_notes.append("audio interleaved across contexts")
    if not interrupt and any(not r["stopped"] for r in rows if r["text"]):
        verdict_notes.append("some contexts never received TTSStoppedFrame")
    for r in rows if not interrupt else rows[:1]:
        lo, hi = MS_PER_CHAR[LANG]
        chars = len((r["text"] or "").strip())
        # Plausible duration is affine in character count: a fixed intercept
        # covers leading and trailing silence on very short sentences.
        lo_s, hi_s = lo * chars / 1000, 0.6 + hi * chars / 1000
        if r["text"] and r["bytes"] == 0:
            verdict_notes.append(f"no audio for {r['context_id'][:8]}: {r['text']!r}")
        elif r["text"] and not (lo_s <= r["duration_s"] <= hi_s):
            verdict_notes.append(f"implausible duration for {r['context_id'][:8]}: {r['duration_s']} s for {chars} chars")
    if timed_out and not interrupt:
        verdict_notes.append(f"timed out after {WAIT_SECS}s waiting for stop frames")
    if probe.errors:
        verdict_notes.append(f"errors: {probe.errors}")
    tap = getattr(type(tts), "tap_log", None)
    report = {
        "service": name,
        "server_messages": tap,
        "mode": mode,
        "lang": LANG,
        "pipecat_wait_s": round(t_end, 2),
        "verdict": "PASS" if not verdict_notes else "FAIL",
        "notes": verdict_notes,
        "requests": [(round(t, 3), cid[:8], txt) for t, cid, txt in requests],
        "sentences": rows,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def print_report(rep: dict):
    print(f"\n=== {rep['service']} [{rep['mode']}] -> {rep['verdict']}")
    if rep.get("server_messages"):
        print("  server messages (type/event, request_id, active ctx):")
        for m in rep["server_messages"]:
            print(f"    {m['type']}/{m['event']} rid={m['request_id']} ctx={m['active_ctx']} audio={m['audio_b64_len']}")
    for n in rep["notes"]:
        print(f"  ! {n}")
    print(f"  {'ctx':8} {'ttfa':>7} {'stop':>7} {'frames':>6} {'dur':>6} {'ms/ch':>5} {'ileave':>6}  text")
    for r in rep["sentences"]:
        print(
            f"  {r['context_id'][:8]:8} {str(r['ttfa_ms']):>7} {str(r['stop_after_request_ms']):>7} "
            f"{r['audio_frames']:>6} {r['duration_s']:>6} {str(r['ms_per_char']):>5} "
            f"{len(r['interleaved_with_later']):>6}  {r['text']!r}"
        )


async def main():
    load_dotenv(ROOT.parent / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("service", choices=ALL + ["all"])
    ap.add_argument("--interrupt", action="store_true")
    ap.add_argument("--sample-rate", type=int, default=24000)
    ap.add_argument("--quiet", action="store_true", help="suppress pipecat logs")
    ap.add_argument("--lang", choices=list(SENTENCES_BY_LANG), default="en")
    ap.add_argument("--long", action="store_true", help="14 short sentences in quick succession")
    args = ap.parse_args()
    global LANG
    LANG = args.lang
    if args.quiet:
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    names = ALL if args.service == "all" else [args.service]
    reports = []
    for n in names:
        try:
            rep = await run_one(n, interrupt=args.interrupt, sr=args.sample_rate, long=args.long)
        except Exception as e:  # noqa: BLE001
            rep = {"service": n, "mode": f"{'interrupt' if args.interrupt else 'clean'}_{LANG}", "verdict": "ERROR", "notes": [repr(e)], "sentences": []}
        reports.append(rep)
        print_report(rep)
    (OUT / "summary.json").write_text(json.dumps(reports, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
