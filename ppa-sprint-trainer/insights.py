"""PPA Insights Engine — sprint-FV interpretation for the coach view.

Each function takes the relevant rep fields (or a full rep dict) and returns
an "insight" record:

    {
        "id":           short snake_case id (matches the rules-engine taxonomy),
        "severity":     "high" | "info" | "ok" | "warn",
        "tag":          short coach-facing classification (e.g. "Power-Deficient"),
        "headline":     one-sentence verdict,
        "detail":       longer explanation interpolating the athlete's numbers,
        "prescribe":    list of 1-3 coach-facing recommendations,
        "do_not":       list of contraindications (optional),
        "evidence":     citation + confidence flag,
    }

`build_report(rep, position_group="back")` runs every applicable Tier-1 module
and returns a structured report ranked by severity. Designed to be JSON-
serialised straight back to the UI.

Sources baked into the rules:
- Lahti et al. 2020 (Individual FV-profile resisted/assisted, rugby cohort):
  rugby-specific slope cutoffs.
- Cross et al. 2017 (Optimal loading for Pmax during sled pulling): Pmax
  bands + Lopt = velocity at half-V0.
- Morin & Samozino 2017 (FV-profile interpretation): RFmax + Drf bands.
- Solberg 2025 meta-analysis: caveat-frame all "fix the profile" language.
"""
from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rugby-specific FV-slope cutoffs (Lahti 2020, n=16 pro rugby cohort).
# Slope is reported in N.s/m/kg — i.e. -F0_per_kg / V0.
RUGBY_SLOPE_FORCE_SATURATED = -0.92
RUGBY_SLOPE_FORCE_LEANING   = -0.86
RUGBY_SLOPE_VELOCITY_SAT    = -0.83

# Pmax bands (W/kg) — Cross 2017 reference values.
PMAX_DEFICIENT_MAX = 14.0
PMAX_SUB_ELITE_MAX = 17.0
PMAX_HIGH_MAX      = 20.0
# > PMAX_HIGH_MAX = elite

# Tau (acceleration time-constant, sec) — relative to typical rugby ranges.
TAU_FAST = 1.0
TAU_SLOW = 1.3

# RFmax / Drf bands (Morin & Samozino 2017).
RFMAX_LOW_PCT      = 50.0
DRF_STEEP_DECAY    = -0.06   # more negative than this = poor effectiveness retention

# Minimum Detectable Change thresholds (Lahti 2020 + Cross 2017).
MDC = {
    "split_5m": 2.0,    # %
    "split_20m": 0.99,
    "v0_mps": 1.45,
    "f0_n": 3.02,
    "fv_slope_per_kg": 5.59,
    "pmax_w": 4.0,
}

# Position-group rugby norms (1080-App schema seed values).
# Values are "max for band" — e.g. v < poor_max → poor; v < fair_max → fair etc.
# "higher_is_better" flips the comparison.
NORMS = {
    "back": {
        "f0_rel_nkg":      {"poor": 4.5,  "fair": 5.5,  "good": 6.5,  "great": 7.5,  "higher_is_better": True},
        "v0_mps":          {"poor": 7.5,  "fair": 8.5,  "good": 9.5,  "great": 10.3, "higher_is_better": True},
        "pmax_rel_wkg":    {"poor": 11.0, "fair": 14.0, "good": 17.0, "great": 20.0, "higher_is_better": True},
        "max_v_ms":        {"poor": 8.5,  "fair": 9.2,  "good": 9.8,  "great": 10.3, "higher_is_better": True},
        "split_10m_s":     {"poor": 2.10, "fair": 2.00, "good": 1.90, "great": 1.82, "higher_is_better": False},
        "split_40m_s":     {"poor": 5.85, "fair": 5.65, "good": 5.45, "great": 5.25, "higher_is_better": False},
    },
    "forward": {
        "f0_rel_nkg":      {"poor": 4.5,  "fair": 5.5,  "good": 6.5,  "great": 7.5,  "higher_is_better": True},
        "v0_mps":          {"poor": 7.0,  "fair": 8.0,  "good": 9.0,  "great": 10.0, "higher_is_better": True},
        "pmax_rel_wkg":    {"poor": 10.0, "fair": 13.0, "good": 16.0, "great": 19.0, "higher_is_better": True},
        "max_v_ms":        {"poor": 7.5,  "fair": 8.3,  "good": 9.0,  "great": 9.5,  "higher_is_better": True},
        "split_10m_s":     {"poor": 2.20, "fair": 2.10, "good": 2.00, "great": 1.95, "higher_is_better": False},
        "split_40m_s":     {"poor": 6.10, "fair": 5.90, "good": 5.70, "great": 5.55, "higher_is_better": False},
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def band_of(value: float, bands: dict) -> str:
    """Return 'poor' | 'fair' | 'good' | 'great' | 'elite' for a value
    against a band dict (the higher_is_better flag flips the direction).
    """
    higher = bands.get("higher_is_better", True)
    if higher:
        if value < bands["poor"]:  return "poor"
        if value < bands["fair"]:  return "fair"
        if value < bands["good"]:  return "good"
        if value < bands["great"]: return "great"
        return "elite"
    else:
        # Lower is better (e.g. split times)
        if value > bands["poor"]:  return "poor"
        if value > bands["fair"]:  return "fair"
        if value > bands["good"]:  return "good"
        if value > bands["great"]: return "great"
        return "elite"


def _evidence(source: str, confidence: str = "moderate") -> str:
    return f"{source} (confidence: {confidence})"


# ---------------------------------------------------------------------------
# Tier 1 — Insight modules
# ---------------------------------------------------------------------------

def fv_orientation(slope_per_kg: Optional[float]) -> Optional[dict]:
    """Tag the rep as Force-saturated / Force-leaning / Balanced /
    Velocity-saturated using Lahti 2020 rugby cutoffs.
    Returns None if slope isn't available."""
    if slope_per_kg is None:
        return None
    s = slope_per_kg
    if s <= RUGBY_SLOPE_FORCE_SATURATED:
        tag = "Force-saturated"
        detail = (f"Slope {s:.2f} N·s/m/kg — already strongly force-oriented. "
                  f"Heavy resisted work likely *will not* yield further gains; "
                  f"the bottleneck has shifted to velocity development.")
        prescribe = ["Bias toward assisted / overspeed work for V0",
                     "Maintain heavy strength as a base, don't add more"]
        do_not = ["Avoid more heavy sled (≥75% BM) — non-responder zone"]
        severity = "info"
    elif s <= RUGBY_SLOPE_FORCE_LEANING:
        tag = "Force-leaning"
        detail = (f"Slope {s:.2f} N·s/m/kg — force-leaning. Responsive to "
                  f"heavy resisted sprint work; well-matched for collision sport demands.")
        prescribe = ["Resisted sprints at individual Lopt for Pmax",
                     "Mixed loading (heavy + light) for breadth"]
        do_not = []
        severity = "info"
    elif s <= RUGBY_SLOPE_VELOCITY_SAT:
        tag = "Balanced"
        detail = (f"Slope {s:.2f} N·s/m/kg — balanced profile. Will respond "
                  f"to either resisted or assisted modality. Orientation isn't "
                  f"the rate-limiter — look at Pmax / mechanical effectiveness flags.")
        prescribe = ["Mixed prescription based on other limiters",
                     "No need to bias toward one end of the curve"]
        do_not = []
        severity = "info"
    else:
        tag = "Velocity-saturated"
        detail = (f"Slope {s:.2f} N·s/m/kg — strongly velocity-oriented. "
                  f"Assisted/overspeed work likely *will not* yield further gains.")
        prescribe = ["Bias toward heavy resisted work for F0",
                     "Lower-body strength block is indicated"]
        do_not = ["Avoid more overspeed/assisted — non-responder zone"]
        severity = "info"
    return {
        "id": "fv_orientation",
        "tag": tag,
        "severity": severity,
        "headline": f"Profile: {tag}",
        "detail": detail,
        "prescribe": prescribe,
        "do_not": do_not,
        "evidence": _evidence("Lahti et al. 2020 rugby cohort", "moderate (n=16, pro rugby)"),
    }


def power_classification(pmax_rel_wkg: Optional[float]) -> Optional[dict]:
    """The single most important flag — catches athletes whose orientation
    looks fine but whose Pmax is just low. Per Cross 2017."""
    if pmax_rel_wkg is None:
        return None
    p = pmax_rel_wkg
    if p < PMAX_DEFICIENT_MAX:
        tag = "Power-Deficient"
        detail = (f"Pmax {p:.1f} W/kg — recreational band. This is the "
                  f"rate-limiter. Building horizontal power is the priority; "
                  f"orientation correction won't move sprint performance "
                  f"meaningfully until this climbs.")
        prescribe = [
            "Heavy lower-body strength (squat / hex bar / hip thrust)",
            "Sled at individual Lopt for Pmax development",
            "Broad force/velocity exposure — don't chase orientation yet",
        ]
        severity = "high"
    elif p < PMAX_SUB_ELITE_MAX:
        tag = "Sub-Elite Power"
        detail = (f"Pmax {p:.1f} W/kg — trained-but-trainable. Continued "
                  f"upside on horizontal power. Mixed loading still indicated.")
        prescribe = ["Continue mixed-loading sprint program",
                     "Resisted at Lopt 1-2× / week for power"]
        severity = "warn"
    elif p < PMAX_HIGH_MAX:
        tag = "High Power"
        detail = (f"Pmax {p:.1f} W/kg — high. Now valuable to bias toward "
                  f"orientation correction or sport-specific patterns.")
        prescribe = ["Orientation-driven prescription is now appropriate",
                     "Specificity over volume"]
        severity = "ok"
    else:
        tag = "Elite Power"
        detail = (f"Pmax {p:.1f} W/kg — elite range. Maintenance and injury "
                  f"management become the priority; further gains are small.")
        prescribe = ["Maintenance sprint dose",
                     "Top-end speed specificity",
                     "Injury-risk monitoring"]
        severity = "ok"
    return {
        "id": "pmax_classification",
        "tag": tag,
        "severity": severity,
        "headline": f"Power: {tag} ({p:.1f} W/kg)",
        "detail": detail,
        "prescribe": prescribe,
        "do_not": [],
        "evidence": _evidence("Cross et al. 2017 (Pmax reference values)", "high"),
    }


def acceleration_vs_maxv(rep: dict, position_group: str = "back") -> Optional[dict]:
    """Compare 10 m split + max-V to position norms; tag as Acceleration-Dominant /
    Max-V-Dominant / Sub-Threshold / Balanced."""
    norms = NORMS.get(position_group)
    if not norms:
        return None
    splits = rep.get("splits_s_extended") or rep.get("splits_s") or {}
    s10 = splits.get("10")
    vmax = rep.get("peak_speed_mps")
    if s10 is None or vmax is None:
        return None
    s10_band = band_of(float(s10), norms["split_10m_s"])
    vmax_band = band_of(float(vmax), norms["max_v_ms"])
    rank_to_int = {"poor": 0, "fair": 1, "good": 2, "great": 3, "elite": 4}
    s10_rank = rank_to_int[s10_band]
    vmax_rank = rank_to_int[vmax_band]
    if s10_rank >= 3 and vmax_rank <= 1:
        tag = "Acceleration-Dominant"
        detail = (f"10 m split {s10:.2f} s ({s10_band}) but Vmax {vmax:.2f} m/s "
                  f"({vmax_band}) — strong starter, weak top-end.")
        prescribe = ["Longer accel runs (30-50 m) to build V0",
                     "Top-speed maintenance work"]
    elif vmax_rank >= 3 and s10_rank <= 1:
        tag = "Max-V-Dominant"
        detail = (f"Vmax {vmax:.2f} m/s ({vmax_band}) but 10 m split {s10:.2f} s "
                  f"({s10_band}) — fast top-end, weak start.")
        prescribe = ["Block starts and short accel work",
                     "Lower-body strength for F0"]
    elif s10_rank <= 1 and vmax_rank <= 1:
        tag = "Sub-Threshold"
        detail = (f"Both 10 m ({s10_band}) and Vmax ({vmax_band}) below norms — "
                  f"general sprint development needed across the curve.")
        prescribe = ["Broad sprint volume + strength foundation",
                     "Don't chase orientation — build base first"]
    else:
        tag = "Balanced"
        detail = (f"10 m split {s10_band}, Vmax {vmax_band} — balanced phase distribution.")
        prescribe = ["Continue mixed-distance sprint program"]
    return {
        "id": "acc_vs_maxv",
        "tag": tag,
        "severity": "high" if tag == "Sub-Threshold" else "info",
        "headline": f"Phases: {tag}",
        "detail": detail,
        "prescribe": prescribe,
        "do_not": [],
        "evidence": _evidence(f"1080-App rugby norms ({position_group})", "moderate"),
    }


def mechanical_effectiveness(rfmax_pct: Optional[float], drf: Optional[float]) -> Optional[dict]:
    """RFmax + Drf interpretation per Morin 2017."""
    if rfmax_pct is None and drf is None:
        return None
    msgs, prescribe = [], []
    severity = "info"
    tag = "Mechanics OK"
    if rfmax_pct is not None and rfmax_pct < RFMAX_LOW_PCT:
        msgs.append(f"RFmax {rfmax_pct:.1f}% — low. Force is going up, not forward.")
        prescribe += ["Shin-angle / body-lean cues",
                      "Heavy sled at low velocity (push mechanics)"]
        severity = "warn"; tag = "Push-off Mechanics"
    if drf is not None and drf < DRF_STEEP_DECAY:
        msgs.append(f"Drf {drf:.3f} — effectiveness drops fast as speed climbs. "
                    f"Likely 'standing up' too early in the run.")
        prescribe += ["Longer accel runs with smooth posture transition",
                      "Wickets / progressive accel drills"]
        severity = "warn"; tag = "Drf decay"
    if not msgs:
        if rfmax_pct is not None and drf is not None:
            msgs.append(f"RFmax {rfmax_pct:.1f}% / Drf {drf:.3f} — mechanics solid. "
                        f"Limiter is physics (Pmax), not technique.")
            tag = "Mechanics Solid"
            severity = "ok"
        else:
            return None
    return {
        "id": "mechanical_effectiveness",
        "tag": tag,
        "severity": severity,
        "headline": tag,
        "detail": " ".join(msgs),
        "prescribe": prescribe,
        "do_not": [],
        "evidence": _evidence("Morin & Samozino 2017 (FV interpretation)", "high"),
    }


def tau_flag(tau_s: Optional[float]) -> Optional[dict]:
    """Acceleration time-constant — within-athlete corroboration metric."""
    if tau_s is None:
        return None
    if tau_s > TAU_SLOW:
        tag = "Slow accel onset"
        detail = (f"Tau {tau_s:.2f} s — slow to reach Vmax. Usually tracks "
                  f"with low F0 / Pmax; expect this to improve as those climb.")
    elif tau_s < TAU_FAST:
        tag = "Fast accel onset"
        detail = (f"Tau {tau_s:.2f} s — quick to top-end speed. Strong "
                  f"acceleration signal.")
    else:
        tag = "Typical accel onset"
        detail = f"Tau {tau_s:.2f} s — within typical range."
    return {
        "id": "tau_flag",
        "tag": tag,
        "severity": "info",
        "headline": tag,
        "detail": detail,
        "prescribe": [],
        "do_not": [],
        "evidence": _evidence("within-athlete tracking only — sensitive to test conditions", "low cross-athlete"),
    }


def lopt_calculator(f0_n: Optional[float], v0_mps: Optional[float],
                    body_mass_kg: Optional[float]) -> Optional[dict]:
    """Compute the individual sled load that maximises Pmax, per Cross 2017.

    Lopt is the resistance corresponding to v_opt = 0.5 × V0 on the F-V
    curve. Linear F-V: F = F0 - (F0/V0) × v. At v=V0/2, F = F0/2. The
    additional kg-equivalent the sled must add to make the athlete pull at
    V0/2 instead of V0 (where they'd pull unloaded) is F0/2 - body_weight.

    We report Lopt as kg of cable/sled load (= F0/2 / 9.81, ignoring rolling
    friction) and as % BM.
    """
    if f0_n is None or v0_mps is None or body_mass_kg is None:
        return None
    half_f0 = f0_n / 2
    lopt_kg = half_f0 / 9.81  # rough — assumes near-frictionless sled / cable
    lopt_pct_bm = round(lopt_kg / body_mass_kg * 100, 1)
    detail = (f"At v_opt = V0/2 = {v0_mps/2:.2f} m/s, force on the cable should "
              f"be ≈ F0/2 = {half_f0:.0f} N (≈ {lopt_kg:.1f} kg). For sled "
              f"work, that's {lopt_pct_bm:.0f}% of body mass. Use this for "
              f"Pmax-targeted resisted sprints.")
    # Cross 2017: typical Lopt 70-96% BM. Flag if outside that range.
    is_unusual = lopt_pct_bm < 50 or lopt_pct_bm > 120
    return {
        "id": "lopt",
        "tag": f"Lopt: {lopt_kg:.0f} kg ({lopt_pct_bm:.0f}% BM)",
        "severity": "warn" if is_unusual else "info",
        "headline": f"Optimal sled load: {lopt_kg:.0f} kg",
        "detail": detail,
        "prescribe": [f"Resisted sprints at ~{lopt_kg:.0f} kg cable load (~{lopt_pct_bm:.0f}% BM)",
                      "4-6 reps × 20 m, 3 min rest, target Pmax"],
        "do_not": [],
        "evidence": _evidence("Cross et al. 2017 (Lopt = velocity at half-V0)", "high"),
    }


def stride_consistency(rep: dict) -> Optional[dict]:
    """Step-to-step stride-length variability across the STEADY-STATE portion
    of the run only (after step 4, before deceleration onset).

    NOT bilateral asymmetry — cable-only data has no L/R foot tagging. This
    is a within-athlete, within-rep coordination/fatigue signal, with a
    much tighter coverage window than the previous "all steps" version per
    PPA KB review (Topic 13).

    Threshold: hide if steady-state CV ≤ 15% (within typical sprint
    biomechanics). Surface as warn at 15-25%, alert above 25%. Asymmetry
    treatment is gated on a foot-strike sensor that we don't have yet.
    """
    events = rep.get("step_events") or []
    if len(events) < 6:
        return None  # need enough strides to have a meaningful steady-state

    # Steady-state window: from stride 5 onwards, ending at decel_start_ms
    # (the point at which we observed >5% velocity drop from peak).
    decel_t_ms = rep.get("decel_start_ms")
    valid = []
    for e in events:
        if e.get("step_number") is None or e.get("step_number") < 5:
            continue
        if e.get("step_length_m") is None:
            continue
        if decel_t_ms is not None and (e.get("t_strike_s") or 0) * 1000 > decel_t_ms:
            continue
        valid.append(e["step_length_m"])
    if len(valid) < 4:
        return None  # not enough steady-state strides

    n = len(valid)
    avg = sum(valid) / n
    if avg <= 0: return None
    var = sum((x - avg) ** 2 for x in valid) / n
    std = var ** 0.5
    cv_pct = std / avg * 100

    if cv_pct < 15:
        return None  # within typical sprint biomechanics — hide

    if cv_pct < 25:
        tag = "Stride consistency: moderate"
        severity = "warn"
        detail = (f"At top speed, stride length varied by ±{std:.2f} m "
                  f"(CV {cv_pct:.1f}% across strides {valid and 5}–{4 + n}). "
                  f"Could reflect fatigue, surface, or a stride-pattern "
                  f"transition mid-run — worth checking on slow-mo video.")
    else:
        tag = "Stride consistency: high"
        severity = "warn"
        detail = (f"At top speed, stride length varied by ±{std:.2f} m "
                  f"(CV {cv_pct:.1f}%) — large spread. Often a fatigue or "
                  f"coordination signal, or the cable's biting into a "
                  f"specific phase of the run. Coach should review video "
                  f"before changing the program.")
    return {
        "id": "stride_consistency",
        "tag": tag,
        "severity": severity,
        "headline": tag,
        "detail": detail,
        "prescribe": [],
        "do_not": [],
        # Per PPA KB review: this is a within-athlete signal only, never a
        # cross-athlete benchmark. Do not auto-prescribe correctives.
        "evidence": _evidence("cable-force peak detection (≈ stride rate; not bilateral asymmetry)", "exploratory"),
    }


# Backwards-compatible alias for any external caller still using the old name
stride_length_consistency = stride_consistency


def responsiveness_predictor(slope_per_kg: Optional[float],
                             planned_modality: Optional[str] = None) -> Optional[dict]:
    """Warn if a planned training modality is contraindicated by the slope."""
    if slope_per_kg is None or not planned_modality:
        return None
    s = slope_per_kg
    plan = planned_modality.lower()
    if plan in ("heavy_resisted", "heavy_sled") and s <= RUGBY_SLOPE_FORCE_SATURATED:
        return {
            "id": "responsiveness",
            "tag": "Predicted non-responder (heavy resisted)",
            "severity": "high",
            "headline": "Predicted non-responder",
            "detail": (f"Slope {s:.2f} is force-saturated. Adding more heavy "
                       f"sled work is unlikely to move the needle. Switch to "
                       f"general strength + velocity-end exposure."),
            "prescribe": ["Replace heavy sled with light sled / overspeed",
                          "Build broad strength foundation"],
            "do_not": ["Don't program more heavy resisted sprints"],
            "evidence": _evidence("Lahti et al. 2020 rugby cutoffs", "moderate"),
        }
    if plan in ("assisted", "overspeed") and s >= RUGBY_SLOPE_VELOCITY_SAT:
        return {
            "id": "responsiveness",
            "tag": "Predicted non-responder (assisted)",
            "severity": "high",
            "headline": "Predicted non-responder",
            "detail": (f"Slope {s:.2f} is velocity-saturated. Adding more "
                       f"overspeed work is unlikely to move V0 further."),
            "prescribe": ["Replace overspeed with sprint volume + strength"],
            "do_not": ["Don't program more overspeed/assisted"],
            "evidence": _evidence("Lahti et al. 2020 rugby cutoffs", "moderate"),
        }
    return None


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

SEVERITY_RANK = {"high": 0, "warn": 1, "info": 2, "ok": 3}

# Category tiebreaker — power/orientation are headline tags; lopt/stride are
# supplementary so they shouldn't outrank the verdict-level insights.
CATEGORY_PRIORITY = {
    "pmax_classification": 0,
    "fv_orientation": 1,
    "responsiveness": 2,
    "acc_vs_maxv": 3,
    "mechanical_effectiveness": 4,
    "tau_flag": 5,
    "lopt": 6,
    "stride_consistency": 7,
}


def _priority(insight: dict) -> tuple:
    return (SEVERITY_RANK.get(insight["severity"], 99),
            CATEGORY_PRIORITY.get(insight["id"], 99))


def build_report(rep: dict, position_group: str = "back",
                 planned_modality: Optional[str] = None) -> dict:
    """Run every applicable Tier-1 module against a rep dict; return a
    structured report. Pmax + FV orientation always front the report
    (the verdict pair); other insights are ranked by severity then category.
    """
    body_mass_kg = (rep.get("_meta") or {}).get("body_mass_kg")

    candidates = [
        power_classification(rep.get("pmax_rel_wkg")),
        fv_orientation(rep.get("fv_slope_per_kg")),
        acceleration_vs_maxv(rep, position_group),
        mechanical_effectiveness(rep.get("rf_max_pct"), rep.get("drf")),
        tau_flag(rep.get("tau_s")),
        lopt_calculator(rep.get("f0_n"), rep.get("v0_mps"), body_mass_kg),
        stride_length_consistency(rep),
        responsiveness_predictor(rep.get("fv_slope_per_kg"), planned_modality),
    ]
    insights = [c for c in candidates if c is not None]
    insights.sort(key=_priority)

    # Primary always includes power + orientation if present (they're the
    # verdict pair), then top 1-2 high/warn extras. Cap at 3-4 total.
    headline_ids = {"pmax_classification", "fv_orientation"}
    primary = [i for i in insights if i["id"] in headline_ids]
    extras = [i for i in insights
              if i["id"] not in headline_ids
              and i["severity"] in ("high", "warn")]
    primary = primary + extras[:max(0, 3 - len(primary))]
    context = [i for i in insights if i not in primary]

    # Headline + primary-limiter heuristic
    pmax = rep.get("pmax_rel_wkg")
    slope = rep.get("fv_slope_per_kg")
    if pmax is not None and pmax < PMAX_DEFICIENT_MAX:
        primary_limiter = "pmax"
    elif slope is not None and slope <= RUGBY_SLOPE_FORCE_SATURATED:
        primary_limiter = "v0"
    elif slope is not None and slope >= RUGBY_SLOPE_VELOCITY_SAT:
        primary_limiter = "f0"
    else:
        primary_limiter = "balanced"

    headline_parts = []
    pmax_ins = next((i for i in insights if i["id"] == "pmax_classification"), None)
    fv_ins   = next((i for i in insights if i["id"] == "fv_orientation"), None)
    if pmax_ins: headline_parts.append(pmax_ins["tag"])
    if fv_ins:   headline_parts.append(fv_ins["tag"])
    headline = " · ".join(headline_parts) if headline_parts else "Insufficient data for full report"

    return {
        "rep_idx": rep.get("rep_idx"),
        "athlete": (rep.get("_meta") or {}).get("athlete_name"),
        "position_group": position_group,
        "headline": headline,
        "primary_limiter": primary_limiter,
        "primary_insights": primary,
        "context_insights": context,
        "framing_caveat": (
            "These insights describe your current mechanical signature. They "
            "are diagnostic, not deterministic — Solberg 2025 meta-analysis "
            "shows FV-optimised training does not consistently outperform "
            "general sprint training. Use these as a guide, not a guarantee."
        ),
    }
