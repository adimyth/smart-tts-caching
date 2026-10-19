"""Audio codecs for stored clips: raw PCM, Ogg/Opus at 32 kbps, and FLAC.

PCM in and out is always 16-bit signed little-endian, interleaved when
``channels > 1``. Opus and FLAC go through libsndfile via ``soundfile``.
"""

import asyncio
import io
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import numpy as np
import soundfile as sf

Codec = Literal["pcm", "opus", "flac"]
CODECS: tuple[Codec, ...] = ("pcm", "opus", "flac")

OPUS_BITRATE = 32_000
# Opus encodes only these input rates.
OPUS_SAMPLE_RATES = frozenset({8000, 12000, 16000, 24000, 48000})


def _opus_compression_level(bitrate: int) -> float:
    # libsndfile maps compression level 0.0 to 1.0 linearly onto 256 kbps down to 6 kbps.
    return min(1.0, max(0.0, (256_000 - bitrate) / 250_000))


def _as_array(pcm: bytes, channels: int) -> np.ndarray:
    samples = np.frombuffer(pcm, dtype="<i2")
    return samples.reshape(-1, channels) if channels > 1 else samples


def encode(pcm: bytes, sample_rate: int, channels: int = 1, codec: Codec = "opus") -> bytes:
    """Encode 16-bit PCM with ``codec``."""
    if codec == "pcm":
        return bytes(pcm)
    if len(pcm) % (2 * channels):
        raise ValueError("PCM length is not a whole number of 16-bit frames")
    buf = io.BytesIO()
    data = _as_array(pcm, channels)
    if codec == "opus":
        if sample_rate not in OPUS_SAMPLE_RATES:
            raise ValueError(f"Opus cannot encode {sample_rate} Hz")
        sf.write(
            buf,
            data,
            sample_rate,
            format="OGG",
            subtype="OPUS",
            compression_level=_opus_compression_level(OPUS_BITRATE * channels),
        )
    elif codec == "flac":
        sf.write(buf, data, sample_rate, format="FLAC", subtype="PCM_16")
    else:
        raise ValueError(f"unknown codec {codec!r}")
    return buf.getvalue()


def decode(data: bytes, codec: Codec, sample_rate: int, channels: int = 1) -> bytes:
    """Decode a clip to 16-bit PCM, checking it has the expected rate and channel count."""
    if codec == "pcm":
        return bytes(data)
    if codec not in ("opus", "flac"):
        raise ValueError(f"unknown codec {codec!r}")
    with sf.SoundFile(io.BytesIO(data)) as f:
        if f.samplerate != sample_rate or f.channels != channels:
            raise ValueError(
                f"clip is {f.samplerate} Hz x{f.channels}, expected {sample_rate} Hz x{channels}"
            )
        samples = f.read(dtype="int16")
    return samples.astype("<i2", copy=False).tobytes()


# Streaming decode. A hit plays from the first decoded 200 ms while the rest of
# the clip decodes in a worker thread, about 2.3 ms of CPU per second of Opus
# audio at 24 kHz, so time to first audio does not wait for the whole clip.

CHUNK_MS = 20
BLOCK_MS = 200
_DECODER = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tts-cache-decode")


def frame_bytes(sample_rate: int, channels: int, ms: int) -> int:
    return sample_rate * ms // 1000 * 2 * channels


class PcmStream:
    """A clip's 16-bit PCM, either complete or still decoding in a worker thread.

    Any number of readers may iterate :meth:`chunks` at once; each waits only
    when it has caught up with the decoder.
    """

    def __init__(self, sample_rate: int, channels: int) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._blocks: list[bytes] = []
        self._done = False
        self._error: BaseException | None = None
        self._cond = threading.Condition()

    @classmethod
    def from_pcm(cls, pcm: bytes, sample_rate: int, channels: int = 1) -> "PcmStream":
        s = cls(sample_rate, channels)
        s._blocks.append(bytes(pcm))
        s._done = True
        return s

    @classmethod
    def decoding(cls, data: bytes, codec: Codec, sample_rate: int, channels: int = 1) -> "PcmStream":
        """Decode the first 200 ms now and the rest in a worker thread."""
        if codec == "pcm":
            return cls.from_pcm(data, sample_rate, channels)
        if codec not in ("opus", "flac"):
            raise ValueError(f"unknown codec {codec!r}")
        f = sf.SoundFile(io.BytesIO(data))
        try:
            if f.samplerate != sample_rate or f.channels != channels:
                raise ValueError(
                    f"clip is {f.samplerate} Hz x{f.channels}, expected {sample_rate} Hz x{channels}"
                )
            block = sample_rate * BLOCK_MS // 1000
            first = f.read(block, dtype="int16").astype("<i2", copy=False).tobytes()
        except BaseException:
            f.close()
            raise
        s = cls(sample_rate, channels)
        s._blocks.append(first)
        if len(first) < block * 2 * channels:
            f.close()
            s._done = True
        else:
            _DECODER.submit(s._decode_rest, f, block)
        return s

    def _decode_rest(self, f: sf.SoundFile, block: int) -> None:
        try:
            with f:
                while True:
                    pcm = f.read(block, dtype="int16").astype("<i2", copy=False).tobytes()
                    if pcm:
                        with self._cond:
                            self._blocks.append(pcm)
                            self._cond.notify_all()
                    if len(pcm) < block * 2 * self.channels:
                        break
        except BaseException as e:  # reported to every reader
            with self._cond:
                self._error = e
        finally:
            with self._cond:
                self._done = True
                self._cond.notify_all()

    @property
    def done(self) -> bool:
        return self._done

    def _block(self, i: int, timeout: float | None) -> bytes | None:
        with self._cond:
            if not self._cond.wait_for(lambda: i < len(self._blocks) or self._done, timeout):
                raise TimeoutError("decode did not keep up")
            if i < len(self._blocks):
                return self._blocks[i]
            if self._error is not None:
                raise self._error
            return None

    def chunks(self, chunk_ms: int = CHUNK_MS, *, timeout: float | None = 5.0) -> Iterator[bytes]:
        """Yield ``chunk_ms`` slices of PCM as they decode; the last one may be shorter."""
        size = frame_bytes(self.sample_rate, self.channels, chunk_ms)
        carry = b""
        i = 0
        while (block := self._block(i, timeout)) is not None:
            i += 1
            buf = carry + block if carry else block
            end = len(buf) - len(buf) % size
            for off in range(0, end, size):
                yield buf[off : off + size]
            carry = buf[end:]
        if carry:
            yield carry

    async def achunks(self, chunk_ms: int = CHUNK_MS, *, max_wait_s: float = 5.0) -> AsyncIterator[bytes]:
        """:meth:`chunks` for an event loop: waits for the decoder without blocking the loop."""
        size = frame_bytes(self.sample_rate, self.channels, chunk_ms)
        carry = b""
        i = 0
        deadline = time.monotonic() + max_wait_s
        while True:
            with self._cond:
                ready = i < len(self._blocks)
                block = self._blocks[i] if ready else None
                finished = self._done and not ready
                error = self._error if finished else None
            if error is not None:
                raise error
            if finished:
                break
            if block is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("decode did not keep up")
                # 200 ms of audio decodes in about 0.5 ms; poll at a fraction of that.
                await asyncio.sleep(0.0002)
                continue
            i += 1
            buf = carry + block if carry else block
            end = len(buf) - len(buf) % size
            for off in range(0, end, size):
                yield buf[off : off + size]
            carry = buf[end:]
        if carry:
            yield carry

    def result(self, timeout: float | None = 5.0) -> bytes:
        """The whole clip, waiting for the decode to finish."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._done, timeout):
                raise TimeoutError("decode did not finish")
            if self._error is not None:
                raise self._error
            if len(self._blocks) > 1:
                self._blocks = [b"".join(self._blocks)]
            return self._blocks[0] if self._blocks else b""


def decode_stream(data: bytes, codec: Codec, sample_rate: int, channels: int = 1) -> Iterator[bytes]:
    """20 ms PCM chunks of a clip, the first of them ready after decoding only 200 ms."""
    return PcmStream.decoding(data, codec, sample_rate, channels).chunks()
