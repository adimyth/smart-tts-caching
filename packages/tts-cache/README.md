# smart-tts-cache

A sentence-level cache for text-to-speech audio. When a voice agent is about to say a sentence it has already said in the same voice, the cache plays the stored audio instead of calling the TTS provider: the sentence is not billed, and it starts playing in milliseconds.

This is the framework-free core: sentence splitting, canonicalisation, voice-safe keys, exact and near-duplicate matching, Redis and in-memory stores, policies and metrics. For a Pipecat pipeline, install [smart-tts-cache-pipecat](https://pypi.org/project/smart-tts-cache-pipecat/) as well, which places the cache around a TTS service.

```bash
pip install "smart-tts-cache[redis,similarity]"
```

The `redis` extra adds the store shared across processes, and `similarity` adds near-duplicate matching; without it, only exact repeats are served. The package imports as `tts_cache`.

Estimate the hit rate from your own agent's transcripts before you deploy:

```bash
tts-cache replay --log transcripts.jsonl --out replay.json
```

The documentation, the evaluation and a live demo are at [github.com/adimyth/smart-tts-caching](https://github.com/adimyth/smart-tts-caching).
