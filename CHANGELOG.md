# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-27

The first tagged release, under the new distribution names.

### Added

- A fallback-service hook for the Pipecat adapter: `register_fallback_service` and `mark_primary_audio`, so the cache never stores a fallback provider's audio under the primary voice's key.
- A live demo in one command: `demo/quickstart.sh` installs the workspace, starts Redis, asks for the keys, and starts a Pipecat agent with the cache over WebRTC.
- `LICENSE`, `CHANGELOG.md` and `CONTRIBUTING.md`.
- The evaluation plan and report under `docs/evaluation/`, with the frozen environment and live summary of the 2026-10-20 run.

### Changed

- The distributions take the names `smart-tts-cache` and `smart-tts-cache-pipecat`, because an unrelated project owns `tts-cache` on PyPI. The import names stay `tts_cache` and `tts_cache_pipecat`, and the commands stay `tts-cache` and `tts-cache-pipecat`.
- The README now centres on results, the quick start and operations.

## 0.1.0 - 2026-10-21

The first release: a sentence-level TTS audio cache in two packages, `tts-cache` (the framework-free core) and `tts-cache-pipecat` (the adapter for `pipecat-ai` 1.12.x).

### Added

- Sentence splitting on `sentencex` with per-language abbreviation rules, and canonicalisation for Latin and eleven Indic scripts.
- Voice-safe keys: provider, model, voice, language, sample rate, channels and audio settings form the scope hash; the canonical text forms the rest of the key.
- Match levels: A (exact), B (near duplicate: punctuation and leading or trailing filler, with a slot veto, a negation veto and a question veto), and C (paraphrase, opt-in, with an encoder, slot and span vetoes, and an NLI judge).
- Stores: `MemoryStore` with warm start, `RedisStore`, `SQLiteStore`, and `TieredStore` with a per-call deadline that fails open.
- Policies: `Probation`, `MinChars`, `SkipIfMatches`, `TurnPosition`, `SpeakFramesOnly`, `Namespace`, `RateLimit`, `Shadow`, `ByTag`, composed with `AllOf` and `AnyOf`; `DEFAULT_POLICY`, `BASE_POLICY` and `LEVEL_C_POLICY`.
- Opus storage at 32 kbps, and clip checks on duration and silence before a capture is stored.
- Metrics: one structured event per hit, miss, store, reject and fail-open through `LogSink`, and `PipecatMetricsSink` for `MetricsFrame`s.
- The `tts-cache` command: `replay` to estimate the hit rate from transcripts, `ping` to check Redis placement, `bench` for the synthetic benchmark, and `warm` to pre-synthesise a replay's hot set.
- The Pipecat adapter: `TTSCacheRead` and `TTSCacheWrite` around a stock TTS service, `scope_from_service`, and websocket subclasses for ElevenLabs (multi-context and Text-to-Dialogue) and Sarvam that close each sentence's context.
- Test fixtures in twelve languages for splitting, canonicalisation, filler, near-duplicate and semantic matching; the offline test matrix; the synthetic benchmark; and the live harness with provider attribution, a Redis pause fault and an integration check.

### Changed

- Level C became opt-in after the offline evaluation, and gained the NLI judge.
- Level B never serves a statement's audio for a question, or the reverse.

[Unreleased]: https://github.com/adimyth/smart-tts-caching/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/adimyth/smart-tts-caching/releases/tag/v0.2.0
