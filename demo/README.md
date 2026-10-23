# The launch demo

A Pipecat voice agent with the cache, reached from a browser over WebRTC. The agent is Meera at the appointment desk of a fictional hospital in Pune. Four inbound calls, two in English on ElevenLabs and two in Hindi on Sarvam: the first call of each language fills the cache, the second lands on every match level and on every refusal. The plan behind it is in [docs/launch-video-plan.md](../docs/launch-video-plan.md).

| File | What it is |
|---|---|
| `script.py` | The persona, the pinned lines, the four calls with the caller's lines and the scripted agent turns, and what the cache is expected to do with each sentence of a second call |
| `rehearse.py` | Offline check that every expected outcome holds against the current matcher, with the Level C judge |
| `synth_caller.py` | Pre-synthesises the caller's lines with OpenAI text-to-speech into `static/caller/` |
| `agent.py` | The agent: FastAPI, SmallWebRTC, Sarvam `saaras:v4` speech-to-text, Smart Turn v3, the LLM (or the scripted stand-in), the cache around the TTS, and a server-sent-events feed of every cache event and sentence timing |
| `static/index.html` | The caller page: joins the call, plays the caller's lines into it, shows the transcript with a badge per sentence, the counters and the cache log |
| `caller_cli.py` | The same caller without a browser: joins, plays the lines, records the agent's audio and saves the events |

## Setup

```bash
uv sync                                   # adds the webrtc extra of Pipecat, FastAPI and uvicorn
make redis-up                             # a local Redis on port 6380; the demo uses database 9
.venv/bin/python demo/rehearse.py         # every expected outcome against the matcher
.venv/bin/python demo/synth_caller.py     # caller audio (skips lines that already have a file)
```

`.env` needs `OPENAI_API_KEY`, `ELEVENLABS_TTS_KEY`, `ELEVENLABS_VOICE_ID`, `SARVAM_API_KEY`, `SARVAM_TTS_MODEL` and `SARVAM_TTS_SPEAKER`.

## Running a call

```bash
.venv/bin/python demo/agent.py --mode scripted --flush    # empty cache, deterministic agent
.venv/bin/python demo/agent.py --mode hybrid              # gpt-5.6-luna with the pinned lines
.venv/bin/python demo/agent.py --mode pure                # gpt-5.6-luna with the persona only
```

Open http://localhost:7860, pick a call and press Connect. The page plays the caller's next line each time the agent finishes a turn; untick auto-advance to step through with the button, or tick "my microphone" to speak the caller's lines yourself. "Pause Redis 5 s" pauses the store mid-call to show the cache failing open.

From a terminal instead:

```bash
.venv/bin/python demo/caller_cli.py en-1
.venv/bin/python demo/caller_cli.py en-2 --pause-redis-before 5
.venv/bin/python demo/caller_cli.py hi-1
.venv/bin/python demo/caller_cli.py hi-2
```

Each run leaves `harness/out/demo/<call>.agent.wav`, the agent's audio as the caller heard it, and `<call>.events.jsonl`, for the listening check and `python -m tts_cache.testplan.asr`.

## What a second call shows

Every call runs its own cache instance over the shared Redis, as one container per session would, so the second call starts from a warm start and the Level B and C indexes built there. Level C is on, with the judge, so that the paraphrase hit can be shown; it is off by default in the package and the page says so.

| Badge | Sentence of the English second call | Why |
|---|---|---|
| HIT A | The greeting, the disclosure, "How can I help you today?" | Same sentence, same voice, from the cache in under 10 ms |
| MISS | "…patient ID ending in 8826." | Carries an identifier: never stored |
| MISS | "…is on Friday at 10:30 AM." | The day changed: a slot |
| MISS | "You would like the morning slot?" | A question is never played from a statement's audio |
| MISS | "Dr. Kulkarni has no slots on Thursday morning." | A negation |
| HIT B · filler | "Okay, I have confirmed your appointment." | A leading filler word |
| MISS | "…is on Thursday at 4:00 PM." | The time changed: a slot |
| HIT B · punctuation | "Please reach the hospital fifteen minutes early, and carry a photo ID." | An inner comma |
| HIT C | "The appointment details will be sent to you by SMS in a moment." | A paraphrase that passed the vetoes and the judge |
| HIT A | The closing question and the closing | Same sentence, same voice |

The milliseconds beside a badge are the time from the moment the sentence could be spoken to its first audio frame, shown for the first sentence of each turn, which is the wait the caller hears. A later sentence queues behind the previous one's audio, so its own figure says nothing about the cache and is not shown.

## Turn taking

Smart Turn v3 decides when the caller has finished, so a pause inside a line does not end the turn and a finished line ends it at once. A synthesised caller line can still arrive as two or three user turns when a pause sounds final ("Hello." then "I have an appointment…"). The scripted agent answers only once what it heard covers most of the line the caller was going to say, as a person would wait for the rest; the LLM modes answer whatever they hear, as a real agent does.

## Recording the launch video

```bash
uv run --no-project --with playwright --with opencv-python-headless --with numpy --with httpx \
    python demo/video/record_video.py            # starts its own agent (scripted, empty cache)
```

`demo/video/record_video.py` drives the caller page in headless Chrome, records the screen with Playwright and the call's own audio mix in the page, aligns the two by a flash-and-beep marker at the start of each call, renders the title cards from `demo/video/cards/card.html`, narrates the intro and outro with ElevenLabs, and writes `harness/out/demo/video/launch.mp4`. `--only-compose` rebuilds the final file from the rendered parts; `--skip-calls` reuses the recordings in `raw/`.

## Try it with your own agent

```bash
make redis-up
.venv/bin/python demo/try.py --prompt my_prompt.txt       # your system prompt, your microphone
.venv/bin/python demo/try.py --lang en --no-level-c       # English voice, Levels A and B only
```

`demo/try.py` starts the agent in pure LLM mode (gpt-5.6-luna by default) with the prompt you give it, Sarvam speech-to-text and voice, Smart Turn, and the cache around the voice, then opens the call page with the microphone on. Press Connect and talk. Hang up, press Connect again and say the same things: the second call opens on a warm cache, and every sentence the bot repeats shows a HIT badge with its time to first audio. The page's "Pause Redis 5 s" button shows the cache failing open mid-call. Without `--prompt` you get the demo persona; `--mode hybrid` adds its pinned lines.
