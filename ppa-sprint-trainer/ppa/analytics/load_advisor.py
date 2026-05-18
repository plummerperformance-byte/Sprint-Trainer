"""Suggest a working resistance (kg) for an athlete's next drill.

Pure functions — no DB, no FastAPI. Fed the derived athlete profile from
:func:`persistence.athlete_profile` and a target strategy; returns a suggested
load. The ``vdec`` (velocity-decrement) target is the basis of auto-loading:
it solves the athlete's Load-Velocity profile for the load that produces a
chosen velocity drop.

Australian English throughout.
"""
from __future__ import annotations

#: Rig usable cable-load range (kg). Upper bound from the 300% torque cap
#: (CLAUDE.md empirical calibration: 300 / 5.64 %/kg ≈ 53 kg); lower bound
#: matches the coach UI's #cfg-resist minimum.
MIN_LOAD_KG = 0.5
MAX_LOAD_KG = 53.0


def _clamp(load: float) -> float:
    return round(max(MIN_LOAD_KG, min(MAX_LOAD_KG, load)), 1)


def _result(suggestion, reason, target, **basis):
    return {"suggestion": suggestion, "reason": reason,
            "target": target, "basis": basis}


def suggest_load(profile: dict, target: str,
                 target_param: float | None = None,
                 drill: str | None = None) -> dict:
    """Suggest a working resistance (kg).

    ``target`` is one of:
      - ``"off"``            — no suggestion.
      - ``"previous"``       — the athlete's last working load for ``drill``.
      - ``"bodyweight_pct"`` — ``target_param`` % of body mass.
      - ``"vdec"``           — load for a ``target_param`` % velocity drop,
                               solved off the stored L-V profile.

    Always returns ``{suggestion, reason, target, basis}`` — ``suggestion`` is
    ``None`` when no load can be advised, with ``reason`` explaining why.
    """
    if target == "off":
        return _result(None, None, target)

    if target == "previous":
        loads = profile.get("recent_loads") or {}
        load = loads.get(drill)
        if load is None:
            return _result(None, "no prior load logged for this drill", target)
        return _result(_clamp(load), None, target, prior_load_kg=load)

    if target == "bodyweight_pct":
        bm = (profile.get("athlete") or {}).get("body_mass_kg")
        if not bm:
            return _result(None, "athlete body mass not set", target)
        pct = target_param if target_param is not None else 0.0
        if pct <= 0:
            return _result(None, "body-weight % must be > 0", target)
        return _result(_clamp(bm * pct / 100.0), None, target,
                       body_mass_kg=bm, pct=pct)

    if target == "vdec":
        if profile.get("lv_profile_quality") != "ok":
            return _result(None, "needs ≥3 valid reps across ≥2 loads",
                           target, quality=profile.get("lv_profile_quality"))
        lv = profile.get("lv_profile") or {}
        v0 = lv.get("v0_mps")
        slope = lv.get("fv_slope_per_kg")
        if not v0 or not slope:
            return _result(None, "L-V profile incomplete", target)
        d = (target_param if target_param is not None else 0.0) / 100.0
        if d <= 0:
            return _result(None, "velocity-drop target must be > 0", target)
        # fv_slope_per_kg is the velocity loss (m/s) per kg of added load;
        # solve load = (desired absolute drop) / |slope|.
        load = d * v0 / abs(slope)
        return _result(_clamp(load), None, target, v0_mps=v0,
                       fv_slope_per_kg=slope, velocity_drop_pct=d * 100)

    return _result(None, f"unknown target {target!r}", target)
