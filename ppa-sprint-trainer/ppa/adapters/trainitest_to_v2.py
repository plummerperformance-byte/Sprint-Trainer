"""Import a Trainitest ``LocalDb.sqlite`` directly into the v2 storage models.

Trainitest (the 1080 desktop client) keeps its data in a 28-table EF Core
SQLite cache. The v2 storage models in :mod:`ppa.models.storage` were built to
mirror that schema, so the import is a near-mechanical column map:
PascalCase SQL columns -> snake_case model fields, with the shared sync
columns (``RowVersion`` / ``CreatedUTC`` / ``CreatedOffset`` / …) folded into
a :class:`~ppa.models.storage.SyncEnvelope`.

Read-only against the Trainitest DB. The vendored schema lives at
``vendor/1080-api/LocalDb_schema.sql``.

KNOWN UNKNOWN — ``CreatedUTC`` and friends are ``INTEGER`` in the EF schema;
the exact encoding (epoch seconds / epoch ms / .NET ticks) cannot be confirmed
without a populated row. :func:`_to_datetime` applies range heuristics and is
flagged for confirmation against a real captured session (brief Phase 5.4).

Australian English throughout.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from ppa.models import storage as s
from ppa.models.storage import SyncEnvelope

#: Trainitest sync columns -> SyncEnvelope field names.
_SYNC_MAP = {
    "RowVersion": "row_version",
    "LocalDbRowVersion": "local_db_row_version",
    "IsLocallyEdited": "is_locally_edited",
    "IsLocallyDeleted": "is_locally_deleted",
    "CreatedUTC": "created_utc",
    "CreatedOffset": "created_offset_minutes",
    "EditedUTC": "edited_utc",
    "EditedOffset": "edited_offset_minutes",
    "DeletedUTC": "deleted_utc",
    "DeletedOffset": "deleted_offset_minutes",
}
_SYNC_COLS = set(_SYNC_MAP)

#: Trainitest table -> v2 storage model.
TABLE_MODEL = {
    "Instructor": s.InstructorStorage,
    "Client": s.ClientStorage,
    "ClientMeasurement": s.ClientMeasurementStorage,
    "ClientPerformanceMeasurement": s.ClientPerformanceMeasurementStorage,
    "Group": s.GroupStorage,
    "GroupXClient": s.GroupXClientStorage,
    "Machine": s.MachineStorage,
    "ControlDevice": s.ControlDeviceStorage,
    "ExerciseArchType": s.ExerciseArchTypeStorage,
    "ExerciseType": s.ExerciseTypeStorage,
    "Protocol": s.ProtocolStorage,
    "ProtocolXExerciseType": s.ProtocolXExerciseTypeStorage,
    "GroupSession": s.GroupSessionStorage,
    "Session": s.SessionStorage,
    "Exercise": s.ExerciseStorage,
    "Set": s.SetStorage,
    "SetData": s.SetDataStorage,
    "MotionGroup": s.MotionGroupStorage,
    "Motion": s.MotionStorage,
    "ActiveSession": s.ActiveSessionStorage,
    "ActiveSessionExercise": s.ActiveSessionExerciseStorage,
    "ActiveSessionParticipant": s.ActiveSessionParticipantStorage,
    "ActiveSessionResult": s.ActiveSessionResultStorage,
    "ActiveSessionTarget": s.ActiveSessionTargetStorage,
}


def _snake(name: str) -> str:
    """PascalCase / acronym-laden SQL column -> snake_case field name."""
    out = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    out = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", out)
    return out.lower()


def _to_datetime(value) -> datetime | None:
    """Best-effort decode of a Trainitest UTC field.

    The EF schema stores these as INTEGER; the encoding is unconfirmed, so
    range heuristics are used. Confirm against a captured row (Phase 5.4)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        v = int(value)
        if v > 10 ** 16:                       # .NET ticks (100 ns since year 1)
            return datetime(1, 1, 1) + timedelta(microseconds=v // 10)
        if v > 10 ** 11:                       # epoch milliseconds
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).replace(tzinfo=None)
        return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _sync_from_row(row: dict) -> SyncEnvelope:
    return SyncEnvelope(
        row_version=row.get("RowVersion") or 0,
        local_db_row_version=row.get("LocalDbRowVersion") or 0,
        is_locally_edited=bool(row.get("IsLocallyEdited")),
        is_locally_deleted=bool(row.get("IsLocallyDeleted")),
        created_utc=_to_datetime(row.get("CreatedUTC")) or datetime(1970, 1, 1),
        created_offset_minutes=row.get("CreatedOffset") or 0,
        edited_utc=_to_datetime(row.get("EditedUTC")),
        edited_offset_minutes=row.get("EditedOffset"),
        deleted_utc=_to_datetime(row.get("DeletedUTC")),
        deleted_offset_minutes=row.get("DeletedOffset"),
    )


def _build(model_cls, row: dict):
    """Map a Trainitest row dict onto its v2 storage model."""
    fields = model_cls.model_fields
    kwargs = {"sync": _sync_from_row(row)}
    for col, val in row.items():
        if col in _SYNC_COLS:
            continue
        snake = _snake(col)
        if snake in fields:
            kwargs[snake] = val
    return model_cls(**kwargs)


def import_entity(conn: sqlite3.Connection, table: str, guid: str):
    """Import one row of any Trainitest table as its v2 storage model."""
    if table not in TABLE_MODEL:
        raise ValueError(f"unknown Trainitest table {table!r}")
    row = conn.execute(
        f'SELECT * FROM "{table}" WHERE GUID = ?', (str(guid),)
    ).fetchone()
    if row is None:
        raise ValueError(f"{table} {guid} not found")
    return _build(TABLE_MODEL[table], dict(row))


# Convenience wrappers for the training-data hierarchy.
def import_motion(conn, guid) -> s.MotionStorage:
    return import_entity(conn, "Motion", guid)


def import_motion_group(conn, guid) -> s.MotionGroupStorage:
    return import_entity(conn, "MotionGroup", guid)


def import_set(conn, guid) -> s.SetStorage:
    return import_entity(conn, "Set", guid)


def import_exercise(conn, guid) -> s.ExerciseStorage:
    return import_entity(conn, "Exercise", guid)


def import_session(conn, guid) -> s.SessionStorage:
    return import_entity(conn, "Session", guid)


def import_client(conn, guid) -> s.ClientStorage:
    return import_entity(conn, "Client", guid)


def import_session_motions(conn: sqlite3.Connection,
                           session_guid: str) -> list[s.MotionStorage]:
    """Every motion of a session, walking Session→Exercise→Set→MotionGroup→Motion."""
    rows = conn.execute(
        """SELECT m.GUID AS g FROM Motion m
             JOIN MotionGroup mg ON mg.GUID = m.MotionGroupGUID
             JOIN "Set" st       ON st.GUID = mg.SetGUID
             JOIN Exercise e     ON e.GUID  = st.ExerciseGUID
            WHERE e.SessionGUID = ?
            ORDER BY m.CreatedUTC""",
        (str(session_guid),),
    ).fetchall()
    return [import_motion(conn, r["g"]) for r in rows]
