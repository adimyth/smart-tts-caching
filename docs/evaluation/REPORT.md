# Evaluation report

The run of the [evaluation plan](PLAN.md) on 2026-10-20, for two deployments:

| | English | Hindi |
|---|---|---|
| Provider | ElevenLabs | Sarvam |
| Model | `eleven_flash_v2_5` | `bulbul:v4-flash` |
| Transport | multi-context websocket | websocket |
| Voice | one voice | `shubh_enhi_ads` |
| Store | local Redis 7.0, same machine | local Redis 7.0, same machine |

Each result applies to its own deployment. The code is the state of this repository as of 2026-10-21 (commits through `388d700`). The frozen environment for each language, with the diff hash, package versions, prices, budget and volume assumptions, is in [`data/`](data/), together with the live summary every number below is drawn from.

Live provider spend: 42,900 characters on ElevenLabs ($2.14 at list price) and 44,800 on Sarvam ($1.07 at the standard rate), $3.21 in all, against a ceiling of $4.65 set before the run.

## Summary

**A+B, the default configuration, earns adoption for both deployments.** It served no wrong audio in 5,308 adversarial requests, cut provider characters by 36 to 39 % live on traffic where half the sentences repeat, started cached turns in under 9 ms, and failed open through a Redis pause and a Redis restart. Level C stays off.

| | English, ElevenLabs websocket | Hindi, Sarvam websocket |
|---|---|---|
| **Safe** (A+B) | **0 wrong serves / 2,649 adversarial requests**, at least 420 per decoy kind, upper 95 % bound under 0.72 % for each kind. 0 of 20 live decoys served | **0 / 2,659**, at least 420 per kind, bound under 0.72 %. 0 of 20 live decoys served |
| **Level C with judge** | Served statements for questions and changed meanings. Stays off | Same. Stays off |
| **Hit rate** on the synthetic replay | 20.0 % of characters [19.1, 20.8] at r = 0.25, 40.1 % [39.2, 41.1] at r = 0.5. Provider requests fall by the same share | 20.0 % [19.3, 20.7] and 39.3 % [38.3, 40.3] |
| **Latency** live | A hit starts audio in **7.7 ms** p50 (11.6 ms p95), against 177 ms from the provider. Turn start, like for like, mean **187 → 127 ms** with a third of turns opening on a hit | Hit **8.7 ms** p50 (16.1 ms p95). Turn start mean **173 → 119 ms**, a third of turns opening on a hit |
| **Cost** at list prices | Net positive at every volume. Break-even character hit rate 6.9 % on Valkey Serverless, 10.3 % on Redis OSS Serverless | Net positive on Valkey, break-even 13.4 %. On Redis OSS Serverless break-even is 20 % |
| **Resilience** | No cache-originated host error through a 5 s Redis pause and a Redis restart. Lookups capped at 23 ms. First hit 3.6 s after the restart | Same faults, no host error. Lookups capped at 21.5 ms. First hit 21.6 s after the restart |
| **Operations** | Warm start 20 ms p50. Process memory +100 MB. Integration: one file, about 15 lines of wiring | Same |
| **Stored data** | 61 % of sentences stored. A four-digit skip pattern removes every identifier | 62 % stored. Same |

**Adoption conditions.** Add a `SkipIfMatches` list for identifiers. Restart or recycle serving processes after a purge that must take effect at once. Leave Level C off.

| Plan condition | English | Hindi |
|---|---|---|
| 4.1 zero serves of `new`, `slot_change`, `meaning_change`, `punct_question`; zero across scope boundaries | A+B pass | A+B pass |
| 4.3 miss delay within 20 ms + decode | pass (live p95 2.3 ms, max 15.7 ms) | pass (p95 2.4 ms, max 5.5 ms) |
| 4.3 turn-start improvement consistent with the share of hit-first turns | pass (mean −60 ms against −59 ms predicted) | pass (−54 ms against −54 ms) |
| 4.4 net saving positive at the chosen volume | pass at every scenario | pass on Valkey; marginal on Redis OSS at r = 0.25 |
| 4.5 zero cache-originated host errors, zero wrong serves | pass | pass |
| 4.7 every pattern-matching sentence skipped; retention clock as documented | pass | pass |

## 1. Safety

**Corpus.** Built from the language's 140-base pool (`tts_cache.testplan.core.form_corpus`), with the counts fixed in `safety/plan.json` before the run. Half the bases are stored in one of three forms: the base, its authored paraphrase, or a filler variant. A later session then asks for each stored base's authored meaning change, the stored form with a question mark added or removed, each single-slot change of the stored form, an exact repeat as a positive control, and the other half's bases, never said before. Both halves and all three forms are run, plus every pair of the semantic, near-duplicate and judge fixtures.

| Decoy kind | English N | served (A+B) | 95 % upper bound | Hindi N | served (A+B) | bound |
|---|---|---|---|---|---|---|
| `new`, never said before | 420 | 0 | 0.71 % | 420 | 0 | 0.71 % |
| `slot_change`, a name, number, date or amount changed | 1,374 | 0 | 0.22 % | 1,391 | 0 | 0.22 % |
| `meaning_change`, authored reversal | 434 | 0 | 0.69 % | 427 | 0 | 0.70 % |
| `punct_question`, a question offered a statement's audio | 421 | 0 | 0.71 % | 421 | 0 | 0.71 % |

The bound is the rule of three (3/N) on this corpus. The positive controls served as intended: 420 of 420 exact repeats in both languages, filler variants 10 of 12 and 11 of 11, inner punctuation 8 of 9 and 9 of 9.

**Level C with the judge stays off.** The entailment judge reads "Your order is confirmed." and "Your order is confirmed?" as entailing each other, so this configuration served the stored statement for 289 of 421 English and 110 of 421 Hindi question decoys. It also served changed meanings: 7 real cases in English and 1 in Hindi. Among them:

- "My colleague Tanvi will not follow up with you by Thursday." played "You will hear back from my colleague Tanvi no later than Thursday."
- "How much would you like to receive from Kiran today?" played "Sure, how much would you like to transfer to Kiran today?"
- "Could you avoid restarting your phone and wait for 30 seconds?" played "Would you mind rebooting your phone and then giving it 30 seconds?"
- Hindi "हमने आपकी अपॉइंटमेंट कैंसिल कर दी है।" (we cancelled your appointment) played "आपका ऑर्डर कैंसिल कर दिया गया है।" (your order has been cancelled).

By the run order, the configuration stopped at the safety gate and was not replayed or run live. The default configuration, A+B, serves only punctuation and filler differences and is unaffected.

**Scope isolation.** The 140 bases were stored under the frozen scope and asked for in exact, filler, inner-punctuation and paraphrase forms, 560 requests, under the most permissive matcher, with the scope differing in one field. The control under the frozen scope served 525 (English) and 516 (Hindi).

| Field changed | English value | hits | Hindi value | hits |
|---|---|---|---|---|
| voice | another voice id | 0 / 560 | `anushka` | 0 / 560 |
| model | `eleven_turbo_v2_5` | 0 | `bulbul:v3` | 0 |
| language | `en-US` | 0 | `en-IN` | 0 |
| sample rate | 16 kHz | 0 | 16 kHz | 0 |
| channels | 2 | 0 | 2 | 0 |
| one setting | `language: en-GB` | 0 | `pace: 1.1` | 0 |
| `Namespace("tenant")` | tenant b, stored under a | 0 | tenant b | 0 |

Under a `Namespace` policy exact repeats served for the right tenant (140 of 140), and only exact repeats: a namespaced deployment runs at Level A.

**Live.** The live A+B runs at r = 0.5 carried 20 decoys each (12 slot changes, 4 meaning changes, 4 questions) and 80 first-time sentences. None was served. The oracle found 0 wrong serves across all four live A+B runs and both fault runs.

## 2. Hit rate

The replay is the synthetic labelled corpus: 200 sentences per run, sessions of 3 to 6 turns, ten seeds, at r = 0.25 and r = 0.5 of servable repeats plus 10 % decoys. **r is an assumption about the traffic.** `tts-cache replay` over a real transcript log gives the number for a given agent.

| | English r = 0.25 | English r = 0.5 | Hindi r = 0.25 | Hindi r = 0.5 |
|---|---|---|---|---|
| Character hit rate [95 % bootstrap over seeds] | 20.0 % [19.1, 20.8] | 40.1 % [39.2, 41.1] | 20.0 % [19.3, 20.7] | 39.3 % [38.3, 40.3] |
| Sentence hit rate | 20.2 % | 40.0 % | 20.1 % | 39.8 % |
| Provider requests avoided (of 2,000) | 404 | 799 | 401 | 795 |
| Hits: Level A / B punctuation / B filler | 320 / 40 / 44 | 631 / 90 / 78 | 319 / 43 / 39 | 630 / 86 / 79 |
| Per-session character hit rate p10 / p50 / p90 | 0 / 17.8 / 36.5 % | 19.3 / 40.2 / 63.0 % | 0 / 18.9 / 38.4 % | 19.1 / 39.1 / 61.3 % |

Level B adds about a fifth on top of Level A at both repeat rates, and the hit rate is the same in both languages at the same r: the saving depends on how often the agent repeats itself, not on the language.

## 3. Latency

A turn's first sentence carries the latency a caller hears (`L1`, from the moment Pipecat can release the sentence to its first audio frame). Later sentences queue behind the previous sentence's audio in the service, so their time to first audio measures playback order rather than the cache.

| | English | Hindi |
|---|---|---|
| Hit, first sentence of a turn (`L2`), p50 / p95 / n | 7.7 / 11.6 ms / 28 | 8.7 / 16.1 ms / 26 |
| Provider, first sentence of a turn, p50 | 177 ms | 170 ms |
| Cache work on a miss (`L4`), p50 / p95 / max, live | 0.79 / 2.3 / 15.7 ms | 1.4 / 2.4 / 5.5 ms |
| Cache work on a miss, offline low-repeat control, p50 / p95 / max | 0.51 / 1.0 / 12.1 ms | 0.55 / 1.1 / 4.4 ms |
| In-turn audio stalls (`L3`) with the cache on | none in 138 turns | none in 134 turns |
| Warm start of a fresh session, p50 / p95 | 20 / 29 ms | 20 / 30 ms |

**Turn start, cache off against on.** The like-for-like comparison is A+B at r = 0.5 against A+B at r = 0, both one session at a time:

| | English | Hindi |
|---|---|---|
| Turns compared (r = 0 / r = 0.5) | 38 / 55 | 39 / 48 |
| Share of turns whose first sentence hit | 0 / 33 % | 0 / 33 % |
| `L1` mean | 187 → 127 ms | 173 → 119 ms |
| `L1` p50 | 177 → 170 ms | 170 → 168 ms |
| `L1` p95 | 231 → 231 ms | 188 → 183 ms |

The mean falls by exactly what a third of turns starting about 170 ms sooner predicts (59 and 54 ms). The median moves once more than half of all turns open on a hit, which is the shape of traffic whose openers repeat: a greeting and a disclosure said on every call. This corpus repeats sentences uniformly, so it shows the per-hit saving and the mean, and the median is the adopter's own traffic to measure.

## 4. Cost

Inputs: the replay's character hit rate and characters per sentence, the stored and promoted shares from the stored-data run, the measured Redis memory per clip (17.6 kB English, 19.2 kB Hindi, audio plus metadata), and list prices. ElevenLabs Flash is $50 and Sarvam `bulbul:v4-flash` $24 per million characters (2026-10-18). ElastiCache Serverless, US East, 2026-10-20: Valkey $0.084 per GB-hour with a 100 MB minimum, Redis OSS $0.125 per GB-hour with a 1 GB minimum, ECPUs $0.0023 and $0.0034 per million. The scenarios assume 20 sentences per call.

The Redis size is an upper bound: every miss stored and kept 48 hours, every clip hit at least once (24 % English, 20 % Hindi) kept 30 days. A `maxmemory` cap with LFU eviction drops mostly clips never hit.

| Calls a month | English r = 0.25: gross / net on Valkey / net on Redis OSS | English r = 0.5: gross / net on Valkey | Hindi r = 0.25: gross / Valkey / Redis OSS | Hindi r = 0.5: gross / net on Valkey |
|---|---|---|---|---|
| 10,000 | $116 / $40 / $91 | $236 / $195 | $58 / $39 / $91 | $114 / $74 |
| 100,000 | $1,163 / $402 / $598 | $2,356 / $1,954 | $584 / $391 / $582 | $1,136 / $744 |
| 1,000,000 | $11,625 / $4,021 / $5,984 | $23,563 / $19,542 | $5,841 / $3,913 / $5,822 | $11,357 / $7,444 |
| Redis working set (upper bound) | 0.65 / 6.5 / 65 GB | same | 0.64 / 6.4 / 64 GB | same |
| Break-even character hit rate | 6.9 % Valkey, 10.3 to 15.7 % Redis OSS | | 13.4 % Valkey, 20 to 31 % Redis OSS | |

**Live accounting agreed with the replay.** Provider characters fell from 11,419 to 7,015 (−38.6 %) in English and from 12,070 to 7,791 (−35.5 %) in Hindi, the same sentences the offline replay of that corpus served. Provider requests equalled misses plus retries.

**Compute.** A+B adds about 100 MB of resident memory per process (61 → 160 MB English, 73 → 170 MB Hindi). Level C with the judge would add 3.2 GB per process and is not part of the recommendation.

## 5. Resilience

During one live A+B run per deployment (r = 0.5, same corpus), Redis took a 5 s `CLIENT PAUSE` after the 20th turn and a shutdown and restart (3.1 s down, no persistence, so it came back empty) after the 60th. The cache ran its normal 20 ms deadline, and no sentence was resent.

| | English | Hindi |
|---|---|---|
| Cache-originated host errors | 0 | 0 |
| Wrong serves | 0 | 0 |
| Before the faults: sentences, hits, cache time p50 / p95 / max | 42, 12, 0.72 / 2.3 / 18.2 ms | 12, 3, 0.51 / 0.94 / 1.2 ms |
| During the pause: sentences, hits, cache time p50 / p95 / max | 8, 5, 0.28 / 22.7 / 23.0 ms | 2, 0, 21.0 / 21.5 / 21.5 ms |
| During the outage | no sentence released | 1 sentence, a miss after 2.2 ms (connection refused at once) |
| After the restart: first hit | 3.6 s, 8 sentences later | 21.6 s, 10 sentences later |

In English the process memory still served 5 of the 8 sentences released during the pause; the rest gave up at the deadline and went to the provider. The deadline held to within 3 ms (23.0 ms English, 21.5 ms Hindi). Provider requests equalled misses in both fault runs (135 and 130). Redis came back empty, so hits resumed when the traffic next repeated a sentence captured after the restart. The two runs logged 6 fail-open events with full tracebacks; nothing reached the pipeline.

## 6. Integration

Starting from the README's wiring and a stock Pipecat pipeline with the live service, Redis, and the same sentence in two turns (`harness/integration_check.py`): **one new file, about 15 lines of wiring** (140 in all with the service set-up and a transport stand-in), about 7 minutes. Both languages pass: turn 1 makes one provider request, turn 2 none and plays the cached clip.

Recipe:

1. `pip install "./packages/tts-cache[redis,similarity]" ./packages/tts-cache-pipecat`.
2. Build the websocket service from `tts_cache_pipecat` (`ElevenLabsTTSService`, `SarvamTTSService`) with `reuse_context_id_within_turn=False` and no `text_filters`.
3. `TTSCache(scope=scope_from_service(tts, language=...), store=TieredStore(MemoryStore(warm_start_bytes=4 << 20), RedisStore(url), deadline_ms=20), policy=AllOf(DEFAULT_POLICY, SkipIfMatches([r"\d{4,}", ...])))`, then `await cache.start()`.
4. `Pipeline([..., llm, TTSCacheRead(cache, tts=tts, text_filters=[...]), tts, TTSCacheWrite(cache, tts=tts), transport.output(), ...])`.
5. Check: the same sentence in a second turn makes no `on_tts_request`.

Two things the run taught, now in the README: Pipecat's websocket services hold every frame after a turn until `BotStoppedSpeakingFrame` comes upstream from `transport.output()`, so a test pipeline without a transport must send it itself; and a skip pattern for identifiers should start at four digits, since card endings and ticket numbers are that short.

## 7. Stored data

One representative replay (r = 0.5, seed 1, A+B) was left in Redis and read back, first under the shipped policy, then with a deployment skip list: runs of four or more digits in any script, phone and card numbers, e-mail addresses, and the words OTP, PIN, CVV, password and passcode.

| | English, shipped | English, with skip list | Hindi, shipped | Hindi, with skip list |
|---|---|---|---|---|
| Sentences stored | 121 of 200 (60.5 %) | 114 (57 %) | 123 of 200 (61.5 %) | 116 (58 %) |
| Skipped by `SkipIfMatches` | not configured | 8 | not configured | 14 |
| Stored with an identifier | 5 | **0** | 7 | **0** |
| Stored with a name / date / amount | 44 / 17 / 11 | 43 / 17 / 11 | 43 / 23 / 19 | 43 / 23 / 19 |
| Character hit rate of the run | 37.5 % | 37.0 % | 36.4 % | 33.4 % |

The skip list caught every identifier and e-mail address at a cost of half a point to three points of hit rate. Names, dates and amounts remain stored, since no regular expression catches a name; a deployment that treats them as personal data uses `TurnPosition`, `SpeakFramesOnly` or its own list. The stored texts are listed for review in the run's `stored/` rows.

**What Redis holds.** Per clip: the Opus audio under `tts:<scope hash>:<text hash>`, and a metadata hash with `text`, `canonical_text`, `language`, `tags`, format fields, timestamps and counters, plus a per-language hot set of text hashes. The text is needed to build the Level B index at warm start and to serve `purge --match`.

**Retention**, on a clock the test controls: a sentence said once was present at 47 h and gone at 48 h + 1 min; a sentence hit once at 1 h was present 30 days − 1 min after that hit and gone at 30 days + 1 min. Real Redis held a TTL of 48.0 h after a store and 30.0 days after one hit.

**Deletion.** With two serving processes warm, `delete` of one key and `purge` by tag (10 clips) and by match (1 clip) through the first process:

| | Same process | Other running process | Process started after |
|---|---|---|---|
| Deleted clip served afterwards | no | yes, until restart | no |
| Tag-purged clips served afterwards | 0 of 10 | 10 of 10, until restart | 0 |
| Match-purged clips served afterwards | 0 of 1 | 1 of 1, until restart | 0 |
| Keys, metadata, hot-set or tag-set members left in Redis | none | | |

A purge clears the shared store and the calling process at once. Every other process keeps the clips it loaded at warm start until it restarts, which gives the deletion recipe: `await store.purge(tags={...})` or `purge(match="...")` against the shared store, then restart or recycle the serving processes. A fresh process serves nothing that was purged.

## Scope of this result

- One provider, voice and language per column, with a local Redis on the test machine. A remote Redis adds its round trip to every miss, which is why the README asks for `tts-cache ping` under 10 ms at p99 before deployment.
- The replay traffic is synthetic, with r chosen. The hit rate for a particular agent comes from `tts-cache replay` over its own transcripts.
- The latency comparison rests on about 50 turns per column.
- Level C is off in the recommended configuration. Where a deployment enables it, a served sentence replaces the LLM's text in the transcript, so logs record what was heard.

## Data

[`data/`](data/) holds, for each language, `environment.offline.json` and `environment.json` (the frozen code hash, versions, prices, budget and volume assumptions) and `summary.json`, the live summary with every latency, character and request figure above. The full row set of the run (175 MB of per-sentence JSONL, audio digests and stored-text listings) is kept outside the repository and is available on request.
