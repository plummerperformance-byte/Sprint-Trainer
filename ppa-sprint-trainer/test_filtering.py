"""Stdlib-only behaviour lock for filtering.py — run: ``python test_filtering.py``.

No pytest, no numpy, no scipy (matches the app's zero-dep rule). Each check
asserts a property the sprint pipeline relies on; a failure prints and exits 1.
"""
import math

import filtering as F

FS = 1000.0


def _sine(freq, n=1000, fs=FS):
    return [math.sin(2 * math.pi * freq * i / fs) for i in range(n)]


def test_estimate_fs():
    t = [i / FS for i in range(500)]
    assert abs(F.estimate_fs(t) - 1000.0) < 1e-6, F.estimate_fs(t)


def test_low_rate_gate():
    # On a 10 Hz live stream the 6 Hz step filter must refuse; 1.3 Hz smooth runs.
    x = _sine(0.5, n=200, fs=10.0)
    _, ok6 = F.butter_lowpass(x, 6.0, 10.0)
    _, ok13 = F.butter_lowpass(x, 1.3, 10.0)
    assert ok6 is False and ok13 is True, (ok6, ok13)


def test_short_signal_safe():
    for n in (0, 1, 3, 8):
        y, ok = F.butter_lowpass(_sine(2, n=n), 6.0, FS)
        assert ok is False and len(y) == n, (n, ok, len(y))


def test_dc_preserved():
    y, ok = F.butter_lowpass([5.0] * 500, 1.3, FS)
    assert ok and all(abs(v - 5.0) < 1e-6 for v in y), (min(y), max(y))


def test_passband_stopband():
    # 6 Hz filter: pass 2 Hz almost untouched, crush 40 Hz. filtfilt squares the
    # magnitude, so gain at the cutoff is ~0.5 (not 0.707) — assert the band edges.
    def amp(freq):
        y, _ = F.butter_lowpass(_sine(freq), 6.0, FS)
        mid = y[200:800]
        return (max(mid) - min(mid)) / 2

    assert amp(2) > 0.95, amp(2)
    assert amp(40) < 0.02, amp(40)


def test_zero_phase_symmetry():
    # A symmetric bump must come out symmetric (zero phase) at both cutoffs.
    n, c = 1001, 500
    bump = [math.exp(-((i - c) ** 2) / (2 * 80 ** 2)) for i in range(n)]
    for fc in (1.3, 6.0):
        y, _ = F.butter_lowpass(bump, fc, FS)
        peak = max(range(n), key=lambda i: y[i])
        assert abs(peak - c) <= 2, (fc, peak)


def test_despikes_peak():
    # A lone spike must not survive as the max of the filtered signal.
    v = [8.0] * 1500                         # flat plateau
    v[700] += 5.0                            # single noise spike -> raw max 13.0
    y, ok = F.butter_lowpass(v, 6.0, FS)
    assert ok and max(v) >= 13.0 and max(y) < 9.0, (max(y), max(v))


def test_sprint_filters_meta():
    t = [i / FS for i in range(2000)]
    v = [9 * (1 - math.exp(-ti / 0.8)) for ti in t]
    out = F.sprint_filters(t, v)
    m = out["meta"]
    assert m["smooth_applied"] and m["step_applied"]
    assert m["sample_rate_hz"] == 1000.0 and m["order"] == 4
    assert len(out["smooth_speed"]) == len(v) == len(out["step_speed"])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
