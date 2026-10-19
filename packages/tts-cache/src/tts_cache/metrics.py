"""Metrics: one structured event per hit, miss, store, reject and fail-open (SOLUTION.md 9.1)."""

import json
import logging
import time
from collections import Counter, defaultdict
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("tts_cache")


@runtime_checkable
class MetricsSink(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...


class LogSink:
    """Writes one JSON line per event to the ``tts_cache`` logger and keeps counters.

    Every lookup emits exactly one ``hit`` or ``miss`` event; a miss for any
    reason other than "not stored" (deadline, fail-open, breaker, policy skip,
    shadow) carries a ``reason``. ``counts[name]`` counts events and
    ``chars[name]`` sums their ``chars`` field, so the character-weighted hit
    rate is ``chars["hit"] / (chars["hit"] + chars["miss"])``: the share of
    characters that would otherwise have gone to the provider.
    ``latencies[name]`` keeps ``lookup_ms``.
    """

    def __init__(
        self, *, log: logging.Logger | None = logger, level: int = logging.INFO, keep: int = 0
    ) -> None:
        self._log = log
        self._level = level
        self.counts: Counter[str] = Counter()
        self.chars: Counter[str] = Counter()
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self._keep = keep
        self.events: list[dict[str, Any]] = []

    def event(self, name: str, **fields: Any) -> None:
        self.counts[name] += 1
        if "reason" in fields:
            self.counts[f"{name}:{fields['reason']}"] += 1
        if fields.get("diff_class"):
            self.counts[f"{name}:{fields['diff_class']}"] += 1
        if isinstance(fields.get("chars"), int):
            self.chars[name] += fields["chars"]
        if isinstance(fields.get("lookup_ms"), (int, float)):
            self.latencies[name].append(float(fields["lookup_ms"]))
        record = {"ts": round(time.time(), 3), "event": name, **fields}
        if self._keep and len(self.events) < self._keep:
            self.events.append(record)
        if self._log is not None and self._log.isEnabledFor(self._level):
            self._log.log(self._level, json.dumps(record, ensure_ascii=False, default=str))

    def hit_rate(self) -> float:
        """Character-weighted hit rate over every lookup that reached the store."""
        total = self.chars["hit"] + self.chars["miss"]
        return self.chars["hit"] / total if total else 0.0


class NullSink:
    def event(self, name: str, **fields: Any) -> None:
        pass


class FanOut:
    """Sends each event to several sinks; a failing sink never stops the others."""

    def __init__(self, *sinks: MetricsSink) -> None:
        self.sinks = sinks

    def event(self, name: str, **fields: Any) -> None:
        for s in self.sinks:
            try:
                s.event(name, **fields)
            except Exception:  # metrics must not break the host
                pass


class FailOpen:
    """Counts a caught exception as a ``fail_open`` event and logs it at most once a minute per site."""

    def __init__(self, metrics: MetricsSink, *, interval_s: float = 60.0) -> None:
        self.metrics = metrics
        self.interval_s = interval_s
        self._last: dict[str, float] = {}

    def __call__(self, site: str, exc: BaseException) -> None:
        try:
            self.metrics.event("fail_open", site=site, error=type(exc).__name__, reason=site)
        except Exception:
            pass
        now = time.monotonic()
        if now - self._last.get(site, -1e18) >= self.interval_s:
            self._last[site] = now
            logger.warning("tts-cache failed open at %s: %r", site, exc, exc_info=exc)
