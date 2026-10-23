"""Which Pipecat services the adapter captures from, and their voice identity (SOLUTION.md 3, 11.8).

The capture-mode table is keyed on the exact service class and the pinned
Pipecat minor version. A class not in the table, a subclass of one that is,
or any class under another Pipecat version is ``serve_only``: it can be
served from the cache but never captured from, so a service whose
attribution has not been verified can forgo captures but never store a
wrong clip.

A fallback service speaks through a primary provider and switches to another
when the primary fails. Its scope is the primary's, and audio is stored only
from frames the service marks as the primary's, so a sentence a fallback
provider spoke never enters the store under the primary's voice.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from importlib.metadata import version
from typing import Any, Literal

from pipecat.frames.frames import Frame
from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService as _StockDialogue
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService as _StockElevenLabs
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.sarvam.tts import SarvamHttpTTSService
from pipecat.services.sarvam.tts import SarvamTTSService as _StockSarvam
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.types import is_given

from tts_cache.scope import Scope
from tts_cache_pipecat import services

CaptureMode = Literal["exact", "serve_only", "off"]

PINNED_PIPECAT = "1.12"


class UnsupportedService(Exception):
    """The adapter cannot derive a scope for this service; the read side passes everything through."""


@dataclass(frozen=True)
class ServiceSpec:
    service: str
    settings: tuple[str, ...]


# Audio-affecting settings per class. Timeouts, latency modes that do not touch
# the audio, and buffer sizes stay out of the scope (section 3).
_ELEVENLABS_VOICE = (
    "stability",
    "similarity_boost",
    "style",
    "use_speaker_boost",
    "speed",
    "apply_text_normalization",
)
_SARVAM = ("enable_preprocessing", "pace", "pitch", "loudness", "temperature")
SERVICES: dict[type, ServiceSpec] = {
    OpenAITTSService: ServiceSpec("openai", ("speed", "instructions")),
    ElevenLabsHttpTTSService: ServiceSpec("elevenlabs", (*_ELEVENLABS_VOICE, "optimize_streaming_latency")),
    _StockElevenLabs: ServiceSpec("elevenlabs", _ELEVENLABS_VOICE),
    _StockDialogue: ServiceSpec("elevenlabs", ("stability",)),
    SarvamHttpTTSService: ServiceSpec("sarvam", _SARVAM),
    _StockSarvam: ServiceSpec("sarvam", _SARVAM),
}

CAPTURE_MODES: dict[type, CaptureMode] = {
    OpenAITTSService: "exact",
    ElevenLabsHttpTTSService: "exact",
    services.ElevenLabsTTSService: "exact",
    services.ElevenLabsDialogueTTSService: "exact",
    SarvamHttpTTSService: "exact",
    services.SarvamTTSService: "exact",
}


FALLBACK_SERVICES: dict[type, Callable[[TTSService], TTSService]] = {}

# Frame metadata key a fallback service sets on audio its primary provider produced.
PRIMARY_AUDIO = "tts_cache_primary"


def register_service(
    cls: type, service: str, settings: Iterable[str] = (), *, capture_mode: CaptureMode | None = None
) -> None:
    """Teach the adapter a service class: its provider name, audio settings and capture mode."""
    SERVICES[cls] = ServiceSpec(service, tuple(settings))
    if capture_mode is not None:
        CAPTURE_MODES[cls] = capture_mode


def register_fallback_service(
    cls: type, *, primary: Callable[[TTSService], TTSService], capture_mode: CaptureMode = "exact"
) -> None:
    """Teach the adapter a service that speaks through ``primary(service)`` and falls back to others.

    The primary's class must be known to the adapter (built in, or added with
    :func:`register_service`). The service must call :func:`mark_primary_audio`
    on every audio frame the primary produced; a sentence with any unmarked
    audio is not stored.
    """
    FALLBACK_SERVICES[cls] = primary
    CAPTURE_MODES[cls] = capture_mode


def mark_primary_audio(frame: Frame) -> Frame:
    """Mark ``frame`` as audio from the fallback service's primary provider."""
    frame.metadata[PRIMARY_AUDIO] = True
    return frame


def is_fallback_service(tts: TTSService) -> bool:
    return type(tts) in FALLBACK_SERVICES


def pipecat_version() -> str:
    return version("pipecat-ai")


def capture_mode_for(tts: TTSService) -> CaptureMode:
    if not isinstance(tts, TTSService):
        return "off"
    if not pipecat_version().startswith(PINNED_PIPECAT + "."):
        return "serve_only"
    return CAPTURE_MODES.get(type(tts), "serve_only")


def _spec(tts: TTSService) -> ServiceSpec:
    for cls in type(tts).__mro__:
        if cls in SERVICES:
            return SERVICES[cls]
    raise UnsupportedService(f"{type(tts).__name__} is not a supported TTS service")


def _value(v: Any) -> Any:
    return getattr(v, "value", v)


def scope_from_settings(
    tts: TTSService, settings: TTSSettings, *, language: str, sample_rate: int | None = None
) -> Scope:
    """The scope ``tts`` would synthesise with under ``settings``."""
    if is_fallback_service(tts):
        # Its settings may not reach the primary; a scope we cannot vouch for must not serve.
        raise UnsupportedService(f"{type(tts).__name__} is a fallback service; its settings cannot change")
    spec = _spec(tts)
    # Until the pipeline starts a service built without a sample rate does not know
    # it; the scope then carries 0, which matches nothing, and the read side fills
    # it in from the StartFrame.
    rate = sample_rate or getattr(tts, "_init_sample_rate", None) or tts.sample_rate or 0
    model = settings.model if is_given(settings.model) else None
    voice = settings.voice if is_given(settings.voice) else None
    if not model or not voice:
        raise UnsupportedService(f"{type(tts).__name__} has no model or voice set")
    audio: dict[str, Any] = {}
    for name in spec.settings:
        v = getattr(settings, name, None)
        if is_given(v) and v is not None:
            audio[name] = _value(v)
    service_language = settings.language if is_given(settings.language) else None
    if service_language is not None:
        audio["language"] = str(_value(service_language))
    return Scope(
        service=spec.service,
        model=str(model),
        voice=str(voice),
        language=language,
        sample_rate=int(rate),
        channels=1,
        settings=audio,
    )


def scope_from_service(tts: TTSService, *, language: str, sample_rate: int | None = None) -> Scope:
    """Service name, model, voice, language, sample rate and audio settings of ``tts``.

    Raises :class:`UnsupportedService` for a class the adapter does not know or a
    service with no model or voice. A sample rate the service does not know yet
    is 0 until the pipeline's StartFrame supplies it.

    A fallback service has its primary's scope at the sample rate the fallback
    service itself emits.
    """
    primary = FALLBACK_SERVICES.get(type(tts))
    if primary is None:
        return scope_from_settings(tts, tts._settings, language=language, sample_rate=sample_rate)
    rate = sample_rate or getattr(tts, "_init_sample_rate", None) or tts.sample_rate or 0
    inner = primary(tts)
    if inner is None:
        raise UnsupportedService(f"{type(tts).__name__} has no primary provider")
    return replace(scope_from_settings(inner, inner._settings, language=language), sample_rate=int(rate))


def settings_after(
    tts: TTSService, delta: TTSSettings | None, legacy: Mapping[str, Any] | None
) -> TTSSettings:
    """The service's settings once a ``TTSUpdateSettingsFrame`` applies, without touching the service."""
    current = tts._settings.copy()
    if delta is None and legacy:
        delta = type(tts._settings).from_mapping(dict(legacy))
    if delta is not None:
        delta = delta.copy()
        # The service converts a Language to its own code before storing it; do the same.
        if is_given(delta.language) and isinstance(delta.language, str):
            try:
                converted = tts.language_to_service_language(Language(delta.language))
            except ValueError:
                converted = None
            if converted is not None:
                delta.language = converted
        current.apply_update(delta)
    return current
