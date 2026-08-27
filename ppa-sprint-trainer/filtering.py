"""filtering.py — 1080-style Butterworth smoothing for sprint cable signals.

Pure-Python (stdlib ``math`` only), to match analytics.py's no-external-deps
rule — no numpy, no scipy. Reproduces 1080 Motion's documented signal
processing (docs.1080motion.com/docs/measurements-and-data/sample-filtering):

  * SMOOTH filter — 4th-order Butterworth low-pass, ~1.3 Hz — removes the
    per-step oscillation *and* cable line-sway, leaving the overall run
    trend. 1080's "Top Speed" is the highest peak of THIS curve.
  * STEP filter — 4th-order Butterworth low-pass, ~6 Hz — keeps step-level
    detail but removes line-sway noise. Feeds step analysis and de-noised
    instantaneous peaks (force/power/accel), instead of raw max() which
    grabs a single noise spike.

Both are applied ZERO-PHASE (forward + backward, like scipy.filtfilt) so a
peak's *timing* is not shifted — essential when the peak's location gives
time/distance-to-max-velocity.

Sample-rate aware: a cutoff too close to Nyquist is refused (returns the
input unchanged with ``applied=False``). This is why the 6 Hz step filter
auto-disables on the 10 Hz live Modbus stream (Nyquist 5 Hz) but runs fine
on a ~1 kHz xlsx export.

Design: cascade of two RBJ biquad low-pass sections carrying the canonical
4th-order Butterworth section-Q values (0.5412, 1.3066), giving a maximally
flat passband. Behaviour is locked by ``test_filtering.py`` (stdlib only) and
was cross-checked against ``scipy.signal.sosfiltfilt`` during development —
scipy is NOT a runtime dependency.
"""
from __future__ import annotations

import math
from statistics import median
from typing import List, Sequence, Tuple

SMOOTH_HZ = 1.3   # 1080 "smooth" filter — run trend / top speed
STEP_HZ = 6.0     # 1080 "step" filter — step-level detail
ORDER = 4         # -> two biquad sections

# Canonical 4th-order Butterworth section quality factors.
#   Q_k = 1 / (2 * cos((2k-1)*pi / (2N))),  N=4, k=1,2
_BUTTER4_Q = (0.541196100146197, 1.306562964876377)

# Refuse a cutoff at/above this fraction of Nyquist (fs/2) — filtfilt near
# Nyquist is numerically unreliable and physically meaningless.
_MAX_CUTOFF_FRAC_OF_NYQUIST = 0.9

Biquad = Tuple[float, float, float, float, float]  # (b0, b1, b2, a1, a2), a0==1


# --------------------------------------------------------------------------
# filter design
# --------------------------------------------------------------------------

def _butter_lowpass_sos(cutoff_hz: float, fs: float, order: int = ORDER) -> List[Biquad]:
    """4th-order Butterworth low-pass as a cascade of RBJ biquad sections."""
    if order != 4:
        raise ValueError("only 4th-order is implemented")
    w0 = 2.0 * math.pi * (cutoff_hz / fs)
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    sections: List[Biquad] = []
    for q in _BUTTER4_Q:
        alpha = sin_w0 / (2.0 * q)
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = (1.0 - cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        sections.append((b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0))
    return sections


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------

def _sos_forward(sections: Sequence[Biquad], x: Sequence[float]) -> List[float]:
    """Run x through the biquad cascade once (Direct Form II transposed).

    Each section is initialised to the STEADY STATE for its first input sample
    (equivalent to scipy's sosfilt_zi * x[0]) so there is no startup step
    transient — essential for low-cutoff filters whose impulse response is far
    longer than any practical amount of edge padding.
    """
    y = list(x)
    for (b0, b1, b2, a1, a2) in sections:
        x0 = y[0]
        dc_gain = (b0 + b1 + b2) / (1.0 + a1 + a2)  # ==1 for a unity-DC low-pass
        y_ss = dc_gain * x0
        z2 = b2 * x0 - a2 * y_ss
        z1 = b1 * x0 - a1 * y_ss + z2
        out = [0.0] * len(y)
        for i, xn in enumerate(y):
            yn = b0 * xn + z1
            z1 = b1 * xn - a1 * yn + z2
            z2 = b2 * xn - a2 * yn
            out[i] = yn
        y = out
    return y


def _odd_extension(x: Sequence[float], n: int) -> List[float]:
    """Odd (point-symmetric) reflection of length n at each end — matches
    scipy.filtfilt's default padtype='odd', which suppresses edge transients."""
    left = [2.0 * x[0] - x[i] for i in range(n, 0, -1)]
    right = [2.0 * x[-1] - x[-2 - i] for i in range(n)]
    return left + list(x) + right


def _filtfilt(sections: Sequence[Biquad], x: Sequence[float], pad: int) -> List[float]:
    """Zero-phase filtering: odd-pad, forward, reverse, forward, reverse, un-pad.

    `pad` must be long enough for the filter to settle within the padded
    region (~a few multiples of fs/cutoff) or the two passes see asymmetric
    edge conditions and a symmetric input comes out shifted.
    """
    n = len(x)
    pad = min(n - 1, pad)
    ext = _odd_extension(x, pad)
    fwd = _sos_forward(sections, ext)
    bwd = _sos_forward(sections, fwd[::-1])[::-1]
    return bwd[pad:pad + n]


def butter_lowpass(
    x: Sequence[float], cutoff_hz: float, fs: float, order: int = ORDER
) -> Tuple[List[float], bool]:
    """Zero-phase Butterworth low-pass.

    Returns ``(filtered, applied)``. If the signal is too short or the cutoff
    is too close to Nyquist, returns ``(list(x), False)`` unchanged rather
    than emitting garbage.
    """
    n = len(x)
    if n < 9 or fs <= 0:
        return list(x), False
    if cutoff_hz >= _MAX_CUTOFF_FRAC_OF_NYQUIST * (fs / 2.0):
        return list(x), False
    sections = _butter_lowpass_sos(cutoff_hz, fs, order)
    # Pad long enough for the filter to settle in the padded region (~3 time
    # constants ≈ 3·fs/cutoff); short pads leave a symmetric input shifted.
    pad = max(3 * (2 * len(sections)), int(3.0 * fs / cutoff_hz))
    return _filtfilt(sections, x, pad), True


# --------------------------------------------------------------------------
# convenience for the sprint pipeline
# --------------------------------------------------------------------------

def estimate_fs(times_s: Sequence[float]) -> float:
    """Sampling rate in Hz from timestamps (robust to jitter via the median)."""
    if len(times_s) < 2:
        return 0.0
    dts = [t2 - t1 for t1, t2 in zip(times_s, times_s[1:]) if t2 > t1]
    if not dts:
        return 0.0
    return 1.0 / median(dts)


def derivative(times_s: Sequence[float], y: Sequence[float]) -> List[float]:
    """Central-difference derivative dy/dt, forward/backward at the ends."""
    n = len(y)
    if n < 2:
        return [0.0] * n
    out = [0.0] * n
    for i in range(n):
        if i == 0:
            dt = times_s[1] - times_s[0]
            out[i] = (y[1] - y[0]) / dt if dt > 0 else 0.0
        elif i == n - 1:
            dt = times_s[-1] - times_s[-2]
            out[i] = (y[-1] - y[-2]) / dt if dt > 0 else 0.0
        else:
            dt = times_s[i + 1] - times_s[i - 1]
            out[i] = (y[i + 1] - y[i - 1]) / dt if dt > 0 else 0.0
    return out


def sprint_filters(
    times_s: Sequence[float], speeds_mps: Sequence[float]
) -> dict:
    """Apply both 1080 filters to a speed signal and report what was applied.

    Returns a dict with the smoothed and step-filtered speed curves plus a
    metadata block describing the sampling rate, cutoffs and which filters
    were actually applied (the step filter is skipped below ~13 Hz sampling).
    """
    fs = estimate_fs(times_s)
    smooth, smooth_ok = butter_lowpass(speeds_mps, SMOOTH_HZ, fs)
    step, step_ok = butter_lowpass(speeds_mps, STEP_HZ, fs)
    return {
        "smooth_speed": smooth,
        "step_speed": step,
        "meta": {
            "sample_rate_hz": round(fs, 1),
            "smooth_cutoff_hz": SMOOTH_HZ,
            "step_cutoff_hz": STEP_HZ,
            "order": ORDER,
            "zero_phase": True,
            "smooth_applied": smooth_ok,
            "step_applied": step_ok,
            "note": (
                "" if step_ok
                else f"step filter skipped: fs={fs:.0f} Hz too low for {STEP_HZ} Hz cutoff"
            ),
        },
    }


if __name__ == "__main__":  # quick stdlib smoke test (test_filtering.py has the assertions)
    import random
    random.seed(1)
    fs = 1000.0
    t = [i / fs for i in range(2000)]
    # 0.5 Hz trend + 4 Hz "steps" + 40 Hz "line sway" + noise
    sig = [
        math.sin(2 * math.pi * 0.5 * ti)
        + 0.4 * math.sin(2 * math.pi * 4 * ti)
        + 0.2 * math.sin(2 * math.pi * 40 * ti)
        + 0.05 * random.gauss(0, 1)
        for ti in t
    ]
    out = sprint_filters(t, sig)
    raw_pp = max(sig) - min(sig)
    sm_pp = max(out["smooth_speed"]) - min(out["smooth_speed"])
    st_pp = max(out["step_speed"]) - min(out["step_speed"])
    print("meta:", out["meta"])
    print(f"peak-to-peak  raw={raw_pp:.3f}  step(6Hz)={st_pp:.3f}  smooth(1.3Hz)={sm_pp:.3f}")
    print("expect: smooth ~= 2.0 (trend only), step keeps the 4 Hz, both < raw")
