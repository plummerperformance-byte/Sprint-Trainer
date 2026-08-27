"""rep_analysis.py — one call that turns a rig trace into a coach-ready rep.

This is the integration layer the coach view calls. It composes the pieces
built/validated this session:
  - direct kinematics (splits, Vmax, accel/decel) from the trace
  - validated step mechanics (reuses load_1080_xlsx step detector)
  - a gated force-velocity profile (sprint_model.profile_from_trace)
  - norm context (reference_norms: sport F-V bands, drill percentiles)

Everything the cable can honestly give — and nothing it can't (no contact time;
Rokke 2018). F-V numbers arrive already validity-gated, so the UI shows a value
with a percentile, or a rejection flag, never a wrong number.

    from rep_analysis import analyse_rep
    rep = analyse_rep(t, pos, vel, force, bodymass=82, sport="soccer",
                      drill="Running", resisted=True)
"""
from __future__ import annotations

import math
from typing import Sequence, Optional

import sprint_model

try:
    import reference_norms as rn
except Exception:
    rn = None

try:
    from load_1080_xlsx import detect_steps_speed_residual, compute_step_aggregates
    _HAVE_STEPS = True
except Exception:
    _HAVE_STEPS = False

SPLIT_DISTANCES = (5, 10, 15, 20, 30, 40)


def _abs(seq):
    return [abs(float(x)) for x in seq]


def _splits(t, dist):
    """Interpolated time to reach each split distance."""
    out = {}
    n = len(dist)
    for D in SPLIT_DISTANCES:
        if dist[-1] < D:
            continue
        for i in range(1, n):
            if dist[i] >= D:
                span = (dist[i] - dist[i - 1]) or 1e-9
                out[D] = round(t[i - 1] + (t[i] - t[i - 1]) * (D - dist[i - 1]) / span, 3)
                break
    return out


def _decel_kpis(t, vel, dist, vmax_i):
    """Deceleration KPIs after peak velocity (the accelDecelStats block)."""
    if vmax_i >= len(vel) - 3:
        return {}
    vmax = vel[vmax_i]
    # max deceleration (most negative dv/dt) after peak
    max_dec = 0.0
    for i in range(vmax_i + 1, len(vel)):
        dt = t[i] - t[i - 1]
        if dt <= 0:
            continue
        dv = (vel[i] - vel[i - 1]) / dt
        if dv < max_dec:
            max_dec = dv
    # distance/time from peak to ~stop (< 25% vmax)
    stop_i = None
    for i in range(vmax_i + 1, len(vel)):
        if vel[i] < 0.25 * vmax:
            stop_i = i
            break
    kpi = {"max_decel_ms2": round(-max_dec, 2)}
    if stop_i:
        kpi["decel_dist_m"] = round(dist[stop_i] - dist[vmax_i], 2)
        kpi["decel_time_s"] = round(t[stop_i] - t[vmax_i], 3)
    return kpi


def analyse_rep(t, pos, vel, force=None, bodymass: float = 75.0,
                sport: Optional[str] = None, sex: Optional[str] = None,
                drill: str = "Running", resisted: bool = True) -> dict:
    """Full coach-ready analysis of one rep's trace. Returns a nested dict."""
    t = [float(x) for x in t]
    dist = _abs(pos)
    d0 = dist[0]
    dist = [d - d0 for d in dist]
    vel = _abs(vel)
    n = len(t)
    if n < 5:
        return {"ok": False, "reason": "too few samples"}

    vmax = max(vel)
    vmax_i = vel.index(vmax)
    fs = (n - 1) / (t[-1] - t[0]) if t[-1] > t[0] else 0.0

    out = {
        "ok": True,
        "meta": {"n": n, "sample_hz": round(fs), "duration_s": round(t[-1] - t[0], 3),
                 "distance_m": round(dist[-1], 2), "resisted": resisted, "drill": drill},
        "kinematics": {
            "vmax_ms": round(vmax, 3),
            "vmax_kmh": round(vmax * 3.6, 2),
            "vmax_at_m": round(dist[vmax_i], 2),
            "vmax_at_s": round(t[vmax_i], 3),
            "splits_s": _splits(t, dist),
        },
        "decel": _decel_kpis(t, vel, dist, vmax_i),
    }

    # ---- step mechanics (cable-safe: length + frequency, NOT contact time) ----
    if _HAVE_STEPS:
        try:
            steps = detect_steps_speed_residual(t, vel, dist)
            out["steps"] = compute_step_aggregates(steps)
        except Exception as e:
            out["steps"] = {"error": str(e)}

    # ---- gated force-velocity profile (accel phase only) ----
    acc = slice(0, vmax_i + 1)
    prof = sprint_model.profile_from_trace(t[acc], dist[acc], vel[acc],
                                           bodymass=bodymass, resisted=resisted)
    if prof.get("ok"):
        fvp, m = prof["fvp"], prof["model"]
        out["fv"] = {
            "valid": prof["valid"],
            "rejected": prof["rejected"],
            "r2": round(m["r2"], 3),
            "f0_rel_nkg": round(fvp["F0_rel"], 2),
            "v0_ms": round(fvp["V0"], 2),
            "pmax_rel_wkg": round(fvp["Pmax_rel"], 2),
            "rfmax_pct": round(fvp["RFmax_pct"], 1) if fvp["RFmax_pct"] else None,
            "fv_slope": round(fvp["FV_slope"], 3) if fvp["FV_slope"] else None,
            "tau_s": round(m["TAU"], 3),
        }
    else:
        out["fv"] = {"valid": False, "rejected": ["fit_failed"], "reason": prof.get("reason")}

    # ---- norm context ----
    if rn is not None:
        bands = {}
        # top speed vs drill cohort (1080 pull)
        ts = rn.band_for(drill, "top_speed_ms", vmax)
        if ts:
            bands["top_speed"] = {"band": ts["band"], "percentile": ts["percentile"], "vs": "drill"}
        # F-V vs sport (Haugen) — ONLY for unresisted reps. Haugen norms are
        # free-sprint; a resisted rep's V0/F0 read artificially low against them
        # (apples-to-oranges), so we withhold the band rather than mislead.
        if sport and out["fv"].get("valid") and not resisted:
            for metric, key in (("v0_ms", "v0_ms"), ("f0_nkg", "f0_rel_nkg"),
                                ("pmax_wkg", "pmax_rel_wkg")):
                val = out["fv"].get(key)
                b = rn.fv_band(sport, metric, val, sex=sex) if val is not None else None
                if b:
                    bands[metric] = {"band": b["band"], "percentile": b["percentile"],
                                     "vs": b["sport"]}
        elif sport and out["fv"].get("valid") and resisted:
            bands["_fv_note"] = "resisted rep — F-V not comparable to free-sprint sport norms"
        out["norms"] = bands

    return out


def to_db_row(rep: dict) -> dict:
    """Flatten analyse_rep output onto the reps-table columns the app persists."""
    fv = rep.get("fv", {})
    k = rep.get("kinematics", {})
    st = rep.get("steps", {})
    return {
        "top_speed": k.get("vmax_ms"),
        "f0_rel_nkg": fv.get("f0_rel_nkg") if fv.get("valid") else None,
        "v0_mps": fv.get("v0_ms") if fv.get("valid") else None,
        "pmax_rel_wkg": fv.get("pmax_rel_wkg") if fv.get("valid") else None,
        "valid": 1 if fv.get("valid") else 0,
        "step_freq_hz": st.get("step_freq_hz"),
        "avg_step_length_m": st.get("avg_step_length_m"),
    }


if __name__ == "__main__":
    import csv
    fn = ("C:/Users/AdamP/1080motion/export/Eric_Hsu_3e017d30/"
          "traces/0036_Running_Unknown.csv")
    t = []; pos = []; vel = []
    with open(fn) as f:
        for r in csv.DictReader(f):
            t.append(float(r["time_s"])); pos.append(float(r["position_m"]))
            vel.append(float(r["velocity_ms"]))
    rep = analyse_rep(t, pos, vel, bodymass=82, sport="soccer", drill="Running")
    import json
    print(json.dumps(rep, indent=2))
    print("\nDB row:", to_db_row(rep))
