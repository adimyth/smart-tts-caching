# Test report: do Levels A, B and C work?

Offline evaluation, 2026-10-19. The owner stopped the test plan before its live stage, for time; no provider was called during the test and nothing was spent. Every number below is **measured**: the cache's own code on this machine, driven by the bench's stand-in provider, which cannot change whether a sentence hits. Numbers marked **derived** are arithmetic on measured ones; **earlier live** numbers come from REPORT.md.

- **Data.** The offline matrix of TEST_PLAN.md section 8: 12 languages × 4 configurations × repetition rate r ∈ {0, 0.25, 0.5} × 10 seeds × 200 labelled sentences, plus the exact-only and single-process controls (2,040 runs, `harness/out/testplan/rehearsal2/offline`, code `b2a835a`). Assamese and Odia were rerun after the F6 fix below (`harness/out/testplan/f6-as-or/offline`); their rows use that rerun.
- **Corpora.** Twelve pools of 140 base sentences a voice agent says, each with an authored paraphrase and meaning change, and rule-made variants (`harness/corpus/`). Every sentence carries a label from the oracle of TEST_PLAN.md 6.1, which says which level may serve it.

## Answer

| Level | Verdict | Evidence |
|---|---|---|
| **A**, exact match | **Works as designed** | Served every repeat whose earlier copy was in an earlier turn (100 % capture of expected hits) and never a wrong-meaning clip, in all 12 languages. |
| **B**, punctuation and filler | **Works** | Adds 4.1 to 4.7 points of character hit rate at r = 0.25, interval above zero in every language; no wrong-meaning serve after the F6 fix. Serves a question as its statement (`punct_question`), 61 to 68 times per language: the risk the owner accepted. |
| **C**, semantic paraphrase | **Not safe to enable by default** | Adds 2.5 to 3.7 points and catches 75 % of paraphrases, but served a clip with the wrong meaning in 11 of 12 languages (Gujarati 0). By the plan's rule (13.1) one such serve makes a level unsafe. |

**Recommendation:** ship Levels A and B on by default; make Level C opt-in until it has a stronger meaning check than the 0.90 floor plus the slot and span vetoes. Done the same day: Level C is opt-in, and with the judge of the addendum below it served no wrong meaning in any language, at about half its gain.

## Per language

Character hit rate at r = 0.5, increments at r = 0.25 with 95 % bootstrap intervals over the 10 seeds, wrong-meaning serves (`new`, `slot_change` or `meaning_change` served) over all offline runs of each configuration, and `punct_question` decoys served by A+B.

| Language | A | A+B | A+B+C | Δ(B), points | Δ(C), points | Wrong-meaning serves A / A+B / A+B+C | `?` decoys served |
|---|---|---|---|---|---|---|---|
| English | 31.2 % | 40.2 % | 46.9 % | +4.6 [4.2, 4.9] | +3.5 [3.0, 3.9] | 0 / 0 / 10 | 64 |
| Hindi | 30.6 % | 39.2 % | 46.7 % | +4.2 [3.8, 4.5] | +3.7 [3.4, 4.0] | 0 / 0 / 2 | 63 |
| Bengali | 31.0 % | 39.7 % | 44.6 % | +4.4 [3.9, 4.8] | +3.2 [2.9, 3.6] | 0 / 0 / 7 | 62 |
| Assamese | 30.6 % | 38.9 % | 44.6 % | +4.3 [3.8, 4.7] | +2.6 [2.2, 3.0] | 0 / 0 / 15 | 64 |
| Punjabi | 30.1 % | 38.8 % | 43.0 % | +4.4 [3.9, 4.8] | +2.5 [2.0, 3.0] | 0 / 0 / 8 | 65 |
| Gujarati | 30.6 % | 39.5 % | 45.6 % | +4.7 [4.2, 5.0] | +3.5 [3.3, 3.7] | 0 / 0 / 0 | 64 |
| Marathi | 31.0 % | 39.0 % | 46.3 % | +4.4 [4.0, 4.8] | +3.2 [2.7, 3.7] | 0 / 0 / 5 | 67 |
| Odia | 29.9 % | 38.7 % | 44.4 % | +4.1 [3.8, 4.5] | +2.7 [2.5, 3.0] | 0 / 0 / 5 | 61 |
| Tamil | 31.3 % | 40.0 % | 47.0 % | +4.7 [4.2, 5.2] | +3.3 [2.9, 3.7] | 0 / 0 / 14 | 65 |
| Telugu | 31.1 % | 39.9 % | 46.1 % | +4.5 [4.0, 5.0] | +3.1 [2.6, 3.6] | 0 / 0 / 13 | 68 |
| Kannada | 30.8 % | 39.5 % | 46.4 % | +4.5 [4.1, 4.9] | +3.4 [2.9, 3.8] | 0 / 0 / 8 | 68 |
| Malayalam | 30.6 % | 39.0 % | 45.5 % | +4.7 [4.1, 5.3] | +3.2 [2.7, 3.7] | 0 / 0 / 10 | 66 |

Hit rate is a property of how often the agent repeats itself, not of the language: the synthetic mix holds r of servable repeats (55 % exact, 10 % surface, 8 % comma, 2 % `.`/`!`, 10 % filler, 15 % paraphrase) and 10 % decoys. For your own traffic, `tts-cache replay` over a real transcript log gives the number to trust.

## What each level serves

Served / occurrences, all 12 languages (Assamese and Odia from the rerun after F6), r = 0.25 and 0.5, 10 seeds.

| Label | A | A+B | A+B+C |
|---|---|---|---|
| exact | 9,647 / 9,960 | 9,652 / 9,960 | 9,645 / 9,960 |
| surface (spacing, Unicode, digits, markdown) | 1,751 / 1,800 | 1,752 / 1,800 | 1,750 / 1,800 |
| comma added or removed | 0 / 1,440 | 1,249 / 1,440 | 1,249 / 1,440 |
| `.` against `!` | 0 / 360 | 297 / 360 | 301 / 360 |
| leading filler | 0 / 1,800 | 1,444 / 1,800 | 1,460 / 1,800 |
| paraphrase | 0 / 2,640 | 0 / 2,640 | 1,968 / 2,640 |
| slot change (decoy, 2,880) | 0 | 0 | 5 served |
| meaning change (decoy, 960) | 0 | 0 | 65 served |
| `?` added or removed (decoy, 960) | 0 | 777 served | 777 served |
| new sentence (25,200) | 0 | 0 | 27 served |

The repeats that were not served are explained: the earlier copy was in the same turn (Level A) or the same session (Levels B and C, whose indexes are built at session start), or for paraphrases the similarity stayed under 0.90 or a veto refused it. Four misses in the whole matrix lack a reason from the plan's closed list: two Redis lookups that hit the 20 ms deadline while the machine was loaded, one near-duplicate the Level B index did not return, and one Assamese paraphrase whose miss recorded no facts.

## What Level C gets wrong

Three kinds, none of which a word list can close:

- **Antonyms and reversals with no negation word.** Asked "Shall I cancel the delivery for next Monday?", it played "Shall I reschedule the delivery to next Monday?"; asked "Good news, we have free slots for the whole of May.", it played "I am sorry, we are fully booked for the whole of May."; asked "The salon charges 15 percent extra on weekday bookings!", it played "… offers 15 percent off …". The encoder scores these at 0.90 or above and every fact (number, date, name) matches, so both vetoes let them through.
- **Unrelated sentences just above the floor.** Asked "Your replacement debit card should reach you in 4 working days.", it played "Your follow-up with the cardiologist is due in 4 weeks." Same number, same shape, different meaning, cosine above 0.90.
- **Facts the detectors cannot see.** A three-word shop name changed (Kannada "ಶ್ರೀ ಗಣೇಶ ಟ್ರೇಡರ್ಸ್" to another name) is longer than the span veto's two-token substitution and unknown to the name lexicon; a negation moved from one clause to another (Assamese) leaves both sentences negated.

## Addendum: Level C with a judge

Added after the test, same day, on the owner's decision; **measured** on this machine, nothing spent. A judge (`tts_cache.match.judge`) now runs after the vetoes: an NLI cross-encoder, mDeBERTa-v3-base fine-tuned on multilingual NLI, reads the candidate and the request together and serves only when each entails the other with probability at least 0.70.

- **Choosing the model.** On the 1,228 distinct pairs Level C served above (93 with a changed meaning, 1,135 paraphrases; `packages/tts-cache/tests/fixtures/judge/`), four candidates were scored. At the lowest threshold that refuses all 93, mDeBERTa keeps 52 to 60 percent of the paraphrases; Qwen3.5-4B as a local LLM judge 37 to 53 percent, at far greater cost; XLM-R large and IndicBERTv2 XNLI let changed meanings through at every threshold. The threshold, 0.70, is the highest changed pair's score (0.60) plus 0.10; on 500 random half-splits that margin let 0.06 percent of held-out changed pairs through, against 2 percent with no margin.
- **End to end.** The offline matrix rerun with and without the judge, 12 languages × r ∈ {0.25, 0.5} × 10 seeds on the current code (480 runs, `harness/out/testplan/judge/offline`): wrong-meaning serves (`new`, `slot_change`, `meaning_change`) went from 97 to **0** in every language, and Level C's hits from 2,097 to 1,019.

| Language | Δ(C) at r = 0.25, without judge | with judge | Wrong-meaning serves, without / with |
|---|---|---|---|
| English | +3.4 | +2.9 | 10 / 0 |
| Hindi | +3.6 | +2.8 | 2 / 0 |
| Bengali | +3.2 | +2.0 | 7 / 0 |
| Assamese | +2.5 | +0.7 | 15 / 0 |
| Punjabi | +2.5 | +1.6 | 8 / 0 |
| Gujarati | +3.5 | +2.3 | 0 / 0 |
| Marathi | +3.3 | +0.2 | 5 / 0 |
| Odia | +2.7 | +0.1 | 5 / 0 |
| Tamil | +3.2 | +1.3 | 14 / 0 |
| Telugu | +3.1 | +2.3 | 13 / 0 |
| Kannada | +3.4 | +2.1 | 8 / 0 |
| Malayalam | +3.0 | +1.8 | 10 / 0 |

- **Where it falls short.** The model rarely calls a true paraphrase entailed in Marathi (9 percent kept), Odia (22) and Assamese (27), so there the judge makes Level C safe but adds almost nothing. Alone, it accepts a transposed amount (₹1,450 against ₹1,540); the slot veto refuses that, so the judge runs after the vetoes, never instead of them.
- **Cost.** A 1.1 GB model (the int8 export flips 7 percent of decisions), about 2.5 GB of resident memory per process, 17.5 ms p50 per check in English, 19 to 27 ms in ten Indic languages and 39 ms in Odia, whose sentences take twice the tokens. A cache whose matcher has a judge adds 100 ms to its lookup deadline; with six processes sharing the CPU, 97 of about 2,000 checks ran past it (54 in Odia) and went to the provider.
- **What changed in the code.** Level C is now opt-in: `DEFAULT_POLICY` is Levels A and B, and `LEVEL_C_POLICY` with `default_matcher(judge=True)` turns on Level C with the judge.

## Checks

- **Billing equals serving (V1):** characters not billed equal characters served, exactly, in every run.
- **Metrics equal truth (V10):** hit rate and saved characters from the cache's own metrics equal the oracle's in every run.
- **More levels never serve less (V7):** hits(A) ≤ hits(A+B) ≤ hits(A+B+C) for every seed and language.
- **Every miss explained (V3):** all but the four above.
- Level A shows 6 "wrong" serves of `exact` and `surface` sentences. They are bookkeeping, not wrong audio: a lookup that failed open left the oracle without the clip's source, so it could not attribute it.

## Fixes made during the test

The test found four defects in the matching code; each was fixed on the owner's decision, with new tests, and the Level C acceptance check rerun (no near-miss accepted; paraphrase recall bn 86, kn 89, te 90, ta 94, as 95, or 86 percent).

- **F1.** Bengali negative copulas (নন, নই, নও) were not recognised as negation, so Level C served "you are not a new patient" for "are you a new patient".
- **F5.** Kannada negative suffixes were written with independent vowel letters that never occur inside a word, so most negated Kannada verbs (ಬರುವುದಿಲ್ಲ "won't come") were not recognised; Tamil and Telugu negative imperatives, "cannot" and future negative forms were missing; Tamil had the same vowel-sign defect as Kannada.
- **F6.** Assamese নহয় ("is not") and Odia ନା were in the Level B filler lists, so Level B served "your limit is ₹1,00,000" for "your limit is not ₹1,00,000". A negation is now never filler. This took Assamese Level B from 5 wrong-meaning serves to 0.
- These fixes were written after reading the pools' decoys, so the negation results on these pools are partly in-sample; the out-of-sample evidence is the new test sentences and the unchanged acceptance fixtures.

## Latency and cost

- **Earlier live:** a cache hit started audio in 1.4 to 8.3 ms, against 131 ms to 2.4 s from the provider (the live round trips below, six provider classes, en, hi, ta).
- **Overhead:** this run's timings are not clean, because the matrix shared the machine with other jobs (Level A miss path 1.5 ms mean, Level C 7.3 ms). On a quiet machine: Level A adds about 0.4 ms per sentence with Redis (200-sentence bench against local Redis, 2026-10-18); Level C's embed, probe and vetoes take 3.7 to 4.0 ms at p99 (test suite, 2026-10-18).
- **Derived saving** at the frozen list prices below, at the r = 0.5 mix: A+B serves about 39 % of characters, so per million characters sent it saves about $20 on ElevenLabs Flash, $9 on Sarvam bulbul:v4-flash and $6 on OpenAI gpt-4o-mini-tts. At r = 0.25 the rate is about 20 % and the savings about half of these.

### Live: time to first audio, provider against cache hit (2026-10-18)

`harness/cache_roundtrip.py`, pipeline `[TTSCacheRead, service, TTSCacheWrite]`, SQLite store, four sentences streamed token by token. Time is measured from the moment Pipecat's aggregator can release the sentence (the first token of the next sentence arrives) to the first audio frame for that sentence. Run 1 is live, run 2 is the cache. The first sentence of the turn is the clean provider number; later live sentences also carry Pipecat's serialisation behind the previous sentence, which is why REST rows grow by about 3 s per sentence (the HTTP stop-frame timeout), so only the first sentence is compared.

| Service | Transport | en live → hit | hi live → hit | ta live → hit |
|---|---|---|---|---|
| ElevenLabs Dialogue | websocket | 131 → 3.4 ms | 134 → 3.3 ms | 133 → 2.9 ms |
| Sarvam | websocket | 172 → 3.2 ms | 218 → 5.0 ms | 201 → 5.3 ms |
| ElevenLabs | websocket | 285 → 4.6 ms | 323 → 1.9 ms | 310 → 5.7 ms |
| ElevenLabs | REST | 344 → 2.0 ms | 417 → 2.7 ms | 548 → 2.9 ms |
| Sarvam | REST | 976 → 2.1 ms | 2438 → 5.7 ms | 654 → 4.8 ms |
| OpenAI | REST | 1844 → 2.2 ms | 1727 → 4.5 ms | 1718 → 3.0 ms |

Across all 72 cached sentences (18 runs × 4) the hit time to first audio ranged from 1.4 to 8.3 ms. The saving per sentence is the provider's own latency minus a few milliseconds: at least 125 ms on the fastest websocket service, 1.7 s or more on OpenAI REST. Provider numbers are single runs on a home connection and vary run to run; the harness `.json` files under `harness/out/roundtrip/` hold every value.

Provider requests: run 1 made 4 per service per language (72 in total), run 2 made 0. Characters not sent to the provider on run 2: 153 to 168 per turn per service, exactly the turn's text.

### Derived: cost per character saved

Public list prices, looked up 2026-10-18 (secondary sources for OpenAI and ElevenLabs; Sarvam's own price list for v3, a news report for v4-flash). The cache saves exactly the hit characters, so the saving is this rate times the characters that hit.

| Provider, model used here | List price | Per million characters | Per the 160-character turn above |
|---|---|---|---|
| OpenAI gpt-4o-mini-tts | $0.60 per 1M input characters plus $12 per 1M audio output tokens; a measured run put the all-in cost near $16 per 1M characters | about $16 | about $0.0026 |
| ElevenLabs eleven_flash_v2_5 (and v4 turbo, same tier) | $0.05 per 1,000 characters pay-as-you-go | $50 | $0.008 |
| Sarvam bulbul:v4-flash | ₹20 per 10,000 characters standard (₹10 promotional until 12 Nov 2026); v3 is ₹30 | ₹2,000, about $24 | ₹0.32 |

Worked example, labelled as arithmetic, not a measurement: an agent doing 1 million calls a month, 20 sentences of 40 characters per call, is 800 million characters. At the 39 to 54 percent character hit rate measured on the r = 0.5 corpus, 310 to 430 million characters never reach the provider: $15,000 to $22,000 a month on ElevenLabs Flash, ₹6.2 to 8.6 lakh on Sarvam v4-flash, $5,000 to $7,000 on OpenAI. At a 10 percent hit rate, divide by four to five. Your own hit rate comes from `tts-cache replay` over a real transcript log.

## Not done

- **The live stage** (section 11), the preflight and ASR: not run, so no live confirmation of latency or billing on this corpus.
- **The size sweeps** (section 9): only the store-capacity sweep finished, and it was not analysed.
- **Usability** (section 10): only U8 ran. Wiring the cache wrongly is caught and named for text filters left on the service, reused context ids and a mismatched sample rate. Two cases are not caught: a missing `TTSCacheWrite` gives zero hits with nothing named, and a scope that names a different voice from the one the service speaks stores its clips under that voice's key, so the next deployment, which really uses that voice, plays the old one (80 of 80) with nothing named. The voice is in the key; the cache trusts the scope it is given.
- The harness for the live stage (`harness/live_bench.py`) passed a fake-provider check of one full cell with a provider error, an empty stream, a 429, a stall, a killed Redis and a harness crash; it is ready to run on the owner's word.

