"""sprint_trends.py — honest longitudinal progress with an MDC noise band.

Turns an athlete's per-session reps into a per-metric trend where each point
carries a Minimum-Detectable-Change corridor. A change inside the corridor is
noise; outside it is a real improvement/decline. This is the difference between
"you got 0.1 m/s faster" (probably nothing) and "act on it".

MDC percentages come from insights.py (Lahti 2020 / Cross 2017) so the whole app
uses one reliability source.

    from sprint_trends import trend, trend_all
    series = trend(sessions, "top_speed")   # sessions: [{date, reps:[{...}]}]
"""
from __future__ import annotations

from typing import Sequence, Optional

try:
    from insights import MDC as _INS_MDC
except Exception:
    _INS_MDC = {}

# metric key -> (rep field, MDC % , higher_is_better)
# MDC % pulled from insights.py where available, with sensible fallbacks.
METRICS = {
    "top_speed":     ("top_speed",     _INS_MDC.get("split_20m", 2.0), True),
    "v0_ms":         ("v0_ms",         _INS_MDC.get("v0_mps", 1.45),   True),
    "f0_rel_nkg":    ("f0_rel_nkg",    _INS_MDC.get("f0_n", 3.02),     True),
    "pmax_rel_wkg":  ("pmax_rel_wkg",  _INS_MDC.get("pmax_w", 4.0),    True),
    "split_10m":     ("split_10m",     2.0,                            False),
}


def _agg_session(reps, field, higher_is_better, valid_only=True):
    """Best value for a metric within one session (best = max, or min for time).
    Only reps flagged valid are counted (invalid F-V fits excluded)."""
    vals = []
    for r in reps:
        if valid_only and field in ("v0_ms", "f0_rel_nkg", "pmax_rel_wkg") and not r.get("valid", True):
            continue
        v = r.get(field)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    return max(vals) if higher_is_better else min(vals)


def trend(sessions: Sequence[dict], metric: str, valid_only: bool = True) -> list:
    """Per-session best for `metric`, each with an MDC corridor vs the previous
    session and a real-change verdict.

    sessions: [{"date": <str/sortable>, "reps": [rep_dict, ...]}, ...] in order.
    Returns [{date, value, mdc_lo, mdc_hi, delta, real_change, direction}].
    """
    if metric not in METRICS:
        raise KeyError(f"unknown metric {metric!r}; known: {list(METRICS)}")
    field, mdc_pct, hib = METRICS[metric]
    out = []
    prev = None
    for s in sessions:
        val = _agg_session(s.get("reps", []), field, hib, valid_only)
        if val is None:
            continue
        point = {"date": s.get("date"), "value": round(val, 3),
                 "mdc_lo": None, "mdc_hi": None, "delta": None,
                 "real_change": None, "direction": "baseline"}
        if prev is not None:
            band = abs(prev) * mdc_pct / 100.0
            point["mdc_lo"] = round(prev - band, 3)
            point["mdc_hi"] = round(prev + band, 3)
            delta = val - prev
            point["delta"] = round(delta, 3)
            real = abs(delta) > band
            point["real_change"] = real
            if not real:
                point["direction"] = "stable"
            else:
                improved = (delta > 0) if hib else (delta < 0)
                point["direction"] = "improved" if improved else "declined"
        out.append(point)
        prev = val
    return out


def trend_all(sessions: Sequence[dict], valid_only: bool = True) -> dict:
    """All tracked metrics at once -> {metric: series}."""
    return {m: trend(sessions, m, valid_only) for m in METRICS}


def latest_verdict(series: list) -> Optional[dict]:
    """Compact summary of the most recent point for a headline/badge."""
    if not series:
        return None
    p = series[-1]
    return {"value": p["value"], "direction": p["direction"],
            "real_change": p["real_change"], "delta": p["delta"]}


if __name__ == "__main__":
    import json
    # synthetic: 8 sessions, top speed drifting up with noise; one real jump.
    seq = [6.61, 6.55, 6.68, 6.71, 6.66, 6.72, 6.85, 6.98]
    sessions = [{"date": f"S{i+1}", "reps": [{"top_speed": v, "valid": True}]}
                for i, v in enumerate(seq)]
    s = trend(sessions, "top_speed")
    print("TOP SPEED trend (MDC 2%):")
    for p in s:
        tag = p["direction"]
        print(f"  {p['date']}: {p['value']} m/s  Δ{p['delta']}  band[{p['mdc_lo']},{p['mdc_hi']}]  -> {tag}")
    print("\nlatest verdict:", json.dumps(latest_verdict(s)))
    print("\ntrend_all keys:", list(trend_all(sessions)))
