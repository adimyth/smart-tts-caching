# tts-cache

**A sentence-level audio cache for Pipecat voice agents.** When an agent is about to say a sentence it has already said in the same voice, the cache plays the stored audio instead of calling the TTS provider: the sentence is not billed, and it starts playing in milliseconds.

![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)
![Pipecat 1.12.x](https://img.shields.io/badge/pipecat--ai-1.12.x-6366f1)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange)

- [Why](#why)
- [Features](#features)
- [How it works](#how-it-works)
- [Results](#results)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Command line](#command-line)
- [Operations](#operations)
- [Guarantees and limitations](#guarantees-and-limitations)
- [Development](#development)

## Why

A voice agent sends every sentence it speaks to a TTS provider. Each request is billed per character, and the caller waits 130 ms to 2.4 s for the first audio. Agents repeat themselves: greetings, confirmations, disclosures and closings recur in the same voice for every caller, and each repeat is synthesised and billed again. The repeats happen across sessions rather than within one, so the cache is shared by every session of a deployment.

## Features

- **Drop-in for Pipecat.** Two frame processors placed around a stock TTS service; the service, the LLM and the transport are unchanged.
- **Exact and near-duplicate matching**, on by default. A sentence that differs only in punctuation or a leading filler ("Okay, …") reuses the stored clip. A changed name, number, date, amount, negation or question mark never does.
- **Voice-safe keys.** Provider, model, voice, language, sample rate and audio settings are part of every key, so changing any of them never replays old audio.
- **Fails open.** Every store call has a 20 ms deadline. A slow, failing or unreachable store turns the sentence into an ordinary provider request, and no cache error reaches the pipeline.
- **Shared and fast.** Redis is shared by all sessions, and each process keeps its most-used clips in memory, loaded at start, so a fresh container hits from its first sentence.
- **Multilingual.** Canonicalisation and near-duplicate matching cover English and eleven Indian languages: Hindi, Bengali, Assamese, Punjabi, Gujarati, Marathi, Odia, Tamil, Telugu, Kannada and Malayalam. Exact matching works in any language.
- **Policies.** Retention, minimum length, skip patterns for personal data, turn position, per-tenant isolation, write rate limits and per-tag routing, composable.
- **Operable.** Estimate the hit rate from your own transcripts before deploying, warm a new deployment, check Redis placement, purge by tag or text, and export metrics as JSON lines or Pipecat `MetricsFrame`s.

## How it works

![System overview](docs/diagrams/overview.svg)

1. The LLM's output is split into sentences, the unit a provider synthesises in one request.
2. Each sentence is keyed on its canonical text, taken after Pipecat's own text filters and transforms, plus the scope: everything that shapes the sound.
3. The key is looked up in the shared store. On a hit, the stored audio plays in the sentence's place and the provider is skipped. On a miss, the provider synthesises as usual, and the clip is captured, checked and stored for next time.

Canonicalisation folds differences that cannot change the audio: Unicode normalisation, Indic nukta and joiner variants, native digits, danda against full stop, whitespace, markdown and case.

### Match levels

Each level runs only when the previous one misses.

| Level | Serves | Status | How it decides |
|---|---|---|---|
| **A**, exact | The same canonical text | On by default | Hash lookup |
| **B**, near duplicate | The same words, differing only in punctuation or leading and trailing filler | On by default | MinHash candidates, an edit-distance check and a diff classifier; a difference in a name, number, date, amount, negation or question mark is never served |
| **C**, paraphrase | The same meaning in different words | Experimental, off | A multilingual encoder (`multilingual-e5-small`) proposes the nearest stored sentence, slot and span vetoes refuse changed facts, and an NLI judge (mDeBERTa-v3-base) must find that each sentence entails the other |

> [!WARNING]
> Level C is not safe for production. On an adversarial corpus it played statements for questions (289 of 421 English pairs) and served real changes of meaning: asked for "My colleague Tanvi will not follow up with you by Thursday", it played "You will hear back from my colleague Tanvi no later than Thursday". Keep it off.

## Results

Measured on an Apple M4 Pro with a local Redis. Offline runs drive the cache's own code with a stand-in provider over labelled corpora in 12 languages. Live runs use ElevenLabs (`eleven_flash_v2_5` over websocket, English) and Sarvam (`bulbul:v4-flash` over websocket, Hindi). A+B, the default configuration, means Levels A and B.

### Correctness

| Check | Result |
|---|---|
| Offline matrix: 12 languages, 2,040 runs, Levels A and A+B | No clip with a different meaning served |
| Adversarial corpus, English and Hindi: at least 420 requests per decoy kind (new sentence, changed slot, changed meaning, question for statement) | 0 of 2,649 and 0 of 2,659 served; upper 95 % bound under 0.72 % for each kind |
| Scope isolation: voice, model, language, sample rate, channels, one setting or tenant changed | 0 of 560 requests served for each field |
| Live, both providers | 0 wrong serves; provider requests equal misses |

### Hit rate

The character-weighted hit rate is the share of characters served from the cache instead of the provider. It depends on how often the agent repeats itself, not on the language. `r` is the share of sentences that repeat something said earlier; each range spans the 12 languages.

| Configuration | r = 0.25 | r = 0.5 |
|---|---|---|
| Level A | 15.4 to 16.9 % | 29.9 to 31.3 % |
| Levels A + B (default) | 19.9 to 21.5 % | 38.7 to 40.2 % |

Live at r = 0.5, provider characters fell by 38.6 % on ElevenLabs and 35.5 % on Sarvam. The corpora are synthetic, so [estimate your own hit rate](#1-estimate-the-hit-rate) from real transcripts before relying on these numbers.

### Latency

| Measure | Result |
|---|---|
| Provider, first audio of a sentence (live, six service classes) | 131 ms to 2.4 s |
| Cache hit, first audio of a turn's first sentence (live) | 7.7 ms p50 on ElevenLabs, 8.7 ms on Sarvam |
| Cache work on a miss (live) | 0.8 to 1.4 ms p50, 2.4 ms p95 |
| Turn start, with a third of turns opening on a hit (live) | 60 ms sooner on average on ElevenLabs, 54 ms on Sarvam |
| Redis paused for 5 s, then restarted (live) | No error reached the pipeline; the cache's added time peaked at 23 ms |

### Cost

The saving is the hit rate times the characters sent times the provider's price. At public list prices of October 2026, per million characters the agent would otherwise send:

| Provider and model | Price per 1M characters | Saved at r = 0.25 | Saved at r = 0.5 |
|---|---|---|---|
| ElevenLabs `eleven_flash_v2_5` | $50 | ~$10 | ~$20 |
| Sarvam `bulbul:v4-flash` | ~$24 (₹2,000) | ~$5 | ~$9 |
| OpenAI `gpt-4o-mini-tts` | ~$16, all in | ~$3 | ~$6 |

Against a managed Redis at ElastiCache Serverless list prices, and at 100,000 calls a month or more, the cache pays for itself above a character hit rate of about 7 % on ElevenLabs with Valkey and about 20 % on Sarvam with Redis OSS. A process with the cache uses about 100 MB more memory.

## Installation

Requirements: Python 3.12, `pipecat-ai` 1.12.x, and, for a store shared across processes, Redis (tested with 7.0) or another Redis-protocol server such as Valkey. The packages are not published to PyPI; install them from a checkout:

```bash
pip install "./packages/tts-cache[redis,similarity]" ./packages/tts-cache-pipecat
```

| Package or extra | Provides |
|---|---|
| `tts-cache` | The framework-free core: splitting, canonicalisation, keys, matching, stores, policies, metrics and the `tts-cache` command |
| `tts-cache[redis]` | `RedisStore` |
| `tts-cache[similarity]` | Levels B and C; without it the cache runs Level A only |
| `tts-cache-pipecat` | `TTSCacheRead`, `TTSCacheWrite` and the websocket service subclasses, for `pipecat-ai>=1.12,<1.13` |

## Quick start

### 1. Estimate the hit rate

Replay a log of what your agent said through the production splitter, canonicaliser and policy:

```bash
tts-cache replay --log transcripts.jsonl --out replay.json
```

The log has one JSON object per line:

```json
{"ts": 1792300000.0, "session": "s-0001", "text": "Hello. How can I help?", "language": "en-US", "scope": "elevenlabs/flash/voice-1"}
```

`text` can be one sentence or a whole turn. `scope` labels a voice identity: only sentences with the same label share audio. The report gives the hit rate the cache would have had and the rules that produced it.

### 2. Wire it into Pipecat

Put `TTSCacheRead` before the TTS service and `TTSCacheWrite` after it:

```python
from pipecat.pipeline.pipeline import Pipeline
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter
from tts_cache import DEFAULT_POLICY, MemoryStore, RedisStore, TieredStore, TTSCache
from tts_cache.policy import AllOf, SkipIfMatches
from tts_cache_pipecat import SarvamTTSService, TTSCacheRead, TTSCacheWrite, scope_from_service

# One context id per sentence, and no text_filters on the service: TTSCacheRead applies them.
tts = SarvamTTSService(api_key=..., settings=..., reuse_context_id_within_turn=False)

cache = TTSCache(
    scope=scope_from_service(tts, language="hi-IN"),
    store=TieredStore(MemoryStore(warm_start_bytes=4 << 20), RedisStore("redis://cache:6379/0"), deadline_ms=20),
    policy=AllOf(DEFAULT_POLICY, SkipIfMatches([r"\d{4,}"])),  # never store account, card or ticket numbers
)
await cache.start()

pipeline = Pipeline([
    transport.input(), stt, context_aggregator.user(), llm,
    TTSCacheRead(cache, tts=tts, text_filters=[MarkdownTextFilter()]),
    tts,
    TTSCacheWrite(cache, tts=tts),
    transport.output(), context_aggregator.assistant(),
])
```

Always build the scope with `scope_from_service`, so the key names the voice the service really uses. Call `await cache.close()` on shutdown to flush pending captures.

[`examples/wiring.py`](examples/wiring.py) runs the same wiring against a fake service, with no provider or credentials:

```bash
python examples/wiring.py   # {'first_run_provider_requests': 2, 'second_run_provider_requests': 0}
```

### 3. Check it

Say the same sentence in two turns. The second must make no provider request (Pipecat's `on_tts_request` event does not fire) and must still play. [`harness/integration_check.py`](harness/integration_check.py) runs this check against a live ElevenLabs or Sarvam service and Redis.

### Supported services

Websocket services need the `tts_cache_pipecat` subclass, which closes each sentence's server-side context so its audio can be attributed to it.

| Provider | Transport | Class to wire |
|---|---|---|
| OpenAI | REST | `pipecat.services.openai.tts.OpenAITTSService` |
| ElevenLabs | REST | `pipecat.services.elevenlabs.tts.ElevenLabsHttpTTSService` |
| ElevenLabs | Multi-context websocket | `tts_cache_pipecat.ElevenLabsTTSService` |
| ElevenLabs | Text-to-Dialogue websocket | `tts_cache_pipecat.ElevenLabsDialogueTTSService` |
| Sarvam | REST | `pipecat.services.sarvam.tts.SarvamHttpTTSService` |
| Sarvam | Websocket | `tts_cache_pipecat.SarvamTTSService` |

## Configuration

### Stores

| Store | Use |
|---|---|
| `MemoryStore` | Process memory: the hot set loaded at start (`warm_start_bytes`) and the clips captured in this process |
| `RedisStore` | The store shared by every process of a deployment |
| `SQLiteStore` | A single process, or a test, without Redis |
| `TieredStore(memory, shared, deadline_ms=20)` | Memory first, then the shared store under a hard deadline; the normal production store |

### Policies

A policy decides what is looked up, what is stored, for how long, and which match levels may serve. Combine policies with `AllOf` and `AnyOf`.

| Policy | Effect |
|---|---|
| `DEFAULT_POLICY` | `Probation("48h", "30d")`, `MinChars(3, 400)`, and Level B for punctuation and filler |
| `BASE_POLICY` | The same without Level B: exact matches only |
| `SkipIfMatches(patterns)` | Never store a sentence matching any of the regular expressions; use it for personal or variable data |
| `TurnPosition(first_n=1, last_n=1)` | Cache only the first and last sentences of each turn |
| `SpeakFramesOnly()` | Cache only scripted `TTSSpeakFrame` utterances, never LLM output |
| `Namespace(tag)` | Fold a tag's value, such as a tenant, into the key; clips are shared only within one value, and only exact matches serve |
| `RateLimit(stores_per_minute)` | Bound the write rate |
| `ByTag(tag, routes, default)` | A different policy for each value of a tag |
| `Shadow(policy)` | Look up and record would-be hits, but always call the provider |

Tags are passed on the read processor, as in `TTSCacheRead(cache, tts=tts, tags={"tenant": "acme"})`, and stored with each clip for purges and routing.

### Level C (experimental)

```python
from tts_cache import LEVEL_C_POLICY, TTSCache
from tts_cache.cache import default_matcher

cache = TTSCache(scope, store, policy=LEVEL_C_POLICY, matcher=default_matcher(judge=True))
```

The judge loads a 1.1 GB model at construction, downloaded from the Hugging Face Hub on first use. It adds about 3.2 GB of memory per process and 20 to 40 ms to every sentence that Levels A and B miss. When it serves, the stored sentence replaces the LLM's text in the transcript. See the warning under [Match levels](#match-levels) before enabling it.

## Command line

| Command | Purpose |
|---|---|
| `tts-cache replay --log FILE [--policy SPEC] [--out FILE]` | The hit rate a transcript log would have had |
| `tts-cache warm --from replay.json --scope scope.json --store URL --provider module:factory` | Synthesise a replay's most frequent sentences once and store them under the deployment's keys |
| `tts-cache ping --url redis://HOST:6379/0` | The Redis round trip, against the 10 ms p99 placement rule |
| `tts-cache bench [--r 0,0.25,0.5] [--out DIR]` | Synthetic cost and latency benchmark of the core |
| `tts-cache-pipecat bench [--r 0,0.5]` | The same benchmark through a real Pipecat pipeline |

## Operations

**Placement.** Redis must run in the same region as the containers that run the Pipecat app; a matching region name is not enough. Run `tts-cache ping` from a running container: the rule is a p99 under 10 ms. If no placement meets it, run without the cache, since every miss pays the round trip.

**Sizing.** Clips are stored as Opus at 32 kbps, about 17 to 19 KB per sentence in Redis with metadata, so 10,000 clips take about 180 MB. Warm start loads up to `warm_start_bytes` (4 MB in the example) in about 20 ms.

**Retention.** A new clip lives 48 hours. Its first hit extends it to 30 days, and every later hit renews that window, so sentences said once expire on their own. To cap Redis's size, set `maxmemory` with an LFU eviction policy.

**What Redis holds.** For each clip, the audio under a hashed key, and a metadata hash with the sentence's text and canonical text, its tags, format, timestamps and counters. The text is needed to build the Level B index and to purge by text. Use `SkipIfMatches` to keep identifiers out; no regular expression catches names.

**Deleting audio.**

```python
await cache.store.delete(key)                      # one clip
await cache.store.purge(tags={"tenant": "acme"})   # every clip with these tags
await cache.store.purge(match="refund")            # every clip whose text contains this
```

A purge clears Redis and the calling process at once. Other running processes keep serving the clips already in their memory until they restart, so restart or recycle them when a removal must take effect immediately.

**Failure behaviour.** A store call past its deadline, a Redis error or an exception anywhere in the cache becomes a miss, counted as a `fail_open` event and logged at most once a minute. If the text the provider received ever differs from the text the cache keyed, the capture is discarded and serving stops for that scope until the process restarts.

**Metrics.** The cache emits one event per hit, miss, store, reject and fail-open. `LogSink` writes them as JSON lines to the `tts_cache` logger and keeps counters, so the character hit rate is `chars["hit"] / (chars["hit"] + chars["miss"])`. `tts_cache_pipecat.PipecatMetricsSink` also pushes a `MetricsFrame` per lookup.

## Guarantees and limitations

### Guarantees

- **No wrong voice.** Everything that shapes the sound is part of the key.
- **No changed words from Levels A and B.** Level A serves identical canonical text, and Level B serves only punctuation and filler differences.
- **Exact capture.** Pipecat tags every audio frame with its sentence's context id, and the cache stores a clip only for the exact text the provider received.
- **No truncated clips.** An interruption drops every partial capture of the turn, and clips whose duration or silence does not fit their text are rejected.
- **Fail open.** A cache failure costs at most the 20 ms deadline and otherwise behaves like no cache.

### Known limitations

- **Level C is unsafe.** It serves statements for questions and some changes of meaning; see [Match levels](#match-levels).
- **A purge reaches other processes only when they restart.** See [Deleting audio](#operations).
- **Prosody seams.** Providers that condition a request on the previous one, such as ElevenLabs request stitching, give a live sentence context that a cached clip lacks, so the join in a mixed turn may be audible. No listening audit has been run. `TurnPosition()` limits caching to turn boundaries.
- **Two wiring mistakes go unnoticed.** Without `TTSCacheWrite` nothing is ever stored, and no warning is given. A hand-built `Scope` naming another voice stores clips under that voice's key.
- **Pipecat coupling.** The adapter relies on internal `TTSService` behaviour verified at `pipecat-ai` 1.12.x, commit `1d1385b6d`, and pins that minor version. After an upgrade, rerun `harness/attribution.py`. Undetected drift trips the key check and degrades to no cache, not to wrong audio.
- **Pipelines without a transport.** Pipecat's websocket services pause after each turn until `transport.output()` sends `BotStoppedSpeakingFrame` upstream. A test pipeline without a transport must send it itself, or nothing after the first turn plays.
- **Limited live evidence.** The live results cover one voice each on ElevenLabs and Sarvam, a local Redis and synthetic traffic.

## Development

Prerequisites: [uv](https://docs.astral.sh/uv/), Python 3.12, a Pipecat checkout at `../pipecat` (the workspace pins it as an editable dependency at commit `1d1385b6d`), and `redis-server` for the Redis tests.

```bash
uv sync                           # create .venv with both packages, editable
make lint                         # ruff check, ruff format --check, pyright
make test                         # unit and pipeline tests
make redis-up && make test-redis  # plus the Redis suite on port 6380
make format                       # apply ruff formatting and fixes
```

The live harness calls real providers. It reads `ELEVENLABS_TTS_KEY`, `ELEVENLABS_VOICE_ID`, `SARVAM_API_KEY`, `SARVAM_TTS_MODEL`, `SARVAM_TTS_SPEKAER` and `OPENAI_API_KEY` from a `.env` file:

```bash
make harness SERVICE=all                    # per-sentence audio attribution for each service class
python harness/integration_check.py en      # the wiring above, against ElevenLabs and Redis
python harness/live_bench.py plan           # the live runs and their budget, without calling a provider
python -m tts_cache.testplan.core --help    # the stages of the core evaluation
```

```text
packages/tts-cache/            the core library and the tts-cache command
packages/tts-cache-pipecat/    the Pipecat processors and service subclasses
harness/                       live provider harnesses and corpora
examples/wiring.py             the wiring above, runnable without credentials
docs/diagrams/                 the design diagrams; generate.py rebuilds them
SOLUTION.md                    the full design
```

The design, with the reasoning behind each decision, is in [SOLUTION.md](SOLUTION.md).
