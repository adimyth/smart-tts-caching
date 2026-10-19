"""The live cells of TEST_PLAN.md 11.1 and the frozen price table, shared by the harness and the report."""

from dataclasses import dataclass

PROVIDER = {
    "openai_http": "openai",
    "elevenlabs_http": "elevenlabs",
    "elevenlabs_ws_fc": "elevenlabs",
    "elevenlabs_dialogue_ws_c": "elevenlabs",
    "sarvam_http": "sarvam",
    "sarvam_ws_flush": "sarvam",
}
REST = frozenset({"openai_http", "elevenlabs_http", "sarvam_http"})
WEBSOCKET = frozenset(PROVIDER) - REST
_INDIC8 = ("bn", "pa", "gu", "mr", "or", "te", "kn", "ml")
CORE: dict[str, tuple[str, ...]] = {
    "en": ("elevenlabs_ws_fc", "openai_http"),
    "hi": ("sarvam_ws_flush", "openai_http"),
    "ta": ("sarvam_ws_flush", "openai_http"),
    "as": ("openai_http",),
    **{lang: ("sarvam_ws_flush", "openai_http") for lang in _INDIC8},
}
SPREAD: dict[str, tuple[str, ...]] = {
    "en": ("elevenlabs_http", "elevenlabs_dialogue_ws_c", "sarvam_http", "sarvam_ws_flush"),
    "hi": ("elevenlabs_http", "elevenlabs_ws_fc", "elevenlabs_dialogue_ws_c", "sarvam_http"),
    "ta": ("elevenlabs_http", "elevenlabs_ws_fc", "elevenlabs_dialogue_ws_c", "sarvam_http"),
}
CORE_RUNS = (
    ("OFF", 0.25),
    ("OFF", 0.5),
    ("A", 0.25),
    ("A+B", 0.25),
    ("A+B+C", 0.25),
    ("A", 0.5),
    ("A+B", 0.5),
    ("A+B+C", 0.5),
)
SPREAD_RUNS = (("OFF", 0.5), ("A+B+C", 0.5))
SEED = 1  # every live run uses seed 1's corpus from the offline matrix
SARVAM_CODE = {
    "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "pa": "pa-IN", "gu": "gu-IN", "mr": "mr-IN",
    "or": "od-IN", "ta": "ta-IN", "te": "te-IN", "kn": "kn-IN", "ml": "ml-IN",
}  # fmt: skip
ELEVENLABS_CODE = {"en": "en", "hi": "hi", "ta": "ta"}

# TEST_REPORT.md, "Derived: cost per character saved", looked up 2026-10-18. Dollars per million characters.
PRICE_DATE = "2026-10-18"
USD_PER_MCHARS = {"openai": 16.0, "elevenlabs": 50.0, "sarvam": 24.0}
INR_PER_MCHARS_SARVAM = 2000.0  # bulbul:v4-flash standard rate; v3 is 3000
PRICE_NOTES = {
    "openai": "gpt-4o-mini-tts: $0.60 per 1M input characters plus $12 per 1M audio output tokens; "
    "about $16 per 1M characters all in, from a measured run",
    "elevenlabs": "eleven_flash_v2_5: $0.05 per 1,000 characters pay-as-you-go",
    "sarvam": "bulbul:v4-flash: ₹20 per 10,000 characters standard "
    "(₹10 promotional until 12 Nov 2026), about $24 per 1M",
}
# Managed Redis for the impact model (13.2), dollars per GB-month. An assumption stated in the
# report, not a price looked up for this run: the order of a managed serverless Redis's storage rate.
REDIS_USD_PER_GB_MONTH = 90.0


@dataclass(frozen=True)
class LiveRun:
    lang: str
    service: str
    config: str
    r: float
    kind: str  # "core" or "spread"

    @property
    def id(self) -> str:
        return f"{self.lang}_{self.service}_{self.config}_{self.r}"

    @property
    def provider(self) -> str:
        return PROVIDER[self.service]

    @property
    def rest(self) -> bool:
        return self.service in REST


def cells(langs: tuple[str, ...] | list[str]) -> list[tuple[str, str, str]]:
    """(language, service, kind) for every cell of 11.1 in ``langs``."""
    out = []
    for lang in langs:
        out += [(lang, s, "core") for s in CORE.get(lang, ())]
        out += [(lang, s, "spread") for s in SPREAD.get(lang, ())]
    return out


def live_runs(langs: tuple[str, ...] | list[str]) -> list[LiveRun]:
    runs = []
    for lang, service, kind in cells(langs):
        for config, r in CORE_RUNS if kind == "core" else SPREAD_RUNS:
            runs.append(LiveRun(lang, service, config, r, kind))
    return runs


def usd(provider: str, chars: float) -> float:
    return chars * USD_PER_MCHARS[provider] / 1e6
