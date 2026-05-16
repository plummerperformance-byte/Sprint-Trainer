"""Tests for the SQLite -> PPA/1080 adapter."""
import json

import pytest

import persistence
from ppa.adapters.sqlite_to_ppa import load_motion, load_session
from ppa.models import onezeroeightzero as o


def _fresh_db():
    conn = persistence.open_db(":memory:")
    persistence.init_schema(conn)
    return conn


def _seed_full(conn):
    conn.execute("INSERT INTO athletes(id, name) VALUES (1, 'Test Athlete')")
    conn.execute(
        "INSERT INTO sessions(id, athlete_id, started_at, ended_at, hmi_load_kg) "
        "VALUES (1, 1, '2026-05-16T10:00:00', '2026-05-16T10:30:00', 6.0)"
    )
    samples = [
        {"t_ms": 0, "v_mps": 0.0, "F_N": 0.0, "P_W": 0.0, "pos_m": 0.0, "a_mps2": 0.0},
        {"t_ms": 500, "v_mps": 4.0, "F_N": 120.0, "P_W": 480.0, "pos_m": 1.5, "a_mps2": 8.0},
    ]
    conn.execute(
        "INSERT INTO reps(id, session_id, started_at, ended_at, "
        "started_t_offset_ms, ended_t_offset_ms, peak_speed_mps, avg_speed_mps, "
        "peak_force_n, peak_power_w, work_j, max_extension_m, is_eccentric, "
        "samples_json, comment) "
        "VALUES (1, 1, '2026-05-16T10:05:00', '2026-05-16T10:05:03', 0, 3000, "
        "8.6, 5.1, 420.0, 809.0, 250.0, 15.0, 0, ?, ?)",
        (json.dumps(samples), "good rep"),
    )
    conn.commit()


def test_load_session():
    conn = _fresh_db()
    _seed_full(conn)
    sess = load_session(conn, 1)
    assert sess.common.Guid == "1"
    assert sess.client_guid == "1"
    assert sess.common.Created is not None


def test_load_motion():
    conn = _fresh_db()
    _seed_full(conn)
    m = load_motion(conn, 1)
    assert m.common.Guid == "1"
    assert m.motion_group_guid == "1"
    assert m.node_no == 1
    assert m.comment == "good rep"
    assert m.values.peak_speed == pytest.approx(8.6)
    assert m.values.avg_speed == pytest.approx(5.1)
    assert m.values.duration == pytest.approx(3.0)
    assert m.values.distance == pytest.approx(15.0)
    assert m.resistance.con_mass == pytest.approx(6.0)
    assert len(m.samples.data) == 2
    assert m.samples.data[1].speed == pytest.approx(4.0)
    assert m.samples.data[1].position == pytest.approx(1.5)


def test_strict_export_validates_against_proto():
    """Dropping ppa_* fields must yield something the bare 1080 proto model
    accepts — the compatibility guarantee."""
    conn = _fresh_db()
    _seed_full(conn)
    m = load_motion(conn, 1)
    dumped = m.model_dump(mode="json")
    strict = {k: v for k, v in dumped.items() if not k.startswith("ppa_")}
    o.Motion.model_validate(strict)  # raises on any drift


def test_legacy_rpm_fallback():
    """Old rows with only peak_speed_rpm convert to m/s."""
    conn = _fresh_db()
    conn.execute("INSERT INTO athletes(id, name) VALUES (1, 'A')")
    conn.execute("INSERT INTO sessions(id, athlete_id, started_at) "
                 "VALUES (1, 1, '2026-05-16T10:00:00')")
    conn.execute(
        "INSERT INTO reps(id, session_id, started_at, started_t_offset_ms, "
        "peak_speed_rpm) VALUES (1, 1, '2026-05-16T10:05:00', 0, 1000)"
    )
    conn.commit()
    import analytics
    m = load_motion(conn, 1)
    assert m.values.peak_speed == pytest.approx(1000 * analytics.MPS_PER_RPM)


def test_missing_ids_raise():
    conn = _fresh_db()
    with pytest.raises(ValueError):
        load_session(conn, 999)
    with pytest.raises(ValueError):
        load_motion(conn, 999)
