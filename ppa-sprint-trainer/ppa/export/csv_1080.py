"""1080-style CSV export of a PPA session.

Two content types, each in a strict or an extended variant:

* **summary** — one row per rep. Strict header is byte-exact to 1080's
  ``CsvApiClient.cs``: ``Set;RepNumber;RepId;RepTime;TopSpeed;TotalTime;Distance``.
  Extended appends the PPA-derived and PPA-only columns.
* **samples** — one row per sample. 1080's ``CsvApiClient`` writes
  ``Time;Position;Speed;Force`` per rep; for a session-level single file we
  prefix ``Set;RepNumber``. Extended appends ``Acceleration;Power``.

Field separator is ``;`` and the decimal point ``.`` — matching 1080.
The strict variant is bare (header on line 1) so it is a drop-in 1080 file;
the extended variant carries an explanatory ``#`` comment block.

Australian English throughout.
"""
from __future__ import annotations

import sqlite3

from ppa.adapters.legacy_to_v2 import load_motion
from ppa.analytics.derived import derive_1080_export_fields
from ppa.codecs.sample_bytes import decode_sample_bytes

SEP = ";"

_EXTENDED_NOTE = [
    "# PPA Sprint Trainer export — 1080-compatible format",
    "# Schema version: PPA-v2 / 1080-equivalent (extended variant)",
    "#",
    "# Notes on column equivalence:",
    "#   - MaxAccel0.5s: 0.5 s sliding-window max (matches the 1080 method).",
    "#     PPA also stores instantaneous PeakAcceleration.",
    "#   - Best5m/Best10m: rolling minimum (fastest window anywhere in the run).",
    "#   - Direction: resisted vs assisted sprint context (PPA field).",
    "#   - Side: not measured — single-cable hardware, no L/R sensor.",
]


def _fmt(value) -> str:
    """Format a value for a CSV cell — empty for None, compact for floats."""
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, "g")
    return str(value)


def _reps_by_set(conn: sqlite3.Connection, session_id: int) -> list[tuple[int, int]]:
    """Return (set_idx, rep_id) for a session, ordered by set then rep."""
    rows = conn.execute(
        "SELECT id, set_idx FROM reps WHERE session_id = ? "
        "ORDER BY COALESCE(set_idx, 1), started_t_offset_ms",
        (session_id,),
    ).fetchall()
    return [(r["set_idx"] or 1, r["id"]) for r in rows]


def export_session_csv_summary(conn: sqlite3.Connection, session_id: int,
                               strict: bool = False) -> str:
    """One row per rep — the 1080 set-summary CSV (strict) or PPA superset."""
    lines: list[str] = []
    if not strict:
        lines.extend(_EXTENDED_NOTE)
    header = ["Set", "RepNumber", "RepId", "RepTime", "TopSpeed",
              "TotalTime", "Distance"]
    if not strict:
        header += ["AvgSpeed", "MaxAccel0.5s", "Best5m", "Best10m",
                   "Direction", "Rpe", "Environment"]
    lines.append(SEP.join(header))

    rep_no_by_set: dict[int, int] = {}
    for set_idx, rep_id in _reps_by_set(conn, session_id):
        m = load_motion(conn, rep_id)
        rep_no_by_set[set_idx] = rep_no_by_set.get(set_idx, 0) + 1
        created = m.sync.created_utc.isoformat() if m.sync.created_utc else ""
        row = [
            _fmt(set_idx), _fmt(rep_no_by_set[set_idx]), str(m.guid), created,
            _fmt(m.top_speed),
            _fmt(m.total_duration if m.total_duration is not None else m.duration),
            _fmt(m.total_distance if m.total_distance is not None else m.distance),
        ]
        if not strict:
            d = derive_1080_export_fields(m)
            row += [
                _fmt(d["avg_speed_mps"]), _fmt(d["max_accel_0p5s_mps2"]),
                _fmt(d["best_5m_split_s"]), _fmt(d["best_10m_split_s"]),
                _fmt(m.ppa_direction), _fmt(m.ppa_rpe), _fmt(m.ppa_environment),
            ]
        lines.append(SEP.join(row))
    return "\n".join(lines) + "\n"


def export_session_csv_samples(conn: sqlite3.Connection, session_id: int,
                               strict: bool = False) -> str:
    """One row per sample across the session's reps."""
    lines: list[str] = []
    if not strict:
        lines.extend(_EXTENDED_NOTE)
    header = ["Set", "RepNumber", "Time", "Position", "Speed", "Force"]
    if not strict:
        header += ["Acceleration", "Power"]
    lines.append(SEP.join(header))

    rep_no_by_set: dict[int, int] = {}
    for set_idx, rep_id in _reps_by_set(conn, session_id):
        m = load_motion(conn, rep_id)
        rep_no_by_set[set_idx] = rep_no_by_set.get(set_idx, 0) + 1
        rep_no = rep_no_by_set[set_idx]
        if not m.data:
            continue
        for s in decode_sample_bytes(m.data):
            row = [_fmt(set_idx), _fmt(rep_no), _fmt(s["time"]),
                   _fmt(s["position"]), _fmt(s["speed"]), _fmt(s["force"])]
            if not strict:
                row += [_fmt(s["acceleration"]), _fmt(s["power"])]
            lines.append(SEP.join(row))
    return "\n".join(lines) + "\n"
