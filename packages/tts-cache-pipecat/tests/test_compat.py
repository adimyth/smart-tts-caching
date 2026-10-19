import aiohttp
import pytest
from fake_tts import FakeTTSService
from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService as StockDialogue
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService as StockElevenLabs
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.sarvam.tts import SarvamHttpTTSService
from pipecat.services.sarvam.tts import SarvamTTSService as StockSarvam
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

import tts_cache_pipecat as tcp
from tts_cache_pipecat.compat import settings_after

VOICE = "21m00Tcm4TlvDq8ikWAM"


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def build(name: str, session: aiohttp.ClientSession, **settings) -> TTSService:
    common = {"sample_rate": 24000, "reuse_context_id_within_turn": False}
    if name == "openai_http":
        return OpenAITTSService(
            api_key="x",
            settings=OpenAITTSService.Settings(model="gpt-4o-mini-tts", voice="alloy", **settings),
            **common,
        )
    if name == "elevenlabs_http":
        return ElevenLabsHttpTTSService(
            api_key="x",
            aiohttp_session=session,
            settings=ElevenLabsHttpTTSService.Settings(
                model="eleven_flash_v2_5", voice=VOICE, language="en", **settings
            ),
            **common,
        )
    if name == "elevenlabs_ws":
        return tcp.ElevenLabsTTSService(
            api_key="x",
            settings=tcp.ElevenLabsTTSService.Settings(
                model="eleven_flash_v2_5", voice=VOICE, language="en", **settings
            ),
            **common,
        )
    if name == "elevenlabs_dialogue_ws":
        return tcp.ElevenLabsDialogueTTSService(
            api_key="x",
            settings=tcp.ElevenLabsDialogueTTSService.Settings(
                model="eleven_v4_turbo", voice=VOICE, **settings
            ),
            **common,
        )
    if name == "sarvam_http":
        return SarvamHttpTTSService(
            api_key="x",
            aiohttp_session=session,
            settings=SarvamHttpTTSService.Settings(
                model="bulbul:v3", voice="shubh", language="hi-IN", **settings
            ),
            **common,
        )
    if name == "sarvam_ws":
        return tcp.SarvamTTSService(
            api_key="x",
            settings=tcp.SarvamTTSService.Settings(
                model="bulbul:v3", voice="shubh", language="hi-IN", **settings
            ),
            **common,
        )
    raise AssertionError(name)


EXPECTED = {
    "openai_http": ("openai", "gpt-4o-mini-tts", "alloy", {}),
    "elevenlabs_http": ("elevenlabs", "eleven_flash_v2_5", VOICE, {"language": "en"}),
    "elevenlabs_ws": ("elevenlabs", "eleven_flash_v2_5", VOICE, {"language": "en"}),
    "elevenlabs_dialogue_ws": ("elevenlabs", "eleven_v4_turbo", VOICE, {}),
    "sarvam_http": (
        "sarvam",
        "bulbul:v3",
        "shubh",
        {"enable_preprocessing": True, "pace": 1.0, "language": "hi-IN"},
    ),
    "sarvam_ws": (
        "sarvam",
        "bulbul:v3",
        "shubh",
        {"enable_preprocessing": True, "pace": 1.0, "language": "hi-IN"},
    ),
}


@pytest.mark.parametrize("name", list(EXPECTED))
async def test_six_classes_capture_exact_with_their_scope(name: str, session) -> None:
    tts = build(name, session)
    assert tcp.capture_mode_for(tts) == "exact"
    scope = tcp.scope_from_service(tts, language="en-US")
    service, model, voice, settings = EXPECTED[name]
    assert (scope.service, scope.model, scope.voice, dict(scope.settings)) == (
        service,
        model,
        voice,
        settings,
    )
    assert scope.sample_rate == 24000 and scope.channels == 1 and scope.language == "en-US"


@pytest.mark.parametrize(
    ("name", "setting", "value"),
    [
        ("openai_http", "speed", 1.2),
        ("openai_http", "instructions", "calm"),
        ("elevenlabs_http", "stability", 0.3),
        ("elevenlabs_ws", "similarity_boost", 0.9),
        ("elevenlabs_dialogue_ws", "stability", 0.5),
        ("sarvam_http", "temperature", 0.3),
        ("sarvam_ws", "pace", 1.3),
    ],
)
async def test_audio_settings_change_the_scope(name: str, setting: str, value, session) -> None:
    plain = tcp.scope_from_service(build(name, session), language="en-US")
    tuned = tcp.scope_from_service(build(name, session, **{setting: value}), language="en-US")
    assert tuned != plain and tuned.settings[setting] == value


async def test_buffer_settings_stay_out_of_the_scope(session) -> None:
    a = tcp.scope_from_service(build("sarvam_ws", session), language="hi-IN")
    b = tcp.scope_from_service(
        build("sarvam_ws", session, min_buffer_size=10, max_chunk_length=100), language="hi-IN"
    )
    assert a == b


async def test_stock_websocket_classes_are_serve_only(session) -> None:
    for tts in [
        StockElevenLabs(
            api_key="x",
            settings=StockElevenLabs.Settings(model="eleven_flash_v2_5", voice=VOICE),
            sample_rate=24000,
        ),
        StockDialogue(
            api_key="x",
            settings=StockDialogue.Settings(model="eleven_v4_turbo", voice=VOICE),
            sample_rate=24000,
        ),
        StockSarvam(
            api_key="x", settings=StockSarvam.Settings(model="bulbul:v3", voice="shubh"), sample_rate=24000
        ),
    ]:
        assert tcp.capture_mode_for(tts) == "serve_only"
        assert tcp.scope_from_service(tts, language="en").service in ("elevenlabs", "sarvam")


def test_unknown_class_is_unsupported() -> None:
    class Other(TTSService):
        async def run_tts(self, text, context_id):
            yield None

    other = Other(settings=TTSSettings(model="m", voice="v", language=None), sample_rate=24000)
    assert tcp.capture_mode_for(other) == "serve_only"
    with pytest.raises(tcp.UnsupportedService):
        tcp.scope_from_service(other, language="en")


def test_other_pipecat_version_is_serve_only(monkeypatch) -> None:
    monkeypatch.setattr("tts_cache_pipecat.compat.pipecat_version", lambda: "1.13.0")
    assert tcp.capture_mode_for(FakeTTSService()) == "serve_only"


def test_settings_after_does_not_touch_the_service() -> None:
    tts = FakeTTSService()
    after = settings_after(tts, TTSSettings(voice="v2"), None)
    assert after.voice == "v2" and tts._settings.voice == "fake-voice"
