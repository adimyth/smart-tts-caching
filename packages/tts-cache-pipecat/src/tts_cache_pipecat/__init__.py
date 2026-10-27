"""Pipecat integration for tts-cache: read and write frame processors around a TTS service."""

from tts_cache_pipecat.compat import (
    UnsupportedService,
    capture_mode_for,
    mark_primary_audio,
    register_fallback_service,
    register_service,
    scope_from_service,
)
from tts_cache_pipecat.metrics import PipecatMetricsSink
from tts_cache_pipecat.read import TTSCacheRead
from tts_cache_pipecat.services import ElevenLabsDialogueTTSService, ElevenLabsTTSService, SarvamTTSService
from tts_cache_pipecat.write import TTSCacheWrite

__version__ = "0.2.0"

__all__ = [
    "ElevenLabsDialogueTTSService",
    "ElevenLabsTTSService",
    "PipecatMetricsSink",
    "SarvamTTSService",
    "TTSCacheRead",
    "TTSCacheWrite",
    "UnsupportedService",
    "__version__",
    "capture_mode_for",
    "mark_primary_audio",
    "register_fallback_service",
    "register_service",
    "scope_from_service",
]
