# Test plan: usefulness, impact, usability and spread of the TTS cache

This plan produces the final evaluation of the cache. It runs once. Each live provider call happens exactly once, and every question the report asks is answered from data saved during that run.

The plan covers Level A alone, Levels A and B, and Levels A, B and C. It runs them in the twelve supported languages (English and eleven Indic languages), at repetition 0.25 and 0.5, against several providers, and at a range of cache sizes. Non-Indic languages other than English are out of scope for this run.

> Amendments of 2026-10-18 17:15 IST, made by the monitoring session to match the code at commit `feat(lang)`: configurations in section 4 reflect that Levels B and C are on by default; `t_lang` is 0.90 in every language; section 5 lists the twelve shipped languages and drops fr, es, pt and ar; section 11.1 adds live cells for the nine new Indic languages; V6's Level C gate is 6 ms p99, the documented budget for the ONNX encoder.

It is written for a coding agent working in this repository; section numbers in parentheses refer to [SOLUTION.md](SOLUTION.md).

Every number carries one of these labels. The first three are the labels `TEST_REPORT.md` uses.

- **live**: a real provider over the network, through a real Pipecat pipeline.
- **measured**: the cache's own code on this machine, with no provider involved.
- **simulated**: the bench's stand-in provider.
- **composed**: live provider latency combined with a measured hit sequence (section 11.3).
- **derived**: arithmetic on the numbers above, such as monthly cost at a stated call volume.

## 1. What the final report answers

| Question | Answered by | Tables |
|---|---|---|
| **Is it safe?** Does any configuration ever play a clip with the wrong facts or meaning? | the oracle over every labelled sentence, offline and live | T1 |
| **Is it useful?** Which level should each language run, and what does each level add? | verdict rules fixed before the run (13.1) | T0, T2, T3 |
| **What is the impact?** How many characters and dollars it saves, and how many milliseconds of waiting it removes per turn and per call, at stated volumes and against the cache's own running cost | the impact model (13.2) | T0, T4, T5, T11 |
| **Is it usable?** How hard it is to integrate, operate, size and predict, and what happens when it is misconfigured or its store fails | the usability tests (section 10) | T12 |
| **What is the spread?** How much results vary across seeds, sessions, turns, languages, providers and cache sizes, including the tails and the worst cell | the distributions of section 7.4 | every table carries its spread, plus T13 |

The report opens with a one-page summary, T0, described in section 14.

## 2. Run once

Offline work costs nothing, but the live part runs exactly once. The protocol below makes that safe.

### 2.1 Freeze

Before any live call, one commit, `testplan: freeze`, fixes the following:

- this plan and the verdict thresholds;
- the corpora: pools, assembled JSONL and seeds;
- the harness and analysis code, the price table with its date, and the provider and model per cell.

Every output file records that commit's SHA, the pinned Pipecat SHA (`1d1385b6d`) and the package versions. Changing anything after the go invalidates the run.

### 2.2 Rehearsal (free)

Run the whole plan end to end with no provider: the offline matrix, the size sweeps, the usability tests, and the live harness against `FakeTTSService`. The fake service is configured with each real cell's measured time to first byte (TEST_REPORT.md) and real audio lengths. The report generator must then produce every table and chart from the rehearsal data, with every V-check evaluated.

The rehearsal injects the following faults, and the harness must survive each one as section 2.4 specifies:

- a provider error;
- an empty audio stream (the OpenAI Hindi case);
- a 429 rate limit;
- a stalled stream;
- a harness crash mid-session, resumed from its checkpoint;
- a Redis instance killed mid-run.

A rehearsal failure is fixed and the rehearsal repeated, still at no cost. The go needs one clean rehearsal.

### 2.3 Preflight and go/no-go (about 10,000 characters)

Each item below must pass. The results are recorded in `preflight.json`.

1. **Credentials and coverage.** One 40-character sentence per language × service class (11.1). A cell that fails is marked n/a with its error and dropped before the go.
2. **Cache path per cell.** The same sentence runs twice through the full live pipeline with the cache on. The second run must make no provider request and must return a byte-identical digest.
3. **REST close.** On each REST class, two back-to-back sentences: the second sentence's audio must start without Pipecat's 3 s idle wait (11.2).
4. **Concurrency.** A 20-sentence probe per provider, at concurrency 1 and at the chosen concurrency. If p50 time to first audio differs by more than 10 percent, lower the concurrency.
5. **Quota.** Account balances where the API exposes them (the ElevenLabs subscription endpoint). The owner confirms the others. The balance must cover the computed worst case (11.4).
6. **Disk and Redis.** At least 10 GB free. One dedicated Redis instance or database per concurrent run; port 6379 is never touched.
7. **Network baseline.** TCP connect time to each provider host, measured 20 times. It is repeated after the run.

### 2.4 During the run

The run always completes. No failure causes a run to be repeated.

- **Provider failure on a sentence.** An error, an empty stream or a stall past 15 s is retried twice inside the session, with backoff.
  - Every attempt is billed and recorded.
  - A retried sentence's latency is excluded from the latency statistics and counted in a provider-reliability table.
  - If the sentence still fails, it is marked `provider_failed` and the session continues. V4 accepts this as an explanation for the sentence.
- **Rate limit.** On a 429, back off and lower that provider's concurrency for the rest of the run.
- **Crash.** Rows are flushed per sentence, and the Redis state survives a harness crash. On restart, the run resumes at the next unsent sentence, in a fresh pipeline, and the resume is recorded. Sentences already sent are never sent again.
- **A correctness failure is a result, not a stop.** If a cache-originated error reaches the host, or audio is attributed to the wrong sentence, the harness records it as critical, with every frame of that context, and carries on. The report leads with it.
- **Everything is saved** under `harness/out/testplan/<run-id>/`, which is git-ignored:
  - per-sentence rows;
  - a frame log, with every frame's type, context id, size and monotonic timestamp;
  - the provider audio of every miss, as FLAC;
  - a digest of every hit;
  - provider request ids and headers;
  - a `RedisStore.export` of every run's store at the end;
  - the environment file.

  That comes to about 3 to 4 GB in all.

### 2.5 After the run

Everything else runs on saved data alone, with no further provider calls: analysis, composition, ASR on the saved audio, the listening check and the report. If the analysis code turns out to have a bug, it is fixed and rerun on the same data.

## 3. Principles

- **Hit decisions offline, provider latency live.** Whether a sentence hits depends only on the corpus, the configuration and the cache code. That makes the hit rate measurable offline, over many seeds, at no cost. Provider latency drifts between runs, so it is measured live. All live runs of a cell go at the same time, so every configuration sees the same network and provider load. The live hit sequence must equal the offline one for the same corpus (V4).
- **The corpus decides the result.** How often the agent repeats itself sets Level A's saving. The share of near-duplicates and paraphrases sets B's and C's. Those shares are assumptions, so the plan publishes them and labels every sentence. It also reports recall per label, so any reader can reweight the results to their own traffic. For a real deployment, `tts-cache replay` over a real transcript log is the number to trust; U4 measures how well replay predicts.
- **Correctness first.** Each label states which configurations may serve it. Any other serve is a wrong serve, and its characters do not count as saved.
- **Enough samples for the spread.** Offline, each cell runs 10 seeds × 200 sentences. Live percentiles are reported only up to p95, with n beside each one. A p99 is reported only where n ≥ 1,000, which in practice means offline.

## 4. Configurations

| Name | Policy | Matchers | Ships |
|---|---|---|---|
| OFF | none | none | baseline: every sentence goes to the provider |
| A | `BASE_POLICY` | `ExactMatcher` | Level A alone |
| A+B | `AllOf(BASE_POLICY, LevelB({"punctuation", "filler"}))` | `NearDuplicateMatcher` | hits are reported per class |
| A+B+C | `DEFAULT_POLICY` | `default_matcher()`: near plus `SemanticMatcher()` with the shipped routes | the shipped default |

`t_lang` is the threshold the acceptance check chooses with the slot and span vetoes: the lowest threshold at or above the 0.90 floor that rejects every near-miss the vetoes let through. The vetoes stop every fixture near-miss in all twelve languages, so `t_lang` is 0.90 everywhere. The 80 percent paraphrase bar in section 5 is not a gate here; paraphrase recall is one of the results (T3).

Repetition rates are r ∈ {0, 0.25, 0.5}. r = 0 runs offline only. It is a control: nothing can hit, so it measures each level's pure overhead.

## 5. Languages

Twelve languages are supported and frozen together: en, hi, bn, as, pa, gu, mr, or, ta, te, kn, ml. Each has a canonicaliser fixture set, splitter overrides, filler and slot lexicons, Level B fixtures, and 200 semantic pairs from which the acceptance check chose `t_lang` = 0.90. What each still needs for this plan is its corpus pool (6.3). Odia uses Sarvam's code `od-IN`; Assamese is not offered by Sarvam or ElevenLabs Flash and runs on OpenAI alone (preflight item 1 confirms).

If a pool is not ready at the freeze, the run goes without that language and the report says so; it is not added afterwards.

## 6. Corpus

### 6.1 Labels and the oracle

Each sentence is labelled by how it relates to earlier sentences in the same corpus. This table is the oracle that runs are checked against.

| Label | Example (en) | A | A+B | A+B+C |
|---|---|---|---|---|
| `new` | first occurrence of a base sentence | miss | miss | miss |
| `exact` | identical string | hit | hit | hit |
| `surface` | differs only in what the canonicaliser removes: spacing, curly quotes, Unicode form, joiners, native digits, emoji, markdown, the trailing period, case | hit | hit | hit |
| `punct_inner` | a comma added or removed | miss | hit | hit |
| `punct_end` | `.` ↔ `!` | miss | hit, flagged | hit, flagged |
| `filler` | "Okay, …", using a word from the language's filler list | miss | hit | hit |
| `paraphrase` | same meaning, same slots, different words | miss | miss | hit if similarity ≥ `t_lang` |
| `slot_change` (decoy) | one name, number, date or amount changed | miss | miss | **must miss** |
| `meaning_change` (decoy) | negation or antonym, no slot changed: "was successful" → "was not successful" | miss | miss | **must miss** (the negation flag and the insertion rule of the span veto stop it; a reworded antonym with no negation word relies on the 0.90 floor) |
| `punct_question` (decoy) | `?` added or removed: "You're all set." → "You're all set?" | miss | **must miss** | **must miss** |

A "hit" in this table means the earlier copy was visible in time. For Level A that means it was captured in an earlier turn. For Levels B and C it means an earlier session, because their indexes are built over the hot set at warm start (`TTSCache.start`). V3 audits every expected hit that missed.

One decoy is expected to be served, by the owner's decision of 2026-10-18, and is reported rather than fixed:

- **`punct_question`.** The Level B classifier labels any difference with identical tokens `punctuation`, so A+B can answer a question with a clip spoken as a statement. The owner accepts this for now; the plan reports every such serve beside the verdict.
- **`meaning_change`** was expected to fail when this plan was written. The negation flag and the insertion rule of the span veto (SOLUTION.md 5) now stop every negation decoy in the fixtures; the oracle keeps it as a must-miss and any serve is a critical finding.

### 6.2 Mix

Each corpus is made of three groups of sentences:

- r: the share of sentences that are servable repeats (`exact`, `surface`, `punct_inner`, `punct_end`, `filler`, `paraphrase`);
- d = 0.10: the share that are decoys;
- the rest: `new`.

| Within repeats | exact 55 % | surface 10 % | punct_inner 8 % | punct_end 2 % | filler 10 % | paraphrase 15 % |
|---|---|---|---|---|---|---|
| **Within decoys** | slot_change 60 % | meaning_change 20 % | punct_question 20 % | | | |

At r = 0.25, a 200-sentence corpus holds 50 repeats, 20 decoys and 130 new sentences. At r = 0.5 it holds 100, 20 and 80.

No traffic data stands behind these shares. They were chosen so that every label gets at least 10 events per language across the 10 seeds. Because recall is reported per label (T3), any reader can recompute the headline under their own mix. An exact-only mix (100 percent `exact`, no decoys) also runs, to give Level A's ceiling and to stay comparable with TEST_REPORT.md.

### 6.3 Pools

Each language has a pool in `harness/corpus/<lang>.yaml`:

- **120 base sentences** of the kind a voice agent says, across five domains: support, banking, delivery, telecom and appointments. They cover openers, acknowledgements, hold lines, questions, confirmations and closers. Lengths run from 15 to 160 characters, with a median near 50.
- **Slot annotations** on each base sentence: span, kind and three alternative values.
- **One authored paraphrase and one authored meaning change** per base sentence. The paraphrases must not appear in `tests/fixtures/semantic/`, because the threshold was chosen on those pairs.
- **Rule-generated variants**: `surface`, `punct_*`, `filler` and `slot_change`.

A shape test checks every pool. An LLM drafts each pool, and a fluent reader reads all of it before the freeze.

### 6.4 Assembly

`tts_cache.corpus.labelled(pool, n=200, r, d, seed)` builds a corpus deterministically for each seed:

1. Each position is drawn as a repeat, a decoy or a new sentence.
2. Repeats and decoys draw from the base sentences already introduced, with Zipf weights (s = 1.0).
3. Sentences are grouped into turns of 1 to 4 sentences, and turns into sessions of 3 to 6 turns.

The output is JSONL in the replay log format, with `label`, `base`, `turn` and `first_in_turn` fields added, which `read_log` ignores. The same file feeds the bench, `tts-cache replay` and the live harness.

## 7. Metrics

### 7.1 Cost

- `billed_chars`: characters sent to the provider, counted as code points of the stripped text, retries included.
- **Character-weighted hit rate**: served characters divided by total characters, with wrong serves excluded. Also reported: the sentence hit rate, and hits by level (A; B by class; C).
- **Increments**: Δ(B) = A+B − A and Δ(C) = A+B+C − A+B, computed per seed and then pooled.
- **Capture efficiency**, in two forms:
  - served characters ÷ characters of all servable repeats. This is the design's real-world yield, after losses to same-turn and same-session repeats.
  - served characters ÷ characters of expected hits (6.1). This shows whether the mechanism does what it should, and V3 explains every point it falls short.
- **Money**: saved characters × the frozen list prices. OpenAI `gpt-4o-mini-tts` bills mostly on audio output tokens, so its saving is also reported in audio seconds.
- **The cache's own cost**: bytes stored per 1,000 clips; Redis `used_memory`; process memory with the Level C model loaded; CPU milliseconds per sentence.

### 7.2 Correctness

- **Wrong serves**, by label; **flagged serves** (`punct_end`).
- **Transcript truth** (live): on every B and C hit, the `TTSTextFrame` equals `Hit.spoken_text`.
- **Attribution** (live): every hit's audio digest equals the stored entry's digest.
- **Listening check**, from saved audio, by a fluent listener per language:
  - up to 20 B serves and 20 C serves, each played next to its requested text and marked acceptable or not;
  - 10 blind pairs of the provider's original against the Opus clip the cache serves, for codec transparency (see U7).
- **ASR.** `harness/transcribe.py` reports each served clip's WER against both the stored text and the requested text.

### 7.3 Latency

- **L1, turn start.** The time from the moment the turn's first sentence becomes releasable, as `cache_roundtrip.py` defines it, to its first audio frame out of `TTSCacheWrite`. This is the headline.
- **L2, sentence time to first audio**, split into hits and misses.
- **L3, in-turn stall.** For each sentence k ≥ 2, the gap max(0, first audio of k − end of playback of k−1), where end of playback = first audio of k−1 + its audio duration. L3 is the sum of these gaps over the turn. REST contexts close as soon as their response ends (11.2), so L3 shows provider and cache time, not Pipecat's idle timer.
- **L4, miss overhead** (offline, r = 0): lookup plus capture on the request path, per configuration.
- **L5, hit path by tier**: time to the first decoded chunk from memory, Redis or SQLite.

### 7.4 Spread

Every metric reports its spread, not only its centre.

- **Across seeds**: a 95 percent bootstrap interval on every rate and increment.
- **Across sessions**, which stand in for calls:
  - the distribution of per-session character hit rate and dollars saved (p10, p50, p90);
  - the share of sessions with zero hits;
  - the share of sessions where every turn started from the cache.
- **Across turns**: the share of turns whose first sentence is a hit (the turns where latency improves), and the CDF of L1 per configuration and provider.
- **Across languages and providers**: the minimum, median and maximum of each headline metric, with the worst cell named.
- **Tails**: offline, L4 and L5 at p99 and p99.9, plus the maximum. Live, p95 and the maximum, each with n.
- **Across time**: live L1 p50 in 10-minute windows over the run, together with the network baseline before and after (2.3, item 7). A drift beyond the OFF run's own spread is reported.

## 8. Offline matrix

The matrix crosses ten languages, the four configurations (OFF, A, A+B, A+B+C), three rates (r ∈ {0, 0.25, 0.5}), 10 seeds and 200 sentences per run. With the exact-only mix, that comes to about 1,300 runs and 260,000 lookups.

- **Profile.** The primary profile is `serverless`: a fresh process per session, with a warm start from Redis. The r = 0 overhead rows also run in profile `single` (SQLite).
- **Provider.** The bench's simulated provider. Its latency is discarded; latency comes from 11.3.
- **Output.** One row per sentence: seed, configuration, label, level, tier, hit, characters, `lookup_ms`, `first_chunk_ms`, `capture_ms`, and the reject reason if any.
- **Parallelism.** 8 worker processes. Each owns one Redis database on port 6380, flushes it before each run, and loads the Level C model once. Expect about 15 minutes.

## 9. Cache size

In this design, "cache size" means three separate limits. Each sweep varies one of them and holds the others at their defaults. Latency at each size is composed (11.3), so size needs no live runs. Redis also samples keys when it evicts, so under memory pressure hit sequences are not deterministic, and V4 does not apply.

| Limit | Knob (default) | What it changes |
|---|---|---|
| Shared store capacity | Redis `maxmemory` with `allkeys-lfu` (4.5); SQLite `max_bytes` | hit rate and cost |
| Process memory per session | `warm_start_bytes` (4 MB), `warm_texts` (2,000), `session_bytes` (64 MB) | which tier serves a hit; warm-start time and memory; what Levels B and C can match |
| Population | entries in one scope | lookup and probe latency; index memory and build time; correctness of B and C when there are many neighbours |

### 9.1 Store capacity: hit rate and cost

A 200-sentence corpus stores about 2.7 MB, so it never fills a store. This sweep uses longer streams instead.

- **Streams.** `generate` streams of 20,000 sentences at r = 0.5: about 10,000 distinct clips, or about 160 MB of Opus. English runs at Zipf s ∈ {0.8, 1.0, 1.2}, Hindi and Tamil at s = 1.0; each runs 3 seeds, exact repeats only, under configuration A. Here a language matters only through its bytes per clip, so the other languages' curves are translated from the bytes per character of their live clips.
- **Capacities.** 5, 10, 25, 50 and 100 percent of the unbounded run's peak.
- **Redis arm.** One dedicated instance per capacity, on ports 6381 and up, started with `--maxmemory <capacity> --maxmemory-policy allkeys-lfu --save ""`.
- **SQLite arm.** English at s = 1.0, with `max_bytes` and a sweep after each session. This arm is deterministic and uses the design's size-aware score.
- **Calibration.** The synthetic tones must encode to within 20 percent of real speech's Opus bytes per second (15.8 KB per 4 s clip). If they do not, the clip lengths are scaled and the factor recorded. Each distinct clip is encoded once per stream and reused across capacities.
- **Levels B and C under pressure.** The labelled corpora run at 10, 25, 50 and 100 percent SQLite capacity, under every configuration, with 10 seeds. A variant whose original was evicted must miss, and V3 must explain the miss as `evicted`.
- **Report.** Hit rate against capacity; the knee, defined as the smallest capacity that keeps 90 percent of the unbounded saving; evictions; and where Redis LFU and SQLite's score diverge. About 10 minutes.

### 9.2 Process memory: tier mix, warm start, and the reach of B and C

First fill the store with a 20,000-sentence stream, then run the labelled corpus in the serverless profile with:

- `warm_start_bytes` ∈ {0, 1, 4, 16, 64} MB, at `warm_texts` = 2,000;
- `warm_texts` ∈ {0, 500, 2,000, 10,000}, at 4 MB.

Per session, measure:

- **Warm-start duration**, split into the Redis fetch, the load and the B and C index builds. `start()` waits for the index build, so this delays a serverless session's first lookup.
- **Process memory (RSS)** after warm start.
- **Tier mix**: the share of hits served from memory rather than Redis, with their L5 timings.
- **First-turn L1**, composed.
- **Hits by level.**

### 9.3 Population: latency, memory and correctness at scale

Run N ∈ {1,000, 10,000, 100,000} entries in one scope, for each frozen language.

- **Fill.**
  - For B and C: slot expansions of every pool sentence (the neighbours the veto must stop), each base sentence's meaning-change variant, and `generate` sentences for the rest.
  - For A: distinct clips, 1 KB each at 100,000 entries so that Redis stays within 256 MB. A 10,000-entry run at real clip size checks the effect of value size.
- **Latency.** Level A lookup p50 and p99 from memory, Redis and SQLite; Level B probe p99; Level C embed plus probe p99; index build time.
- **Memory.** Index memory, and Redis `used_memory` per entry.
- **Correctness.** Probe every labelled query in the pool at each N, and report paraphrase recall and wrong serves against N. The Level C probe takes only the single nearest entry (k = 1) and then applies the veto. A nearer neighbour that the veto rejects therefore hides a valid paraphrase, so recall should fall as slot expansions crowd the index; that fall is the number to report. Negation decoys meet more near-identical neighbours as N grows.
- About 15 minutes.

## 10. Usability

These tests measure what it takes to adopt, configure and run the cache, and what an integrator or operator sees when something goes wrong. All of them are offline, except U7, which uses saved live audio.

- **U1, integration from the docs alone.** A fresh agent with no context gets a stock Pipecat bot, the README and the package docstrings. A human developer new to the code is better, if one is available; otherwise the agent. It gets three tasks:
  1. add the cache with a Redis store;
  2. enable Level B for punctuation;
  3. estimate the saving from a given transcript log with `tts-cache replay`.

  Record lines added and files touched, wall-clock, attempts until the bot's test passes, and every doc gap, quoted verbatim. Today's wiring example (`examples/wiring.py`) is the reference: about 20 lines of cache wiring.
- **U2, defaults versus tuned.** From sections 8 and 9: the default configuration's saving as a share of the best configuration found for each language. Also list every knob a typical deployment must change from its default, if any.
- **U3, footprint.** Install size per extra, measured in a fresh venv. Import time of `tts_cache` and `tts_cache_pipecat`. The Level C model's download size and load time. Process memory (RSS) per configuration. The time each configuration adds to a serverless session's start (warm start plus model load).
- **U4, prediction before deploying.** `tts-cache replay` over each live run's log, set against that run's measured hit rate. This tells an adopter how far to trust the estimate they get before deploying.
- **U5, the store failing.** A 200-sentence serverless run with each of these faults injected into its dedicated Redis:
  - the instance killed and restarted;
  - `CLIENT PAUSE` for 5 s;
  - `DEBUG SLEEP` 1 s, repeated;
  - memory full under `noeviction`;
  - a corrupt entry.

  For each fault, measure: errors reaching the host (must be 0), the latency added per sentence during the fault (p50, p99 and max, which must stay within the 20 ms deadline plus decode), the hit rate during the fault, and the time to recover after it ends.
- **U6, observability.** From the cache's emitted metrics alone (`LogSink`, `PipecatMetricsSink`), with no access to the oracle, an operator computes the hit rate, the saved characters and the rejects by reason. These must equal the ground truth (V10).
- **U7, what the caller hears.** The listening check of 7.2. For B and C serves, the share judged acceptable. For codec transparency, the share of blind pairs the listener tells apart (about 50 percent means the Opus clip is indistinguishable from the original).
- **U8, misconfiguration.** Wire each documented constraint wrongly, one at a time, against the fake service:
  - text filters left on the service;
  - `reuse_context_id_within_turn=True`;
  - a missing `TTSCacheWrite`;
  - the scope built for the wrong voice;
  - a mismatched sample rate.

  For each, record whether any clip is served wrongly (must be no), whether the cache warns or emits a metric that names the problem, and the hit rate that results.

## 11. Live runs

### 11.1 Cells

| Languages | Core cells: 8 runs each | Provider-spread cells: 2 runs each |
|---|---|---|
| hi, ta | `sarvam_ws_flush`, `openai_http` | `elevenlabs_http`, `elevenlabs_ws_fc`, `elevenlabs_dialogue_ws_c`, `sarvam_http` |
| bn, pa, gu, mr, or, te, kn, ml | `sarvam_ws_flush`, `openai_http` | none |
| as | `openai_http` | none |
| en | `elevenlabs_ws_fc`, `openai_http` | `elevenlabs_http`, `elevenlabs_dialogue_ws_c`, `sarvam_http`, `sarvam_ws_flush` |

- **Core cell runs (8).** OFF at r = 0.25 and at r = 0.5, then A, A+B and A+B+C at each r. All of them use seed 1's corpus from the offline matrix.
- **Provider-spread cell runs (2).** OFF and A+B+C at r = 0.5. Hit decisions do not depend on the provider, so these cells add provider latency spread across all six service classes for en, hi and ta, where TEST_REPORT.md already showed per-provider differences from 131 ms to 2.4 s.

### 11.2 Procedure

`harness/live_bench.py` runs every cell.

- **Session setup.** Each session is a fresh pipeline, `[TTSCacheRead, service, TTSCacheWrite, Probe]`, with a fresh cache that warm-starts from that run's own Redis database. The connection opens before the first turn and is not counted in L1.
- **Turns.** Each turn streams token by token, 20 ms apart, to mimic LLM output. Nothing simulates user speech. Between turns, the harness only waits for queued captures to land.
- **REST contexts close when their response ends.** In stock Pipecat, a REST sentence's audio context stays open until `stop_frame_timeout_s` (3 s) passes with no new audio (`tts_service.py`, `_handle_audio_context`). The next sentence's audio waits behind it. The harness's REST classes instead append a `TTSStoppedFrame` and remove the audio context as soon as the HTTP response ends, as Pipecat's own Sarvam websocket service does on its `final` event. OFF and ON runs use the same classes. Shortening the timer instead would be unsafe: it also runs before the first audio chunk arrives, and OpenAI and Sarvam REST take 1.7 to 2.4 s to their first byte.
- **Concurrency.**
  - All cells and runs start together, at most 8 concurrent runs per provider, or the account limit if lower.
  - The sessions of an OFF run go in parallel.
  - The sessions of an ON run go in order, because each warm-starts from the sessions before it.
- **What is recorded.** Per sentence: release and first-audio times, audio duration, label, hit, level and tier. Also: provider requests, counted with `on_tts_request`; transcript frames; digests; and any `ErrorFrame`. Everything else that section 2.4 lists is saved too.
- **Budget guard.** Before starting, the harness computes the worst-case billable characters from the frozen corpora, and refuses to start if they exceed `--max-chars`. The cap is set at that worst case, so it can never stop a run partway.

### 11.3 Composition

For each (language, provider) cell, the two OFF runs give the empirical distributions of provider time to first audio, for all sentences and for turn starts. Each offline seed's hit sequence is then replayed:

- a hit costs the measured hit path;
- a miss costs a provider time to first audio resampled from those distributions, plus the measured miss overhead.

This gives **composed** L1 and L3 for every configuration, rate and cache size, with intervals over seeds. The live runs of the same corpus validate it: composed and live L1 p50 must agree within the OFF runs' own spread, which is estimated by comparing the two OFF runs.

### 11.4 Budget and time

The estimate assumes about 60 characters per sentence, or 12,000 per corpus. An ON run sends about 80 percent of that at r = 0.25 and about 55 percent at r = 0.5. Preflight replaces these estimates with exact figures computed from the frozen corpora and the offline misses.

| Provider | Expected characters | Expected cost | Worst case (nothing hits) |
|---|---|---|---|
| ElevenLabs | about 510,000 | about $25 | about $34 |
| Sarvam | about 430,000 | about $10 to $16, depending on the model | about $14 to $21 |
| OpenAI | about 720,000 | about $12 | about $15 |
| **Total** | about 1.66 million | **about $50** | **about $70** |

Speech-to-text for the listening sample and ASR costs under $1.

**Time.** A miss takes about 1 s on a websocket and about 3 s on OpenAI REST; a hit takes milliseconds. OpenAI sets the pace: 60 ON runs at 8 at a time take under an hour. The whole plan needs about 3 hours of machine time:

| Stage | Time |
|---|---|
| Rehearsal | about 30 minutes |
| Offline matrix, size sweeps and usability tests | about 45 minutes |
| Preflight | about 10 minutes |
| Live run | under 1 hour |
| ASR and report | about 15 minutes |

The listening check is separate: about 10 minutes per language for a fluent listener.

## 12. Validity checks

Each check is a test or a harness assertion. A failing check is reported next to the numbers it affects; those numbers are marked as not established.

- **V1, accounting.** Offline: billed(OFF) − billed(config) = served characters, exactly. Live: provider requests equal misses plus retries, and billed text equals the missed sentences.
- **V2, oracle.** Every serve is allowed by its label (6.1). A serve of a `slot_change`, `meaning_change` or `new` sentence is a critical finding. `punct_question` serves are counted as wrong and excluded from savings.
- **V3, explained misses.** Every expected hit that missed carries a reason from a closed list:
  - its earlier copy was in the same turn (A) or the same session (B, C);
  - the capture was rejected, with the reason;
  - the earlier copy was evicted;
  - it was outside the warm set;
  - its similarity was below `t_lang`;
  - Level B classified it `other`;
  - the slot veto stopped it;
  - `provider_failed`.

  A miss with no reason fails the check.
- **V4, live = offline.** The same corpus and configuration give the same hit sequence, sentence by sentence. The only exceptions are live capture rejects and `provider_failed`. V4 does not apply under Redis eviction.
- **V5, replay = bench.** Replay reproduces the offline hit rate within 2 points (M4).
- **V6, overhead gates in every language.** The A miss path adds under 1 ms per sentence; the B probe is under 1 ms p99; C embed plus probe plus vetoes is under 6 ms p99 with the ONNX encoder (measured 3.6 ms).
- **V7, monotonicity.** Per seed, hits(A) ≤ hits(A+B) ≤ hits(A+B+C). Any violation is explained.
- **V8, eviction is safe.** No cache error reaches the host and nothing is served wrongly at any capacity. An entry with its audio evicted but its metadata kept, or the reverse, is a miss.
- **V9, defaults hold when full.** With 2,000 warm texts indexed, the M5 gates still hold.
- **V10, metrics = truth.** Hit rate, saved characters and rejects computed from the emitted metrics equal the oracle's (U6).

## 13. Verdict rules and impact model

These rules are part of the freeze and do not change after the run. The owner confirms the thresholds before the freeze (section 18).

### 13.1 Verdict per language

A level is **safe** in a language when it has zero `slot_change`, `meaning_change` and `new` serves across:

- its offline runs (2,000 sentences per rate);
- the population sweep at the default size;
- its live runs.

A single serve makes it unsafe.

Level B, and separately Level C, is **worth enabling** in a language when all of these hold:

- it is safe;
- its Δ has a 95 percent interval above 0 at r = 0.25;
- V6 holds.

Level A is **working as designed** when it is safe, its capture efficiency against expected hits (7.1) is at least 99 percent, and V6 holds.

The recommendation for each language is the largest configuration that is safe and worth enabling: A, A+B with the named classes, or A+B+C. `punct_question` serves are reported beside it; enabling Level B's punctuation class carries that risk until the classifier changes.

### 13.2 Impact model (derived)

**Inputs, all measured:** the character and sentence hit rates with their intervals (T2); the composed L1 per provider (T5); characters per sentence; bytes per clip; and the knee capacity (9.1).

**Inputs, stated in the report:** call volumes of 10,000, 100,000 and 1,000,000 calls a month, at the corpus's sentences per call; the frozen provider prices; and a managed-Redis price per GB-month.

**Outputs, per provider and configuration, at r = 0.25 and r = 0.5:**

- characters and money saved per month, with intervals;
- the share of turns that start from the cache;
- milliseconds of waiting removed per hit turn and per call (p50 and p90);
- the Redis memory needed at the knee for that volume's working set, and its monthly cost;
- the net saving;
- the break-even hit rate, at which the saving equals the cache's own running cost.

## 14. Report

`TEST_REPORT.md` is generated from the saved data. Every number is labelled, and every table carries its spread (7.4).

- **T0, one-page summary.**
  - The verdict table: one row per language, with its recommended configuration, safe yes or no, Δ(B), Δ(C), and turn-start L1 OFF → ON for its websocket and REST provider.
  - The impact at 100,000 calls a month.
  - The usability findings in one line each.
  - The largest spreads and the worst cell.
  - Any critical finding first.
- **T1, correctness**: wrong serves by label and flagged serves, per language × configuration × r.
- **T2, cost**: the character hit rate with its interval; Δ(B) and Δ(C); the sentence hit rate; capture efficiency; saving per million characters per provider; audio seconds saved.
- **T3, per-label recall** × configuration, per language, with paraphrase recall at `t_lang`.
- **T4, live latency** per language × provider: L1 p50, p95 and max for OFF and each configuration; hit time to first audio; L3; n for each.
- **T5, composed latency** for every configuration and r, with its check against T4.
- **T6, overhead**: L4 and L5 at p50, p99, p99.9 and max, per configuration and language.
- **T7, languages**: support status, `t_lang`, the preflight result and the provider reliability (retries and `provider_failed`).
- **T8, capacity**: hit rate against capacity per Zipf s and arm; the knee; evictions; B and C under pressure.
- **T9, process memory**: warm-start time, process memory (RSS), tier mix, first-turn L1, and B and C hits, against `warm_start_bytes` and `warm_texts`.
- **T10, population**: latency per level, memory, build time, paraphrase recall and wrong serves, against N.
- **T11, impact**: the 13.2 outputs at all three volumes.
- **T12, usability**: U1 to U8.
- **T13, spread**: per-session distributions, the share of zero-hit sessions, CDFs of L1, the drift over time, and the minimum, median and maximum across languages and providers.

**Charts:**

- L1 CDF per provider, OFF against each configuration;
- hit rate against r per language and configuration;
- hit rate against capacity;
- per-session saving histograms;
- B and C recall and wrong serves against N.

## 15. What to build

1. `tts_cache.corpus.labelled` and the pool schema (6.3, 6.4), with a shape test.
2. Bench support for the four configurations: a `levels` argument to `run_core` that wires `SemanticMatcher` with a forced route and `t_lang`, per-sentence rows, and the exact-only mix.
3. Size knobs in the bench: one Redis instance per capacity; SQLite `max_bytes` with a sweep after each session; `warm_start_bytes`, `warm_texts` and `session_bytes`; sampling of process memory (RSS) and `used_memory`; encoded-clip reuse; and `fill(store, scope, n, kind)`.
4. The fault and misconfiguration scenarios of U5 and U8, the metrics-only calculator of U6, and the footprint script of U3.
5. `tts_cache.testplan`. It runs the offline matrix, the size sweeps and the usability tests on a worker pool, applies the oracle and the V-checks, composes latency, applies the verdict rules, computes the impact model and writes the report.
6. `harness/live_bench.py`. It generalises `cache_roundtrip.py` to a corpus of sessions and adds:
   - OFF mode and the configuration flag;
   - the REST close subclasses;
   - per-provider concurrency with 429 backoff;
   - one Redis database per run;
   - in-session retries;
   - per-sentence checkpoint and resume;
   - the budget guard;
   - digest and transcript checks;
   - full raw capture (2.4);
   - the preflight (2.3).
7. A fake-provider mode for the live harness, with injected faults, for the rehearsal (2.2).
8. The pools: en, hi and ta first, then each other language as its support lands.

## 16. Order of work

1. Build items 1 to 7, and the en, hi and ta pools.
2. Pools for the other languages as their support lands, each one read in full by a fluent reader.
3. Rehearsal (2.2), repeated until clean.
4. Freeze (2.1).
5. Offline matrix, size sweeps and usability tests.
6. Preflight (2.3), then go/no-go.
7. The live run (section 11), once.
8. ASR, the listening check, composition, verdicts and the report, all from saved data.

## 17. Not covered, and threats to validity

- **The synthetic mix is not your traffic.** The mitigations are per-label recall (T3), the exact-only ceiling, and U4's measure of how well replay predicts a real log.
- **LLM-authored paraphrases** may be easier or harder than a production LLM's.
- **Compressed time.** Offline runs fit days of traffic into minutes:
  - Redis's LFU counter decays every `lfu-decay-time` minutes;
  - the hot set's forward decay runs on wall-clock time;
  - probation's 48-hour TTL never expires inside a run.

  So the capacity curves show frequency without ageing. In production, probation clears singletons before eviction has to, so 9.1 probably overstates the capacity a given hit rate needs.
- **One window, one connection, one region.** The live run takes about an hour from a home connection. The drift check (7.4) shows variation within that hour, not across days. Datacentre latency, remote Redis and placement are out of scope; `tts-cache ping` is the first step there.
- **Cold start.** Each 200-sentence corpus starts from an empty store. The 9.1 streams come closer to steady state.
- **One voice per language.** Hits never cross scopes, so several voices divide the hit rate.
- **Provider character counting** may differ from code points, especially for Indic scripts. Where a provider exposes usage, the report reconciles it with V1.
- **Prosody at sentence seams** is out of scope, apart from the listening check.
- **Load.** Concurrent sessions sharing one store at production load are not tested; the concurrency of 11.2 runs separate stores.

## 18. Decisions before the freeze

The plan's defaults are shown in brackets.

- **Verdict thresholds:** Δ interval above zero [at r = 0.25]; Level A capture efficiency against expected hits [99 percent]. The listening acceptability rule is dropped by the owner's decision of 2026-10-18: the owner spot-checks a few English and Hindi serves, and the ASR check of 7.2 stands in for every language.
- **Budget cap** [the computed worst case, about $70].
- **Provider concurrency limits** for the ElevenLabs, Sarvam and OpenAI accounts [8 each].
- **Sarvam model** [`bulbul:v3`, as the harness uses; v4-flash is cheaper].
- **Fluent readers** for the pools and the listening check, per language.
- **Production transcript logs,** if any exist: they would add a replay row to T2 and T11 alongside the synthetic mix.
- **Whether a `?` change may ever be served** [the oracle says no].
- **Integrator for U1** [a fresh agent; a human developer new to the code if available].
