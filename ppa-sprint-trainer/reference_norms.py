"""Reference norms lookup — contextualise a rep against a real 1080 cohort.

Bands are built from 2,430 reps by 267 clients (predominantly D1/pro basketball)
pulled from the 1080 Motion Public API. See scripts/build_reference_norms.py to
regenerate, and data/reference_norms_1080.json for the numbers + provenance.

Typical use in the coach/athlete view:

    from reference_norms import band_for, drill_aliases
    b = band_for("5-0-5_Right", "top_speed_ms", 4.62)
    # -> {"band": "good", "percentile": 71, "p50": 4.463, "unit": "m/s", ...}

`band` is one of: poor / below_avg / average / good / elite (or None when the
drill/metric isn't in the reference set). Everything degrades gracefully — an
unknown drill or metric returns None, never raises, so the UI can just skip the
badge when there's no reference.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_PATH = Path(__file__).parent / "data" / "reference_norms_1080.json"

# Drill-name normalisation: map the app's internal drill ids onto the exact
# exercise names the 1080 export uses. Extend as you add drills. Keys are
# lower-cased/stripped; values must match a key under "drills" in the JSON.
_ALIASES = {
    "505_right": "5-0-5_Right", "505_left": "5-0-5_Left",
    "5-0-5 right": "5-0-5_Right", "5-0-5 left": "5-0-5_Left",
    "10m decel": "10m Decel", "5m decel": "5m Decel",
    "lateral shuffle left": "Lateral Shuffle_Left",
    "lateral shuffle right": "Lateral Shuffle_Right",
    "running": "Running", "sprint": "Running",
    # the rig's free-sprint drill — same movement as the 1080 "Running" export
    "freetest": "Running", "free run": "Running",
}

# percentile -> band label. Anchored on the p-values stored per metric.
_BAND_ORDER = ["poor", "below_avg", "average", "good", "elite"]


def _load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"drills": {}, "meta": {}}


_DATA = _load()


def reload() -> None:
    """Re-read the JSON from disk (e.g. after regenerating it)."""
    global _DATA
    _DATA = _load()


def drill_aliases() -> dict:
    """Expose the alias map so callers can see what's wired up."""
    return dict(_ALIASES)


def _resolve_drill(drill: str) -> Optional[dict]:
    drills = _DATA.get("drills", {})
    if drill in drills:
        return drills[drill]
    alias = _ALIASES.get((drill or "").strip().lower())
    if alias and alias in drills:
        return drills[alias]
    return None


def _percentile(m: dict, value: float) -> float:
    """Estimate the value's percentile by linear interpolation across the
    stored p10..p95 anchors (plus min/max as the 0/100 ends)."""
    anchors = [(0, m["min"]), (10, m["p10"]), (25, m["p25"]), (50, m["p50"]),
               (75, m["p75"]), (90, m["p90"]), (95, m["p95"]), (100, m["max"])]
    hib = m.get("higher_is_better", True)
    if value <= anchors[0][1]:
        pctile = 0.0
    elif value >= anchors[-1][1]:
        pctile = 100.0
    else:
        pctile = 100.0
        for (p_lo, v_lo), (p_hi, v_hi) in zip(anchors, anchors[1:]):
            if v_lo <= value <= v_hi:
                span = (v_hi - v_lo) or 1e-9
                pctile = p_lo + (p_hi - p_lo) * (value - v_lo) / span
                break
    return pctile if hib else 100.0 - pctile


def _band(pctile: float) -> str:
    if pctile < 20:
        return "poor"
    if pctile < 40:
        return "below_avg"
    if pctile < 60:
        return "average"
    if pctile < 85:
        return "good"
    return "elite"


def band_for(drill: str, metric: str, value: float) -> Optional[dict]:
    """Return band context for one metric of one rep, or None if unavailable."""
    if value is None:
        return None
    d = _resolve_drill(drill)
    if not d:
        return None
    m = d.get("metrics", {}).get(metric)
    if not m:
        return None
    pctile = _percentile(m, float(value))
    return {
        "drill": drill,
        "metric": metric,
        "value": round(float(value), 3),
        "unit": m.get("unit"),
        "percentile": round(pctile),
        "band": _band(pctile),
        "p50": m["p50"],
        "p90": m["p90"],
        "cohort_n": m["n"],
        "cohort": _DATA.get("meta", {}).get("cohort"),
    }


def bands_for_rep(drill: str, rep: dict) -> dict:
    """Label every reference-able metric present on a rep dict.

    `rep` keys are matched to metric names in the JSON (top_speed_ms,
    peak_power_wkg, ...). Returns {metric: band_dict} for those with a match.
    """
    d = _resolve_drill(drill)
    if not d:
        return {}
    out = {}
    for metric in d.get("metrics", {}):
        if metric in rep and rep[metric] is not None:
            b = band_for(drill, metric, rep[metric])
            if b:
                out[metric] = b
    return out


# ---------------------------------------------------------------------------
# Force-velocity norms (Haugen 666-athlete dataset) + validity gate + ceilings
# ---------------------------------------------------------------------------

_FV_PATH = Path(__file__).parent / "data" / "reference_norms_fv.json"

# Map the app's sport labels / common aliases onto the dataset's sport keys.
_SPORT_ALIASES = {
    "football": "Soccer", "soccer": "Soccer",
    "basketball": "Basket", "basket": "Basket",
    "rugby": "Soccer",  # nearest team-sport proxy in the set; override if you add rugby norms
    "hockey": "Ice_hockey", "ice hockey": "Ice_hockey",
    "sprint": "Athletics_sprinting", "track": "Athletics_sprinting",
    "volleyball": "Beach/volleyball",
}


def _load_fv() -> dict:
    try:
        with open(_FV_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"fv_by_sport": {}, "fv_overall_by_sex": {}, "validity_gates": {}}


_FV = _load_fv()


def _fv_percentile(m: dict, value: float) -> float:
    anchors = [(10, m["p10"]), (25, m["p25"]), (50, m["p50"]), (75, m["p75"]), (90, m["p90"])]
    hib = m.get("higher_is_better")
    if value <= anchors[0][1]:
        pctile = 10.0 * (value / anchors[0][1]) if anchors[0][1] else 0.0
    elif value >= anchors[-1][1]:
        pctile = 90.0
    else:
        pctile = 50.0
        for (p_lo, v_lo), (p_hi, v_hi) in zip(anchors, anchors[1:]):
            if v_lo <= value <= v_hi:
                span = (v_hi - v_lo) or 1e-9
                pctile = p_lo + (p_hi - p_lo) * (value - v_lo) / span
                break
    # for splits (lower is better) invert; for slope/DRF (hib None) percentile is
    # ambiguous so we still report position but caller should read it as "vs cohort"
    if hib is False:
        pctile = 100.0 - pctile
    return max(0.0, min(100.0, pctile))


def _resolve_sport(sport: str):
    d = _FV.get("fv_by_sport", {})
    if sport in d:
        return sport, d[sport]
    alias = _SPORT_ALIASES.get((sport or "").strip().lower())
    if alias and alias in d:
        return alias, d[alias]
    return None, None


def fv_band(sport: str, metric: str, value: float, sex: str = None) -> Optional[dict]:
    """Percentile + band for an F-V metric vs a sport (optionally sex-specific).

    metric: f0_nkg | v0_ms | pmax_wkg | rfmax_pct | drf_pct | fv_slope |
            split_10m_s | split_20m_s | split_30m_s | split_40m_s
    Falls back to the whole-sport pool if the sex cell is missing, then to None.
    """
    if value is None:
        return None
    key, node = _resolve_sport(sport)
    if not node:
        return None
    metrics = None
    if sex and node.get("by_sex", {}).get(sex.upper(), {}).get("metrics"):
        metrics = node["by_sex"][sex.upper()]["metrics"]
        pool_n = node["by_sex"][sex.upper()]["n"]
    else:
        metrics = node.get("metrics")
        pool_n = node.get("n")
    m = (metrics or {}).get(metric)
    if not m:
        return None
    pctile = _fv_percentile(m, float(value))
    return {
        "sport": key, "sex": (sex.upper() if sex else "all"),
        "metric": metric, "value": round(float(value), 3), "unit": m.get("unit"),
        "percentile": round(pctile), "band": _band(pctile),
        "p50": m["p50"], "cohort_n": pool_n,
    }


def validity_check(metric: str, value: float) -> dict:
    """Gate a modelled F-V value against physiological bounds. Returns
    {ok, metric, value, gate:[min,max], reason}. Unknown metric -> ok=True.

    metric keys: f0_nkg | v0_ms | pmax_wkg | rfmax_pct | drf_pct | tau_s
    """
    g = _FV.get("validity_gates", {}).get(metric)
    if g is None or value is None:
        return {"ok": True, "metric": metric, "value": value, "gate": None, "reason": "no gate"}
    ok = g["min"] <= float(value) <= g["max"]
    return {
        "ok": ok, "metric": metric, "value": round(float(value), 3),
        "gate": [g["min"], g["max"]], "unit": g.get("unit"),
        "reason": ("within bounds" if ok else f"outside {g['min']}-{g['max']} {g.get('unit','')} — likely a fit error"),
    }


def elite_ceiling() -> dict:
    """Rabita 2015 force-plate world-class reference (a ceiling, not a norm)."""
    return _FV.get("elite_ceiling_rabita_2015", {})


def stride_reference() -> dict:
    """Chang 2024 step-level contact/flight/length, unloaded vs resisted."""
    return _FV.get("stride_under_load_chang_2024", {})


def accel_step_reference() -> dict:
    """Ettema 2016 per-step means (step length, contact/aerial time, velocity)
    for 24 trained sprinters — the ground-truth 'shape' to validate step
    detection against."""
    return _FV.get("accel_step_reference_ettema_2016", {})


def reload_fv() -> None:
    global _FV
    _FV = _load_fv()


if __name__ == "__main__":  # quick smoke test
    import sys
    drill = sys.argv[1] if len(sys.argv) > 1 else "5-0-5_Right"
    val = float(sys.argv[2]) if len(sys.argv) > 2 else 4.62
    print(json.dumps(band_for(drill, "top_speed_ms", val), indent=2))
