"""Codec for the 1080 raw data-sample wire format.

The 1080 web API returns a set's raw samples as a base64-encoded byte array.
Each sample is **20 bytes** — five little-endian ``float32`` values in the
order ``time, position, speed, acceleration, force``. Power is *not* on the
wire; the proto ``DataSample`` message carries it as a sixth field, derived as
``force * speed``.

Wire format confirmed against the vendored reference:
``vendor/1080-api/Samples/webapi-dotnet/DataSampleExtractor.cs`` (BinaryReader
order: TimeSinceStart, Position, Speed, Acceleration, Force) and
``vendor/1080-api/Samples/webapp-python/program.py`` (``struct.unpack`` with
the ``[0::5]``..``[4::5]`` slicing).

Australian English used throughout.
"""
from __future__ import annotations

import struct
from typing import TypedDict

#: Number of float32 values per sample on the wire (power is excluded).
FLOATS_PER_SAMPLE = 5
#: Bytes per sample = five 4-byte floats.
BYTES_PER_SAMPLE = FLOATS_PER_SAMPLE * 4  # 20


class Sample(TypedDict):
    """A single decoded data sample.

    ``time``/``position``/``speed``/``acceleration``/``force`` are read from
    the wire; ``power`` is derived (``force * speed``) and is not stored on
    the wire.
    """

    time: float
    position: float
    speed: float
    acceleration: float
    force: float
    power: float


def decode_sample_bytes(data: bytes) -> list[Sample]:
    """Decode a 1080 raw-sample byte array into a list of :class:`Sample`.

    Args:
        data: the raw (already base64-decoded) sample byte array. Its length
            must be a whole multiple of :data:`BYTES_PER_SAMPLE`.

    Returns:
        One :class:`Sample` per 20-byte record, in wire order.

    Raises:
        ValueError: if ``len(data)`` is not a multiple of 20.
    """
    if len(data) % BYTES_PER_SAMPLE != 0:
        raise ValueError(
            f"sample byte array length {len(data)} is not a multiple "
            f"of {BYTES_PER_SAMPLE}"
        )
    n_samples = len(data) // BYTES_PER_SAMPLE
    if n_samples == 0:
        return []
    flat = struct.unpack(f"<{n_samples * FLOATS_PER_SAMPLE}f", data)
    out: list[Sample] = []
    for i in range(n_samples):
        t, pos, spd, acc, frc = flat[i * 5:i * 5 + 5]
        out.append(Sample(
            time=t, position=pos, speed=spd, acceleration=acc, force=frc,
            power=frc * spd,
        ))
    return out


def encode_sample_bytes(samples: list[Sample]) -> bytes:
    """Encode samples back into the 1080 raw-sample wire format.

    The derived ``power`` field is dropped — only the five wire floats
    (``time, position, speed, acceleration, force``) are written, so
    ``decode_sample_bytes(encode_sample_bytes(s))`` round-trips those fields.

    Args:
        samples: the samples to encode.

    Returns:
        The packed byte array, 20 bytes per sample.
    """
    flat: list[float] = []
    for s in samples:
        flat.extend((
            s["time"], s["position"], s["speed"],
            s["acceleration"], s["force"],
        ))
    return struct.pack(f"<{len(samples) * FLOATS_PER_SAMPLE}f", *flat)
