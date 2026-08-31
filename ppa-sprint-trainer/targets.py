"""targets.py — the target loop: set a goal, grade every rep, score the session.

The autoregulation layer 1080's app threads through everything and we lacked:
each athlete/exercise carries a target; every rep is graded reached / near /
missed against it; a session rolls up to a success rate. Targets can be set by
hand or auto-suggested from the athlete's own recent bests, so the bar rises as
they improve.

Metric directions and the "real change" band reuse the same MDC reliability
source as sprint_trends, so a target set one MDC above the current best is, by
construction, a genuine-improvement target rather than noise.

    from targets import suggest_target, grade_rep, score_session
    tgt = suggest_target(history_values, "top_speed")     # e.g. 7.10
    g   = grade_rep(6.98, tgt, "top_speed")               # {"status": "near", ...}
    s   = score_session(rep_values, tgt, "top_speed")     # met/near/missed + rate
"""
from __future__ import annotations

from typing import Sequence, Optional

try:
    from sprint_trends import METRICS as _TREND_METRICS  # (field, mdc_pct, hib)
except Exception:
    _TREND_METRICS = {}

# metric -> higher_is_better; falls back to trend table, else assume True
_HIB = {m: v[2] for m, v in _TREND_METRICS.items()}
_MDC = {m: v[1] for m, v in _TREND_METRICS.items()}

# a rep within this fraction of the target (below, for higher-is-better) is "near".
# 5% matches the 1080 Control-App target grading: green = beat, yellow = within
# 5% below, red = more than 5% below.
NEAR_BAND_PCT = 5.0


def _hib(metric: str) -> bool:
    return _HIB.get(metric, True)


def suggest_target(history: Sequence[float], metric: str,
                   method: str = "stretch", window: int = 3) -> Optional[float]:
    """Auto-suggest a target from an athlete's past values for this metric.

    method:
      'best'    -> match the recent best (window sessions)
      'stretch' -> recent best plus one MDC (a target you must genuinely improve
                   to hit); default, this is the autoregulation sweet spot
      'median'  -> hold the recent median (maintenance)
    """
    vals = [float(v) for v in history if v is not None]
    if not vals:
        return None
    hib = _hib(metric)
    recent = vals[-window:] if len(vals) >= window else vals
    best = max(recent) if hib else min(recent)
    if method == "best":
        return round(best, 3)
    if method == "median":
        s = sorted(recent)
        return round(s[len(s) // 2], 3)
    # stretch: best +/- one MDC
    band = abs(best) * _MDC.get(metric, 2.0) / 100.0
    return round(best + band if hib else best - band, 3)


def grade_rep(value: float, target: float, metric: str,
              near_band_pct: float = NEAR_BAND_PCT) -> dict:
    """Grade one rep against a target -> reached / near / missed."""
    if value is None or target is None:
        return {"status": "none", "value": value, "target": target}
    hib = _hib(metric)
    reached = value >= target if hib else value <= target
    band = abs(target) * near_band_pct / 100.0
    near = (not reached) and (abs(value - target) <= band)
    status = "reached" if reached else ("near" if near else "missed")
    # signed gap to target, positive = better than target
    gap = (value - target) if hib else (target - value)
    return {"status": status, "value": round(value, 3), "target": round(target, 3),
            "gap": round(gap, 3), "pct_of_target": round(100.0 * value / target, 1) if target else None}


def score_session(values: Sequence[float], target: float, metric: str) -> dict:
    """Roll a session's reps up into met/near/missed counts + success rate."""
    vals = [float(v) for v in values if v is not None]
    grades = [grade_rep(v, target, metric) for v in vals]
    met = sum(1 for g in grades if g["status"] == "reached")
    near = sum(1 for g in grades if g["status"] == "near")
    missed = sum(1 for g in grades if g["status"] == "missed")
    n = len(vals)
    hib = _hib(metric)
    best = (max(vals) if hib else min(vals)) if vals else None
    return {
        "target": round(target, 3) if target is not None else None,
        "metric": metric, "reps": n,
        "met": met, "near": near, "missed": missed,
        "success_rate": round(100.0 * met / n) if n else 0,
        "best": round(best, 3) if best is not None else None,
        "best_hit": (best is not None and (best >= target if hib else best <= target)),
        "grades": grades,
    }


if __name__ == "__main__":
    import json
    # Eric Hsu Running rep top speeds (real, from the 1080 pull)
    history = [6.56, 6.83, 6.95]          # last 3 session bests
    session = [7.24, 6.59, 6.88, 6.95, 6.98, 6.87, 7.46, 6.68, 7.56, 6.7, 6.91]
    tgt = suggest_target(history, "top_speed", method="stretch")
    print(f"suggested target (stretch = best+MDC): {tgt} m/s")
    s = score_session(session, tgt, "top_speed")
    print(f"session: {s['met']} met / {s['near']} near / {s['missed']} missed  "
          f"-> {s['success_rate']}%  (best {s['best']}, hit={s['best_hit']})")
    print("per-rep:", [g["status"][0].upper() for g in s["grades"]])
    print("\nexample grades:")
    for v in (7.56, 6.98, 6.5):
        print(" ", v, "->", json.dumps(grade_rep(v, tgt, "top_speed")))
