"""quadrant.py — athlete quadrant classification for the coach report.

Two 2x2 quadrants that turn the per-rep numbers into a *picture of the limiter*:

  1. F-V quadrant        — velocity capability (V0, x) vs force capability
                           (F0/kg, y). Answers "what to train": force-oriented,
                           velocity-oriented, well-rounded, or under-powered.
                           Grounds on the same Lahti-2020 / Cross-2017 constants
                           the insights engine already uses; orientation tag comes
                           straight from insights.fv_orientation().
  2. Accel-MaxV quadrant — top speed (Vmax, x) vs acceleration (1/tau, y).
                           Answers "which phase limits": accelerator, speedster,
                           complete, or developing. Pure velocity data — works
                           from a single 1080 run, no GRF needed.

Pure-Python, stdlib only (matches insights.py). Reuses insights.py's NORMS,
band_of(), tau + slope constants and fv_orientation() so the quadrant can never
disagree with the text insights. Returns plottable coordinates (normalised 0..1,
origin bottom-left) plus a classification and a plain verdict; the UI draws the
square, the split lines at `center`, and the athlete dot at `point`.

Evidence: Samozino 2022 (sprint optimal F-V profile) and Solberg 2025 meta frame
the F-V quadrant; Cross 2017 (Pmax/Lopt) and Lahti 2020 (rugby slope cutoffs) set
the bands; Bezodis 2017 backs reading step frequency alongside these.
"""
from __future__ import annotations

from typing import Any, Optional

from insights import NORMS, TAU_FAST, TAU_SLOW

# Quadrant split is the band boundary a coach reads as "at/above standard".
SPLIT_BAND = "good"
# Acceleration axis split (s): midpoint of the fast/slow tau thresholds.
TAU_SPLIT = (TAU_FAST + TAU_SLOW) / 2.0  # 1.15 s


def _norm(value: float, bands: dict) -> tuple[float, float]:
    """Map a value to 0..1 across the band range, and return the normalised
    position of the SPLIT band too. Handles higher- and lower-is-better.
    Returns (norm_value, norm_split), both clamped to [0, 1]."""
    higher = bands.get("higher_is_better", True)
    poor, fair, good, great = bands["poor"], bands["fair"], bands["good"], bands["great"]
    # Plot range: one band below 'poor' to one band above 'great'.
    if higher:
        lo = poor - (fair - poor)
        hi = great + (great - good)
        nv = (value - lo) / (hi - lo)
        ns = (bands[SPLIT_BAND] - lo) / (hi - lo)
    else:  # lower is better — invert so "good" is up/right
        hi = poor + (poor - fair)
        lo = great - (good - great)
        nv = (hi - value) / (hi - lo)
        ns = (hi - bands[SPLIT_BAND]) / (hi - lo)
    clamp = lambda x: max(0.0, min(1.0, x))
    return clamp(nv), clamp(ns)


def _axis(metric: str, label: str, unit: str, value: Optional[float], bands: dict) -> Optional[dict]:
    if value is None:
        return None
    nv, ns = _norm(value, bands)
    higher = bands.get("higher_is_better", True)
    strong = value >= bands[SPLIT_BAND] if higher else value <= bands[SPLIT_BAND]
    return {"metric": metric, "label": label, "unit": unit, "value": round(value, 3),
            "norm": round(nv, 3), "split": round(ns, 3), "strong": strong}


# ---------------------------------------------------------------------------
# 1. Force-Velocity quadrant
# ---------------------------------------------------------------------------

_FV_LABELS = {
    (True, True):   ("Well-rounded", "Strong on both force and velocity — maintain breadth; look to Pmax and mechanical effectiveness for the next gain."),
    (True, False):  ("Force-oriented", "Force-strong but velocity-limited — bias assisted / overspeed and top-speed exposure to raise V0."),
    (False, True):  ("Velocity-oriented", "Velocity-strong but force-limited — a lower-body strength block and heavy resisted work at Lopt is indicated."),
    (False, False): ("Under-powered", "Below standard on both — build horizontal power broadly (strength + Lopt sled); don't chase orientation until Pmax climbs."),
}
_FV_PRESCRIBE = {
    (True, True):   ["Maintain heavy strength base + speed work", "Chase Pmax / effectiveness, not orientation"],
    (True, False):  ["Assisted / overspeed for V0", "Top-speed sprint exposure", "Hold strength, don't add more heavy work"],
    (False, True):  ["Heavy lower-body strength block", "Resisted sprints at individual Lopt"],
    (False, False): ["Broad force + velocity exposure", "Sled at Lopt for Pmax", "Lower-body strength"],
}


def fv_quadrant(rep: dict, position_group: str = "back") -> Optional[dict]:
    pg = NORMS.get(position_group, NORMS["back"])
    x = _axis("v0_mps", "Velocity (V0)", "m/s", rep.get("v0_mps"), pg["v0_mps"])
    y = _axis("f0_rel_nkg", "Force (F0/kg)", "N/kg", rep.get("f0_rel_nkg"), pg["f0_rel_nkg"])
    if x is None or y is None:
        return None
    key = (y["strong"], x["strong"])  # (force_strong, velocity_strong)
    tag, verdict = _FV_LABELS[key]
    return {
        "id": "fv_quadrant",
        "title": "Force–Velocity",
        "x": x, "y": y,
        "point": {"x": x["norm"], "y": y["norm"]},
        "center": {"x": x["split"], "y": y["split"]},
        "quadrant": tag,
        "verdict": verdict,
        "prescribe": _FV_PRESCRIBE[key],
        "evidence": "Samozino 2022 / Solberg 2025 (optimal F-V); Lahti 2020 bands",
    }


# ---------------------------------------------------------------------------
# 2. Acceleration - Max-Velocity quadrant
# ---------------------------------------------------------------------------

_AM_LABELS = {
    (True, True):   ("Complete", "Quick to top speed and a high top speed — a complete sprint profile; maintain both."),
    (True, False):  ("Accelerator", "Accelerates well but a modest top-speed ceiling — add max-velocity / assisted work to extend the ceiling."),
    (False, True):  ("Speedster", "High top speed but slow to reach it — improve early acceleration: horizontal force, starts, heavy resisted work."),
    (False, False): ("Developing", "Below standard on both acceleration and top speed — build the acceleration base first (strength + Lopt sled)."),
}
_AM_PRESCRIBE = {
    (True, True):   ["Maintain — vary stimulus to hold both ends"],
    (True, False):  ["Max-velocity sprint exposure (flys)", "Assisted / overspeed to lift the ceiling"],
    (False, True):  ["Acceleration blocks + starts", "Heavy resisted sprints for horizontal force"],
    (False, False): ["Acceleration + strength base", "Sled at Lopt"],
}


def accel_maxv_quadrant(rep: dict, position_group: str = "back") -> Optional[dict]:
    pg = NORMS.get(position_group, NORMS["back"])
    max_v = (rep.get("max_v_ms") or rep.get("top_speed_mps")
             or rep.get("peak_speed_mps"))  # DB/hydrated reps use peak_speed_mps
    tau = rep.get("tau_s")
    x = _axis("max_v_ms", "Top speed (Vmax)", "m/s", max_v, pg["max_v_ms"])
    if x is None or tau is None:
        return None
    # Acceleration axis: lower tau = faster = higher on the axis.
    TAU_LO, TAU_HI = 0.6, 1.6  # typical sprint range
    ny = max(0.0, min(1.0, (TAU_HI - tau) / (TAU_HI - TAU_LO)))
    ns = (TAU_HI - TAU_SPLIT) / (TAU_HI - TAU_LO)
    accel_strong = tau <= TAU_SPLIT
    y = {"metric": "tau_s", "label": "Acceleration (1/tau)", "unit": "s",
         "value": round(tau, 3), "norm": round(ny, 3), "split": round(ns, 3),
         "strong": accel_strong}
    key = (accel_strong, x["strong"])  # (accel_strong, topspeed_strong)
    tag, verdict = _AM_LABELS[key]
    return {
        "id": "accel_maxv_quadrant",
        "title": "Acceleration–Top speed",
        "x": x, "y": y,
        "point": {"x": x["norm"], "y": y["norm"]},
        "center": {"x": x["split"], "y": y["split"]},
        "quadrant": tag,
        "verdict": verdict,
        "prescribe": _AM_PRESCRIBE[key],
        "evidence": "tau bands (insights); Cross 2017 Pmax/Lopt; Bezodis 2017 (step frequency)",
    }


def quadrants(rep: dict, position_group: str = "back") -> dict:
    """Both quadrants for a rep. Each entry is None if its inputs are missing."""
    return {
        "fv": fv_quadrant(rep, position_group),
        "accel_maxv": accel_maxv_quadrant(rep, position_group),
    }
