"""PPA Prescription Library.

Concrete dosed training prescriptions, each with both athlete-facing and
coach-facing copy. Picked by `synthesis.py` based on primary limiter +
supporting findings. Synthesis NEVER improvises copy — it pulls from this
library and string-formats the dose against the rep's numbers.

Each entry:
    id:                 short snake_case
    label:              short coach-readable name
    athlete_text:       2-3 sentences, dosed, no jargon. Format string —
                        accepts numeric placeholders like {lopt_kg}.
    coach_text:         technical phrasing for the coach view.
    day_hint:           suggestive day-of-week ("Mon" / "Tue" / etc.)
                        — not authoritative, the coach owns scheduling.
    frequency_per_week: 1, 2, 3 ...
    contraindications:  list of insight tags that should NOT pull this prescription.
"""
from __future__ import annotations

PRESCRIPTIONS = {
    "f0_resisted_sprints": {
        "label": "Resisted sprints",
        "athlete_text": (
            "Resisted sprints — {lopt_kg:.0f} kg on the cable. "
            "5 × 20 m, 3 min rest between reps. Drive low and hard out of the start."
        ),
        "coach_text": (
            "Resisted at individual Lopt = {lopt_kg:.0f} kg ({lopt_pct_bm:.0f}% BM). "
            "5 × 20 m × 3 min rest. Target Pmax/F0 development. Cross 2017."
        ),
        "day_hint": "Mon",
        "frequency_per_week": 2,
        "contraindications": ["Force-saturated"],
    },
    "lower_body_strength": {
        "label": "Heavy lower-body strength",
        "athlete_text": (
            "Heavy back squat: 4 × 4 reps at 85%. "
            "Hex bar deadlift: 4 × 3 reps at 85%. "
            "Build the foundation that drives every step."
        ),
        "coach_text": (
            "Lower-body strength block. Squat 4×4 @ 85%, hex bar DL 4×3 @ 85%. "
            "F0 has clear upside. 1-2× / week, 48 h spacing from sprint days."
        ),
        "day_hint": "Tue",
        "frequency_per_week": 2,
        "contraindications": [],
    },
    "free_sprint_maintenance": {
        "label": "Free sprint maintenance",
        "athlete_text": (
            "Free sprints: 4 × 30 m at max effort, full rest between (3 min). "
            "Keep your top-end sharp."
        ),
        "coach_text": (
            "Free 4 × 30 m @ max, 3 min rest. V0 maintenance. 1× / week minimum."
        ),
        "day_hint": "Fri",
        "frequency_per_week": 1,
        "contraindications": [],
    },
    "max_v_specificity": {
        "label": "Top-end specificity",
        "athlete_text": (
            "Flying sprints: 4 × 30 m with 20 m run-up, full rest. "
            "Focus on staying tall and relaxed at top speed."
        ),
        "coach_text": (
            "Flying 30s with 20 m run-up, 4 reps × full rest. "
            "Max-V specificity. Pair with light sled (≤20% BM) for over/under contrast."
        ),
        "day_hint": "Wed",
        "frequency_per_week": 1,
        "contraindications": ["Velocity-saturated"],
    },
    "assisted_overspeed": {
        "label": "Assisted / overspeed",
        "athlete_text": (
            "Light tow or downhill (~3°) sprints: 4 × 30 m. "
            "Let the assistance carry you to higher speeds than you can hit alone."
        ),
        "coach_text": (
            "Assisted (tow or downhill 3°) 4 × 30 m × full rest. "
            "Targets V0. Lahti 2020: contraindicated for slope ≥ -0.83."
        ),
        "day_hint": "Wed",
        "frequency_per_week": 1,
        "contraindications": ["Velocity-saturated", "Balanced"],
    },
    "block_starts": {
        "label": "Block / falling starts",
        "athlete_text": (
            "Block-position starts: 6 × 10 m, full rest. "
            "Drive horizontally, low to the ground for the first 4-5 steps."
        ),
        "coach_text": (
            "Block starts 6 × 10 m × full rest. Targets RFmax + early acceleration. "
            "Cue forward shin angle, slow-build hip rise."
        ),
        "day_hint": "Thu",
        "frequency_per_week": 1,
        "contraindications": [],
    },
    "wickets_progressive": {
        "label": "Wickets / progressive accel",
        "athlete_text": (
            "Progressive acceleration drills with markers every 2-3 m. "
            "5 × 30 m runs through wickets, focus on smooth posture transition."
        ),
        "coach_text": (
            "Wickets / progressive accel — 30 m runs, 5 reps. "
            "Targets Drf (effectiveness retention as v climbs). Posture cue: "
            "delay vertical extension to 8-10 m mark."
        ),
        "day_hint": "Wed",
        "frequency_per_week": 1,
        "contraindications": [],
    },
    "plyo_reactive": {
        "label": "Reactive plyometrics",
        "athlete_text": (
            "Bounding: 3 × 30 m. "
            "Depth jumps off 30 cm box: 4 × 5 reps, focus on quick contact."
        ),
        "coach_text": (
            "Bounding 3 × 30 m + depth jumps 4 × 5 from 30 cm. "
            "Targets reactive strength index, leg stiffness. Use when GCT > 110 ms."
        ),
        "day_hint": "Sat",
        "frequency_per_week": 1,
        "contraindications": [],
    },
}


# Avoid blocks — explicit don'ts keyed to the same triggers
AVOIDS = {
    "no_assisted_velocity_saturated": {
        "athlete_text": (
            "Avoid overspeed / assisted runs and towed or downhill sprinting. "
            "Your top-end machinery is already saturated — extra work there "
            "won't translate to faster rugby starts. Build force, not more velocity."
        ),
        "coach_text": "Slope ≥ -0.83: assisted/overspeed predicted non-responder per Lahti 2020.",
    },
    "no_heavy_resisted_force_saturated": {
        "athlete_text": (
            "Avoid heavy sled work (anything above ~50% body mass). "
            "Your start machinery is already heavily loaded — adding more "
            "resistance won't yield further gains. Focus on sharper top-end speed."
        ),
        "coach_text": "Slope ≤ -0.92: heavy resisted predicted non-responder per Lahti 2020.",
    },
    "no_orientation_chasing_low_pmax": {
        "athlete_text": (
            "Avoid chasing 'fixing' your profile shape until your power "
            "output climbs. Build the engine first; refine the gearing later."
        ),
        "coach_text": (
            "Pmax < 14 W/kg: orientation correction premature. Build absolute "
            "horizontal power (Cross 2017) before biasing F0 vs V0."
        ),
    },
}


def render_prescription(rx_id: str, rep: dict, view: str = "athlete") -> str:
    """Render a prescription's copy with the rep's numbers interpolated."""
    rx = PRESCRIPTIONS.get(rx_id)
    if not rx:
        return ""
    body_mass_kg = (rep.get("_meta") or {}).get("body_mass_kg")
    f0 = rep.get("f0_n")
    v0 = rep.get("v0_mps")
    text = rx.get("athlete_text" if view == "athlete" else "coach_text", "")
    if "{lopt" in text and not f0:
        # No valid F-V for this rep — never render "0 kg on the cable".
        if view == "athlete":
            return (f"{rx.get('label', rx_id)} — your coach will set the "
                    f"load (it needs a valid force-velocity profile first).")
        return ("Lopt undosed — no valid F-V on this rep (f0_n missing). "
                "Capture a 20-30 m resisted profile rep to dose per Cross 2017.")
    lopt_kg = (f0 / 2 / 9.81) if f0 else 0.0
    lopt_pct_bm = (lopt_kg / body_mass_kg * 100) if (lopt_kg and body_mass_kg) else 0.0
    ctx = {"lopt_kg": lopt_kg, "lopt_pct_bm": lopt_pct_bm,
           "f0_n": f0 or 0, "v0_mps": v0 or 0,
           "body_mass_kg": body_mass_kg or 0}
    try:
        return text.format(**ctx)
    except Exception:
        return text


def render_avoid(avoid_id: str, view: str = "athlete") -> str:
    av = AVOIDS.get(avoid_id)
    if not av:
        return ""
    return av.get("athlete_text" if view == "athlete" else "coach_text", "")
