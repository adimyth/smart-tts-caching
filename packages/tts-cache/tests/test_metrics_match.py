import json
import logging

from helpers import scope

from tts_cache.key import build_key
from tts_cache.match import ExactMatcher, Matcher
from tts_cache.metrics import FailOpen, FanOut, LogSink, MetricsSink


def test_log_sink_counts_and_logs(caplog) -> None:
    sink = LogSink(keep=10)
    assert isinstance(sink, MetricsSink)
    with caplog.at_level(logging.INFO, logger="tts_cache"):
        sink.event("hit", chars=10, lookup_ms=0.2, key="k")
        sink.event("miss", chars=30, lookup_ms=0.3)
        sink.event("reject", reason="duration")
    assert sink.counts["hit"] == 1 and sink.counts["reject:duration"] == 1
    assert sink.hit_rate() == 0.25
    assert sink.latencies["hit"] == [0.2]
    record = json.loads(caplog.records[0].getMessage())
    assert record["event"] == "hit" and record["chars"] == 10 and "ts" in record
    assert len(sink.events) == 3


def test_fail_open_logs_once_per_minute(caplog) -> None:
    sink = LogSink(log=None)
    fo = FailOpen(sink)
    with caplog.at_level(logging.WARNING, logger="tts_cache"):
        for _ in range(5):
            fo("lookup", RuntimeError("boom"))
        fo("capture", RuntimeError("boom"))
    assert sink.counts["fail_open"] == 6
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_fan_out_isolates_sinks() -> None:
    class Broken:
        def event(self, name, **fields):
            raise RuntimeError

    good = LogSink(log=None)
    FanOut(Broken(), good).event("hit", chars=1)
    assert good.counts["hit"] == 1


async def test_exact_matcher() -> None:
    m = ExactMatcher()
    assert isinstance(m, Matcher)
    found = await m.find(scope(), "hello", {"A"})
    assert found is not None and found.key == build_key(scope(), "hello") and found.level == "A"
    assert await m.find(scope(), "hello", {"B", "C"}) is None
