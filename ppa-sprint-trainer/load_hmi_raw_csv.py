"""Load raw HMI rep CSVs (time(ms),load(g),speed(mm/s),position(mm) @ 1 kHz),
auto-detect the sprint start, build rep dicts matching ppa_service's internal
shape (same contract as load_1080_xlsx), and import them into the DB as ONE
session — or POST them into a running service.

Unlike the 1080 xlsx export there is no vendor "Sprint Start" column, so the
start is detected from the trace itself, in two stages:

  1. first movement — "arm-and-backtrack": arm when 15 ms-smoothed speed holds
     >= 100 mm/s for 200 ms AND position gains >= 0.5 m within 1 s of the arm
     point (rejects the pre-start rock-back); onset = backtrack to the last
     zero-crossing of speed.
  2. run start — for the two-phase protocol (light lead-in tension while the
     athlete walks out to the zone boundary, then launches): first point past
     3 m where 25 ms-smoothed speed holds >= 2.5 m/s for 300 ms, backtracked
     to the preceding local speed minimum (the walk trough just before the
     launch). Validated on the 2026-08-24 session: landed at 4.86-5.16 m on
     all 10 reps, within ~0.1 s of the programmed 5 m boundary.

If no run start is found (free rep, no walk-out) the first-movement onset is
used instead. The rep is sliced from the chosen start, exactly as the xlsx
path slices at the vendor marker.

Usage:
    python load_hmi_raw_csv.py "D:\\2026-08-24_*_raw__rep*_PLUM.csv" --athlete-id 1
    python load_hmi_raw_csv.py <files...> --athlete-id 1 --post   # via running service
    python load_hmi_raw_csv.py <files...> --dry-run               # detect + print only
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import statistics
import urllib.request
from pathlib import Path

import sprint_model

from load_1080_xlsx import (
    CHART_SAMPLE_BUDGET,
    SPLIT_LENGTH_M,
    SERVICE_URL,
    annotate_steps,
    build_split_report,
    compute_asymmetry,
    compute_step_aggregates,
    detect_steps_speed_residual,
    label_feet,
)

# ---- start-detection tuning (validated 2026-08-24, 10/10 reps) ----
MOVE_ARM_MMPS = 100        # first-movement arm threshold
MOVE_SUSTAIN_MS = 200
MOVE_CONFIRM_GAIN_MM = 500
MOVE_CONFIRM_WIN_MS = 1000
RUN_ARM_MMPS = 2500        # run-start (launch) arm threshold
RUN_SUSTAIN_MS = 300
RUN_MIN_POS_MM = 3000      # don't arm during early walk wobble
LEAD_BAND_MM = (1000, 4000)   # where the lead-in load is sampled
ZONE_FROM_MM = 6500           # where the zone load is sampled


def _smooth_int(xs: list[int], window: int) -> list[float]:
    half = window // 2
    n = len(xs)
    out = []
    for i in range(n):
        a = max(0, i - half); b = min(n, i + half + 1)
        out.append(sum(xs[a:b]) / (b - a))
    return out


def read_csv(path: Path):
    t, load_g, spd_mmps, pos_mm = [], [], [], []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) < 4:
                continue
            t.append(int(row[0])); load_g.append(int(row[1]))
            spd_mmps.append(int(row[2])); pos_mm.append(int(row[3]))
    return t, load_g, spd_mmps, pos_mm


def detect_first_movement(spd: list[int], pos: list[int]):
    """Arm-and-backtrack onset of any movement. Returns index or None."""
    n = len(spd)
    ssm = _smooth_int(spd, 15)
    for i in range(n - MOVE_SUSTAIN_MS):
        if ssm[i] >= MOVE_ARM_MMPS and \
                statistics.fmean(spd[i:i + MOVE_SUSTAIN_MS]) >= MOVE_ARM_MMPS:
            j_end = min(n - 1, i + MOVE_CONFIRM_WIN_MS)
            if pos[j_end] - pos[i] >= MOVE_CONFIRM_GAIN_MM:
                j = i
                while j > 0 and spd[j] > 0:
                    j -= 1
                return j + 1 if spd[j] <= 0 else j
    return None


def detect_run_start(spd: list[int], pos: list[int]):
    """Velocity-surge launch out of the walk-out. Returns index or None."""
    n = len(spd)
    ssm = _smooth_int(spd, 25)
    for i in range(n - RUN_SUSTAIN_MS):
        if pos[i] >= RUN_MIN_POS_MM and ssm[i] >= RUN_ARM_MMPS and \
                statistics.fmean(spd[i:i + RUN_SUSTAIN_MS]) >= RUN_ARM_MMPS:
            j = i
            while j > 1 and ssm[j - 1] <= ssm[j]:
                j -= 1
            return j
    return None


def build_rep(path: Path, rep_idx: int, start_foot: str = "left",
              body_mass_kg: float | None = None) -> dict:
    t_ms_raw, load_g, spd_mmps, pos_mm = read_csv(path)
    if len(t_ms_raw) < 100:
        raise ValueError(f"{path.name}: too few samples")

    move_idx = detect_first_movement(spd_mmps, pos_mm)
    run_idx = detect_run_start(spd_mmps, pos_mm)
    start_idx = run_idx if run_idx is not None else (move_idx or 0)
    start_kind = "run_start" if run_idx is not None else (
        "first_movement" if move_idx is not None else "none")

    # Lead-in / zone load context (pre-slice, absolute position)
    lead_samples = [load_g[i] for i in range(len(pos_mm))
                    if LEAD_BAND_MM[0] <= pos_mm[i] <= LEAD_BAND_MM[1]]
    zone_samples = [load_g[i] for i in range(len(pos_mm)) if pos_mm[i] >= ZONE_FROM_MM]
    lead_load_kg = round(statistics.median(lead_samples) / 1000, 1) if lead_samples else None
    zone_load_kg = round(statistics.median(zone_samples) / 1000, 1) if zone_samples else None

    # Slice from the detected start; rep-relative time and position
    t0 = t_ms_raw[start_idx]
    pos0 = pos_mm[start_idx]
    times_s, speeds_mps, forces_n, pos_m = [], [], [], []
    for i in range(start_idx, len(t_ms_raw)):
        times_s.append((t_ms_raw[i] - t0) / 1000.0)
        speeds_mps.append(spd_mmps[i] / 1000.0)
        forces_n.append((load_g[i] / 1000.0) * 9.81)
        pos_m.append((pos_mm[i] - pos0) / 1000.0)

    if len(times_s) < 2:
        raise ValueError(f"{path.name}: no samples past detected start")

    # ---- aggregates (mirrors the load_1080_xlsx builder) ----
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

    accs = []
    for i in range(1, n):
        dt = times_s[i] - times_s[i - 1]
        if dt > 0:
            accs.append((speeds_mps[i] - speeds_mps[i - 1]) / dt)
    peak_a = max(accs) if accs else 0.0
    avg_a = sum(accs) / len(accs) if accs else 0.0

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

    splits = {}
    for marker in (5, 10, 20):
        for i, p in enumerate(pos_m):
            if p >= marker:
                splits[str(marker)] = round(times_s[i], 3)
                break
    splits_extended = {}
    for marker in (5, 10, 20, 30, 40):
        for i, p in enumerate(pos_m):
            if p >= marker:
                splits_extended[str(marker)] = round(times_s[i], 3)
                break

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

    # Same pipeline as the xlsx path: speed-residual detector (primary),
    # force annotation, declared-foot labels, aggregates + L/R asymmetry.
    step_events = detect_steps_speed_residual(times_s, speeds_mps, pos_m)
    step_events = annotate_steps(step_events, times_s, forces_n)
    step_events = label_feet(step_events, start_foot)
    step_aggs = compute_step_aggregates(step_events)
    asymmetry = compute_asymmetry(step_events)
    split_report = build_split_report(
        times_s, speeds_mps, forces_n, powers_w, accs, pos_m, SPLIT_LENGTH_M)

    ttpf_ms = int(times_s[peak_f_idx] * 1000)
    ttps_ms = int(times_s[peak_v_idx] * 1000)

    # Gated single-rep F-V (tether model) from the FULL-RESOLUTION trace —
    # the same fit + validity gate the live path runs, so a direct-DB import
    # carries the identical F-V columns a --post import would get. Relative
    # fields (F0_rel/V0/Pmax_rel/slope/tau) are mass-invariant; absolute
    # F0/Pmax are only computed when a real body mass was given.
    fv_fields = {"f0_n": None, "f0_rel_nkg": None, "v0_mps": None,
                 "pmax_w_morin": None, "pmax_rel_wkg": None,
                 "fv_slope_per_kg": None, "tau_s": None}
    try:
        prof = sprint_model.profile_from_trace(
            times_s[:peak_v_idx + 1], pos_m[:peak_v_idx + 1],
            speeds_mps[:peak_v_idx + 1],
            bodymass=body_mass_kg or 75.0, resisted=True)
        if prof.get("ok") and prof.get("valid"):
            fvp, m = prof["fvp"], prof["model"]
            fv_fields.update(
                f0_rel_nkg=round(fvp["F0_rel"], 2),
                v0_mps=round(fvp["V0"], 2),
                pmax_rel_wkg=round(fvp["Pmax_rel"], 2),
                fv_slope_per_kg=(round(fvp["FV_slope"], 3)
                                 if fvp.get("FV_slope") else None),
                tau_s=round(m["TAU"], 3))
            if body_mass_kg:
                fv_fields["f0_n"] = round(fvp["F0_rel"] * body_mass_kg, 1)
                fv_fields["pmax_w_morin"] = round(fvp["Pmax_rel"] * body_mass_kg, 1)
    except Exception:
        pass  # a failed fit imports as NULLs, never as a wrong number

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

    drill = (f"Resisted sprint {zone_load_kg:.0f} kg"
             if zone_load_kg is not None and start_kind == "run_start"
             else "HMI sprint")

    return {
        "rep_idx": rep_idx,
        "duration_s": round(duration_s, 3),
        "max_extension_m": round(max_extension_m, 3),
        "total_distance_m": round(max_extension_m, 3),
        "total_time_s": round(duration_s, 3),
        "peak_speed_mps": round(peak_v, 3),
        "top_speed_mps": round(peak_v, 3),
        "peak_force_n": round(peak_f, 1),
        "peak_power_w": round(peak_p, 1),
        "peak_power_rel_wkg": (round(peak_p / body_mass_kg, 2) if body_mass_kg else None),
        "peak_acceleration_mps2": round(peak_a, 3),
        "peak_torque_pct": None,
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
        "drill": drill,
        "splits_s": splits,
        "splits_s_extended": splits_extended,
        "split_report": split_report,
        "rf_max_pct": None,
        "drf": None,
        "time_to_max_v_s": round(time_to_max_v_s, 3),
        "dist_to_max_v_m": round(dist_to_max_v_m, 2),
        "time_to_90pct_v_s": round(time_to_90pct_v_s, 3) if time_to_90pct_v_s is not None else None,
        "dist_to_90pct_v_m": round(dist_to_90pct_v_m, 2) if dist_to_90pct_v_m is not None else None,
        "v_dropoff_pct": round(v_dropoff_pct, 1),
        "step_events": step_events,
        **step_aggs,
        "asymmetry": asymmetry,
        "samples": samples,
        # Gated tether-model F-V (NULLs when the fit failed the gate).
        **fv_fields,
        "_meta": {
            "source": "hmi_raw_csv",
            "file": path.name,
            "body_mass_kg": body_mass_kg,
            "raw_sample_count": len(t_ms_raw),
            "sprint_start_index": start_idx,
            "start_kind": start_kind,
            "start_t_ms": t0,
            "start_pos_m": round(pos0 / 1000, 3),
            "entry_v_mps": round(spd_mmps[start_idx] / 1000, 2),
            "first_movement_t_ms": (t_ms_raw[move_idx] if move_idx is not None else None),
            "lead_load_kg": lead_load_kg,
            "zone_load_kg": zone_load_kg,
        },
    }


def _rep_sort_key(path: Path):
    m = re.search(r"rep(\d+)", path.name)
    return int(m.group(1)) if m else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="CSV paths or glob patterns")
    ap.add_argument("--athlete-id", type=int, default=None)
    ap.add_argument("--db", default=None, help="DB path (default: persistence.DB_PATH)")
    ap.add_argument("--post", action="store_true",
                    help="POST to the running service instead of writing the DB directly")
    ap.add_argument("--dry-run", action="store_true", help="detect + print, no write")
    ap.add_argument("--note", default=None, help="extra text for the session notes")
    ap.add_argument("--start-foot", default="left", choices=["left", "right"],
                    help="declared first-contact foot for L/R step labels")
    ap.add_argument("--body-mass", type=float, default=None,
                    help="athlete body mass kg (enables F-V + relative power; "
                         "stored on the athlete row if not already set)")
    args = ap.parse_args()

    paths = []
    for pattern in args.files:
        expanded = glob.glob(pattern)
        paths.extend(Path(p) for p in (expanded or [pattern]))
    paths = sorted(set(paths), key=_rep_sort_key)
    if not paths:
        raise SystemExit("no files matched")

    reps = []
    for i, p in enumerate(paths, start=1):
        rep = build_rep(p, i, start_foot=args.start_foot, body_mass_kg=args.body_mass)
        m = rep["_meta"]
        s5 = rep["splits_s"].get("5")
        s10 = rep["splits_s"].get("10")
        print(f"  {p.name}: {m['start_kind']} @ {m['start_t_ms']/1000:.2f}s "
              f"({m['start_pos_m']:.2f} m, entry {m['entry_v_mps']:.2f} m/s) | "
              f"zone {m['zone_load_kg']} kg | 5m {s5} s | 10m {s10} s | "
              f"peak {rep['peak_speed_mps']:.2f} m/s | {rep['total_distance_m']:.1f} m")
        reps.append(rep)

    if args.dry_run:
        print(f"\n(dry run) {len(reps)} reps parsed, nothing written")
        return

    if args.athlete_id is None:
        raise SystemExit("--athlete-id required unless --dry-run")

    if args.post:
        body = json.dumps({"reps": reps, "athlete_id": args.athlete_id}).encode()
        req = urllib.request.Request(
            f"{SERVICE_URL}/api/c/dev/load_rep", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            print(json.loads(r.read()))
        return

    import persistence
    db_path = args.db or persistence.DB_PATH
    conn = persistence.open_db(db_path)
    persistence.init_schema(conn)  # idempotent — ensures new columns exist
    date_tag = paths[0].name[:10]
    notes = f"hmi_raw_import | {date_tag} | {len(reps)} reps"
    if args.note:
        notes += f" | {args.note}"
    session_id = None
    for rep in reps:
        out = persistence.import_rep(
            conn, args.athlete_id, rep, source="hmi_raw_csv",
            session_notes=notes, session_id=session_id)
        session_id = out["session_id"]
    conn.close()
    print(f"\nimported {len(reps)} reps into session {session_id} "
          f"(athlete {args.athlete_id}, db {db_path})")


if __name__ == "__main__":
    main()
