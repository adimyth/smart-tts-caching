"""Voice identity: everything that changes the sound of a sentence (SOLUTION.md section 3)."""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from tts_cache.canonical import SCHEMA_VERSION


def _normalise(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        if value.is_integer():
            return int(value)
        return float(f"{value:.6g}")
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return str(value)


def canonical_json(value: Any) -> str:
    """JSON with sorted keys, no whitespace, and floats normalised so 1 and 1.0 agree."""
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Scope:
    """The fields of the section 3 table. ``settings`` holds only settings that change the audio."""

    service: str
    model: str
    voice: str
    language: str
    sample_rate: int
    channels: int = 1
    settings: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    generation: int = 0

    def as_dict(self, *, with_generation: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "service": self.service,
            "model": self.model,
            "voice": self.voice,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "settings": dict(self.settings),
            "schema_version": self.schema_version,
        }
        if with_generation:
            d["generation"] = self.generation
        return d

    @property
    def lang(self) -> str:
        """The primary language subtag, lowercased: ``hi`` for ``hi-IN``."""
        return self.language.replace("_", "-").split("-")[0].lower()

    def with_generation(self, generation: int) -> "Scope":
        return replace(self, generation=generation)


def scope_hash(scope: Scope, namespace: str | None = None) -> str:
    """``sha256(canonical_json(scope))[:16]``; a ``Namespace`` policy folds its tag value in."""
    d = scope.as_dict()
    if namespace is not None:
        d["namespace"] = namespace
    return _sha(canonical_json(d))[:16]


def scope_base_hash(scope: Scope) -> str:
    """The scope hash without the generation, which names the generation counter itself."""
    return _sha(canonical_json(scope.as_dict(with_generation=False)))[:16]
