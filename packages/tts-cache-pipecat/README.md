# smart-tts-cache-pipecat

The Pipecat adapter for [smart-tts-cache](https://pypi.org/project/smart-tts-cache/). Two frame processors go around a stock Pipecat TTS service, `TTSCacheRead` before it and `TTSCacheWrite` after it, and every sentence the agent has already said in the same voice plays from the cache instead of the provider.

Install it with Pipecat from the commit the adapter is verified against:

```bash
pip install "pipecat-ai @ git+https://github.com/pipecat-ai/pipecat.git@1d1385b6d5e3375199f98b2d456e56472bee16d2" \
    "smart-tts-cache[redis,similarity]" smart-tts-cache-pipecat
```

Pipecat 1.13 and later releases are not supported yet. The supported services are OpenAI over REST, ElevenLabs over REST, websocket and Text-to-Dialogue websocket, and Sarvam over REST and websocket. The package imports as `tts_cache_pipecat`.

The wiring, configuration and results are at [github.com/adimyth/smart-tts-caching](https://github.com/adimyth/smart-tts-caching).
