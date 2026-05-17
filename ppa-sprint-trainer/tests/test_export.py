"""Tests for the 1080-compatible export layer (JSON + CSV)."""
import json

import pytest

import persistence
from ppa.analytics.derived import derive_1080_export_fields
from ppa.adapters.legacy_to_v2 import load_motion
from ppa.cli.export import render
from ppa.export.csv_1080 import (
    export_session_csv_samples,
    export_session_csv_summary,
)
from ppa.export.json_1080 import build_session_doc
from ppa.models import onezeroeightzero as o

SAMPLES = [
    {"t_ms": 0, "v_mps": 0.0, "F_N": 0.0, "P_W": 0.0, "pos_m": 0.0, "a_mps2": 0.0},
    {"t_ms": 400, "v_mps": 5.0, "F_N": 150.0, "P_W": 750.0, "pos_m": 2.0, "a_mps2": 9.0},
    {"t_ms": 900, "v_mps": 8.5, "F_N": 80.0, "P_W": 680.0, "pos_m": 8.0, "a_mps2": 2.0},
    {"t_ms": 1500, "v_mps": 6.0, "F_N": 40.0, "P_W": 240.0, "pos_m": 12.0, "a_mps2": -3.0},
]


def _db():
    conn = persistence.open_db(":memory:")
    persistence.init_schema(conn)
    conn.execute("INSERT INTO athletes(id, name) VALUES (1, 'Adam Plummer')")
    conn.execute(
        "INSERT INTO sessions(id, athlete_id, started_at, hmi_load_kg) "
        "VALUES (1, 1, '2026-05-16T10:00:00', 8.0)"
    )
    for rep_id, set_idx in ((1, 1), (2, 2)):
        conn.execute(
            "INSERT INTO reps(id, session_id, set_idx, started_at, ended_at, "
            "started_t_offset_ms, ended_t_offset_ms, peak_speed_mps, "
            "peak_force_n, peak_power_w, work_j, max_extension_m, samples_json) "
            "VALUES (?, 1, ?, '2026-05-16T10:05:00', '2026-05-16T10:05:02', "
            "0, 2000, 8.5, 150.0, 750.0, 200.0, 12.0, ?)",
            (rep_id, set_idx, json.dumps(SAMPLES)),
        )
    conn.commit()
    return conn


# --- JSON ------------------------------------------------------------------

def test_json_strict_validates_against_1080_models():
    conn = _db()
    doc = build_session_doc(conn, 1, strict=True)
    o.Session.model_validate(doc["session"])
    for s in doc["sets"]:
        o.Set.model_validate(s)
        for mg in s["motion_groups"]:
            o.MotionGroup.model_validate(mg)
            for m in mg["motions"]:
                o.Motion.model_validate(m)
    assert "ppa_" not in json.dumps(doc)


def test_json_nonstrict_keeps_ppa_fields():
    conn = _db()
    assert "ppa_" in json.dumps(build_session_doc(conn, 1, strict=False))


# --- CSV summary -----------------------------------------------------------

def test_csv_summary_strict_header_is_byte_exact_to_1080():
    conn = _db()
    csv = export_session_csv_summary(conn, 1, strict=True)
    assert csv.splitlines()[0] == \
        "Set;RepNumber;RepId;RepTime;TopSpeed;TotalTime;Distance"


def test_csv_summary_has_one_row_per_rep():
    conn = _db()
    rows = [l for l in export_session_csv_summary(conn, 1, strict=True).splitlines()
            if l][1:]
    assert len(rows) == 2


def test_csv_summary_extended_adds_columns_and_note():
    conn = _db()
    csv = export_session_csv_summary(conn, 1, strict=False)
    assert csv.startswith("#")  # honesty comment block
    header = next(l for l in csv.splitlines() if not l.startswith("#"))
    for col in ("AvgSpeed", "MaxAccel0.5s", "Best5m", "Best10m", "Direction"):
        assert col in header


# --- CSV samples -----------------------------------------------------------

def test_csv_samples_strict_header_and_numeric_rows():
    conn = _db()
    csv = export_session_csv_samples(conn, 1, strict=True)
    assert csv.splitlines()[0] == "Set;RepNumber;Time;Position;Speed;Force"
    rows = [l for l in csv.splitlines()[1:] if l]
    assert len(rows) == len(SAMPLES) * 2  # two reps
    for r in rows:
        cells = r.split(";")
        assert len(cells) == 6
        for c in cells:
            assert c != "" and float(c) == float(c)  # numeric, no None strings


def test_csv_samples_extended_has_acceleration_and_power():
    conn = _db()
    header = next(l for l in export_session_csv_samples(conn, 1, strict=False)
                  .splitlines() if not l.startswith("#"))
    assert "Acceleration" in header and "Power" in header


# --- derived fields --------------------------------------------------------

def test_derived_export_fields():
    conn = _db()
    d = derive_1080_export_fields(load_motion(conn, 1))
    assert set(d) == {"avg_speed_mps", "max_accel_0p5s_mps2",
                      "best_5m_split_s", "best_10m_split_s"}
    assert d["best_5m_split_s"] is not None      # run covers >5 m
    assert d["max_accel_0p5s_mps2"] > 0


# --- CLI dispatch ----------------------------------------------------------

@pytest.mark.parametrize("fmt", ["json", "csv-summary", "csv-samples"])
def test_cli_render_every_format(fmt):
    conn = _db()
    assert render(conn, 1, fmt, strict=True).strip()
    assert render(conn, 1, fmt, strict=False).strip()
