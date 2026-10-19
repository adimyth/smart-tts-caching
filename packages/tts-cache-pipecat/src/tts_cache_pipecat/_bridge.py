"""State the read and write sides share through the TTS service they sit around."""

from collections import deque
from dataclasses import dataclass, field

from pipecat.services.tts_service import TTSService


@dataclass
class PendingSpeak:
    """A ``TTSSpeakFrame`` miss the read side forwarded: the service makes its own text frame for it."""

    filtered: str
    key: str
    predicted: str


@dataclass
class Bridge:
    tags: dict[str, str] = field(default_factory=dict)
    speak: deque[PendingSpeak] = field(default_factory=lambda: deque(maxlen=32))


def bridge_for(tts: TTSService) -> Bridge:
    bridge = getattr(tts, "_tts_cache_bridge", None)
    if not isinstance(bridge, Bridge):
        bridge = Bridge()
        tts._tts_cache_bridge = bridge  # type: ignore[attr-defined]
    return bridge
