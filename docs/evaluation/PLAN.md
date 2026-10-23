# Evaluation plan

This plan decides one question for one deployment: should the cache be switched on for this provider, voice, language and deployment shape? The run of it is in [REPORT.md](REPORT.md).

## 1. The questions

1. **Safety.** Does the cache ever play audio for a request it must not serve?
2. **Hit rate.** On representative traffic, what share of provider requests, characters and spend does it avoid?
3. **Latency.** How much sooner does a caller hear the first audio, and what does a miss cost?
4. **Cost.** Does the saving exceed the cache's own infrastructure cost?
5. **Resilience and integration.** Is it safe to run through a store failure, and how much work is a correct first integration?
6. **Stored data.** Which sentences are stored, for how long, and can an operator remove them?

## 2. Fixed scope

One commit is frozen before measuring, and the result applies to that configuration only:

- one provider service and transport;
- one provider model and voice;
- one language;
- one deployment profile: a local Redis, or the intended remote Redis placement;
- one cache policy, **A+B**, with the enabled Level B classes recorded;
- one optional configuration, **A+B+C+judge**, using `LEVEL_C_POLICY` and `default_matcher(judge=True)`;
- the package, Pipecat, Python, Redis, machine, network region and provider price date, recorded in `environment.json`.

Three configurations run: **OFF**, **A+B**, and **A+B+C+judge** where semantic reuse is being considered. Level C runs only with the judge.

## 3. Inputs

Three inputs, kept separate in the report because each answers a different question.

**3.1 Safety corpus.** The labelled corpus and adversarial fixtures for the language: new sentences, exact repeats, surface variants, punctuation and filler changes, paraphrases, changed names, dates and amounts, negations, meaning reversals, and statement and question pairs. It measures whether matching is correct. It does not estimate the production hit rate.

**3.2 Representative replay log.** A transcript sample from the intended agent, with turn and session boundaries, language, voice and provider configuration intact, and secrets and personal data removed. Where no production log exists, the plan's synthetic corpus stands in, and the result is labelled an assumption rather than a forecast. It estimates the benefit on the intended traffic.

**3.3 Low-repeat control.** A corpus with few repeated sentences. It shows the miss-path cost, so that a high-repeat run cannot hide the cache's overhead.

## 4. Measurements and pass conditions

### 4.1 Safety

*Does the cache play audio for a different fact, meaning or question type?*

The safety corpus runs through A+B and A+B+C+judge offline, then through the live pipeline. Every serve is recorded with the requested text, the served text, its label, the match level and the audio digest.

Then the scope-isolation check: the corpus is stored under the frozen scope and replayed under a scope that differs in exactly one field, in turn `voice`, `model`, `language`, `sample_rate`, `channels`, one entry of `settings`, and a second `Namespace` tag. Every field must give zero hits.

**Pass:** zero serves for `new`, `slot_change`, `meaning_change` and `punct_question` requests, and zero serves across any scope boundary. A miss is always acceptable. Any other serve blocks the configuration.

**Report:** `0 observed wrong serves / N adversarial requests` by label and configuration, with N fixed before the run and at least 300 requests per decoy label, and the rule-of-three upper bound (3/N) next to every zero. The result is a bound on this corpus, not a guarantee for production traffic.

### 4.2 Hit rate

*What fraction of provider work can the cache avoid on the intended traffic?*

The representative log replays through OFF, A+B and, optionally, A+B+C+judge, preserving session order and cache warm-up.

Measured: the character-weighted hit rate (served characters over total characters), the sentence hit rate, provider requests avoided, hits by Level A, Level B class and Level C, the per-session character hit rate at p10, p50 and p90, the share of sessions with no hit, and misses by reason.

**Decision rule:** publish the observed result with its bootstrap interval. There is no universal minimum hit rate; the measured saving is compared with the adopter's required return in 4.4.

### 4.3 Latency

*How much waiting does a hit remove, and what does a miss cost?*

The live pipeline runs the same corpus and cache state under OFF, A+B and, optionally, A+B+C+judge, with connection setup outside the timed interval and enough turn starts to report p50 and p95 with their sample sizes.

Measured:

- `L1`, turn start: from the first releasable sentence to the first audio frame;
- `L2`, sentence time to first audio, split into hits and misses;
- `L4`, cache work on a miss, against the low-repeat control;
- `L3`, in-turn audio stalls;
- warm-start duration and resident memory for a fresh session.

**Pass:** the cache adds no user-visible delay on a miss beyond the deployment budget, stated before the run as 20 ms plus audio decode. The latency claim rests on the turn-start result, not on the hit timing alone: a hit is faster than a provider request by two orders of magnitude in any deployment, so that comparison is not a test. The OFF to ON change in `L1` is reported at p50 and p95 and must be consistent with the share of turns whose first sentence was a hit.

### 4.4 Cost

*What is the net monthly saving at the adopter's volume?*

Provider requests and billed characters are counted from the live run and reconciled with cache misses. The replay's character-weighted hit rate and provider prices frozen on a stated date give the saving.

Measured: provider characters, requests and dollars avoided; Redis memory per clip and the working set the replay needs; cache-process memory and any judge compute, including the memory Level C adds per process; Redis and compute cost per month at managed list prices; gross saving, cache cost, net saving and break-even character hit rate.

The monthly model uses the adopter's calls per month and sentences per call. Where unknown, 10,000, 100,000 and 1,000,000 calls a month are shown as labelled scenarios.

**Pass:** net saving is positive at the chosen volume, or the latency benefit justifies adoption on its own.

### 4.5 Resilience

*What does the caller experience when Redis is unavailable or slow?*

During a representative live run, one Redis restart and one five-second pause are injected, under the normal cache deadline, with no provider request repeated to improve the result.

Measured: host-visible errors, wrong serves, added sentence latency at p50, p95 and max, the hit rate during the fault, and recovery time.

**Pass:** zero cache-originated host errors and zero wrong serves. A failure degrades to provider synthesis, with the added delay within the miss budget wherever the store deadline can be enforced.

### 4.6 Integration

*How much work is a correct first integration?*

Starting from the documented wiring and a stock Pipecat bot, a Redis-backed cache is added and a second identical sentence is shown to make no provider request. Files touched, lines added, elapsed time and documentation gaps are recorded.

**Report:** a short reproducible recipe, not a usability score.

### 4.7 Stored data

*Which sentences are stored, do any carry personal data, and can an operator remove them?*

The representative log, with synthetic names, amounts and account details standing in for the ones removed, replays through the frozen policy, and every stored and skipped sentence is recorded.

Measured:

- the share of sentences stored, and the share skipped by each policy reason;
- sentences the deployment's `SkipIfMatches` patterns were meant to exclude but which were stored, found by a labelled pass over the stored texts;
- the retention clock: a sentence said once is gone after 48 hours, a sentence hit once lives 30 days from its last hit, both measured against a clock the test controls;
- deletion: `delete` on one key, and `purge` by tag and by match, and what each tier serves afterwards;
- what Redis holds in plain text, listed for the adopter's review.

**Pass:** every sentence that matches a sensitive pattern is skipped, and the retention clock behaves as documented. A stored sentence the patterns should have caught blocks that policy, not the cache.

## 5. Run order

1. Freeze the commit, provider configuration, inputs, prices, latency budget and volume assumption.
2. Run the safety gate offline. A configuration with a wrong serve goes no further.
3. Replay the representative and low-repeat inputs.
4. Run the stored-data check on the same replay, and the deletion recipe against the chosen store.
5. Run the live pipeline for latency, request accounting and attribution.
6. Run the Redis-failure scenario.
7. Calculate the monthly cost model from the saved rows only.
8. Publish the result with the rows needed to reproduce each number.

## 6. What the report states

- **Safe in this test:** `0 observed wrong serves / N adversarial requests`, or the precise serves.
- **Useful on this replay:** the character-weighted hit rate, requests avoided and per-session spread.
- **Faster in this live cell:** OFF to ON turn start at p50 and p95, with hit and miss timings.
- **Financial result:** gross monthly saving, cache cost, net saving, break-even hit rate, price date and traffic assumptions.
- **Operational result:** Redis-fault behaviour, warm-start time, process memory and integration effort.
- **Stored data:** the share stored, the sensitive sentences found, the retention clock and the deletion recipe.
- **Scope:** the languages, provider and voice, traffic sample, deployment placement and any disabled matching class. If Level C is enabled, that a served sentence replaces the LLM's text in the transcript.

## 7. The claim the plan supports

If every pass condition holds, the project may say:

> In the tested provider, voice, language and deployment configuration, the cache produced the reported hit rate, provider-cost reduction and latency change on the specified replay traffic. It had zero observed wrong serves in the labelled adversarial corpus and failed open in the tested Redis faults.

It may not say that semantic reuse is universally safe, that every language and provider perform the same, or that every deployment will save the reported amount.
