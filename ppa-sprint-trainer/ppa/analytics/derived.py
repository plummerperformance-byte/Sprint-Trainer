"""Export-only derived metrics — the four columns 1080 exports that PPA does
not store natively.

All are computed from a motion's existing fields / sample blob; none need new
storage and none are round-tripped back in. Helpers live here rather than in
the top-level ``analytics.py`` so the rig code path is untouched.

Australian English throughout.
"""
from __future__ import annotations

from ppa.codecs.sample_bytes import decode_sample_bytes
from ppa.models.storage import MotionStorage


def _sliding_window_max_accel(sample_data: bytes | None,
                              window_s: float = 0.5) -> float | None:
    """Maximum acceleration over any ``window_s`` window of the run.

    Matches the 1080 ``AccelDecelStats`` method (``Δspeed`` across a 0.5 s
    window) — distinct from the instantaneous ``peak_acceleration``.
    """
    if not sample_data:
        return None
    samples = decode_sample_bytes(sample_data)
    if len(samples) < 2:
        return None
    best: float | None = None
    n = len(samples)
    for i in range(n):
        t0, v0 = samples[i]["time"], samples[i]["speed"]
        # Walk forward to the last sample still inside the window.
        j = i
        while j + 1 < n and samples[j + 1]["time"] - t0 <= window_s:
            j += 1
        dt = samples[j]["time"] - t0
        if dt <= 0:
            continue
        accel = (samples[j]["speed"] - v0) / dt
        if best is None or accel > best:
            best = accel
    return best


def _best_rolling_split(sample_data: bytes | None,
                        distance_m: float) -> float | None:
    """Fastest time to cover any rolling ``distance_m`` window of the run.

    Returns None if the run never covers ``distance_m``.
    """
    if not sample_data:
        return None
    samples = decode_sample_bytes(sample_data)
    if len(samples) < 2:
        return None
    best: float | None = None
    n = len(samples)
    for i in range(n):
        target = samples[i]["position"] + distance_m
        for j in range(i + 1, n):
            if samples[j]["position"] >= target:
                split = samples[j]["time"] - samples[i]["time"]
                if split > 0 and (best is None or split < best):
                    best = split
                break
    return best


def derive_1080_export_fields(motion: MotionStorage) -> dict:
    """Compute the four 1080-export fields PPA does not store natively.

    Export-only — never written back into storage.
    """
    distance = motion.total_distance if motion.total_distance is not None else motion.distance
    duration = motion.total_duration if motion.total_duration is not None else motion.duration
    avg_speed = (distance / duration) if (distance and duration) else 0.0
    return {
        "avg_speed_mps": avg_speed,
        "max_accel_0p5s_mps2": _sliding_window_max_accel(motion.data, window_s=0.5),
        "best_5m_split_s": _best_rolling_split(motion.data, distance_m=5.0),
        "best_10m_split_s": _best_rolling_split(motion.data, distance_m=10.0),
    }
