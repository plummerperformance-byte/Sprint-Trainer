"""Regenerate data/reference_norms_1080.json from a 1080 export folder.

The export folder is produced by the 1080 downloader (one subfolder per client,
each with a summary.csv, plus a top-level clients.csv). Point --export at it.

    python scripts/build_reference_norms.py --export C:/Users/AdamP/1080motion/export

Bands are percentiles per drill per metric. Relative metrics (W/kg, N/kg) need
bodyweight, taken from clients.csv by matching the folder's id8 suffix.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics as st
from collections import defaultdict

UNITS = {
    "top_speed_ms": "m/s", "avg_speed_ms": "m/s", "peak_force_n": "N",
    "peak_power_w": "W", "total_dist_m": "m", "peak_power_wkg": "W/kg",
    "avg_power_wkg": "W/kg", "peak_force_nkg": "N/kg",
}
# every listed metric is higher-is-better
MIN_REPS = 20


def pct(v, p):
    v = sorted(v)
    k = (len(v) - 1) * p / 100
    f = int(k)
    return v[f] if f + 1 >= len(v) else v[f] + (v[f + 1] - v[f]) * (k - f)


def stats(v):
    return {
        "n": len(v), "mean": round(st.mean(v), 3),
        "sd": round(st.pstdev(v), 3) if len(v) > 1 else 0.0,
        "min": round(min(v), 3), "max": round(max(v), 3),
        **{f"p{p}": round(pct(v, p), 3) for p in (10, 25, 50, 75, 90, 95)},
    }


def build(export_dir: str) -> dict:
    id_weight = {}
    clients_csv = os.path.join(export_dir, "clients.csv")
    with open(clients_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                id_weight[r["id"][:8]] = float(r["weight_kg"]) if r.get("weight_kg") else None
            except ValueError:
                id_weight[r["id"][:8]] = None

    raw = defaultdict(lambda: defaultdict(list))
    athletes = defaultdict(set)
    total_reps = 0
    for sm in glob.glob(os.path.join(export_dir, "*", "summary.csv")):
        id8 = os.path.basename(os.path.dirname(sm)).split("_")[-1]
        wt = id_weight.get(id8)
        with open(sm) as f:
            for row in csv.DictReader(f):
                total_reps += 1
                ex = row.get("exercise") or "?"

                def fv(k):
                    try:
                        return float(row.get(k) or "")
                    except ValueError:
                        return None

                ts, asp = fv("top_speed_ms"), fv("avg_speed_ms")
                pf, ap = fv("peak_force_n"), fv("avg_power_w")
                pp, dist = fv("peak_power_w"), fv("total_dist_m")
                m = raw[ex]
                if ts and ts > 0: m["top_speed_ms"].append(ts)
                if asp and asp > 0: m["avg_speed_ms"].append(asp)
                if pf and pf > 0: m["peak_force_n"].append(pf)
                if pp and pp > 0: m["peak_power_w"].append(pp)
                if dist and dist > 0: m["total_dist_m"].append(dist)
                if wt and pp and pp > 0: m["peak_power_wkg"].append(pp / wt)
                if wt and ap and ap > 0: m["avg_power_wkg"].append(ap / wt)
                if wt and pf and pf > 0: m["peak_force_nkg"].append(pf / wt)
                athletes[ex].add(id8)

    out = {
        "meta": {
            "source": "1080 Motion Public API export (TrainingData)",
            "cohort": "267 clients, predominantly D1/pro basketball + a few HS/volleyball",
            "n_reps": total_reps,
            "load_note": ("Reps pooled across the loads athletes used (predominantly light "
                          "NFW ~1-3 kg). Force/power are load-sensitive; treat as broad "
                          "reference, not exact matches to your load."),
            "speed_note": "Uses device topSpeed (cleaned), not the noisy instantaneous peak.",
            "bands": ("p10=poor .. p50=median .. p90=elite within this already-elite cohort. "
                      "higher_is_better=true for all listed metrics."),
            "min_reps_included": MIN_REPS,
        },
        "drills": {},
    }
    for ex in sorted(raw, key=lambda e: -sum(len(x) for x in raw[e].values())):
        mets = {k: v for k, v in raw[ex].items() if len(v) >= MIN_REPS}
        if not mets:
            continue
        out["drills"][ex] = {
            "n_athletes": len(athletes[ex]),
            "metrics": {
                k: {"unit": UNITS[k], "higher_is_better": True, **stats(v)}
                for k, v in mets.items()
            },
        }
    return out


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default="C:/Users/AdamP/1080motion/export",
                    help="1080 export folder (per-client summaries + clients.csv)")
    ap.add_argument("--out", default=os.path.join(here, "data", "reference_norms_1080.json"))
    a = ap.parse_args()
    data = build(a.export)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"wrote {a.out}: {len(data['drills'])} drills, {data['meta']['n_reps']} reps")


if __name__ == "__main__":
    main()
