"""Migrate existing PPA storage (the legacy ``reps`` / ``sessions`` tables)
onto the v2 storage models — read-only against the legacy tables.

A legacy rep maps to a :class:`~ppa.models.storage.MotionStorage` (and,
structurally, to one ``MotionGroup`` wrapping it). Legacy rows have integer
ids; v2 entities key on GUIDs, so a *deterministic* UUID is derived from each
legacy id (``uuid5``) — the same legacy row always yields the same GUID, which
keeps migration idempotent and round-trips stable.

Unit conversions:
  * legacy ``reps.peak_speed_rpm`` -> m/s via ``analytics.MPS_PER_RPM``
    (modern rows store ``peak_speed_mps`` directly — preferred).
  * legacy ``reps.total_distance_counts`` -> metres via
    ``analytics.COUNTS_PER_METRE``.
  * ``*_t_offset_ms`` -> seconds.
  * ``reps.samples_json`` ({t_ms, v_mps, F_N, P_W, pos_m, a_mps2}) ->
    the 1080 5-float wire blob via ``ppa.codecs.sample_bytes``.

Fields with no legacy source are left at ``None`` / defaults and recorded in
``docs/legacy_to_v2_gaps.md``.

Australian English throughout.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import UUID, uuid5

import analytics

from ppa.codecs.sample_bytes import encode_sample_bytes
from ppa.models.storage import (
    MotionStorage,
    ResistanceMode,
    SessionStorage,
    SyncEnvelope,
)

#: Fixed namespace for deriving stable v2 GUIDs from legacy integer ids.
PPA_LEGACY_NS = UUID("0a9f1e2c-3b4d-5e6f-7a8b-9c0d1e2f3a4b")


def legacy_guid(kind: str, legacy_id: int) -> UUID:
    """Deterministic v2 GUID for a legacy row, e.g. ``legacy_guid("rep", 42)``."""
    return uuid5(PPA_LEGACY_NS, f"{kind}:{legacy_id}")


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _sync_from(created, edited) -> SyncEnvelope:
    """Build a sync envelope from legacy timestamps. The legacy schema has no
    timezone offset, so ``created_offset_minutes`` is 0 (a documented gap)."""
    return SyncEnvelope(
        created_utc=_ts(created) or datetime(1970, 1, 1),
        created_offset_minutes=0,            # GAP: legacy stores no UTC offset
        edited_utc=_ts(edited),
        edited_offset_minutes=0 if edited else None,
    )


def _samples_blob(samples_json: str | None) -> bytes | None:
    """Re-encode the legacy JSON sample list into the 1080 5-float wire blob."""
    if not samples_json:
        return None
    rows = json.loads(samples_json)
    samples = [
        {
            "time": (s.get("t_ms") or 0) / 1000.0,
            "position": s.get("pos_m") or 0.0,
            "speed": s.get("v_mps") or 0.0,
            "acceleration": s.get("a_mps2") or 0.0,
            "force": s.get("F_N") or 0.0,
            "power": s.get("P_W") or 0.0,
        }
        for s in rows
    ]
    return encode_sample_bytes(samples)


def _peak_speed_mps(rep: dict) -> float | None:
    if rep.get("peak_speed_mps") is not None:
        return rep["peak_speed_mps"]
    if rep.get("peak_speed_rpm") is not None:
        return rep["peak_speed_rpm"] * analytics.MPS_PER_RPM
    return None


def load_session(conn: sqlite3.Connection, session_id: int) -> SessionStorage:
    """Load a legacy session row as a :class:`SessionStorage`."""
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"session {session_id} not found")
    row = dict(row)
    return SessionStorage(
        guid=legacy_guid("session", session_id),
        client_guid=legacy_guid("client", row["athlete_id"]),
        is_test=False,
        title=row.get("notes"),
        sync=_sync_from(row.get("started_at"), row.get("ended_at")),
    )


def load_motion(conn: sqlite3.Connection, rep_id: int) -> MotionStorage:
    """Load a legacy rep row as a :class:`MotionStorage`."""
    row = conn.execute("SELECT * FROM reps WHERE id = ?", (rep_id,)).fetchone()
    if row is None:
        raise ValueError(f"rep {rep_id} not found")
    rep = dict(row)

    sess_row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (rep["session_id"],)
    ).fetchone()
    sess = dict(sess_row) if sess_row is not None else {}

    duration_s: float | None = None
    if rep.get("ended_t_offset_ms") is not None:
        duration_s = (rep["ended_t_offset_ms"] - rep["started_t_offset_ms"]) / 1000.0

    distance_m = rep.get("max_extension_m")
    if distance_m is None and rep.get("total_distance_counts") is not None:
        distance_m = rep["total_distance_counts"] / analytics.COUNTS_PER_METRE

    return MotionStorage(
        guid=legacy_guid("rep", rep_id),
        motion_group_guid=legacy_guid("motiongroup", rep_id),
        machine_guid=legacy_guid("machine", sess.get("rig_id") or 1),
        is_eccentric=bool(rep.get("is_eccentric")),
        node_no=1,
        trim_type=0,
        # Resistance — only the concentric load is recoverable (session HMI load).
        con_mass=sess.get("hmi_load_kg") or 0.0,
        mode=ResistanceMode.NORMAL,          # GAP: legacy stores no mode
        gear=1,                               # GAP: legacy stores no per-rep gear
        # Aggregates
        peak_speed=_peak_speed_mps(rep),
        avg_speed=rep.get("avg_speed_mps"),
        peak_force=rep.get("peak_force_n"),
        avg_force=rep.get("avg_force_n"),
        peak_power=rep.get("peak_power_w"),
        avg_power=rep.get("avg_power_w"),
        peak_acceleration=rep.get("peak_acceleration_mps2"),
        avg_acceleration=rep.get("avg_acceleration_mps2"),
        top_speed=_peak_speed_mps(rep),
        work=rep.get("work_j"),
        distance=distance_m,
        duration=duration_s,
        stop_position=rep.get("max_extension_m"),
        data=_samples_blob(rep.get("samples_json")),
        sync=_sync_from(rep.get("started_at"), rep.get("ended_at")),
    )


# --- reverse adapter — write a MotionStorage into the v2 schema -----------

_MOTION_FIELDS = [
    "motion_group_guid", "machine_guid", "is_eccentric", "node_no", "trim_type",
    "start_index", "stop_index", "con_mass", "ecc_mass", "mode", "gear",
    "con_speed", "ecc_speed", "is_boost_active", "boost_load", "mass_at_v0",
    "mass_at_v1", "speed_v1", "peak_speed", "avg_speed", "peak_force",
    "avg_force", "peak_power", "avg_power", "peak_acceleration",
    "avg_acceleration", "top_speed", "work", "distance", "total_distance",
    "duration", "total_duration", "start_position", "stop_position", "data",
    "ppa_environment", "ppa_wind_mps", "ppa_surface_temp_c", "ppa_video_url",
    "ppa_rpe", "ppa_direction",
]
_SYNC_FIELDS = [
    "row_version", "local_db_row_version", "is_locally_edited",
    "is_locally_deleted", "created_utc", "created_offset_minutes",
    "edited_utc", "edited_offset_minutes", "deleted_utc", "deleted_offset_minutes",
]


def _scalar(value):
    """Coerce a model value to a SQLite-storable scalar."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ResistanceMode):
        return int(value)
    return value


def save_motion(conn: sqlite3.Connection, motion: MotionStorage) -> None:
    """Write a :class:`MotionStorage` into the ``v2_motions`` table (upsert)."""
    cols = ["guid", *_MOTION_FIELDS, *_SYNC_FIELDS]
    values = [_scalar(motion.guid)]
    values += [_scalar(getattr(motion, f)) for f in _MOTION_FIELDS]
    values += [_scalar(getattr(motion.sync, f)) for f in _SYNC_FIELDS]
    placeholders = ", ".join("?" for _ in cols)
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO v2_motions ({', '.join(cols)}) "
            f"VALUES ({placeholders})",
            values,
        )


def load_v2_motion(conn: sqlite3.Connection, guid: UUID) -> MotionStorage:
    """Read a :class:`MotionStorage` back out of the ``v2_motions`` table."""
    row = conn.execute(
        "SELECT * FROM v2_motions WHERE guid = ?", (str(guid),)
    ).fetchone()
    if row is None:
        raise ValueError(f"v2 motion {guid} not found")
    r = dict(row)
    sync = SyncEnvelope(**{f: r[f] for f in _SYNC_FIELDS})
    return MotionStorage(
        guid=r["guid"],
        sync=sync,
        **{f: r[f] for f in _MOTION_FIELDS},
    )
