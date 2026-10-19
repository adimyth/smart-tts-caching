---
name: testplan
description: Build and run TEST_PLAN.md, the final evaluation of the TTS cache across twelve languages and three providers, in stages with a free rehearsal, a frozen commit, a preflight and an explicit owner go before any paid live run, then write TEST_REPORT.md from the saved data.
---

# Test plan: build it, rehearse it, run it once, report

You are the implementer and operator. Build the tooling that `TEST_PLAN.md` section 15 lists, rehearse the whole plan against a fake provider, freeze, run the preflight, stop for the owner's go, run the live stage exactly once, and generate `TEST_REPORT.md` from saved data. Section numbers below refer to `TEST_PLAN.md` unless they say `SOLUTION.md`.

Read, in this order, before writing code: `TEST_PLAN.md` in full (its amendments at the top are binding), `REPORT.md`, `README.md` ("Status" and "Extend support for your language"), `SOLUTION.md` sections 5, 9 and 11, `IMPLEMENTATION_PLAN.md` section 3, `harness/cache_roundtrip.py`, `harness/attribution.py`, and `packages/tts-cache/src/tts_cache/{bench,corpus,replay,cli}.py`. The cache, adapter, Levels A, B and C, the harness and the bench exist and are tested (1,616 tests). You are not changing how the cache works; you are measuring it.

## Operating rules

1. **Autonomous until the go.** Make routine decisions yourself and record them in `TESTPLAN_LOG.md`. The only points at which you stop are: the go/no-go before the live stage, an abort, or completion.
2. **The oracle and the verdict rules are fixed.** Never change a label in section 6.1, a threshold in section 13, a V-check in section 12, or a fixture under `packages/tts-cache/tests/`, to make a result look better. A failing V-check or a wrong serve is a result; record it and carry on. The owner's decisions already in the plan stand: the listening check is dropped, the `punct_question` decoy is reported not fixed, `meaning_change` is a must-miss.
3. **Do not change the matching code.** `packages/tts-cache/src/tts_cache/match/`, `policy.py`, `canonical.py`, `split.py` and the adapter are frozen for this run. If you find a defect in them, record it as a finding with a failing test and stop at the next stage boundary; the owner decides whether to fix and restart. The files you may add or change are the ones section 15 names: `corpus.py` (`labelled`, pools), `bench.py` (configuration switches, size knobs, per-sentence rows), a new `tts_cache/testplan/` package, `harness/live_bench.py`, `harness/fake_provider.py`, and their tests.
4. **Fail open is still law.** Any cache-originated exception that reaches the host during any run is a critical finding (section 2.4), recorded with every frame of that context.
5. **Pinned Pipecat.** `.venv` has Pipecat editable from `~/PersonalProjects/pipecat` at `1d1385b6d`. Do not upgrade, reinstall or modify it.
6. **Secrets and infrastructure.** Never commit `.env`, `harness/out/`, `.venv`, model weights or audio. Never touch the shared Redis on port 6379. Start dedicated instances on 6380 and up as the plan says, and stop them when the run ends. Keep at least 10 GB free.
7. **Money.** No live provider call before the go, except the preflight (about 10,000 characters) which itself happens only after the owner says "run preflight". The budget guard (section 11.2) is computed from the frozen corpora and refuses to start above `--max-chars`; set the cap at the computed worst case and report it at the go/no-go in characters and in money per provider, using the price table frozen in `REPORT.md` ("Derived: cost per character saved"). The Sarvam model is whatever `.env` sets in `SARVAM_TTS_MODEL` (today `bulbul:v4-flash`); record it.
8. **Scope.** Twelve languages: en, hi, bn, as, pa, gu, mr, or, ta, te, kn, ml. No other language. Cells as in section 11.1 as amended. Assamese runs on OpenAI only. Odia uses Sarvam's code `od-IN`.
9. **Commit per stage** with a message `testplan: <stage>` ending with the attribution line the environment gives you. `make lint && make test` green before every commit.
10. **Working style.** Small verified steps. Write the shape test before the pool, the V-check before the runner. Everything the report states comes from a saved file under `harness/out/testplan/<run-id>/`.

## TESTPLAN_LOG.md

Create it at the start and append continuously. Structure:

```
# Test plan log

## Status
<one line: current stage, last gate passed, timestamp>

## Stage 1 Build ...
- started <timestamp>
- decisions: <bullet per non-obvious choice, with the reason>
- gates: <each gate, PASS or FAIL, the number measured>
- committed <sha>
```

## Stages and gates

### Stage 1: build (section 15, items 1 to 7)

- `tts_cache.corpus.labelled(pool, n, r, d, seed)` and the pool schema (6.3, 6.4), writing JSONL in the replay log format with `label`, `base`, `turn`, `first_in_turn`. Shape test: label shares within 1 sentence of 6.2 at n = 200; turns of 1 to 4, sessions of 3 to 6; the exact-only mix.
- **Pool dissimilarity check, required.** For every language, no two base sentences may score 0.85 or higher under the shipped Level C encoder (`tts_cache.match.semantic.load_model()`), and no base sentence's authored paraphrase may appear in `tests/fixtures/semantic/<lang>.yaml`. Both are shape tests. This is what separates a Level C failure from a corpus that contains accidental paraphrases.
- Bench: a `levels` argument to `run_core` wiring the four configurations exactly as section 4 lists them (`BASE_POLICY` with `ExactMatcher` for A; `BASE_POLICY` plus `LevelB` with `NearDuplicateMatcher` for A+B; `DEFAULT_POLICY` with `default_matcher()` for A+B+C); per-sentence rows with the fields of section 8; size knobs of section 9; `fill(store, scope, n, kind)`.
- `tts_cache.testplan`: worker pool, offline matrix, size sweeps, usability tests U2 to U6 and U8, the oracle, V1 to V10, composition (11.3), verdict rules (13.1), impact model (13.2), and the report generator producing every table T0 to T13 and the charts of section 14 as SVG.
- `harness/live_bench.py` with everything section 15 item 6 lists, and `harness/fake_provider.py` with the faults of 2.2. `FakeTTSService` takes each real cell's time to first byte from `REPORT.md`'s live table.
- Gates: `make lint && make test` green including the new tests; `tts_cache.testplan` runs the full offline part end to end on seed 1 in under 20 minutes on 8 workers.

### Stage 2: pools (section 15, item 8)

Twelve pools in `harness/corpus/<lang>.yaml`, 120 base sentences each with slot annotations, one authored paraphrase and one authored meaning change per base, following 6.3. Draft each with a subagent per language, briefed with the schema and the dissimilarity rule; read every pool yourself before committing. The owner spot-reads English and Hindi. Gate: every shape test passes for every language.

### Stage 3: rehearsal (2.2)

Run the entire plan against the fake provider, with every fault of 2.2 injected at least once, and generate the full `TEST_REPORT.md` from the rehearsal data with every V-check evaluated. Gate: one clean rehearsal, no unexplained V-check failure, every table and chart present. Repeat until clean. Record how long each stage took.

### Stage 4: freeze (2.1)

Commit `testplan: freeze`. Record its SHA, the Pipecat SHA and the package versions in the run directory. After this commit, change nothing but `TESTPLAN_LOG.md` and `TEST_REPORT.md` until the run is over.

### Stage 5: offline (sections 8, 9, 10)

Run the offline matrix, the size sweeps and the usability tests. Gate: V5, V6, V7, V8, V9 and V10 evaluated and recorded; any failure marked on the numbers it affects.

### Stage 6: preflight and go/no-go (2.3)

Stop and write to the owner: "Ready for preflight." Run the preflight only when the owner says so. Then stop again and present, in one message:

- the preflight results, cell by cell, with any cell dropped and why;
- the exact characters and money per provider the live stage will send, expected and worst case, from the frozen corpora and the offline misses;
- the budget cap you set;
- the live wall-time estimate at the concurrency the preflight chose.

Run the live stage only when the owner replies with the word **go**. Nothing else counts.

### Stage 7: live (section 11), once

Run every cell as 11.2 specifies. Never repeat a run. Record everything 2.4 lists. If the harness crashes, resume from the checkpoint and record the resume. A correctness failure is recorded and the run continues.

### Stage 8: analysis and report (2.5, 14)

From saved data only: ASR on every served clip with `harness/transcribe.py` (model from `STT_MODEL`), composition, V1 to V4 on the live rows, verdicts, impact model, `TEST_REPORT.md` with T0 first and every number labelled live, measured, simulated, composed or derived, with its spread. Then the final Status line in `TESTPLAN_LOG.md`, commit `testplan: report`, and stop with a three-line summary pointing to `TEST_REPORT.md`.

## Abort

Abort when: a stage gate fails after three genuinely different attempts; a rule above would have to be broken to proceed; or the rehearsal shows a defect in the frozen matching code. To abort: stop, write `## ABORT` in `TESTPLAN_LOG.md` with the stage, what was expected and measured, the attempts, your diagnosis, and the state of the tree; commit what is green on a branch `abort/testplan-<reason>`; leave main at the last green stage; end with a three-line summary. Never abort during the live stage: it always completes (2.4).

## Start

Create `TESTPLAN_LOG.md` with Status `Stage 1 starting`, and begin.
