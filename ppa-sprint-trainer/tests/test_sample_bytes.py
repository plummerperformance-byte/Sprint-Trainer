"""Tests for the 1080 sample-byte codec."""
import pytest

from ppa.codecs.sample_bytes import (
    BYTES_PER_SAMPLE,
    decode_sample_bytes,
    encode_sample_bytes,
)


def _make_samples(n):
    """Build n samples with float32-friendly values."""
    return [
        {
            "time": i * 0.5,
            "position": i * 1.25,
            "speed": i * 0.1,
            "acceleration": i * 0.25,
            "force": i * 2.0,
            "power": 0.0,
        }
        for i in range(n)
    ]


@pytest.mark.parametrize("n", [1, 100, 10000])
def test_round_trip(n):
    samples = _make_samples(n)
    raw = encode_sample_bytes(samples)
    assert len(raw) == n * BYTES_PER_SAMPLE
    assert len(raw) % 20 == 0
    decoded = decode_sample_bytes(raw)
    assert len(decoded) == n
    # Byte-level round trip — encode(decode(x)) == x, precision-safe.
    assert encode_sample_bytes(decoded) == raw
    # Power is derived, not on the wire.
    for s in decoded:
        assert s["power"] == pytest.approx(s["force"] * s["speed"])
        assert set(s) == {
            "time", "position", "speed", "acceleration", "force", "power"
        }


def test_empty_input():
    assert decode_sample_bytes(b"") == []
    assert encode_sample_bytes([]) == b""


def test_zero_sample():
    decoded = decode_sample_bytes(b"\x00" * 20)
    assert len(decoded) == 1
    assert decoded[0] == {
        "time": 0.0, "position": 0.0, "speed": 0.0,
        "acceleration": 0.0, "force": 0.0, "power": 0.0,
    }


@pytest.mark.parametrize("bad_len", [1, 19, 21, 39])
def test_bad_length_raises(bad_len):
    with pytest.raises(ValueError):
        decode_sample_bytes(b"\x00" * bad_len)
