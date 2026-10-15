# TTS Caching

A sentence-level cache for text-to-speech. When a voice agent is about to say a sentence it has said before, in the same voice, the cache plays the audio it stored the first time. The provider never sees the sentence, so you pay nothing for it and the user hears it with no synthesis delay.

This README explains the problem and the shape of the solution. [SOLUTION.md](SOLUTION.md) has the full design.

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
- `tts-cache-pipecat` is the first integration. Two frame processors sit around any stock Pipecat TTS service: one upstream that splits, looks up and injects cached audio, one downstream that captures audio per sentence. The processors leave the TTS service untouched. Other hosts, such as a plain SDK loop or LiveKit, use the same core.

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
- Level B, near duplicate. Character n-gram shingles with MinHash find candidates; an edit-distance check and a diff classifier decide. A difference in punctuation or a leading filler word may serve under policy. A difference in a name, number, date or amount never serves, because a listener can hear a synthesised span spliced into stored audio. Character shingles work the same across Indian scripts.
- Level C, semantic paraphrase, off by default. A static multilingual embedding (model2vec, about 50 microseconds per sentence on CPU) with a per-language routing map. When this level serves, the stored text replaces the LLM text in the transcript, so the record matches what the user heard. You opt in per application.

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

## What success looks like

- Zero served clips with wrong words or wrong voice.
- Zero cache-originated errors reach the host when the store is down, slow or flapping.
- Billed characters fall by the character-weighted hit rate.
- Cached first sentences start in under 10 ms instead of the provider's time to first byte.
- A fresh serverless container serves hits from its first sentence.
