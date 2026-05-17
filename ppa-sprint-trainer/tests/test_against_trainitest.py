"""Harness — import a Trainitest LocalDb.sqlite into the v2 storage models.

Runs against a synthetic Trainitest DB built from the vendored DDL
(``vendor/1080-api/LocalDb_schema.sql``). When a real captured
``vendor/1080-api/reference_sessions/reference_localdb.sqlite`` is present
(brief Phase 5.1–5.3), the fixture test also imports every session from it.
"""
import os
import re
import sqlite3

import pytest

from ppa.codecs.sample_bytes import encode_sample_bytes
from ppa.models.storage import MotionStorage, ResistanceMode
from ppa.adapters.trainitest_to_v2 import (
    import_motion,
    import_session_motions,
)

_DDL = "vendor/1080-api/LocalDb_schema.sql"
_REF_DB = "vendor/1080-api/reference_sessions/reference_localdb.sqlite"

_BLOB = encode_sample_bytes([
    {"time": 0.0, "position": 0.0, "speed": 0.0,
     "acceleration": 0.0, "force": 0.0, "power": 0.0},
    {"time": 0.4, "position": 2.0, "speed": 5.0,
     "acceleration": 9.0, "force": 150.0, "power": 750.0},
])

# NOT NULL columns of the Trainitest Motion table.
_MOTION_REQUIRED = dict(
    IsEccentric=0, NodeNo=1, ConMass=8.0, EccMass=0.0, Mode=1, Gear=1,
    ConSpeed=12.0, EccSpeed=8.0, IsBoostActive=0, BoostLoad=0.0,
    TrimType=0, RowVersion=1, LocalDbRowVersion=1, IsLocallyEdited=0,
    IsLocallyDeleted=0, CreatedUTC=1779000000, CreatedOffset=600,
)


def _trainitest_db() -> sqlite3.Connection:
    """An empty in-memory DB carrying the Trainitest schema."""
    ddl = open(_DDL, encoding="utf-8").read()
    # sqlite_sequence is auto-managed — its explicit CREATE cannot be replayed.
    ddl = re.sub(r"CREATE TABLE sqlite_sequence\([^)]*\);", "", ddl)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ddl)
    return conn


def _insert(conn, table, **cols):
    names = ", ".join(f'"{c}"' for c in cols)
    qs = ", ".join("?" for _ in cols)
    conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({qs})', list(cols.values()))


def _seed_motion(conn, guid="11111111-1111-1111-1111-111111111111",
                 motion_group_guid="22222222-2222-2222-2222-222222222222",
                 machine_guid="33333333-3333-3333-3333-333333333333"):
    _insert(conn, "Motion", GUID=guid, MotionGroupGUID=motion_group_guid,
            MachineGUID=machine_guid, Data=_BLOB, PeakSpeed=8.5, AvgSpeed=5.1,
            PeakForce=420.0, Work=250.0, Distance=15.0, TopSpeed=8.5,
            **_MOTION_REQUIRED)
    conn.commit()
    return guid


def test_ddl_builds_full_schema():
    conn = _trainitest_db()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("Motion", "MotionGroup", "Set", "Exercise", "Session",
                     "Client", "ExerciseType"):
        assert expected in tables


def test_import_motion_maps_fields():
    conn = _trainitest_db()
    guid = _seed_motion(conn)
    m = import_motion(conn, guid)
    assert isinstance(m, MotionStorage)
    assert str(m.guid) == guid
    assert m.con_mass == 8.0
    assert m.mode == ResistanceMode.NFW          # Trainitest Mode=1
    assert m.gear == 1
    assert m.peak_speed == 8.5
    assert m.is_eccentric is False
    assert m.node_no == 1
    assert m.data == _BLOB
    assert m.sync.created_offset_minutes == 600
    assert m.sync.created_utc.year == 2026       # 1779000000 -> 2026 (epoch s)


def test_import_is_deterministic():
    conn = _trainitest_db()
    guid = _seed_motion(conn)
    assert import_motion(conn, guid) == import_motion(conn, guid)


def test_import_strict_export_validates_against_1080():
    """Imported storage, ppa_* stripped, is pure 1080 schema."""
    from ppa.models import onezeroeightzero as o
    conn = _trainitest_db()
    m = import_motion(conn, _seed_motion(conn))
    dumped = m.model_dump(mode="json")
    strict = {k: v for k, v in dumped.items() if not k.startswith("ppa_")}
    # The aggregate sub-set maps straight onto the 1080 proto AggregatedValues.
    o.AggregatedValues.model_validate({
        "peak_speed": strict["peak_speed"], "avg_speed": strict["avg_speed"],
        "peak_force": strict["peak_force"], "work": strict["work"],
        "distance": strict["distance"], "duration": strict["duration"] or 0.0,
    })


def test_import_session_motions_walks_the_hierarchy():
    conn = _trainitest_db()
    sess = "aaaaaaaa-0000-0000-0000-000000000001"
    exid = "bbbbbbbb-0000-0000-0000-000000000001"
    setid = "cccccccc-0000-0000-0000-000000000001"
    mg = "dddddddd-0000-0000-0000-000000000001"
    _sync = dict(RowVersion=1, LocalDbRowVersion=1, IsLocallyEdited=0,
                 IsLocallyDeleted=0, CreatedUTC=1779000000, CreatedOffset=600)
    _insert(conn, "Session", GUID=sess, ClientGUID="e0000000-0000-0000-0000-000000000001",
            IsTest=0, **_sync)
    _insert(conn, "Exercise", GUID=exid, SessionGUID=sess,
            ExerciseTypeGUID="f0000000-0000-0000-0000-000000000001",
            IsTest=0, SortIndex=0, **_sync)
    _insert(conn, "Set", GUID=setid, ExerciseGUID=exid, BargraphVisibility=0,
            IsAverageVisible=0, IsSplitBarSumOrAvgActive=0, ExternalLoad=0,
            Unit=0, ChildrenRowVersionSum=0, **_sync)
    _insert(conn, "MotionGroup", GUID=mg, SetGUID=setid, IsCurveSelected=0,
            IsFvSelected=0, Side=2, **_sync)
    _seed_motion(conn, motion_group_guid=mg)
    motions = import_session_motions(conn, sess)
    assert len(motions) == 1
    assert isinstance(motions[0], MotionStorage)


@pytest.mark.skipif(not os.path.exists(_REF_DB),
                    reason="no captured Trainitest reference DB (Phase 5.1-5.3)")
def test_reference_sessions_import_cleanly():
    conn = sqlite3.connect(_REF_DB)
    conn.row_factory = sqlite3.Row
    sessions = [r["GUID"] for r in conn.execute("SELECT GUID FROM Session")]
    assert sessions, "reference DB has no sessions"
    for sguid in sessions:
        for motion in import_session_motions(conn, sguid):
            assert isinstance(motion, MotionStorage)
