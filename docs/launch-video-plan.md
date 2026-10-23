# Launch video: simulation plan

One recording, about four minutes, that shows a judge everything the cache does and everything it refuses to do. Four inbound calls to the same voice agent: two in English, two in Hindi. The first call of each language fills the cache. The second lands on every match level and misses on purpose where a hit would be wrong. The agent, the calls and the caller are built under [`demo/`](../demo/README.md); this page is the shooting plan.

## The agent

Meera, at the appointment desk of Tulsi Multispeciality Hospital, Hinjewadi, Pune (fictional). Inbound calls to book or move an appointment. The hospital has standard lines, as real desks do: a greeting, a recording disclosure, the request for the registered mobile number, the appointment statement, the confirmation, the arrival instructions, the SMS notice, the closing question and the closing. Those are what repeat across calls, and those are what the cache serves.

- Transport: WebRTC from a browser page served by the agent process. The page is the caller: it plays the caller's pre-synthesised lines into the call, plays the agent out loud, and shows the transcript, the counters and the cache log beside the call. One window holds the whole recording.
- Speech-to-text: Sarvam `saaras:v4`. Turn taking: Smart Turn v3.
- Text-to-speech: ElevenLabs `eleven_flash_v2_5` over the multi-context websocket for English, Sarvam `bulbul` over websocket for Hindi.
- Agent brain: `--mode scripted` says the script's turns exactly (the take that is guaranteed), `--mode hybrid` is a real LLM with the standard lines pinned, `--mode pure` the same LLM with the persona only.
- Cache: `TieredStore(MemoryStore, RedisStore)` on a local Redis, `LEVEL_C_POLICY` with the judge so that Level C can be shown, and a skip pattern for identifiers. Each call builds its own cache instance, as one container per session would, so the second call opens on a warm start.

## What the viewer sees

1. **The call.** The agent's voice, the caller's voice, and a transcript: the caller's lines on the right, the agent's sentences on the left, each with a badge: `HIT A`, `HIT B · filler`, `HIT B · punctuation`, `HIT C`, or `MISS`, and a one-line caption saying why.
2. **The counters.** Sentences, sentences from the cache, provider requests, characters billed, characters from the cache, and the turn-start time from the cache in milliseconds.
3. **The cache log.** One line per event: hit, miss, store, reject, deadline, fail-open, prefetch, provider request.

## Call 1, English: filling the cache (about 50 s)

Rohan Deshmukh books a cardiology follow-up with Dr. Kulkarni. Sixteen agent sentences, sixteen provider requests, every sentence stored except the one carrying the patient ID. The counters show 0 from the cache.

## Call 2, English: the demonstration (about 80 s)

Sneha Iyer moves her Friday appointment. A fresh process: the warm start shows first, then:

| # | The agent says | Badge | Caption |
|---|---|---|---|
| 1 | Thank you for calling Tulsi Multispeciality Hospital, this is Meera speaking. | HIT A, under 10 ms | Same sentence, same voice: from the cache. The caller hears the greeting 170 ms sooner |
| 2 | This call is being recorded for quality and training purposes. | HIT A | Never billed again |
| 3 | How can I help you today? | HIT A | |
| 4 | Certainly. May I have your registered mobile number, please? | HIT A, HIT A | A question, cached as itself |
| 5 | Thank you, I can see your record, patient ID ending in 8826. | MISS | Carries an identifier: never stored, so nothing to hit. The policy did this |
| 6 | Your appointment with Dr. Kulkarni is on Friday at 10:30 AM. | MISS | The day changed. A fact: the caller must hear Friday |
| 7 | When would you like to move it to? | MISS | Never said before. Provider, and stored for the next caller |
| 8 | You would like the morning slot? | MISS | A question is never played from a statement's audio |
| 9 | Dr. Kulkarni has no slots on Thursday morning. | MISS | A negation |
| 10 | He can see you on Thursday at 4:00 PM instead. | MISS | New |
| 11 | Okay, I have confirmed your appointment. | HIT B · filler | A leading "Okay" is filler. Same words, same clip |
| 12 | Your appointment with Dr. Kulkarni is on Thursday at 4:00 PM. | MISS | The time changed |
| 13 | Please reach the hospital fifteen minutes early, and carry a photo ID. | HIT B · punctuation | An inner comma does not change the audio |
| 14 | The appointment details will be sent to you by SMS in a moment. | HIT C | A paraphrase: the encoder proposed the stored sentence, the vetoes passed, the judge agreed both ways. Off by default |
| 15 | Is there anything else I can help you with today? | HIT A | |
| 16 | Thank you for calling Tulsi Multispeciality Hospital, take care. | HIT A | |

Before the caller's last line, Redis is paused for five seconds. The closing plays from process memory; the log shows the store's deadline being hit; nothing errors. Caption: "Redis paused. The cache fails open."

Counters at the end: 17 sentences, 11 from the cache, 6 provider requests instead of 17.

## Calls 3 and 4, Hindi (about 70 s together)

Anjali Pawar books with Dr. Sharma; Vikram Jadhav moves his appointment to Friday. The same shape in Hindi on Sarvam: the first call fills, the second opens on three Level A hits, misses on the identifier and the changed day, and hits Level B on "ठीक है, मैंने आपकी अपॉइंटमेंट कन्फर्म कर दी है।" Caption: "Twelve languages. Same code, same result."

## Rehearsal before recording

```bash
.venv/bin/python demo/rehearse.py          # every expected outcome against the matcher, offline
.venv/bin/python demo/agent.py --mode scripted --flush
.venv/bin/python demo/caller_cli.py en-1   # then en-2 --pause-redis-before 5, hi-1, hi-2
```

The rehearsal prints the expected and actual outcome for every sentence of a second call; a line that differs is changed before recording. The headless caller runs the whole call without a browser and leaves the agent's audio and the events under `harness/out/demo/`.

## Listening check

After a headless run, play each served clip of the second calls against the provider's clip of the same sentence from the first call, and confirm that the stored sentence of a Level B or C hit is what the caller should hear. Then `python -m tts_cache.testplan.asr` over the served set, so the check is on record by ear and by transcription.

## Timeline

| Time | Segment |
|---|---|
| 0:00 | Title. One sentence: an agent repeats itself; the cache plays what it already said, in the same voice, without paying for it again. |
| 0:10 | Call 1. The cache fills; every sentence goes to the provider. |
| 1:00 | Call 2, fresh process. Warm start. Sentences 1 to 4: five hits, each at a few milliseconds. |
| 1:30 | Sentences 5 to 10: the refusals. Identifier, slot, question, negation. |
| 2:00 | Sentences 11 to 16: Level B filler, a slot, Level B punctuation, Level C, two Level A. Redis pause. Final counters. |
| 2:40 | Calls 3 and 4 in Hindi on Sarvam. |
| 3:50 | Closing card: the results table from the README, the repository link, `pip install`. |
