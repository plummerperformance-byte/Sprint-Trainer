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
        d = (target_param if target_param is not None else 0.0) / 100.0
        if d <= 0:
            return _result(None, "velocity-drop target must be > 0", target)
        # PREFERRED basis: the empirical Load-Velocity regression (top speed
        # vs actual cable load across a multi-load sweep). Its slope is a
        # true m/s-per-kg-of-load figure with friction/losses baked in, so
        # the classic vdec solve applies directly:
        #   drop d.v0 = |slope|.L  ->  L = d.v0 / |slope|
        reg = profile.get("lv_regression") or {}
        if profile.get("lv_regression_quality") == "ok":
            v0_lv = reg.get("v0_mps")
            slope_lv = reg.get("slope_mps_per_kg")
            if v0_lv and slope_lv:
                load = d * v0_lv / abs(slope_lv)
                return _result(_clamp(load), None, target, basis_model="lv_regression",
                               v0_mps=v0_lv, slope_mps_per_kg=slope_lv,
                               r2=reg.get("r2"), n=reg.get("n"),
                               velocity_drop_pct=d * 100)
        # FALLBACK: solve off the tether F-V profile (modelled, friction
        # ignored) when no measured multi-load sweep exists yet.
        if profile.get("lv_profile_quality") != "ok":
            return _result(None, "needs a multi-load sweep (or ≥3 valid F-V reps across ≥2 loads)",
                           target, quality=profile.get("lv_profile_quality"),
                           lv_regression_quality=profile.get("lv_regression_quality"))
        lv = profile.get("lv_profile") or {}
        v0 = lv.get("v0_mps")
        slope = lv.get("fv_slope_per_kg")
        f0_rel = lv.get("f0_rel_nkg")
        bm = (profile.get("athlete") or {}).get("body_mass_kg")
        if not v0 or not slope:
            return _result(None, "F-V profile incomplete", target)
        if not bm:
            return _result(None, "athlete body mass not set", target)
        # fv_slope_per_kg is the F-V PROFILE slope (-F0_rel/V0, N.s/m per kg
        # of BODY mass), not a velocity-per-kg-of-load slope. On the linear
        # F-V relation v = V0.(1 - F_rel/F0_rel), a fractional velocity drop
        # d needs sustained horizontal force F_rel = d.F0_rel (N/kg), i.e. a
        # cable load of  L = d . F0_rel . bm / g  kg. Friction and belt
        # losses are ignored, so treat as a starting estimate.
        if not f0_rel:
            f0_rel = abs(slope) * v0  # F0_rel = |slope|.V0 by definition
        load = d * f0_rel * bm / 9.81
        return _result(_clamp(load), None, target, basis_model="fv_profile",
                       v0_mps=v0, f0_rel_nkg=round(f0_rel, 2), body_mass_kg=bm,
                       fv_slope_per_kg=slope, velocity_drop_pct=d * 100)

    return _result(None, f"unknown target {target!r}", target)
