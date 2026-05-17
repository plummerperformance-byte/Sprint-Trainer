"""Tests for the legacy -> v2 storage migration adapter.

The repo's ``ppa.db`` carries no real sessions in this environment, so these
tests seed a synthetic legacy rep in an in-memory DB. The adapter logic
exercised is identical to what runs against a populated ``ppa.db``.
"""
import json

import pytest

import persistence
from ppa.adapters.legacy_to_v2 import (
    load_motion,
    load_session,
    load_v2_motion,
    save_motion,
)
from ppa.codecs.sample_bytes import decode_sample_bytes

MIGRATION = "migrations/002_storage_schema.sql"

SAMPLES = [
    {"t_ms": 0, "v_mps": 0.0, "F_N": 0.0, "P_W": 0.0, "pos_m": 0.0, "a_mps2": 0.0},
    {"t_ms": 500, "v_mps": 4.0, "F_N": 120.0, "P_W": 480.0, "pos_m": 1.5, "a_mps2": 8.0},
    {"t_ms": 1000, "v_mps": 7.2, "F_N": 90.0, "P_W": 648.0, "pos_m": 6.0, "a_mps2": 3.0},
]


def _db():
    """In-memory DB carrying both the legacy schema and the v2_* tables."""
    conn = persistence.open_db(":memory:")
    persistence.init_schema(conn)
    with open(MIGRATION, encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


def _seed(conn):
    conn.execute("INSERT INTO athletes(id, name) VALUES (1, 'Test Athlete')")
    conn.execute(
        "INSERT INTO sessions(id, athlete_id, started_at, ended_at, hmi_load_kg) "
        "VALUES (1, 1, '2026-05-16T10:00:00', '2026-05-16T10:30:00', 8.0)"
    )
    conn.execute(
        "INSERT INTO reps(id, session_id, started_at, ended_at, "
        "started_t_offset_ms, ended_t_offset_ms, peak_speed_mps, avg_speed_mps, "
        "peak_force_n, peak_power_w, peak_acceleration_mps2, work_j, "
        "max_extension_m, is_eccentric, samples_json) "
        "VALUES (1, 1, '2026-05-16T10:05:00', '2026-05-16T10:05:03', 0, 3000, "
        "7.2, 4.1, 420.0, 809.0, 9.5, 250.0, 15.0, 0, ?)",
        (json.dumps(SAMPLES),),
    )
    conn.commit()


def test_load_session():
    conn = _db()
    _seed(conn)
    s = load_session(conn, 1)
    assert str(s.guid)
    assert s.title is None          # seeded session has no notes
    assert s.is_test is False
    assert s.sync.created_utc.year == 2026


def test_load_motion_maps_aggregates():
    conn = _db()
    _seed(conn)
    m = load_motion(conn, 1)
    assert m.peak_speed == pytest.approx(7.2)
    assert m.peak_force == pytest.approx(420.0)
    assert m.work == pytest.approx(250.0)
    assert m.duration == pytest.approx(3.0)
    assert m.con_mass == pytest.approx(8.0)   # from session HMI load
    assert m.node_no == 1
    # The legacy JSON samples re-encode to the 1080 wire blob.
    assert m.data is not None
    decoded = decode_sample_bytes(m.data)
    assert len(decoded) == 3
    assert decoded[2]["speed"] == pytest.approx(7.2)


def test_round_trip_load_save_load():
    """load (legacy) -> save (v2_motions) -> load (v2_motions) is identity."""
    conn = _db()
    _seed(conn)
    m1 = load_motion(conn, 1)
    save_motion(conn, m1)
    m2 = load_v2_motion(conn, m1.guid)
    assert m2 == m1


def test_gap_fields_are_none_and_documented():
    conn = _db()
    _seed(conn)
    m = load_motion(conn, 1)
    gap_doc = open("docs/legacy_to_v2_gaps.md", encoding="utf-8").read()
    # Every field with no legacy source must be None/default AND in the report.
    for field in ("ecc_speed", "boost_load", "mass_at_v0", "speed_v1",
                  "start_position", "total_distance", "ppa_rpe",
                  "ppa_environment"):
        assert getattr(m, field) in (None, 0.0), f"{field} unexpectedly populated"
        assert field in gap_doc, f"{field} missing from the gap report"


def test_deterministic_guids():
    """The same legacy id always yields the same v2 GUID — idempotent migration."""
    conn = _db()
    _seed(conn)
    assert load_motion(conn, 1).guid == load_motion(conn, 1).guid


def test_missing_ids_raise():
    conn = _db()
    with pytest.raises(ValueError):
        load_motion(conn, 999)
    with pytest.raises(ValueError):
        load_session(conn, 999)
