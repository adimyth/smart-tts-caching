import time

import numpy as np
import pytest

from tts_cache import codec

SR = 24_000


def speechlike(seconds: float = 4.0, sr: int = SR) -> bytes:
    """A voiced signal with a moving pitch and syllable-rate envelope, deterministic."""
    rng = np.random.default_rng(7)
    t = np.arange(int(seconds * sr)) / sr
    pitch = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / sr
    voiced = sum(np.sin(k * phase) / k for k in range(1, 12))
    envelope = np.clip(np.sin(2 * np.pi * 4 * t), 0, None)
    signal = 0.25 * voiced * envelope + 0.01 * rng.standard_normal(t.size)
    return (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()


def test_pcm_round_trip_is_identity() -> None:
    pcm = speechlike(1.0)
    assert codec.decode(codec.encode(pcm, SR, 1, "pcm"), "pcm", SR, 1) == pcm


def test_flac_round_trip_is_lossless() -> None:
    pcm = speechlike()
    data = codec.encode(pcm, SR, 1, "flac")
    assert codec.decode(data, "flac", SR, 1) == pcm
    assert len(data) < len(pcm)


def test_flac_round_trip_stereo() -> None:
    mono = np.frombuffer(speechlike(1.0), dtype="<i2")
    pcm = np.stack([mono, mono // 2], axis=1).astype("<i2").tobytes()
    assert codec.decode(codec.encode(pcm, SR, 2, "flac"), "flac", SR, 2) == pcm


def test_opus_round_trip_keeps_length_and_waveform() -> None:
    pcm = speechlike()
    data = codec.encode(pcm, SR, 1, "opus")
    out = codec.decode(data, "opus", SR, 1)
    assert len(out) == len(pcm)
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    b = np.frombuffer(out, dtype="<i2").astype(np.float64)
    assert np.corrcoef(a, b)[0, 1] > 0.9


def test_opus_bitrate_is_about_32_kbps() -> None:
    pcm = speechlike()
    data = codec.encode(pcm, SR, 1, "opus")
    kbps = len(data) * 8 / 4.0 / 1000
    assert 32 * 0.8 <= kbps <= 32 * 1.2


def test_opus_rejects_unsupported_rate() -> None:
    with pytest.raises(ValueError):
        codec.encode(speechlike(0.5, 22_050), 22_050, 1, "opus")


def test_decode_checks_rate() -> None:
    data = codec.encode(speechlike(0.5), SR, 1, "flac")
    with pytest.raises(ValueError):
        codec.decode(data, "flac", 16_000, 1)


# Streaming decode (4.2b): hits play from the first 200 ms while the rest decodes.


def _median(xs: list[float]) -> float:
    return sorted(xs)[len(xs) // 2]


@pytest.mark.parametrize("name", codec.CODECS)
def test_decode_stream_matches_decode(name: codec.Codec) -> None:
    pcm = speechlike()
    data = codec.encode(pcm, SR, 1, name)
    chunks = list(codec.decode_stream(data, name, SR, 1))
    assert b"".join(chunks) == codec.decode(data, name, SR, 1)
    size = codec.frame_bytes(SR, 1, 20)
    assert all(len(c) == size for c in chunks[:-1]) and 0 < len(chunks[-1]) <= size


def test_pcm_stream_supports_many_readers() -> None:
    pcm = speechlike()
    stream = codec.PcmStream.decoding(codec.encode(pcm, SR, 1, "opus"), "opus", SR, 1)
    a, b = stream.chunks(), stream.chunks(40)
    first_a = next(a)
    rest_b = b"".join(b)
    assert first_a + b"".join(a) == rest_b == stream.result()


def test_decode_stream_short_clip() -> None:
    pcm = speechlike(0.05)
    data = codec.encode(pcm, SR, 1, "flac")
    assert b"".join(codec.decode_stream(data, "flac", SR, 1)) == pcm


def test_decode_stream_checks_rate() -> None:
    data = codec.encode(speechlike(0.5), SR, 1, "opus")
    with pytest.raises(ValueError):
        codec.PcmStream.decoding(data, "opus", 16_000, 1)


@pytest.mark.slow  # a wall-clock budget: a local check, deselected in CI
def test_opus_first_200_ms_under_1_ms_and_full_clip_under_15_ms() -> None:
    """The M1 codec gate, on a 4 s 24 kHz clip; median of 9 runs."""
    data = codec.encode(speechlike(), SR, 1, "opus")
    first, full = [], []
    for _ in range(9):
        t = time.perf_counter()
        chunks = codec.decode_stream(data, "opus", SR, 1)
        head = b"".join(next(chunks) for _ in range(10))
        first.append((time.perf_counter() - t) * 1000)
        for _ in chunks:
            pass
        full.append((time.perf_counter() - t) * 1000)
        assert len(head) == codec.frame_bytes(SR, 1, 200)
    assert _median(first) < 1.0, first
    assert _median(full) < 15.0, full


def test_opus_size_matches_table() -> None:
    """Opus at 32 kbps holds 16 KB per 4 s clip (4.2b), within 20 percent; FLAC is smaller than PCM.

    The FLAC row of the table (about 80 KB) is checked on real TTS output by
    scripts/codec_gate.py, since FLAC's ratio depends on the signal.
    """
    pcm = speechlike()
    assert abs(len(codec.encode(pcm, SR, 1, "opus")) / 1000 - 16) <= 16 * 0.2
    assert len(codec.encode(pcm, SR, 1, "flac")) < len(pcm)


async def test_async_chunks_match_and_never_block() -> None:
    import asyncio

    pcm = speechlike()
    data = codec.encode(pcm, SR, 1, "opus")
    stream = codec.PcmStream.decoding(data, "opus", SR, 1)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    t = asyncio.ensure_future(ticker())
    out = b"".join([c async for c in stream.achunks()])
    t.cancel()
    assert out == codec.decode(data, "opus", SR, 1)
    assert ticks > 1  # the loop kept running while the clip decoded
