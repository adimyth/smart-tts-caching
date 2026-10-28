# The launch demo

A Pipecat voice agent with the cache, reached from a browser over WebRTC. The agent is Ananya at the appointment desk of City General Hospital, a fictional hospital in Pune. Two inbound calls from the same caller: the first books an appointment and fills the cache, the second moves it and lands on every match level and on every refusal. Two editions share that script, Hinglish (`hi`, the default) and Indian English (`en`), both on Sarvam: `saaras:v4` hears the caller and `bulbul:v4-flash` speaks for the agent. The plan behind it is in [docs/launch-video-plan.md](../docs/launch-video-plan.md).

## Try it in one command

On macOS or Linux, from a fresh clone:

```bash
./demo/quickstart.sh                      # talk to the agent yourself, in Hinglish
./demo/quickstart.sh --lang en            # the same agent in English
./demo/quickstart.sh --prompt my.txt      # your own system prompt
./demo/quickstart.sh --mode scripted      # the launch video's two calls; needs only a Sarvam key
./demo/quickstart.sh --mode pure          # the LLM words every line itself, so fewer repeat
./demo/quickstart.sh --no-level-c         # Levels A and B only; skips the Level C models
```

The script installs [uv](https://docs.astral.sh/uv/) if it is missing, clones the pinned Pipecat commit beside this repository (where `pyproject.toml` and CI expect it), runs `uv sync`, starts Redis on port 6380 with `redis-server` or else Docker, and asks for the keys it needs: `SARVAM_API_KEY` ([dashboard.sarvam.ai](https://dashboard.sarvam.ai)) always, and `OPENAI_API_KEY` for the LLM unless the mode is scripted. It saves them to `.env`. It then starts the agent, waits until it serves, and opens http://127.0.0.1:7860.

The demo persona runs in hybrid mode: the LLM answers the caller, and says the hospital's standard lines (the greeting, the confirmations, the closing) word for word, as a production agent's script does. In pure mode it rewords them on every call, so few sentences repeat; your own prompt runs in pure mode.

Press Connect and talk. Hang up, press Connect again and say the same things: the second call opens on a warm cache, and every sentence the agent repeats shows a HIT badge with its time to first audio. "Pause Redis 5 s" pauses the store mid-call to show the cache failing open. In scripted mode, pick call 1, then call 2.

Level C is on by default, so the first run downloads about 1.6 GB of models (`multilingual-e5-small` and the `mDeBERTa-v3-base` judge); `--no-level-c` skips them. Building `hnswlib` needs a C++ compiler, which macOS has once `git` works and Linux gets from `build-essential`.

## Files

| File | What it is |
|---|---|
| `quickstart.sh` | Installs and sets up everything, then runs `try.py` |
| `try.py` | Checks the keys and Redis, starts the agent, waits until it serves, and opens the call page |
| `script.py` | The persona, the pinned lines, the two calls with the caller's lines and the scripted agent turns, and what the cache is expected to do with each sentence of the second call |
| `rehearse.py` | Offline check that every expected outcome holds against the current matcher, with the Level C judge |
| `synth_caller.py` | Pre-synthesises the caller's lines with Sarvam into `static/caller/<edition>/` |
| `agent.py` | The agent: FastAPI, SmallWebRTC, Sarvam `saaras:v4` speech-to-text, Smart Turn v3, the LLM (or the scripted stand-in), the cache around the TTS, and a server-sent-events feed of every cache event and sentence timing |
| `static/index.html` | The caller page: joins the call, plays the caller's lines into it, shows the transcript with a badge per sentence, the counters and the cache log |
| `caller_cli.py` | The same caller without a browser: joins, plays the lines, records the agent's audio and saves the events |

## Setup by hand

```bash
uv sync                                   # needs ../pipecat at the pinned commit, as quickstart.sh clones it
make redis-up                             # a local Redis on port 6380; the demo uses database 9
.venv/bin/python demo/rehearse.py         # every expected outcome against the matcher
.venv/bin/python demo/synth_caller.py     # caller audio (skips lines that already have a file)
```

`.env` needs `SARVAM_API_KEY`, and `OPENAI_API_KEY` for the LLM modes. Recording the video also needs `ELEVENLABS_API_KEY` for the narration. `DEMO_LANG=en` selects the English edition for every script.

## Running a call

```bash
.venv/bin/python demo/agent.py --mode scripted --flush    # empty cache, deterministic agent
.venv/bin/python demo/agent.py --mode hybrid              # gpt-5.6-luna with the pinned lines
.venv/bin/python demo/agent.py --mode pure                # gpt-5.6-luna with the persona only
```

Open http://localhost:7860, pick a call and press Connect. The page plays the caller's next line each time the agent finishes a turn; untick auto-advance to step through with the button, or tick "my microphone" to speak the caller's lines yourself.

From a terminal instead, against a running agent:

```bash
.venv/bin/python demo/caller_cli.py call-1
.venv/bin/python demo/caller_cli.py call-2 --pause-redis-before 5
```

The caller plays the recordings of the agent's edition. Each run leaves `harness/out/demo/<call>.agent.wav`, the agent's audio as the caller heard it, and `<call>.events.jsonl`, for the listening check and `python -m tts_cache.testplan.asr`.

## What the second call shows

Every call runs its own cache instance over the shared Redis, as one container per session would, so the second call starts from a warm start and the Level B and C indexes built there. Level C is on, with the judge, so that the paraphrase hit can be shown; it is off by default in the package and the page says so. Of the second call's 17 sentences, 11 play from the cache.

| Badge | Sentence of the English second call | Why |
|---|---|---|
| HIT A | The greeting, the disclosure, "How can I help you today?", "Certainly.", the request for the number | Same sentence, same voice, from the cache in under 10 ms |
| MISS | "…patient ID ending in 4471." | Carries an identifier: never stored |
| HIT A | "Your appointment with Dr. Kulkarni is on Thursday at 10:30 AM." | Same sentence, same voice |
| MISS | "Which day would you like to move it to?" | A sentence the agent has never said |
| MISS | "You would like the morning slot?" | A question is never played from a statement's audio |
| MISS | "Dr. Kulkarni is not available on Friday morning." | A negation |
| MISS | "He is available on Friday at 4:00 PM." | A sentence the agent has never said |
| HIT B · filler | "Okay, I have confirmed your appointment." | A leading filler word |
| MISS | "…is on Friday at 4:00 PM." | The day and time changed: a slot |
| HIT B · punctuation | "Please reach the hospital fifteen minutes early, and carry a photo ID." | Only the punctuation differs |
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
