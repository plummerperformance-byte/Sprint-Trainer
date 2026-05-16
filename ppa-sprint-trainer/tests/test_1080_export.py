"""Round-trip test for the 1080-shaped session export.

A strict export (ppa_* stripped) must parse cleanly against the bare 1080
proto models — this is the auditable proof of "1080-compatible".
"""
import json

import pytest

import persistence
from ppa.cli.export import export_session
from ppa.models import onezeroeightzero as o


def _db_with_session():
    conn = persistence.open_db(":memory:")
    persistence.init_schema(conn)
    conn.execute("INSERT INTO athletes(id, name) VALUES (1, 'Test Athlete')")
    conn.execute(
        "INSERT INTO sessions(id, athlete_id, started_at, ended_at, hmi_load_kg) "
        "VALUES (1, 1, '2026-05-16T10:00:00', '2026-05-16T10:30:00', 6.0)"
    )
    samples = [
        {"t_ms": 0, "v_mps": 0.0, "F_N": 0.0, "P_W": 0.0, "pos_m": 0.0, "a_mps2": 0.0},
        {"t_ms": 500, "v_mps": 4.0, "F_N": 120.0, "P_W": 480.0, "pos_m": 1.5, "a_mps2": 8.0},
    ]
    # Two reps across two sets.
    for rep_id, set_idx in ((1, 1), (2, 2)):
        conn.execute(
            "INSERT INTO reps(id, session_id, set_idx, started_at, ended_at, "
            "started_t_offset_ms, ended_t_offset_ms, peak_speed_mps, peak_force_n, "
            "peak_power_w, work_j, max_extension_m, samples_json) "
            "VALUES (?, 1, ?, '2026-05-16T10:05:00', '2026-05-16T10:05:03', "
            "0, 3000, 8.6, 420.0, 809.0, 250.0, 15.0, ?)",
            (rep_id, set_idx, json.dumps(samples)),
        )
    conn.commit()
    return conn


def test_export_structure():
    conn = _db_with_session()
    doc = export_session(conn, 1)
    assert set(doc) == {"session", "sets"}
    assert doc["session"]["common"]["Guid"] == "1"
    assert len(doc["sets"]) == 2  # two set_idx groups
    motion = doc["sets"][0]["motion_groups"][0]["motions"][0]
    assert motion["values"]["peak_speed"] == pytest.approx(8.6)
    assert len(motion["samples"]["data"]) == 2


def test_strict_drops_ppa_fields():
    conn = _db_with_session()
    doc = export_session(conn, 1, strict=True)
    blob = json.dumps(doc)
    assert "ppa_" not in blob, "strict export still contains ppa_* fields"
    # non-strict keeps them
    assert "ppa_" in json.dumps(export_session(conn, 1, strict=False))


def test_strict_export_validates_against_1080_models():
    """The compatibility guarantee: strict output parses as pure 1080 proto."""
    conn = _db_with_session()
    doc = export_session(conn, 1, strict=True)
    o.Session.model_validate(doc["session"])
    for s in doc["sets"]:
        o.Set.model_validate(s)
        for mg in s["motion_groups"]:
            o.MotionGroup.model_validate(mg)
            for m in mg["motions"]:
                o.Motion.model_validate(m)
