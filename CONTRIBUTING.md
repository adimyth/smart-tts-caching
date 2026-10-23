# Contributing

Thank you for considering a contribution. This page covers the development setup, the checks a change has to pass, and the shape of the changes we are glad to receive.

## Setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), `libsndfile`, and Redis 7 for the Redis test suite.

The workspace expects the pinned Pipecat checkout next to this repository, as CI does:

```bash
git clone git@github.com:adimyth/smart-tts-caching.git
git clone https://github.com/pipecat-ai/pipecat.git && git -C pipecat checkout 1d1385b6d
cd smart-tts-caching
uv sync
```

## Checks

Every pull request runs lint and the unit tests in CI. Run them before pushing:

```bash
make lint          # ruff check, ruff format --check, pyright
make test          # unit tests; Redis tests skip unless TTS_CACHE_REDIS_URL is set
make redis-up      # a dedicated Redis on port 6380 for the Redis suite
make test-redis    # unit tests plus the Redis suite
make redis-down
```

`make format` applies ruff's fixes. The live provider harness (`make harness`) needs API keys in `.env` and is not part of CI.

## Layout

| Path | What lives there |
|---|---|
| `packages/tts-cache` | The framework-free core. It must not import Pipecat; `tests/test_no_pipecat_import.py` enforces this |
| `packages/tts-cache-pipecat` | The Pipecat adapter: the two processors, `scope_from_service`, and the websocket service subclasses |
| `packages/tts-cache/tests/fixtures/<kind>/<lang>.yaml` | Labelled fixtures per language for splitting, canonicalisation, filler, near-duplicate and semantic matching |
| `harness/` | Live provider runs, the corpus, and the integration check |
| `docs/evaluation` | The evaluation plan and its report |
| `SOLUTION.md` | The design and the reasoning behind each decision |

## Kinds of change

**A fix.** Add a failing test first, in the package the bug belongs to. A matching bug in a particular language belongs in that language's fixture file.

**A language.** Add the fixture files for the language under `tests/fixtures/`, the canonicalisation and filler rules in `tts_cache/canonical.py` and `tts_cache/match/lexicon.py`, and the corpus under `harness/corpus/`. The labelled test matrix picks the language up from the fixtures.

**A provider.** REST services work through Pipecat's own classes. A websocket service needs a subclass in `tts_cache_pipecat/services.py` that closes each sentence's server-side context, and a cell in `harness/attribution.py` that proves every audio frame is attributed to its sentence.

**A matching change.** Any change to Levels B or C has to pass the safety stage of the evaluation plan (`python -m tts_cache.testplan.core safety`), not only the fixtures. A change that serves a slot change, a negation or a question for a statement is not accepted, whatever it gains in hit rate.

**The adapter.** The adapter relies on `TTSService` behaviour verified at the pinned Pipecat commit. A change to the pin must rerun `harness/attribution.py` for every service class.

## Pull requests

- One change per pull request, with a description of what it does and how it was tested.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `fix(match): ...`, `feat(pipecat): ...`, `docs: ...`.
- Keep the README and `CHANGELOG.md` in step with the change.

## Reporting a wrong serve

If the cache ever plays audio that says something other than the sentence the agent asked for, open an issue with the requested text, the served text, the language, the match level from the log event, and the policy. That is the one class of bug that takes priority over everything else.
