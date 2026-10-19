"""A metrics sink that also reports cache events as Pipecat ``MetricsFrame``s (SOLUTION.md 9.1)."""

import asyncio
from typing import Any

from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.processors.frame_processor import FrameProcessor

from tts_cache.metrics import LogSink, MetricsSink


class PipecatMetricsSink:
    """Forwards every event to ``inner`` and, once attached to a processor, pushes a
    ``MetricsFrame`` per hit and miss with the lookup time as the TTFB value, so
    dashboards built on Pipecat metrics see the cache."""

    def __init__(self, inner: MetricsSink | None = None) -> None:
        self.inner: MetricsSink = inner or LogSink()
        self._processor: FrameProcessor | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, processor: FrameProcessor) -> None:
        self._processor = processor

    def event(self, name: str, **fields: Any) -> None:
        try:
            self.inner.event(name, **fields)
        except Exception:
            pass
        if self._processor is None or name not in ("hit", "miss"):
            return
        try:
            lookup_ms = float(fields.get("lookup_ms") or 0.0)
            frame = MetricsFrame(data=[TTFBMetricsData(processor=f"TTSCache#{name}", value=lookup_ms / 1000)])
            task = asyncio.get_running_loop().create_task(self._processor.push_frame(frame))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            pass
