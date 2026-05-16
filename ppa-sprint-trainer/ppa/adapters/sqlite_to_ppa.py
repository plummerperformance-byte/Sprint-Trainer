"""Load existing PPA SQLite rows as 1080-superset Pydantic models.

This adapter is read-only — it does not migrate or modify ``ppa.db``. It maps
the current ``sessions`` / ``reps`` columns onto the 1080 proto field names so
existing training data can be served, exported or validated as
1080-compatible.

Unit conversions applied here:
  * legacy ``reps.peak_speed_rpm`` -> m/s via ``analytics.MPS_PER_RPM`` (~0.00576).
    Modern live reps already store ``peak_speed_mps`` directly and that is
    preferred; the rpm path is only a fallback for old rows.
  * ``*_counts`` -> metres via ``analytics.COUNTS_PER_METRE``.
  * ``*_t_offset_ms`` -> seconds (divide by 1000).
  * ISO-8601 timestamp strings -> ``datetime``.

The per-rep sample curve is read from ``reps.samples_json`` (a JSON list of
``{t_ms, v_mps, F_N, P_W, pos_m, a_mps2}`` dicts written by the athletic loop).

A PPA rep maps to a 1080 ``Motion`` (and, structurally, to a single-machine
``MotionGroup`` containing that one ``Motion``). See
``docs/schema_mapping.md`` for the full column-by-column mapping and the list
of 1080 fields that have no PPA source yet.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import analytics

from ppa.models import onezeroeightzero as o
from ppa.models.ppa import PpaMotion, PpaMotionGroup, PpaSession, PpaSet


def _ts(value) -> datetime | None:
    """Parse an ISO-8601 / SQLite datetime string into a ``datetime``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _samples_from_json(raw_json: str | None) -> o.DataSamples:
    """Decode ``reps.samples_json`` into a 1080 ``DataSamples`` message."""
    if not raw_json:
        return o.DataSamples()
    rows = json.loads(raw_json)
    data = [
        o.DataSample(
            position=s.get("pos_m") or 0.0,
            time=(s.get("t_ms") or 0) / 1000.0,
            speed=s.get("v_mps") or 0.0,
            acceleration=s.get("a_mps2") or 0.0,
            force=s.get("F_N") or 0.0,
            power=s.get("P_W") or 0.0,
        )
        for s in rows
    ]
    return o.DataSamples(data=data)


def _peak_speed_mps(rep: dict) -> float:
    """Prefer the stored m/s value; fall back to converting legacy rpm."""
    if rep.get("peak_speed_mps") is not None:
        return rep["peak_speed_mps"]
    if rep.get("peak_speed_rpm") is not None:
        return rep["peak_speed_rpm"] * analytics.MPS_PER_RPM
    return 0.0


def load_session(conn: sqlite3.Connection, session_id: int) -> PpaSession:
    """Load a PPA session as a 1080 ``PpaSession``.

    The 1080 ``Session`` message is intentionally thin (it carries only the
    common header and the client reference); the reps/motions for the session
    are loaded separately via :func:`load_motion`.
    """
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"session {session_id} not found")
    row = dict(row)
    return PpaSession(
        common=o.BaseProto(
            Guid=str(row["id"]),
            Created=_ts(row.get("started_at")),
            Edited=_ts(row.get("ended_at")),
        ),
        client_guid=str(row["athlete_id"]),
    )


def load_motion(conn: sqlite3.Connection, rep_id: int) -> PpaMotion:
    """Load a single PPA rep as a 1080 ``PpaMotion`` (samples decoded)."""
    row = conn.execute("SELECT * FROM reps WHERE id = ?", (rep_id,)).fetchone()
    if row is None:
        raise ValueError(f"rep {rep_id} not found")
    rep = dict(row)

    sess_row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (rep["session_id"],)
    ).fetchone()
    sess = dict(sess_row) if sess_row is not None else {}

    duration_s = 0.0
    if rep.get("ended_t_offset_ms") is not None:
        duration_s = (rep["ended_t_offset_ms"] - rep["started_t_offset_ms"]) / 1000.0

    distance_m = rep.get("max_extension_m")
    if distance_m is None and rep.get("total_distance_counts") is not None:
        distance_m = rep["total_distance_counts"] / analytics.COUNTS_PER_METRE

    values = o.AggregatedValues(
        peak_speed=_peak_speed_mps(rep),
        avg_speed=rep.get("avg_speed_mps") or 0.0,
        peak_force=rep.get("peak_force_n") or 0.0,
        avg_force=rep.get("avg_force_n") or 0.0,
        peak_power=rep.get("peak_power_w") or 0.0,
        avg_power=rep.get("avg_power_w") or 0.0,
        peak_acceleration=rep.get("peak_acceleration_mps2") or 0.0,
        avg_acceleration=rep.get("avg_acceleration_mps2") or 0.0,
        start_position=0.0,  # GAP: PPA does not record a start position
        stop_position=rep.get("max_extension_m") or 0.0,
        work=rep.get("work_j") or 0.0,
        distance=distance_m or 0.0,
        duration=duration_s,
    )

    # GAP: PPA reps do not snapshot per-rep resistance settings. The session's
    # HMI load is the closest available value for the concentric load.
    resistance = o.Resistance(
        con_mass=sess.get("hmi_load_kg") or 0.0,
    )

    return PpaMotion(
        common=o.BaseProto(
            Guid=str(rep["id"]),
            Created=_ts(rep.get("started_at")),
            Edited=_ts(rep.get("ended_at")),
        ),
        # A PPA rep is a single-machine MotionGroup; reuse the rep id.
        motion_group_guid=str(rep["id"]),
        resistance=resistance,
        samples=_samples_from_json(rep.get("samples_json")),
        values=values,
        node_no=1,  # single-cable rig — always node 1
        comment=rep.get("comment") or "",
        is_eccentric=bool(rep.get("is_eccentric")),
    )


def load_session_sets(conn: sqlite3.Connection, session_id: int) -> list[PpaSet]:
    """Assemble a session's reps into the 1080 ``Set -> MotionGroup -> Motion``
    tree, grouped by the PPA ``reps.set_idx``.

    Each PPA rep becomes one ``MotionGroup`` (single machine) wrapping one
    ``Motion``. PPA has no exercise-library layer, so ``Set.exercise_guid`` is
    left blank (see ``docs/schema_mapping.md``).
    """
    rows = conn.execute(
        "SELECT id, set_idx FROM reps WHERE session_id = ? "
        "ORDER BY COALESCE(set_idx, 1), started_t_offset_ms",
        (session_id,),
    ).fetchall()
    by_set: dict[int, list[int]] = {}
    for r in rows:
        by_set.setdefault(r["set_idx"] or 1, []).append(r["id"])

    sets: list[PpaSet] = []
    for set_idx in sorted(by_set):
        groups: list[PpaMotionGroup] = []
        for rep_id in by_set[set_idx]:
            motion = load_motion(conn, rep_id)
            groups.append(PpaMotionGroup(
                common=o.BaseProto(
                    Guid=motion.common.Guid if motion.common else str(rep_id),
                ),
                set_guid=f"{session_id}-{set_idx}",
                motions=[motion],
                side=o.Side.BILATERAL,  # single-cable rig
                comment=motion.comment,
            ))
        sets.append(PpaSet(
            common=o.BaseProto(Guid=f"{session_id}-{set_idx}"),
            exercise_guid="",  # GAP: PPA has no exercise-library layer
            motion_groups=groups,
        ))
    return sets
