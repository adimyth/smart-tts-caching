# TTS Caching

A sentence-level cache for text-to-speech. When a voice agent is about to say a sentence it has said before, in the same voice, the cache plays the audio it stored the first time. The provider never sees the sentence, so you pay nothing for it and the user hears it with no synthesis delay.

This README explains the problem and the shape of the solution. [SOLUTION.md](SOLUTION.md) has the full design.

## Status

Built as of 2026-10-18. [TEST_PLAN.md](TEST_PLAN.md) is the evaluation method and [TEST_REPORT.md](TEST_REPORT.md) its results.

- `tts-cache`, the framework-free core: scope and key, the per-script canonicaliser (en, hi, ta fixtures), the sentence splitter, Opus, FLAC and PCM codecs with streaming decode, `MemoryStore`, `SQLiteStore`, `RedisStore` and `TieredStore`, the policies, Level A, B and C matchers, metrics, the `TTSCache` facade with fail-open boundaries, and the `tts-cache` command (`ping`, `replay`, `bench`, `warm`).
- `tts-cache-pipecat`: `TTSCacheRead` and `TTSCacheWrite`, the three per-sentence websocket subclasses, the capture-mode table and `scope_from_service` for the six supported classes, a Pipecat metrics sink, the REST provider for `warm`, and `tts-cache-pipecat bench`.

### Test results, 2026-10-19

Does each match level serve only what it should? The full answer, with every table, is in [TEST_REPORT.md](TEST_REPORT.md). The evaluation ran offline: the cache's own code over 12 languages × repetition rates 0, 0.25 and 0.5 × 10 seeds × 200 labelled sentences (2,040 runs), plus 480 reruns with the Level C judge, driven by a stand-in provider that cannot change whether a sentence hits. Every sentence carries a label saying which level may serve it, and the corpora include decoys a level must not serve: a changed number or name, a changed meaning, a statement turned into a question. No provider was called and nothing was spent.

| Level | Verdict | Evidence |
|---|---|---|
| A, exact match (default) | Works | Served every repeat whose earlier copy was in an earlier turn, and never a wrong clip, in all 12 languages. |
| B, punctuation and filler (default) | Works, one accepted risk | Adds 4.1 to 4.7 points of character hit rate at r = 0.25, never a wrong meaning. It does play a statement's clip for the same words as a question ("Your order is confirmed." for "Your order is confirmed?"), 61 to 68 times per language, a risk the plan accepted. |
| C, paraphrase, encoder and vetoes only | Not safe | Adds 2.5 to 3.7 points, but served a different meaning in 11 of 12 languages, 97 times: "Shall I reschedule the delivery to next Monday?" for "Shall I cancel …", "we are fully booked" for "we have free slots". |
| C with the judge (opt-in) | Safe on this data | 0 wrong-meaning serves in 480 runs across all 12 languages, at about half Level C's gain; almost no gain in Assamese, Marathi and Odia. |

Character hit rate at r = 0.5 (half the sentences repeat something said earlier), Level C's increment at r = 0.25, and wrong-meaning serves over all runs:

| Language | A | A+B, the default | Level C adds, no judge | Level C adds, with judge | Wrong-meaning serves, A+B / C / C with judge |
|---|---|---|---|---|---|
| English | 31.2 % | 40.2 % | +3.4 | +2.9 | 0 / 10 / 0 |
| Hindi | 30.6 % | 39.2 % | +3.6 | +2.8 | 0 / 2 / 0 |
| Bengali | 31.0 % | 39.7 % | +3.2 | +2.0 | 0 / 7 / 0 |
| Assamese | 30.6 % | 38.9 % | +2.5 | +0.7 | 0 / 15 / 0 |
| Punjabi | 30.1 % | 38.8 % | +2.5 | +1.6 | 0 / 8 / 0 |
| Gujarati | 30.6 % | 39.5 % | +3.5 | +2.3 | 0 / 0 / 0 |
| Marathi | 31.0 % | 39.0 % | +3.3 | +0.2 | 0 / 5 / 0 |
| Odia | 29.9 % | 38.7 % | +2.7 | +0.1 | 0 / 5 / 0 |
| Tamil | 31.3 % | 40.0 % | +3.2 | +1.3 | 0 / 14 / 0 |
| Telugu | 31.1 % | 39.9 % | +3.1 | +2.3 | 0 / 13 / 0 |
| Kannada | 30.8 % | 39.5 % | +3.4 | +2.1 | 0 / 8 / 0 |
| Malayalam | 30.6 % | 39.0 % | +3.0 | +1.8 | 0 / 10 / 0 |

- The hit rate follows how often the agent repeats itself, not the language; `tts-cache replay` over your own transcript log gives the number to trust.
- The bookkeeping holds in every run: characters not billed equal characters served, the cache's own metrics equal the oracle's counts, and adding a level never serves less. Four misses in 2,040 runs lack an explanation from the plan's closed list.
- The test found four negation defects in the matching code: Bengali negative copulas were not recognised; Kannada and Tamil negative suffixes were written with vowel letters that never occur inside a word; Tamil and Telugu negative imperatives and future negatives were missing; and Assamese and Odia negations were listed as filler words, which let Level B drop a "not". All four are fixed and tested.
- A hit starts audio in 1.4 to 8.3 ms against 131 ms to 2.4 s from the provider (earlier live round trips, six provider classes, en, hi, ta). At r = 0.5, A+B serves about 39 percent of characters, which at list prices saves about $20 per million characters sent on ElevenLabs Flash, $9 on Sarvam bulbul:v4-flash and $6 on OpenAI gpt-4o-mini-tts.
- Two wiring mistakes go unflagged: leaving out `TTSCacheWrite` gives zero hits with no warning, and a scope that names a different voice from the one the service speaks (built by hand rather than with `scope_from_service`) stores the clips under the other voice's key, so a later deployment that does use that voice plays the wrong voice. The voice is part of the key; the cache trusts the scope it is given.
- Not done: the live stage of the plan (real providers, ASR of served clips), the size sweeps, and most usability tests.

### Component measurements

Measured on an Apple M4 Pro, local Redis 7.0.11:

| What | Result |
|---|---|
| Opus per 4 s clip at 24 kHz, real TTS output from six services | 15.8 KB mean; first 200 ms decoded in 0.45 to 0.59 ms; whole clip 9.5 to 10 ms, off the critical path |
| hit from process memory | lookup 0.08 ms p50, 0.15 ms p99; first audio 0.09 ms p50, 0.15 ms p99 |
| cache work added per sentence when nothing hits (r = 0, 200 sentences) | 0.27 ms mean in one process (SQLite); 0.41 ms with Redis, round trip included |
| `tts-cache ping`, local Redis | 0.06 ms p50, 0.12 ms p99 |
| billed characters, simulated provider, 200 sentences | fall by exactly the character-weighted hit rate: 23.6 percent at r = 0.25, 38.8 at 0.5, 69.3 at 0.75, 87.1 at 0.9 |
| `replay` over the benchmark's own log | the same hit rate as the benchmark at every r |
| first sentence of a turn, r = 0.5 | p50 time to first audio 0.13 ms against 253 ms with the cache off |
| serverless simulation, 200 fresh caches over one Redis | every sentence captured in an earlier session hits; the opener comes from the warm set in all 199 later sessions; all 117 hot-set hits outside the warm set were fetched by the prefix prefetch while streaming |
| live round trip, six services in English and Hindi | the second run makes zero provider requests and replays byte-identical audio (FLAC store); transcripts as specified |
| Level B, probe over 1,000 entries | 0.10 ms p50, 0.32 ms p99; no slot change served across 1,000 generated variations |
| Level B on the synthetic corpus, r = 0.5, 40 percent of repeats near-duplicates | hit rate 33.1 percent without Level B, 39.4 serving punctuation, 45.1 serving punctuation and filler |
| Level C, embed, probe and vetoes over 1,000 entries | 2.8 ms p50, 3.6 ms p99 with multilingual-e5-small; 0.12 ms p50 with the static model |
| Level C judge (opt-in), one NLI check of the candidate | 17.5 ms p50 in English, 19 to 27 ms in ten Indic languages, 39 ms in Odia (twice the tokens); mDeBERTa-v3-base through onnxruntime on two threads |

Level C routing map, the languages Level C may serve in once an application enables it, after the acceptance check of section 5 (200 pairs per language in `packages/tts-cache/tests/fixtures/semantic/`, half paraphrases, half one-slot near-misses), with `multilingual-e5-small` through onnxruntime and the slot and span vetoes. Zero near-misses accepted in every language; the vetoes stop 100 of 100.

| Language | Paraphrases accepted at the 0.90 floor (80 % required) | Route |
|---|---|---|
| English | 98 % | on |
| Hindi | 96 % | on |
| Assamese | 95 % | on |
| Marathi | 95 % | on |
| Kannada | 91 % | on |
| Telugu | 90 % | on |
| Gujarati | 89 % | on |
| Tamil | 89 % | on |
| Bengali | 86 % | on |
| Odia | 86 % | on |
| Punjabi | 84 % | on |
| Malayalam | 82 % | on |
| everything else | not checked | Levels A and B only |

Why these pieces. Every sentence encoder scores a changed name, date or amount (mean cosine 0.88 to 0.97) as closer than a true paraphrase, so the decision cannot rest on similarity alone. The slot veto refuses a candidate whose numbers, amounts, dates, lexicon names or foreign-script words differ; the span veto refuses one that differs by a single substitution of one or two tokens, or up to four when a digit is involved, or by one insertion or deletion of one or two non-filler tokens, which is what a changed fact or a negation looks like and a paraphrase never does. A negation word anywhere also marks the sentence, so "सफल रहा" never matches "सफल नहीं रहा" however the rest is phrased. Together they stopped every slot near-miss and every negation decoy in all twelve languages. Their cost is recall: 2 to 11 paraphrases per 100 refused. With safety handled, the encoder question is recall alone: the static model `potion-multilingual-128M` accepts 57, 21 and 21 percent of paraphrases in English, Hindi and Tamil, because it scores a paraphrase that swaps native words for English loanwords at 0.2 to 0.5; `multilingual-e5-small` accepts 85 to 100 percent, at 2.3 ms per sentence through onnxruntime. Level C runs only after Levels A and B miss. On the 200-sentence synthetic corpus Level C served nothing it should not have: hit counts equal the exact-repeat counts.

What the vetoes cannot see, and why Level C is opt-in. The test plan's corpora (TEST_REPORT.md) add an authored meaning change for every base sentence, and there Level C served a different meaning in 11 of 12 languages: "cancel" for "reschedule", "fully booked" for "free slots", "15 percent extra" for "15 percent off", an unrelated sentence that shared one number. Such a pair differs in more than one place and carries the same slots, so neither veto applies, and the encoder scores it above 0.90 because both sentences are about the same thing. The judge in `tts_cache.match.judge` reads the candidate and the request together instead: an NLI cross-encoder (mDeBERTa-v3-base fine-tuned on multilingual NLI, through onnxruntime) that serves only when each sentence entails the other with probability at least 0.70, a margin of 0.10 over the highest-scoring wrong serve. Rerun end to end on the same matrix (12 languages, r = 0.25 and 0.5, 10 seeds, `harness/out/testplan/judge`), it took Level C's wrong-meaning serves from 97 to 0 and halved its hits (per language in "Test results" above). In Assamese, Marathi and Odia the model rarely calls a true paraphrase entailed (27, 9 and 22 percent of them), so there the judge makes Level C safe but nearly useless. The judge needs the vetoes as well: on its own it accepts a transposed amount, ₹1,450 against ₹1,540. It costs a 1.1 GB model (the int8 export flips 7 percent of decisions), about 2.5 GB of resident memory per process, shared by every cache in it, and one check per Level C candidate; the cache adds 100 ms to its default lookup deadline when the matcher has a judge, and a check that runs past it sends the sentence to the provider, which happened to 97 of about 2,000 checks with six benchmark processes sharing the CPU, 54 of them in Odia. To turn it on:

```python
from tts_cache import LEVEL_C_POLICY, TTSCache
from tts_cache.cache import default_matcher

cache = TTSCache(scope, store, policy=LEVEL_C_POLICY, matcher=default_matcher(judge=True))
```

Run the tests and the harness:

```bash
make lint && make test                      # unit and pipeline tests
make redis-up && make test-redis            # plus the Redis suite on a dedicated instance at port 6380
.venv/bin/tts-cache bench --out bench/      # the benchmark sweep: table, JSON and an SVG plot
.venv/bin/tts-cache-pipecat bench           # the same corpus through a real Pipecat pipeline
.venv/bin/python harness/attribution.py elevenlabs_ws_fc --lang hi --quiet   # live, needs .env
.venv/bin/python harness/cache_roundtrip.py all --lang en --quiet           # live, needs .env
```

## The problem

A voice agent speaks by sending text to a TTS provider and streaming the returned audio to the user. Each request costs money, because providers bill per character, and time, because the provider takes 100 to 400 ms before the first byte of audio arrives.

Agents repeat themselves. The same greetings, acknowledgements, standard explanations and closings come up across sessions, in the same voice. On real traffic the repeats make up anywhere from a few percent to half of the characters spoken, depending on how scripted the agent is. Today the provider synthesises each repeat from scratch, and the user waits for it each time.

Sentences repeat across sessions. Within one call the user hears most sentences once, while the thousandth caller hears the same opener as the first. A cache that remembers the current session alone cannot help.

## The solution in one paragraph

Split the LLM's output into sentences. For each sentence, build a key from the canonical text plus everything that shapes the sound: provider, model, voice, language, sample rate and audio settings. Look the key up in a store shared across sessions. On a hit, play the stored audio in the sentence's position and skip the provider. On a miss, send the sentence to the provider as usual, capture the audio it returns, and store it for next time. If anything in the cache fails or runs slow, the sentence takes the normal path, so a broken cache behaves like no cache.

![System overview](docs/diagrams/overview.svg)

## How it works

### Two packages

- `tts-cache` is the core. It knows nothing about any framework or provider. Two operations: `lookup(text)` returns stored audio or nothing, and `capture(text, audio)` stores what the provider just produced.
- `tts-cache-pipecat` is the first integration, pinned to `pipecat-ai` 1.12.x and verified against commit `1d1385b6d` of 2026-10-12. Two frame processors sit around the Pipecat TTS service: one upstream that splits, looks up and injects cached audio, one downstream that captures audio per sentence. REST services are used as they are; websocket services are wired through a thin subclass that finishes each sentence's context. Other hosts, such as a plain SDK loop or LiveKit, use the same core.

### The unit is a sentence

A sentence is what a provider synthesises as one request, so a stored clip carries its own prosody. It is short enough to recur across sessions, which few whole turns do. The splitter handles Latin terminators and the Indic danda, with per-language abbreviation rules so "डॉ. शर्मा" stays one sentence.

### The key is text plus voice identity

Two sentences share audio only if everything that affects the sound is identical. That set is the scope and it goes into the key:

| Field | Example for Sarvam |
|---|---|
| service | `sarvam` |
| model | `bulbul:v3` |
| voice | `shubh`, `priya` |
| language | `hi-IN`, `ta-IN`, `en-IN` |
| sample rate, channels | `24000`, `1` |
| settings that change the audio | `{"pace": 1.0, "temperature": 0.6}` |

The text half of the key is the canonical form: Unicode NFC with Indic nukta and joiner cleanup, native digits folded to ASCII, danda unified with the full stop, whitespace collapsed, case folded for cased scripts. Two sentences that cannot sound different hash to the same key. Application concepts such as tenant or campaign stay out of the key; they attach as tags for analytics and purges.

### Three match levels, by what you may do with a match

- Level A, exact match on canonical text. A hash lookup, on by default, and where most hits come from.
- Level B, near duplicate, on by default for punctuation and filler differences. Character n-gram shingles with MinHash find candidates; an edit-distance check and a diff classifier decide. A difference in a name, number, date or amount never serves, because a listener can hear a synthesised span spliced into stored audio. Character shingles work the same across Indian scripts.
- Level C, semantic paraphrase, opt-in (`LEVEL_C_POLICY` with `default_matcher(judge=True)`), in languages that pass the acceptance check (English and eleven Indic languages today). A multilingual sentence encoder (`multilingual-e5-small` through onnxruntime, about 2.5 ms per sentence on one CPU thread) proposes the nearest stored sentence; a slot veto refuses it unless both sentences carry exactly the same numbers, amounts, dates, names and foreign-script words, and a span veto refuses it if the two differ by a single short substitution. The judge, an NLI cross-encoder, then reads the candidate and the request together and refuses it unless each entails the other; without it this level served changed meanings in the test plan. When this level serves, the stored text replaces the LLM text in the transcript, so the record matches what the user heard.

### Where the audio lives

| Tier | Role |
|---|---|
| Redis, in the same region as the agent containers | the cache. Every lookup that memory cannot answer, every capture. A sentence captured in one session hits in the next, on any container |
| Process memory | the hot set, loaded from Redis at warm start, plus whatever this session fetched. Serves the opener of a fresh container with no network call |
| SQLite | the same data for a single process: development, tests, one server |

Redis holds clips as Opus at 32 kbps, about 16 KB for a 4 second sentence, so 10,000 clips take about 156 MB. Process memory holds decoded PCM so a hit is a memory copy.

### Why it stays fast on serverless

The deployment that matters is one container per session on containerised services such as Modal, Cloud Run or AWS Bedrock AgentCore. Three things keep the cache from adding latency there:

1. Warm start. A fresh container fetches the top few hundred clips from Redis in one request during start, so it serves hits from its first sentence.
2. Prefix prefetch. While a sentence is still streaming from the LLM, the read side matches the prefix against the hot set's texts. When exactly one candidate remains, it fetches that clip from Redis at once. When the sentence completes, it plays the parked clip only if the final key matches; otherwise it discards it. Nothing plays because of a prefix. The round trip runs while the LLM is still talking.
3. A hard deadline on every Redis call, default 20 ms. A slow store means a miss, never a stall. The rule for placement is a measured round trip under 10 ms at p99 from the container, and the library ships a `ping` command to check it.

The lookup sits on the critical path for the first sentence of a turn alone, and there it competes with the provider's time to first byte. At a 200 ms time to first byte and a 2 ms round trip, the cache pays for itself above a 1 percent hit rate on first sentences. Openers sit near 100 percent.

### Keeping the cache correct

- Capture is exact. Pipecat tags each audio frame with a per-sentence context id, so the cache knows which bytes belong to which text without word timestamps or any provider API.
- The key describes the provider's input. Pipecat's TTS service filters and transforms a sentence before sending it, stripping markdown or inserting pronunciation markup for example. The cache keys on the text that reaches the provider, runs the filters itself, and checks on every miss that the text the service sent matches what it keyed.
- Ingest checks duration and silence before storing. An optional verifier transcribes the clip with a small speech-to-text model and rejects it if the transcript disagrees.
- A user interruption drops every partial capture for that turn, so a truncated clip never enters the store.
- Changing the voice, model, settings or canonicalisation rules changes the key, so old audio stops matching on its own. A per-scope generation counter and tag purges handle everything else.

### Keeping it small

A new clip lives 48 hours. Its first hit promotes it to a 30 day sliding window that each later hit refreshes. Sentences said once expire; anything that repeats stays. Redis runs LFU eviction with decay for the rest.

### Policy decides the strategy

Every "should we" decision goes through one interface: whether to look up, whether to store, the TTL, which match levels may serve. Built-in policies cover shadow mode for rollout, minimum and maximum sentence length, patterns that must never be stored, turn-initial and turn-final only, and tag-based routing. You compose them.

### Measuring it

- Runtime metrics: character-weighted hit rate per language and voice, hits by level, lookup latency split by tier, and the overhead the cache adds on a miss.
- A replay harness runs the real splitter, canonicaliser and policies over an application's transcript log and reports the hit rate it would have had.
- A synthetic benchmark generates 200 sentences at a chosen repetition rate, sweeps the rate from 0 to 0.9, and runs with the cache off and on against the real provider or a simulated one, reporting characters billed and time to first audio for each.

## Extend support for your language

Twelve languages ship with full support: English, Hindi, Bengali, Assamese, Punjabi, Gujarati, Marathi, Odia, Tamil, Telugu, Kannada and Malayalam, each with canonicaliser, splitter, Level B and Level C fixtures and word lists. Level A works for any language with no setup: the key is a hash of the canonical text, and canonicalisation is Unicode-general. Level B serves punctuation differences in any language with no setup; to also serve leading and trailing filler words it needs a filler list. Level C needs an encoder that covers the language and, to be routed, the acceptance check below; with the judge, it also needs the judge to recognise the language's paraphrases (see "Status"). What each step takes:

1. **Character set.** Add the script's code point range to `_SCRIPT_RANGES` in `tts_cache/match/slots.py` so a word in another script counts as a fact for the slot veto. Digits in any script are already folded to ASCII, and the span veto (one short substitution is a changed fact) needs nothing per language. This is the safety core, and it is language-independent.
2. **Word lists, optional but recommended.** In `tts_cache/match/lexicon.py`: filler words and phrases (Level B's filler class), date and time words, currency words, and common names. In `slots.py`: titles that precede a name, honorifics that follow one, and frequent function words. Each list is 10 to 60 tokens. Abbreviations that end with a period but not a sentence go in `NO_SPLIT_AFTER` in `tts_cache/split.py`.
3. **Encoder coverage.** The default encoder, `multilingual-e5-small`, covers about 100 languages. The routing map `ROUTES` in `tts_cache/match/semantic.py` maps a language code to a model name. Any sentence-transformer on the Hugging Face hub that ships `onnx/model.onnx` and `onnx/tokenizer.json` works by name, as does any model2vec static model (`minishlab/...`). You can swap the model per language, or pass your own `routes` to `SemanticMatcher` without changing the package.
4. **Acceptance check, which is what routes Level C.** Write `tests/fixtures/semantic/<lang>.yaml`: 200 pairs, 100 paraphrases that keep every fact, and 100 near-misses that change exactly one fact (25 each of name, number, date, amount) and nothing else. Run `acceptance(lang, pairs, load_model(name))`. The language passes when no near-miss is accepted and at least 80 percent of paraphrases are, and then its entry in `ROUTES` points at the model. Until then the language gets Levels A and B, and an application that accepts the risk can still route it on through `routes`.

Fixtures for the canonicaliser (`fixtures/canonical/<lang>.yaml`), the splitter (`fixtures/split/`) and Level B (`fixtures/near/`, `fixtures/filler/`) follow the same per-language files; the tests pick up every language that has a file.

## Known risks, stated plainly

These are the things that can make the cache less useful than the design promises. None of them can make it play wrong audio or add a failure, because every cache fault degrades to a miss. They are ordered by how much they worry us.

### Prosody seams

A provider that conditions each request on the previous one, such as ElevenLabs with request stitching or Cartesia with continuations, gives a live sentence a slightly different intonation depending on what came before it. A cached clip has no such conditioning. In a turn that mixes cached and live sentences, a listener may hear the join. We do not know yet how audible this is for each provider and voice, and we will not claim it is inaudible until a listening audit says so.

The mitigation is the `TurnPosition` policy, which caches only the first and last sentence of a turn. Openers and closers are where most repeats and most of the first-sentence latency live, so this keeps most of the value. Expect to run in this mode for providers that condition across requests until the audit for that provider passes.

### Hit rate on free-form agents

If your agent improvises every sentence, exact matches will concentrate in openers, acknowledgements and closings, and the character-weighted saving may be small. This is a property of your prompt, not the cache. Installing the cache costs under a millisecond per sentence in a single process and one bounded Redis round trip per sentence in a serverless container, so the worst case is little benefit, not harm. The replay harness tells you your hit rate from your own logs before you deploy.

### Per-sentence audio attribution, by provider and transport

The cache needs each sentence's audio tagged with that sentence's context id. REST service classes do this by construction. Websocket classes need each sentence's server-side context flushed and closed as soon as its text is sent, which stock Pipecat does only at end of turn, so `tts-cache-pipecat` ships a drop-in subclass for each websocket service that does it per sentence. You wire the subclass instead of the stock class.

| Provider | Transport | Class | Status |
|---|---|---|---|
| OpenAI | REST | `OpenAITTSService` | supported |
| OpenAI | Realtime API | `OpenAIRealtimeLLMService` | not applicable: speech-to-speech, no text-to-speech stage |
| ElevenLabs | REST | `ElevenLabsHttpTTSService` | supported |
| ElevenLabs | multi-context websocket | `tts_cache_pipecat.ElevenLabsTTSService` | supported; flushes and closes each sentence's context |
| ElevenLabs | Text-to-Dialogue websocket, v3 and v4 | `tts_cache_pipecat.ElevenLabsDialogueTTSService` | supported; closes each sentence's context |
| Sarvam | REST | `SarvamHttpTTSService` | supported |
| Sarvam | websocket | `tts_cache_pipecat.SarvamTTSService` | supported; flushes after each sentence |

All seven rows were verified against the live APIs on 2026-10-18 with the harness in `harness/attribution.py`: a four-sentence turn per class in English, Hindi and Tamil, clean and interrupted mid-sentence, plus a 14-sentence turn of two-to-four-word sentences on the ElevenLabs websocket to churn through its five-context limit. Checks: one context per sentence, no interleaved audio, a plausible duration for the character count, a stop frame per context, and a transcript of every captured clip compared with its keyed text. In English every clip matched. In Hindi and Tamil every clip contained its own sentence; the transcript differences were the speech-to-text model writing the same words in Urdu, Bengali, Malayalam or Telugu script, and Tamil sandhi spelling variants. One voice per provider. The check proves attribution, not voice quality per language, which the listening audit covers.

Two Pipecat behaviours the adapter accounts for. An HTTP context's stop frame arrives about 3 s after its last audio, often after the next sentence has begun, so a capture closes when the next context starts. With a context per sentence, ElevenLabs' word-timestamp assembly can drop a word at a sentence join in the per-word transcript frames; audio is unaffected, and we will report it upstream along with the per-sentence flush and close.

### Pipecat coupling

The adapter relies on behaviour inside Pipecat's `TTSService` that is not a public contract: upstream aggregated frames bypassing the service's own splitter, injected frames entering the serialization queue in order, the `on_tts_request` event carrying the exact provider text, and the private text preparation method the read side mirrors to predict that text. All of it is verified against `pipecat-ai` 1.12.x at commit `1d1385b6d`, and the package pins that minor version. A Pipecat upgrade is a re-verification task, not a drop-in. The `key_mismatch` metric catches drift in the text path on the first miss and suspends serving for that scope, so an unnoticed upgrade degrades to no cache rather than to wrong audio.

## What success looks like

- Zero served clips with wrong words or wrong voice.
- Zero cache-originated errors reach the host when the store is down, slow or flapping.
- Billed characters fall by the character-weighted hit rate.
- Cached first sentences start in under 10 ms instead of the provider's time to first byte.
- A fresh serverless container serves hits from its first sentence.
