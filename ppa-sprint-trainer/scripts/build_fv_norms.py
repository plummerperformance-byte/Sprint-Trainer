"""Build data/reference_norms_fv.json — sport-specific horizontal force-velocity
norms, an elite force-plate ceiling, stride-under-load reference ranges, and
validity gates for the sprint F-V model.

Sources (all open / published):
- Haugen, Breitschadel & Seiler — 666 elite athletes, 23 sports, 40 m sprints.
  DataverseNO doi:10.18710/PJONBM (CC0). Per-athlete F0/V0/P0/FV-slope/RFmax/DRF
  + 10/20/30/40 m splits. Source copied to data/sources/haugen_olympiatoppen_666.txt.
- Rabita et al. 2015, Scand J Med Sci Sports — force-plate, 9 world-class sprinters.
  Table 1 elite ceiling (spatiotemporal + mechanical). Values embedded below.
- Chang et al. 2024, PLoS ONE e0298517 — n=14, motorised device, step-level
  contact/flight/step-length under load. Aggregated ranges embedded below.

Run: python scripts/build_fv_norms.py
"""
from __future__ import annotations

import csv
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "data", "sources", "haugen_olympiatoppen_666.txt")
OUT = os.path.join(HERE, "data", "reference_norms_fv.json")

# Haugen column -> our metric key + unit
COLS = {
    "F0_(N/kg)": ("f0_nkg", "N/kg", True),
    "V0_(m/s)": ("v0_ms", "m/s", True),
    "P0_(W/kg)": ("pmax_wkg", "W/kg", True),
    "FV_Slope_(N/s/m/kg)": ("fv_slope", "N/s/m/kg", None),
    "RF_max_(%)": ("rfmax_pct", "%", True),
    "DRF_(%)": ("drf_pct", "%", None),
    "10m_(s)": ("split_10m_s", "s", False),
    "20m_(s)": ("split_20m_s", "s", False),
    "30m_(s)": ("split_30m_s", "s", False),
    "40m_(s)": ("split_40m_s", "s", False),
}
MIN_N = 12  # minimum athletes for a per-sport band


def num(x):
    if x is None:
        return None
    try:
        return float(str(x).replace("%", "").strip())
    except ValueError:
        return None


def pct(v, p):
    v = sorted(v)
    k = (len(v) - 1) * p / 100
    f = int(k)
    return v[f] if f + 1 >= len(v) else v[f] + (v[f + 1] - v[f]) * (k - f)


def stats(v, hib):
    d = {
        "n": len(v), "mean": round(st.mean(v), 3),
        "sd": round(st.pstdev(v), 3) if len(v) > 1 else 0.0,
        **{f"p{p}": round(pct(v, p), 3) for p in (10, 25, 50, 75, 90)},
    }
    if hib is not None:
        d["higher_is_better"] = hib
    return d


def metric_block(rows):
    out = {}
    for col, (key, unit, hib) in COLS.items():
        vals = [num(r.get(col)) for r in rows]
        vals = [x for x in vals if x is not None]
        if len(vals) >= MIN_N:
            out[key] = {"unit": unit, **stats(vals, hib)}
    return out


def build():
    with open(SRC, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    rows = [r for r in rows if r.get("Sport")]

    by_sport = {}
    sports = sorted({r["Sport"] for r in rows})
    for sp in sports:
        grp = [r for r in rows if r["Sport"] == sp]
        if len(grp) < MIN_N:
            continue
        entry = {"n": len(grp), "metrics": metric_block(grp)}
        for sex in ("M", "F"):
            sg = [r for r in grp if r["Sex"] == sex]
            if len(sg) >= MIN_N:
                entry.setdefault("by_sex", {})[sex] = {"n": len(sg), "metrics": metric_block(sg)}
        by_sport[sp] = entry

    overall_by_sex = {}
    for sex in ("M", "F"):
        sg = [r for r in rows if r["Sex"] == sex]
        overall_by_sex[sex] = {"n": len(sg), "metrics": metric_block(sg)}

    doc = {
        "meta": {
            "purpose": "Sport-specific horizontal force-velocity norms + elite ceiling + validity gates.",
            "primary_source": "Haugen, Breitschadel & Seiler; DataverseNO doi:10.18710/PJONBM (CC0)",
            "n_athletes": len(rows),
            "n_sports": len(by_sport),
            "min_n_per_band": MIN_N,
            "band_reading": "p10=lower .. p50=median .. p90=upper WITHIN elite/national-team athletes; a general athlete near p40-60 here is already strong.",
            "method_note": "Haugen values are Samozino split-time field-method. Do NOT compare directly to Rabita force-plate ceiling (different construct); keep them separate.",
        },
        # Hard sanity bounds — any modelled value outside these is a fit error, not an athlete.
        "validity_gates": {
            "f0_nkg": {"min": 3.5, "max": 12.0, "unit": "N/kg",
                       "note": "Haugen elite ~7-10; Rabita force-plate ~9.8. Outside -> reject fit."},
            "v0_ms": {"min": 6.0, "max": 12.5, "unit": "m/s",
                      "note": "Haugen 7.3-10.9; Bolt ~12.3 is the human ceiling."},
            "pmax_wkg": {"min": 8.0, "max": 33.0, "unit": "W/kg",
                         "note": "Haugen field ~13-22; Rabita force-plate elite ~29-31."},
            "rfmax_pct": {"min": 30.0, "max": 60.0, "unit": "%",
                          "note": "Haugen RFmax 41-52%. A naive regression giving 70-90% is broken."},
            "drf_pct": {"min": -14.0, "max": -3.0, "unit": "%"},
            "tau_s": {"min": 0.6, "max": 1.8, "unit": "s"},
        },
        "fv_by_sport": by_sport,
        "fv_overall_by_sex": overall_by_sex,
        # Rabita 2015 Table 1 — force-plate, block-start, 9 world-class sprinters.
        "elite_ceiling_rabita_2015": {
            "source": "Rabita et al. 2015 Scand J Med Sci Sports; force plate; n=9 world-class (100m 9.95-10.60s)",
            "method": "true ground-reaction-force, block start — a ceiling benchmark, NOT comparable to field-method norms",
            "spatiotemporal": {
                "vmax_ms": 9.78, "vmax_ms_elite4": 10.24,
                "step_length_max_m": 2.19, "step_length_min_m": 1.11,
                "step_freq_max_hz": 4.87, "step_freq_min_hz": 3.92,
                "contact_time_min_ms": 94, "contact_time_max_ms": 193,
                "aerial_time_min_ms": 50, "aerial_time_max_ms": 124,
                "split_10m_s": 1.85, "split_20m_s": 3.05, "split_30m_s": 4.08, "split_40m_s": 5.10,
            },
            "mechanical": {"f0_nkg": 9.77, "pmax_wkg": 29.3, "rf0_pct": 70.6, "drf": -0.067},
        },
        # Ettema et al. 2016 — 24 well-trained sprinters, 25 m from blocks, 3D
        # kinematics. Per-step means (steps 1-10) across the cohort. The reference
        # "shape" of acceleration: step length rises, contact time falls, aerial
        # time and velocity rise. Use to validate your rig's step detector.
        "accel_step_reference_ettema_2016": {
            "source": "Ettema et al. 2016 PLoS ONE e0159701; n=24 trained sprinters; 3D kinematics; blocks start",
            "note": "Unloaded ground-truth step mechanics vs step number. Your cable-derived step length/contact should track this shape.",
            "step": list(range(1, 11)),
            "step_length_m": [1.05, 1.13, 1.31, 1.42, 1.55, 1.65, 1.74, 1.80, 1.89, 1.92],
            "contact_time_s": [0.194, 0.178, 0.164, 0.145, 0.136, 0.133, 0.130, 0.122, 0.116, 0.114],
            "aerial_time_s": [0.066, 0.048, 0.057, 0.075, 0.083, 0.088, 0.090, 0.096, 0.103, 0.106],
            "horiz_velocity_ms": [None, 4.13, 5.18, 5.96, 6.64, 7.18, 7.63, 8.00, 8.35, 8.61],
        },
        # Chang 2024 — step-level contact/flight/length, unloaded vs cable-resisted.
        "stride_under_load_chang_2024": {
            "source": "Chang et al. 2024 PLoS ONE e0298517; n=14; motorised resistance device; 193 steps",
            "note": "Under cable resistance: contact time lengthens, step length shortens, flight time ~unchanged.",
            "unloaded": {
                "step_length_m": {"mean": 1.61, "sd": 0.33, "range": [0.57, 2.52]},
                "flight_time_s": {"mean": 0.092, "sd": 0.021, "range": [0.029, 0.141]},
                "contact_time_s": {"mean": 0.133, "sd": 0.025, "range": [0.095, 0.229]},
            },
            "resisted": {
                "step_length_m": {"mean": 1.55, "sd": 0.28, "range": [0.78, 2.19]},
                "flight_time_s": {"mean": 0.093, "sd": 0.019, "range": [0.037, 0.133]},
                "contact_time_s": {"mean": 0.138, "sd": 0.026, "range": [0.100, 0.237]},
            },
        },
    }
    return doc


def main():
    doc = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    print(f"wrote {OUT}")
    print(f"  {doc['meta']['n_athletes']} athletes, {doc['meta']['n_sports']} sports with bands")
    print("  sports:", ", ".join(sorted(doc["fv_by_sport"])))


if __name__ == "__main__":
    main()
