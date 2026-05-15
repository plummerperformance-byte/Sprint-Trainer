"""Load a 1080 Sprint xlsx export, parse the raw time-series, auto-detect
the actual sprint start (using the "Sprint Start" column the export marks),
build a rep dict matching ppa_service's internal shape, and POST it into
the running service via /api/c/dev/load_rep.

Usage:
    python load_1080_xlsx.py "C:\\path\\to\\Tyrone 1080.xlsx"

Run ppa_service.py (or ppa_app.py) first so the endpoint is available.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path
from typing import Optional

import openpyxl

SERVICE_URL = "http://127.0.0.1:8765"
CHART_SAMPLE_BUDGET = 600  # decimate raw 1 kHz → ~600 chart points (≈50 Hz effective)
SPLIT_LENGTH_M = 5.0       # 1080-style uniform split bucket


def parse_xlsx(path: Path) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)

    # Find raw-data sheet (one with "Raw Data" in the name)
    raw_name = next((s for s in wb.sheetnames if "raw data" in s.lower()), None)
    if not raw_name:
        raise ValueError(f"no Raw Data sheet found in {path.name}; sheets={wb.sheetnames}")
    ws = wb[raw_name]

    # Header row is row 1. Columns we care about:
    #   A time(ms)  B load(g)  C speed(mm/s)  D position(mm)  E Sprint Start
    # Find Sprint Start marker — it's a single number on row 2 indicating the
    # 0-indexed sample where the actual sprint kicks off.
    sprint_start_idx = None
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    if rows and len(rows[0]) >= 5 and isinstance(rows[0][4], (int, float)):
        sprint_start_idx = int(rows[0][4])
    print(f"  raw rows: {len(rows)}  sprint start sample: {sprint_start_idx}")
    if sprint_start_idx is None or sprint_start_idx < 0:
        raise ValueError("Sprint Start column missing or empty in raw-data sheet")

    # Slice from sprint start onward; subtract start position so it's rep-relative.
    run_rows = rows[sprint_start_idx:]
    if not run_rows:
        raise ValueError("Sprint Start index is past the end of the raw data")
    t0 = run_rows[0][0]
    pos0_mm = run_rows[0][3]

    # Compute aggregates from FULL 1 kHz data. Decimate samples just for chart.
    # body mass for power-rel calculation (Dashboard sheet, "Weight (kg)" cell)
    # + athlete name + Morin pre-computes (F0, V0, Pmax, Slope) if 1080 wrote them
    body_mass_kg = None
    athlete_name = None
    morin = {"f0_n": None, "v0_mps": None, "pmax_w": None,
             "f0_rel_nkg": None, "pmax_rel_wkg": None, "fv_slope_per_kg": None}
    if "Dashboard" in wb.sheetnames:
        for row in wb["Dashboard"].iter_rows(values_only=True):
            if not row or row[0] is None: continue
            label = str(row[0]).strip().lower()
            val = row[1]
            try: f = float(val) if val is not None else None
            except Exception: f = None
            if label.startswith("weight") and f is not None: body_mass_kg = f
            elif label == "athlete" and val is not None: athlete_name = str(val)
            elif label.startswith("f₀ / kg") and f is not None: morin["f0_rel_nkg"] = f
            elif label.startswith("f₀") and f is not None: morin["f0_n"] = f
            elif label.startswith("v₀") and f is not None: morin["v0_mps"] = f
            elif label.startswith("pmax / kg") and f is not None: morin["pmax_rel_wkg"] = f
            elif label.startswith("pmax") and f is not None: morin["pmax_w"] = f
            elif label.startswith("slope") and f is not None and body_mass_kg:
                # Dashboard reports slope in N.s/m (not per kg); normalise to per-kg
                # which is what the Morin/Lahti cutoffs operate on.
                morin["fv_slope_per_kg"] = round(f / body_mass_kg, 4)

    # Build full-rate metric arrays
    times_s = []
    speeds_mps = []
    forces_n = []
    pos_m = []
    horiz_forces_n = []   # col 13 — horizontal force (Morin's F_h)
    rf_pct_list = []      # col 18 — RF% (horizontal / resultant)
    for r in run_rows:
        if r[0] is None or r[2] is None or r[3] is None:
            continue
        t_ms = r[0] - t0
        v_mps = (r[2] or 0) / 1000.0
        load_g = r[1] or 0
        f_n = (load_g / 1000.0) * 9.81
        p_mm = (r[3] or 0) - pos0_mm
        times_s.append(t_ms / 1000.0)
        speeds_mps.append(v_mps)
        forces_n.append(f_n)
        pos_m.append(p_mm / 1000.0)
        # Horizontal force + RF% if present (cols 13 / 18, 0-indexed 12 / 17)
        horiz_forces_n.append(r[12] if len(r) > 12 else None)
        rf = r[17] if len(r) > 17 else None
        # 1080 stores RF as 0–1 fraction; promote to %
        rf_pct_list.append(float(rf) * 100 if rf is not None else None)

    if len(times_s) < 2:
        raise ValueError("not enough samples in the run section")

    # Aggregates (full-rate)
    peak_v = max(speeds_mps)
    peak_v_idx = speeds_mps.index(peak_v)
    peak_f = max(forces_n)
    peak_f_idx = forces_n.index(peak_f)
    powers_w = [f * v for f, v in zip(forces_n, speeds_mps)]
    peak_p = max(powers_w)

    n = len(speeds_mps)
    avg_v = sum(speeds_mps) / n
    avg_f = sum(forces_n) / n
    avg_p = sum(powers_w) / n

    # Acceleration (Δv / Δt)
    accs = []
    for i in range(1, n):
        dt = times_s[i] - times_s[i - 1]
        if dt > 0:
            accs.append((speeds_mps[i] - speeds_mps[i - 1]) / dt)
    peak_a = max(accs) if accs else 0.0
    avg_a = sum(accs) / len(accs) if accs else 0.0

    # ∫F·v dt  and  ∫F dt
    work_j = 0.0
    impulse_ns = 0.0
    for i in range(1, n):
        dt = times_s[i] - times_s[i - 1]
        if dt <= 0: continue
        f_avg = (forces_n[i] + forces_n[i - 1]) / 2
        v_avg = (speeds_mps[i] + speeds_mps[i - 1]) / 2
        work_j += f_avg * v_avg * dt
        impulse_ns += f_avg * dt

    duration_s = times_s[-1]
    max_extension_m = max(pos_m)

    # Splits (5/10/20 m — kept for back-compat with existing UI; extended below)
    splits = {}
    for marker in (5, 10, 20):
        for i, p in enumerate(pos_m):
            if p >= marker:
                splits[str(marker)] = round(times_s[i], 3)
                break

    # Sprint phase segmentation (90% / 95% of peak v)
    accel_end_ms = None
    decel_start_ms = None
    past_peak = False
    for i, v in enumerate(speeds_mps):
        if accel_end_ms is None and v >= 0.90 * peak_v:
            accel_end_ms = int(times_s[i] * 1000)
        if v >= peak_v:
            past_peak = True
        if past_peak and decel_start_ms is None and v < 0.95 * peak_v:
            decel_start_ms = int(times_s[i] * 1000)

    # ---- 1080-App sprint metric extensions ----
    # Time / distance to peak v + 90% peak v (acceleration profile shape)
    time_to_max_v_s = times_s[peak_v_idx]
    dist_to_max_v_m = pos_m[peak_v_idx]
    time_to_90pct_v_s = None
    dist_to_90pct_v_m = None
    target_90 = 0.90 * peak_v
    for i, v in enumerate(speeds_mps):
        if v >= target_90:
            time_to_90pct_v_s = times_s[i]
            dist_to_90pct_v_m = pos_m[i]
            break
    end_v = speeds_mps[-1]
    v_dropoff_pct = (peak_v - end_v) / peak_v * 100 if peak_v > 0 else 0.0

    # Splits extended to 5/10/20/30/40 m
    splits_extended = {}
    for marker in (5, 10, 20, 30, 40):
        for i, p in enumerate(pos_m):
            if p >= marker:
                splits_extended[str(marker)] = round(times_s[i], 3)
                break

    # ---- Step detection (force-peak) ----
    step_events = detect_steps(times_s, forces_n, pos_m)
    step_aggs = compute_step_aggregates(step_events)

    # ---- RFmax + Drf are NOT derivable from cable-only sprint data ----
    # Morin's RFmax = horizontal_GRF / resultant_GRF, requires either a
    # force plate or Samozino's inverse-dynamics method on a *free* sprint.
    # Cable load during a resisted sprint is not horizontal GRF — it's a
    # small fraction of total propulsion. The 1080 xlsx Force/RF% columns
    # (cols 13/18) are populated only in the pre-sprint loading rows, not
    # during the run, and reflect cable-loading mechanics rather than
    # sprint mechanics. Mechanical-effectiveness module stays silent for
    # cable-only imports until we add a force plate or implement the
    # full Samozino model on a free-sprint capture path.
    rfmax_pct = None
    drf = None

    # 1080-shaped split report (uniform 5 m buckets)
    split_report = build_split_report(times_s, speeds_mps, forces_n, powers_w, accs, pos_m, SPLIT_LENGTH_M)

    # Time-to-peak metrics (ms)
    ttpf_ms = int(times_s[peak_f_idx] * 1000)
    ttps_ms = int(times_s[peak_v_idx] * 1000)

    # Decimate to ~CHART_SAMPLE_BUDGET points for the UI chart
    step = max(1, n // CHART_SAMPLE_BUDGET)
    samples = []
    for i in range(0, n, step):
        a = accs[i - 1] if 0 < i <= len(accs) else 0.0
        samples.append({
            "t_ms": int(times_s[i] * 1000),
            "v_mps": round(speeds_mps[i], 3),
            "F_N": round(forces_n[i], 1),
            "P_W": round(powers_w[i], 1),
            "a_mps2": round(a, 3),
            "pos_m": round(pos_m[i], 3),
        })
    print(f"  decimated samples: {len(samples)} (every {step}th of {n})")

    # Per-kg power if body mass known
    peak_power_rel_wkg = round(peak_p / body_mass_kg, 2) if body_mass_kg else None

    rep = {
        "rep_idx": 1,
        "duration_s": round(duration_s, 3),
        "max_extension_m": round(max_extension_m, 3),
        "total_distance_m": round(max_extension_m, 3),
        "total_time_s": round(duration_s, 3),
        "peak_speed_mps": round(peak_v, 3),
        "top_speed_mps": round(peak_v, 3),
        "peak_force_n": round(peak_f, 1),
        "peak_power_w": round(peak_p, 1),
        "peak_power_rel_wkg": peak_power_rel_wkg,
        "peak_acceleration_mps2": round(peak_a, 3),
        "peak_torque_pct": None,  # not present in xlsx (1080 doesn't expose torque %)
        "avg_speed_mps": round(avg_v, 3),
        "avg_force_n": round(avg_f, 1),
        "avg_power_w": round(avg_p, 1),
        "avg_acceleration_mps2": round(avg_a, 3),
        "work_j": round(work_j, 1),
        "impulse_ns": round(impulse_ns, 1),
        "ttpf_ms": ttpf_ms,
        "ttps_ms": ttps_ms,
        "accel_end_ms": accel_end_ms,
        "decel_start_ms": decel_start_ms,
        "is_eccentric": False,
        "splits_s": splits,
        "splits_s_extended": splits_extended,
        "split_report": split_report,
        # Morin mechanical effectiveness — computed from cols 13/18 of the xlsx
        "rf_max_pct": rfmax_pct,
        "drf": drf,
        # 1080-App sprint_metrics extensions
        "time_to_max_v_s": round(time_to_max_v_s, 3),
        "dist_to_max_v_m": round(dist_to_max_v_m, 2),
        "time_to_90pct_v_s": round(time_to_90pct_v_s, 3) if time_to_90pct_v_s is not None else None,
        "dist_to_90pct_v_m": round(dist_to_90pct_v_m, 2) if dist_to_90pct_v_m is not None else None,
        "v_dropoff_pct": round(v_dropoff_pct, 1),
        # Step events + aggregates (1080-App step_events table shape)
        "step_events": step_events,
        **step_aggs,
        "samples": samples,
        # Morin sprint-FV outputs (1080's own per-rep regression, imported from
        # Dashboard sheet). Keys mirror the 1080-App `sprint_metrics` table.
        "f0_n": morin["f0_n"],
        "f0_rel_nkg": morin["f0_rel_nkg"],
        "v0_mps": morin["v0_mps"],
        "pmax_w_morin": morin["pmax_w"],   # 1080's Pmax (multi-load curve), not raw F.v peak
        "pmax_rel_wkg": morin["pmax_rel_wkg"] if morin["pmax_rel_wkg"] is not None
                        else (round(peak_p / body_mass_kg, 2) if body_mass_kg else None),
        "fv_slope_per_kg": morin["fv_slope_per_kg"],
        "_meta": {
            "source": "1080 xlsx",
            "athlete_name": athlete_name,
            "body_mass_kg": body_mass_kg,
            "raw_sample_count": n,
            "sprint_start_index": sprint_start_idx,
        },
    }
    return rep


def _smooth(xs, window=15):
    """Simple centred moving-average smooth. Window is in samples (~15 ms at 1 kHz).
    Reduces high-frequency noise without smearing real peaks (~50-100 ms wide)."""
    half = window // 2
    n = len(xs)
    out = []
    for i in range(n):
        a = max(0, i - half); b = min(n, i + half + 1)
        out.append(sum(xs[a:b]) / (b - a))
    return out


def detect_steps(t_s, f_n, pos_m, min_period_s=0.15, min_prominence_n=4.0, smooth_window=15):
    """Peak-detect foot strikes on the cable force trace.

    Cable trainers maintain constant baseline tension so peaks are subtle
    (typically 5-30 N above the local valley, not 0 → peak like a force
    plate). Use prominence-based detection on a smoothed trace:
      1. Light moving-average smooth (~15 ms) to remove digitisation noise
      2. Find every local maximum
      3. Compute prominence (how far it rises above the lower of the two
         adjacent valleys); discard peaks below `min_prominence_n`
      4. Enforce minimum spacing of `min_period_s` between accepted peaks

    Returns list of step dicts (1080-App step_events shape).
    """
    if len(f_n) < 3:
        return []
    fs = _smooth(f_n, smooth_window)

    # Find ALL local maxima (no threshold yet)
    maxima = []
    for i in range(1, len(fs) - 1):
        if fs[i] > fs[i - 1] and fs[i] >= fs[i + 1]:
            maxima.append(i)
    if not maxima:
        return []

    # For each maximum, compute prominence vs adjacent valleys (look in a
    # ±0.5 s window, which comfortably brackets one full step at 2-5 Hz).
    win = max(20, int(0.5 / max(t_s[1] - t_s[0], 1e-4)))  # samples in 0.5 s
    accepted_indices = []
    last_t = -1e9
    for idx in maxima:
        a = max(0, idx - win); b = min(len(fs), idx + win + 1)
        valley_left = min(fs[a:idx + 1]) if idx > a else fs[idx]
        valley_right = min(fs[idx:b]) if idx < b - 1 else fs[idx]
        prominence = fs[idx] - max(valley_left, valley_right)
        if prominence < min_prominence_n:
            continue
        # min spacing
        if t_s[idx] - last_t < min_period_s:
            # If the new candidate is more prominent, replace the previous one
            if accepted_indices and fs[idx] > fs[accepted_indices[-1]]:
                accepted_indices[-1] = idx
                last_t = t_s[idx]
            continue
        accepted_indices.append(idx)
        last_t = t_s[idx]

    # Build step events using ORIGINAL (un-smoothed) force values for the recorded peak
    steps = []
    for i, idx in enumerate(accepted_indices):
        prev_idx = accepted_indices[i - 1] if i > 0 else None
        period_ms = round((t_s[idx] - t_s[prev_idx]) * 1000, 1) if prev_idx is not None else None
        length_m = round(pos_m[idx] - pos_m[prev_idx], 3) if prev_idx is not None else None
        inst_freq_hz = round(1000 / period_ms, 2) if period_ms and period_ms > 0 else None
        steps.append({
            "step_number": i + 1,
            "t_strike_s": round(t_s[idx], 4),
            "pos_m": round(pos_m[idx], 3),
            "peak_force_n": round(f_n[idx], 1),
            "step_period_ms": period_ms,
            "step_length_m": length_m,
            "step_frequency_hz": inst_freq_hz,
        })
    return steps


def compute_step_aggregates(steps):
    """Roll up a step-events list into per-rep aggregates."""
    if len(steps) < 2:
        return {"total_steps": len(steps)}
    valid = [s for s in steps if s.get("step_period_ms") is not None]
    if not valid:
        return {"total_steps": len(steps)}
    periods = [s["step_period_ms"] for s in valid]
    lengths = [s["step_length_m"] for s in valid if s.get("step_length_m") is not None]
    avg_period = sum(periods) / len(periods)
    step_freq_hz = 1000 / avg_period if avg_period > 0 else 0
    avg_length = sum(lengths) / len(lengths) if lengths else 0
    var_l = sum((l - avg_length) ** 2 for l in lengths) / len(lengths) if lengths else 0
    std_length = var_l ** 0.5
    return {
        "total_steps": len(steps),
        "step_freq_hz": round(step_freq_hz, 2),
        "avg_step_length_m": round(avg_length, 3),
        "step_length_std_m": round(std_length, 3),
        "avg_step_period_ms": round(avg_period, 1),
    }


def build_split_report(t_s, v_mps, f_n, p_w, accs, pos_m, split_length_m):
    if not pos_m:
        return {"splits": [], "splitLength": split_length_m, "isYards": False}
    max_pos = max(pos_m)
    n_splits = int(max_pos // split_length_m)
    if n_splits <= 0:
        return {"splits": [], "splitLength": split_length_m, "isYards": False}
    splits_out = []
    for i in range(n_splits):
        start_m = i * split_length_m
        end_m = (i + 1) * split_length_m
        idxs = [j for j, p in enumerate(pos_m) if start_m <= p < end_m]
        if not idxs:
            continue
        n = len(idxs)
        bs = [v_mps[j] for j in idxs]
        bf = [f_n[j] for j in idxs]
        bp = [p_w[j] for j in idxs]
        ba = [accs[max(0, j - 1)] if j > 0 else 0.0 for j in idxs]
        splits_out.append({
            "start": round(start_m, 2),
            "end": round(end_m, 2),
            "time": round(t_s[idxs[-1]] - t_s[idxs[0]], 3),
            "averages": {
                "avg_speed": round(sum(bs) / n, 3),
                "avg_force": round(sum(bf) / n, 1),
                "avg_power": round(sum(bp) / n, 1),
                "avg_acceleration": round(sum(ba) / n, 3),
            },
            "peaks": {
                "peak_speed": round(max(bs), 3),
                "peak_force": round(max(bf), 1),
                "peak_power": round(max(bp), 1),
                "peak_acceleration": round(max(ba), 3),
            },
        })
    return {"splits": splits_out, "splitLength": split_length_m, "isYards": False}


def find_or_create_athlete(name: str) -> Optional[int]:
    """Look up athlete by name (exact case-insensitive) or create one. Returns id."""
    if not name:
        return None
    try:
        with urllib.request.urlopen(SERVICE_URL + "/api/athletes", timeout=5) as r:
            athletes = json.loads(r.read().decode("utf-8"))
        for a in athletes:
            if a.get("name", "").strip().lower() == name.strip().lower():
                return a["id"]
        # Not found — create
        body = json.dumps({"name": name}).encode("utf-8")
        req = urllib.request.Request(
            SERVICE_URL + "/api/athletes",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read().decode("utf-8"))
            print(f"  created athlete '{name}' id={j['id']}")
            return j["id"]
    except Exception as e:
        print(f"  athlete lookup/create failed: {e}", file=sys.stderr)
        return None


def post_rep(rep: dict, athlete_id: Optional[int] = None) -> None:
    payload = {"reps": [rep], "athlete_id": athlete_id} if athlete_id else rep
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SERVICE_URL + "/api/c/dev/load_rep",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"  POST -> {r.status}: {r.read().decode('utf-8')}")


def main(path_str: str) -> None:
    path = Path(path_str)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr); sys.exit(1)
    print(f"loading {path.name}")
    rep = parse_xlsx(path)
    print(f"  athlete:         {rep['_meta'].get('athlete_name')} ({rep['_meta'].get('body_mass_kg')} kg)")
    print(f"  duration:        {rep['duration_s']:.2f} s")
    print(f"  distance:        {rep['max_extension_m']:.2f} m")
    print(f"  peak speed:      {rep['peak_speed_mps']:.2f} m/s @ {rep['time_to_max_v_s']:.2f}s / {rep['dist_to_max_v_m']:.1f}m")
    if rep.get('time_to_90pct_v_s') is not None:
        print(f"  90% peak v:      {rep['time_to_90pct_v_s']:.2f}s / {rep['dist_to_90pct_v_m']:.1f}m")
    print(f"  v dropoff:       {rep['v_dropoff_pct']:.1f}%")
    print(f"  peak force:      {rep['peak_force_n']:.1f} N")
    print(f"  peak power:      {rep['peak_power_w']:.1f} W (= {rep.get('peak_power_rel_wkg', '?')} W/kg)")
    print(f"  work:            {rep['work_j']:.0f} J     impulse: {rep['impulse_ns']:.0f} N.s")
    print(f"  splits (m -> s): {rep['splits_s_extended']}")
    if 'total_steps' in rep:
        print(f"  steps:           {rep.get('total_steps', 0)} steps, "
              f"{rep.get('step_freq_hz', '?')} Hz, "
              f"{rep.get('avg_step_length_m', '?')} m avg "
              f"(std {rep.get('step_length_std_m', '?')} m)")
    if rep.get('rf_max_pct') is not None:
        print(f"  RFmax:           {rep['rf_max_pct']:.1f}%   Drf: {rep.get('drf')}")
    print("posting to service…")
    # If the xlsx had an athlete name, look-up-or-create and persist with linkage.
    athlete_name = (rep.get("_meta") or {}).get("athlete_name")
    athlete_id = find_or_create_athlete(athlete_name) if athlete_name else None
    if athlete_id:
        print(f"  using athlete_id={athlete_id} ({athlete_name}) — rep will persist to DB")
    post_rep(rep, athlete_id=athlete_id)
    print(f"done. open {SERVICE_URL}/coach to view.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python load_1080_xlsx.py <path-to-xlsx>"); sys.exit(2)
    main(sys.argv[1])
