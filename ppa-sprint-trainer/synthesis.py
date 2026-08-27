"""PPA Synthesis Layer.

Sits between insights.py and the renderer. Takes the full list of insight
objects + the rep dict and produces a single layered report:

    {
        "primary_limiter":   "f0" | "v0" | "pmax" | "mechanics" | "balanced",
        "headline":          plain-English ≤7 words ("Fast top-end. Slow start.")
        "verdict_paragraph": 2-3 sentences, narrative.
        "chase_metric":      one number to track this block (current + target + units)
        "prescriptions":     list of 3 prescription IDs from prescriptions.py
        "avoids":            list of 1-2 avoid IDs from prescriptions.py
        "supporting_insights": original insight objects, for the coach expander
    }

The rule table is explicit (no nested ifs) — easy to extend.

Anti-jargon lint: athlete-facing copy is checked against a banned-term list
before the report is returned. If any banned term appears, the build fails
loudly so the offending template gets fixed.
"""
from __future__ import annotations

from typing import Optional

import insights as _ins
import prescriptions as _rx


JARGON = {"Pmax", "Lopt", "F0", "V0", "Drf", "Tau", "RFmax", "FV slope",
          "N·s/m/kg", "N.s/m/kg", "CV ", "MDC", "ES ", "stiffness coefficient"}


def _has_jargon(text: str) -> Optional[str]:
    for term in JARGON:
        if term in text:
            return term
    return None


def _classify_limiter(rep: dict, position_group: str = "back") -> str:
    """Decision rule: which single thing should the athlete fix first?"""
    pmax_kg = rep.get("pmax_rel_wkg")
    slope = rep.get("fv_slope_per_kg")
    f0_kg = rep.get("f0_rel_nkg")
    v0 = rep.get("v0_mps")
    rfmax = rep.get("rf_max_pct")
    drf = rep.get("drf")
    norms = _ins.NORMS.get(position_group, _ins.NORMS["back"])

    # Mechanics red flag overrides physics — fix posture/push-off first
    if (rfmax is not None and rfmax < _ins.RFMAX_LOW_PCT) or \
       (drf is not None and drf < _ins.DRF_STEEP_DECAY):
        return "mechanics"
    # Pmax-deficient overrides orientation per the user's rule
    if pmax_kg is not None and pmax_kg < _ins.PMAX_DEFICIENT_MAX:
        return "pmax"
    # Velocity-saturated AND f0 below position norm → build f0
    if slope is not None and slope >= _ins.RUGBY_SLOPE_VELOCITY_SAT:
        if f0_kg is None or f0_kg < norms["f0_rel_nkg"]["good"]:
            return "f0"
    # Force-saturated AND v0 below position norm → build v0
    if slope is not None and slope <= _ins.RUGBY_SLOPE_FORCE_SATURATED:
        if v0 is None or v0 < norms["v0_mps"]["good"]:
            return "v0"
    return "balanced"


# Rule table — primary limiter → headline / prescriptions / avoid / chase metric.
LIMITER_RULES = {
    "f0": {
        "headline": "Fast top-end. Slow start.",
        "verdict_template": (
            "You hit {peak_v:.2f} m/s at top speed — that's strong for a rugby player. "
            "But your 10 m split is {s10:.2f} s, which is well off where it should be for a {position}. "
            "The gap between your top-end and your start is your biggest opportunity."
        ),
        "prescriptions": ["f0_resisted_sprints", "lower_body_strength", "free_sprint_maintenance"],
        "avoids": ["no_assisted_velocity_saturated"],
        "chase": ("split_10m", "10 m split", "s", lambda v: v - 0.22),  # target -0.22s
    },
    "v0": {
        "headline": "Strong start. Limited top-end.",
        "verdict_template": (
            "Your start is solid — 10 m split of {s10:.2f} s is good for a {position}. "
            "But your top-end speed of {peak_v:.2f} m/s leaves room. "
            "The opportunity is sharpening the second half of the run."
        ),
        "prescriptions": ["max_v_specificity", "assisted_overspeed", "free_sprint_maintenance"],
        "avoids": ["no_heavy_resisted_force_saturated"],
        "chase": ("peak_speed_mps", "Top speed", "m/s", lambda v: v + 0.5),
    },
    "pmax": {
        "headline": "Build the engine first.",
        "verdict_template": (
            "Your raw power output is in the developable range. "
            "Before chasing profile shape, the priority is building horizontal power. "
            "Heavier strength + resisted sprints across loads is the highest-leverage work."
        ),
        "prescriptions": ["lower_body_strength", "f0_resisted_sprints", "free_sprint_maintenance"],
        "avoids": ["no_orientation_chasing_low_pmax"],
        "chase": ("peak_power_w", "Peak power", "W", lambda v: v * 1.15),  # +15%
    },
    "mechanics": {
        "headline": "Push it forward, not up.",
        "verdict_template": (
            "Your physical engine is fine — the issue is technique. "
            "Force is being applied vertically when it should be going horizontally. "
            "Drill the push-off mechanics; the speed will follow."
        ),
        "prescriptions": ["block_starts", "wickets_progressive", "plyo_reactive"],
        "avoids": [],
        "chase": ("split_10m", "10 m split", "s", lambda v: v - 0.15),
    },
    "balanced": {
        "headline": "Solid profile. Hold the line.",
        "verdict_template": (
            "Your profile is balanced and your numbers are in good shape. "
            "Continue current programming — small dose adjustments rather than direction changes."
        ),
        "prescriptions": ["free_sprint_maintenance", "f0_resisted_sprints", "max_v_specificity"],
        "avoids": [],
        "chase": ("peak_power_w", "Peak power", "W", lambda v: v * 1.05),
    },
}


def _compute_chase_delta(chase_field: str, current, previous_rep: Optional[dict]):
    """Compute (previous_value, delta_pct, exceeds_mdc) for the chase metric."""
    if previous_rep is None or current is None:
        return (None, None, None)
    if chase_field == "split_10m":
        prev_splits = previous_rep.get("splits_s_extended") or previous_rep.get("splits_s") or {}
        prev = prev_splits.get("10")
        prev = float(prev) if prev is not None else None
    else:
        prev = previous_rep.get(chase_field)
    if prev is None or prev == 0:
        return (None, None, None)
    delta_pct = (current - prev) / abs(prev) * 100
    # MDC threshold by metric
    mdc_pct = _ins.MDC.get({
        "split_10m": "split_5m",        # closest available; no 10 m MDC published
        "peak_speed_mps": "v0_mps",
        "peak_power_w": "pmax_w",
        "peak_force_n": "f0_n",
        "f0_rel_nkg": "f0_n",
        "v0_mps": "v0_mps",
        "pmax_rel_wkg": "pmax_w",
    }.get(chase_field, ""))
    exceeds_mdc = (mdc_pct is not None) and (abs(delta_pct) >= mdc_pct)
    return (prev, delta_pct, exceeds_mdc)


def synthesise(rep: dict, all_insights: list, position_group: str = "back",
               previous_rep: Optional[dict] = None) -> dict:
    """Produce the layered report from raw insights.
    `previous_rep`: most recent prior rep for the same athlete, used to compute
    vs-previous-session deltas + MDC pass/fail flags on the chase metric.
    """
    limiter = _classify_limiter(rep, position_group)
    rule = LIMITER_RULES.get(limiter, LIMITER_RULES["balanced"])

    # Verdict paragraph values
    splits = rep.get("splits_s_extended") or rep.get("splits_s") or {}
    s10 = splits.get("10")
    peak_v = rep.get("peak_speed_mps") or 0
    if s10 is None and "{s10" in rule["verdict_template"]:
        # Rep never reached 10 m (short resisted rep, gym drill, …) — never
        # fabricate a "0.00 s" split in the athlete's face. Give an honest
        # verdict off what we do have.
        verdict = (f"You hit {peak_v:.2f} m/s at top speed. This rep didn't "
                   f"cover 10 m, so there's no 10 m split to judge the start "
                   f"by — the call below rests on the profile numbers.")
    else:
        ctx = {"peak_v": peak_v,
               "s10": float(s10) if s10 is not None else 0.0,
               "position": position_group}
        try:
            verdict = rule["verdict_template"].format(**ctx)
        except Exception:
            verdict = rule["verdict_template"]

    # Chase metric — pick the field, current value, target
    chase_field, chase_label, chase_units, target_fn = rule["chase"]
    if chase_field == "split_10m":
        current = float(s10) if s10 is not None else None
    else:
        current = rep.get(chase_field)
    target = target_fn(current) if current is not None else None
    prev, delta_pct, exceeds_mdc = _compute_chase_delta(chase_field, current, previous_rep)
    chase_metric = {
        "name": chase_label,
        "current": round(current, 2) if current is not None else None,
        "target": round(target, 2) if target is not None else None,
        "previous": round(prev, 2) if prev is not None else None,
        "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        "exceeds_mdc": exceeds_mdc,
        "units": chase_units,
    }

    # Filter prescriptions through contraindications keyed off insight tags
    insight_tags = {ins["tag"] for ins in all_insights if ins.get("tag")}
    rx_ids = []
    for rid in rule["prescriptions"]:
        rx = _rx.PRESCRIPTIONS.get(rid)
        if not rx:
            continue
        if any(c in insight_tags for c in rx.get("contraindications", [])):
            continue
        rx_ids.append(rid)

    # COPY the rule's avoid list — appending to the reference would weld this
    # athlete's contextual avoid into the module-level LIMITER_RULES for every
    # later athlete in the process (real cross-athlete contamination bug).
    avoids = list(rule["avoids"])
    # Contextual augmentation: if responsiveness predictor fired, add the
    # corresponding avoid.
    for ins in all_insights:
        if ins["id"] == "responsiveness" and ins["severity"] == "high":
            if "Predicted non-responder (assisted)" in ins["tag"]:
                if "no_assisted_velocity_saturated" not in avoids:
                    avoids.append("no_assisted_velocity_saturated")
            elif "Predicted non-responder (heavy resisted)" in ins["tag"]:
                if "no_heavy_resisted_force_saturated" not in avoids:
                    avoids.append("no_heavy_resisted_force_saturated")

    # Render copy
    rx_blocks = []
    for rid in rx_ids:
        rx_blocks.append({
            "id": rid,
            "label": _rx.PRESCRIPTIONS[rid].get("label", rid),
            "athlete_text": _rx.render_prescription(rid, rep, view="athlete"),
            "coach_text": _rx.render_prescription(rid, rep, view="coach"),
            "day_hint": _rx.PRESCRIPTIONS[rid].get("day_hint"),
            "frequency_per_week": _rx.PRESCRIPTIONS[rid].get("frequency_per_week", 1),
        })
    avoid_blocks = []
    for aid in avoids:
        avoid_blocks.append({
            "id": aid,
            "athlete_text": _rx.render_avoid(aid, view="athlete"),
            "coach_text": _rx.render_avoid(aid, view="coach"),
        })

    # Filter exploratory insights out of the athlete-side report.
    athlete_supporting = [ins for ins in all_insights
                          if "exploratory" not in (ins.get("evidence") or "")]

    report = {
        "primary_limiter": limiter,
        "headline": rule["headline"],
        "verdict_paragraph": verdict,
        "chase_metric": chase_metric,
        "prescriptions": rx_blocks,
        "avoids": avoid_blocks,
        "supporting_insights_athlete": athlete_supporting,
        "supporting_insights_coach": all_insights,
    }

    # Lint — fail loudly if jargon leaks into athlete-facing copy
    athlete_strings = [report["headline"], report["verdict_paragraph"]]
    for rx in rx_blocks:
        athlete_strings.append(rx["athlete_text"])
        athlete_strings.append(rx["label"])  # labels render to athletes too
    for av in avoid_blocks: athlete_strings.append(av["athlete_text"])
    for s in athlete_strings:
        bad = _has_jargon(s)
        if bad:
            report["_lint_warning"] = f"Jargon '{bad}' leaked into athlete copy: '{s[:80]}…'"
            break

    return report
