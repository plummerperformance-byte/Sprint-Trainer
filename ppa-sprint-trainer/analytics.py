"""analytics.py - per-rep metrics computed on demand from samples.

Pure-Python, no external deps.  Called by ppa_service for the
GET /api/sessions/{id}/reps/{rep_id}/analytics endpoint.

NOT called from end_rep — keeps end_rep fast.  Computed from samples on
each request; for a typical rep (<1000 samples) this is sub-millisecond.

----------------------------------------------------------------------------
CALIBRATION CONSTANTS - both currently placeholders.  See CALIBRATION.md
----------------------------------------------------------------------------
"""
from __future__ import annotations

from typing import Optional

# ===== CALIBRATION CONSTANTS - LOCKED IN 2026-05-10 =====
# 17-bit serial encoder per V5.1 manual p21 (MA 2KW low-inertia, EIA422).
# HARDWARE FACT ONLY — do NOT combine with COUNTS_PER_METRE to derive
# metres/rev or speed: the drive's position register is scaled, not raw
# encoder counts, so 131072/27917 gives a metres/rev ~13.6x too large
# (it silently reproduces the superseded 379,288 counts/m error). Speed
# conversions must go through CABLE_METRES_PER_REV / MPS_PER_RPM below.
ENCODER_COUNTS_PER_REV = 131_072

# Re-measured 2026-05-15 by a direct 10 m cable pull over CN4/RS485: the
# cable moved the position counter 279,171 counts over 10.0 m, giving
# counts_per_metre = 27,917. The earlier 379,288 figure (a 3-point HMI
# sweep) was ~13.6x too large and made every distance read as a tiny
# fraction of the real value.
COUNTS_PER_METRE = 27_917.0
# Cable metres per motor rev — held fixed (independent of COUNTS_PER_METRE)
# so the RPM->m/s speed factor below is not disturbed by the position
# recalibration. May still need its own calibration pass.
CABLE_METRES_PER_REV = 0.3456

# Force calibration: HMI kg setpoint → drive P21.04 commanded torque %.
# Locked 4-point fit 2026-05-10 (2/5/10/15 kg, R²≈1.0).
PCT_PER_KG = 5.64
KG_LIMIT_FACTORY = 300.0 / PCT_PER_KG  # ~53 kg before 300% factory cap

# RPM → m/s (assumes 1:1 motor:drum, derived from above).
MPS_PER_RPM = CABLE_METRES_PER_REV / 60.0  # ≈ 0.00576

# Sprint split distances (metres).  Order matters for output.
SPLIT_DISTANCES_M = (5, 10, 20, 30, 40)

# Phase tagging thresholds (fraction of peak velocity).
ACCEL_END_FRAC = 0.90  # accel ends when |v| first reaches 90% of peak
MAXV_END_FRAC = 0.95   # max-V ends when |v| first drops below 95% of peak

# Step detection.  Min realistic step rate ~5 Hz -> ~200 ms minimum spacing.
STEP_MIN_GAP_MS = 120
# Don't count tiny noise wiggles as steps.
STEP_MIN_PROMINENCE_FRAC = 0.05  # peak must rise >= 5% of peak velocity above
                                 # the smoothed trend


# ----- unit conversions -----

def rpm_to_mps(rpm: float) -> float:
    """Cable speed in metres/sec from motor RPM."""
    return rpm / 60.0 * CABLE_METRES_PER_REV


def counts_to_metres(counts: int) -> float:
    return counts / COUNTS_PER_METRE


# ----- public entry point -----

def compute_rep_analytics(samples: list[dict]) -> dict:
    """samples: list of dicts ordered by t_offset_ms, each with at least
    t_offset_ms, speed_rpm, position_counts.  None values tolerated.

    Returns dict with all metrics.  None values where a metric can't be
    computed (e.g. splits not reached, step detection inconclusive).

    API field naming aligns with 1080 Motion's public proto vocabulary:
    `peak_speed_mps`, `avg_speed_mps`, `distance` (metres), `duration`
    (seconds).  Internal computation stays in ms — `duration` is converted
    at the API edge.
    """
    if not samples or len(samples) < 2:
        return _empty_metrics()

    t_ms = [s.get("t_offset_ms") or 0 for s in samples]
    speed_rpm = [s.get("speed_rpm") or 0 for s in samples]
    pos = [s.get("position_counts") or 0 for s in samples]
    torque_pct = [s.get("torque_pct") for s in samples]

    speed_mps = [rpm_to_mps(r) for r in speed_rpm]
    abs_speed_mps = [abs(v) for v in speed_mps]

    if not any(abs_speed_mps):
        return _empty_metrics(samples_count=len(samples))

    # Peak speed & time-to-peak
    peak_speed = max(abs_speed_mps)
    peak_idx = abs_speed_mps.index(peak_speed)
    time_to_peak_ms = t_ms[peak_idx] - t_ms[0]

    # Average speed across all samples
    avg_speed = sum(abs_speed_mps) / len(abs_speed_mps)

    # Average torque across samples that reported a value
    abs_torque = [abs(v) for v in torque_pct if v is not None]
    avg_torque_pct = sum(abs_torque) / len(abs_torque) if abs_torque else None
    peak_torque_pct = max(abs_torque) if abs_torque else None

    # Distance (path length) and cumulative for splits
    deltas_counts = [abs(pos[i + 1] - pos[i]) for i in range(len(pos) - 1)]
    distance_counts = sum(deltas_counts)
    distance_m = counts_to_metres(distance_counts)

    cum_dist_m = [0.0]
    for d in deltas_counts:
        cum_dist_m.append(cum_dist_m[-1] + counts_to_metres(d))

    splits_ms = {}
    for thresh in SPLIT_DISTANCES_M:
        idx = next((i for i, d in enumerate(cum_dist_m) if d >= thresh), None)
        splits_ms[thresh] = (t_ms[idx] - t_ms[0]) if idx is not None else None

    # Phase windows (offsets relative to rep start in ms)
    phase_windows = _compute_phases(t_ms, abs_speed_mps, peak_speed)

    # Step detection within accel + maxv phases
    step_indices = _detect_steps(t_ms, abs_speed_mps, phase_windows, peak_speed)
    step_count = len(step_indices)

    # Step rate over the running window (accel + maxv).
    if phase_windows["accel"] and phase_windows["maxv"]:
        running_start_ms = phase_windows["accel"][0]
        running_end_ms = phase_windows["maxv"][1]
    elif phase_windows["accel"]:
        running_start_ms, running_end_ms = phase_windows["accel"]
    else:
        running_start_ms, running_end_ms = (0, 0)
    running_seconds = (running_end_ms - running_start_ms) / 1000.0
    step_rate_hz = (
        step_count / running_seconds
        if (step_count > 0 and running_seconds > 0)
        else None
    )

    avg_step_length_m = (distance_m / step_count) if step_count > 0 else None

    # Decelerations (positive = decreasing speed) across whole rep
    all_decels = []
    for i in range(len(speed_mps) - 1):
        dt_s = (t_ms[i + 1] - t_ms[i]) / 1000.0
        if dt_s > 0:
            all_decels.append(-(speed_mps[i + 1] - speed_mps[i]) / dt_s)
    peak_decel_mps2 = max(all_decels) if all_decels else 0.0

    # Average decel: confined to the decel phase, positive values only
    avg_decel_mps2 = _avg_decel_in_phase(t_ms, speed_mps, phase_windows)

    duration_ms = t_ms[-1] - t_ms[0]

    return {
        "samples_count": len(samples),
        "duration": round(duration_ms / 1000.0, 3),
        "peak_speed_mps": round(peak_speed, 3),
        "avg_speed_mps": round(avg_speed, 3),
        "distance": round(distance_m, 3),
        "time_to_peak_speed_ms": time_to_peak_ms,
        "splits_ms": splits_ms,
        "step_count": step_count,
        "step_rate_hz": round(step_rate_hz, 2) if step_rate_hz is not None else None,
        "avg_step_length_m": round(avg_step_length_m, 3) if avg_step_length_m is not None else None,
        "peak_decel_mps2": round(peak_decel_mps2, 3),
        "avg_decel_mps2": round(avg_decel_mps2, 3) if avg_decel_mps2 is not None else None,
        "peak_torque_pct": round(peak_torque_pct, 2) if peak_torque_pct is not None else None,
        "avg_torque_pct": round(avg_torque_pct, 2) if avg_torque_pct is not None else None,
        "phase_windows": phase_windows,
        "calibration": {
            "encoder_counts_per_rev": ENCODER_COUNTS_PER_REV,
            "cable_metres_per_rev": CABLE_METRES_PER_REV,
            "counts_per_metre": round(COUNTS_PER_METRE, 3),
            "calibrated": False,  # set True once measured at the rig
        },
    }


def _avg_decel_in_phase(t_ms, speed_mps, phase_windows):
    """Mean of POSITIVE decel values during the decel phase only.
    Returns None if there's no decel phase or no positive-decel samples in it."""
    decel_window = phase_windows.get("decel")
    if not decel_window or not t_ms:
        return None
    rep_start = t_ms[0]
    lo_off, hi_off = decel_window
    idx = [i for i, t in enumerate(t_ms) if lo_off <= (t - rep_start) <= hi_off]
    if len(idx) < 2:
        return None
    pos_decels = []
    for k in range(len(idx) - 1):
        a, b = idx[k], idx[k + 1]
        dt_s = (t_ms[b] - t_ms[a]) / 1000.0
        if dt_s <= 0:
            continue
        d = -(speed_mps[b] - speed_mps[a]) / dt_s
        if d > 0:
            pos_decels.append(d)
    return (sum(pos_decels) / len(pos_decels)) if pos_decels else None


def _empty_metrics(samples_count: int = 0) -> dict:
    return {
        "samples_count": samples_count,
        "duration": 0.0,
        "peak_speed_mps": None,
        "avg_speed_mps": None,
        "distance": None,
        "time_to_peak_speed_ms": None,
        "splits_ms": {d: None for d in SPLIT_DISTANCES_M},
        "step_count": 0,
        "step_rate_hz": None,
        "avg_step_length_m": None,
        "peak_decel_mps2": None,
        "avg_decel_mps2": None,
        "peak_torque_pct": None,
        "avg_torque_pct": None,
        "phase_windows": {"accel": None, "maxv": None, "decel": None},
        "calibration": {
            "encoder_counts_per_rev": ENCODER_COUNTS_PER_REV,
            "cable_metres_per_rev": CABLE_METRES_PER_REV,
            "counts_per_metre": round(COUNTS_PER_METRE, 3),
            "calibrated": False,
        },
    }


# ----- phase tagging -----

def _compute_phases(t_ms: list[int], abs_speed_mps: list[float], peak_v: float) -> dict:
    """Returns offsets *relative to rep start* (first t_ms entry)."""
    if not t_ms:
        return {"accel": None, "maxv": None, "decel": None}

    rep_start = t_ms[0]
    rep_end_off = t_ms[-1] - rep_start

    accel_threshold = peak_v * ACCEL_END_FRAC
    accel_end_idx = next(
        (i for i, v in enumerate(abs_speed_mps) if v >= accel_threshold), None
    )

    if accel_end_idx is None:
        # never hit 90% peak — entire rep is accel
        return {
            "accel": [0, rep_end_off],
            "maxv": None,
            "decel": None,
        }

    accel_end_off = t_ms[accel_end_idx] - rep_start

    # max-V ends when speed first drops below 95% of peak after accel_end
    maxv_threshold = peak_v * MAXV_END_FRAC
    maxv_end_idx = next(
        (
            i
            for i in range(accel_end_idx + 1, len(abs_speed_mps))
            if abs_speed_mps[i] < maxv_threshold
        ),
        None,
    )

    if maxv_end_idx is None:
        # never dropped — accel + sustained maxv to end
        return {
            "accel": [0, accel_end_off],
            "maxv": [accel_end_off, rep_end_off],
            "decel": None,
        }

    maxv_end_off = t_ms[maxv_end_idx] - rep_start
    return {
        "accel": [0, accel_end_off],
        "maxv": [accel_end_off, maxv_end_off],
        "decel": [maxv_end_off, rep_end_off],
    }


# ----- step detection -----

def _detect_steps(
    t_ms: list[int],
    abs_speed_mps: list[float],
    phase_windows: dict,
    peak_v: float,
) -> list[int]:
    """Return indices of detected step events.

    Method: in the accel+maxv window, take velocity, subtract a 5-sample
    moving-average trend to isolate the step-frequency oscillation, and
    pick local maxima above STEP_MIN_PROMINENCE_FRAC of peak velocity,
    enforcing STEP_MIN_GAP_MS between adjacent peaks.

    NOTE: 10 Hz sampling is at the Nyquist limit for typical 4-5 Hz step
    rates.  Detection will be approximate until the sample rate is raised.
    Returns empty list if the running window is too short.
    """
    if not phase_windows["accel"]:
        return []
    rep_start = t_ms[0]
    running_start_ms = phase_windows["accel"][0] + rep_start
    running_end_ms = (
        phase_windows["maxv"][1] + rep_start
        if phase_windows["maxv"]
        else phase_windows["accel"][1] + rep_start
    )

    # Indices within the running window
    in_window = [
        i for i, t in enumerate(t_ms) if running_start_ms <= t <= running_end_ms
    ]
    if len(in_window) < 4:
        return []

    win_v = [abs_speed_mps[i] for i in in_window]

    # Moving average to detrend (window size 5 = 500 ms at 10 Hz)
    smooth = _moving_average(win_v, window=5)
    residual = [a - b for a, b in zip(win_v, smooth)]

    min_prom = peak_v * STEP_MIN_PROMINENCE_FRAC
    min_gap_samples = max(1, STEP_MIN_GAP_MS // _typical_dt_ms(t_ms, in_window))

    peak_local = _find_peaks_simple(
        residual, min_distance=min_gap_samples, min_value=min_prom
    )
    return [in_window[i] for i in peak_local]


def _typical_dt_ms(t_ms: list[int], indices: list[int]) -> int:
    """Median-ish dt between consecutive samples in `indices`."""
    if len(indices) < 2:
        return 100
    diffs = [t_ms[indices[i + 1]] - t_ms[indices[i]] for i in range(len(indices) - 1)]
    diffs.sort()
    return max(1, diffs[len(diffs) // 2])


def _moving_average(values: list[float], window: int) -> list[float]:
    """Centred-ish moving average; edges use shorter windows."""
    n = len(values)
    half = window // 2
    out = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _find_peaks_simple(
    values: list[float], min_distance: int = 1, min_value: float = 0.0
) -> list[int]:
    """Local maxima with min_distance between adjacent peaks and minimum value."""
    n = len(values)
    if n < 3:
        return []
    peaks: list[int] = []
    for i in range(1, n - 1):
        if values[i] < min_value:
            continue
        # Local max (strictly greater than neighbour, ties allowed on right)
        if values[i] > values[i - 1] and values[i] >= values[i + 1]:
            if peaks and (i - peaks[-1]) < min_distance:
                # Keep whichever is taller within the min_distance window
                if values[i] > values[peaks[-1]]:
                    peaks[-1] = i
            else:
                peaks.append(i)
    return peaks
