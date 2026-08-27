"""persistence.py - SQLite store for ppa_service sessions/reps/samples.

No rig contact. Plain sqlite3 stdlib. Safe to import and use without COM6.

Threading: caller is expected to serialize access to a single connection
(the service uses a threading.Lock around all DB calls inside its executor).
This keeps the sqlite3 module's per-connection thread requirement satisfied
while allowing async polling-loop writes via run_in_executor.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# DB lives alongside this module so the path is portable across machines and
# usernames (was previously hardcoded to C:\Users\trigo\, which broke on any
# other machine). Override with the service's --db flag if needed.
DB_PATH = str(Path(__file__).resolve().parent / "ppa.db")

log = logging.getLogger("ppa.persistence")


class SessionAlreadyOpenError(Exception):
    """Raised by start_session when another session is still open (ended_at IS NULL)."""


# --- schema ---

_SCHEMA = """
CREATE TABLE IF NOT EXISTS athletes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id    INTEGER NOT NULL REFERENCES athletes(id),
    started_at    TEXT    NOT NULL,
    ended_at      TEXT,
    notes         TEXT,
    hmi_load_kg   REAL,
    recovered_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_athlete_started
    ON sessions(athlete_id, started_at DESC);

CREATE TABLE IF NOT EXISTS reps (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id               INTEGER NOT NULL REFERENCES sessions(id),
    started_at               TEXT    NOT NULL,
    ended_at                 TEXT,
    started_t_offset_ms      INTEGER NOT NULL,
    ended_t_offset_ms        INTEGER,
    peak_speed_rpm           INTEGER,
    peak_torque_pct          REAL,
    total_distance_counts    INTEGER,
    net_displacement_counts  INTEGER,
    peak_decel_rpm_per_s     REAL
);
CREATE INDEX IF NOT EXISTS idx_reps_session ON reps(session_id);

CREATE TABLE IF NOT EXISTS rigs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    location    TEXT,
    serial      TEXT,
    notes       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    position_group  TEXT,
    sport           TEXT,
    config_json     TEXT    NOT NULL,
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_templates_name ON templates(name);

CREATE TABLE IF NOT EXISTS resistance_curves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    points_json TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS samples (
    session_id       INTEGER NOT NULL REFERENCES sessions(id),
    t_offset_ms      INTEGER NOT NULL,
    status           INTEGER,
    speed_rpm        INTEGER,
    torque_pct       REAL,
    position_counts  INTEGER,
    bus_voltage_v    REAL,
    PRIMARY KEY (session_id, t_offset_ms)
) WITHOUT ROWID;
"""


def open_db(path: str = DB_PATH) -> sqlite3.Connection:
    """Open the DB. Caller is responsible for serializing access."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Pragmas — must run outside any transaction.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.commit()
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing, run idempotent column migrations, then
    run orphan-recovery pass."""
    conn.executescript(_SCHEMA)
    conn.commit()
    _ensure_column(conn, "athletes", "external_id", "external_id TEXT")
    _ensure_column(conn, "sessions", "protocol_id", "protocol_id INTEGER")
    # 1080 AggregatedValues + PPA-extension columns for athletic-mode reps.
    # All nullable so rows written by the legacy peak_speed_rpm path stay valid.
    for col, ddl in [
        ("peak_speed_mps", "peak_speed_mps REAL"),
        ("avg_speed_mps", "avg_speed_mps REAL"),
        ("peak_force_n", "peak_force_n REAL"),
        ("avg_force_n", "avg_force_n REAL"),
        ("peak_power_w", "peak_power_w REAL"),
        ("avg_power_w", "avg_power_w REAL"),
        ("peak_acceleration_mps2", "peak_acceleration_mps2 REAL"),
        ("avg_acceleration_mps2", "avg_acceleration_mps2 REAL"),
        ("work_j", "work_j REAL"),
        ("impulse_ns", "impulse_ns REAL"),
        ("ttpf_ms", "ttpf_ms INTEGER"),
        ("ttps_ms", "ttps_ms INTEGER"),
        ("accel_end_ms", "accel_end_ms INTEGER"),
        ("decel_start_ms", "decel_start_ms INTEGER"),
        ("max_extension_m", "max_extension_m REAL"),
        ("drill", "drill TEXT"),
        ("splits_s_json", "splits_s_json TEXT"),
        ("is_eccentric", "is_eccentric INTEGER"),
        # 1080-App / Morin metrics (imported from xlsx Dashboard or computed
        # via Samozino fit). Nullable so live-rig reps that don't carry these
        # stay valid.
        ("f0_n", "f0_n REAL"),
        ("f0_rel_nkg", "f0_rel_nkg REAL"),
        ("v0_mps", "v0_mps REAL"),
        ("pmax_w_morin", "pmax_w_morin REAL"),
        ("pmax_rel_wkg", "pmax_rel_wkg REAL"),
        ("fv_slope_per_kg", "fv_slope_per_kg REAL"),
        ("time_to_max_v_s", "time_to_max_v_s REAL"),
        ("dist_to_max_v_m", "dist_to_max_v_m REAL"),
        ("v_dropoff_pct", "v_dropoff_pct REAL"),
        ("source", "source TEXT"),  # "live" | "xlsx_import" | etc
        ("step_events_json", "step_events_json TEXT"),
        ("total_steps", "total_steps INTEGER"),
        ("step_freq_hz", "step_freq_hz REAL"),
        ("avg_step_length_m", "avg_step_length_m REAL"),
        ("step_length_std_m", "step_length_std_m REAL"),
        ("flagged_steps", "flagged_steps INTEGER"),
        ("step_confidence", "step_confidence REAL"),
        # Foot-labelled L/R asymmetry block (global/pairwise/zone). Experimental
        # — declared feet, not measured — but persisted so the Steps tab can show it.
        ("asymmetry_json", "asymmetry_json TEXT"),
        # Full in-memory sample list, preserved as JSON so we can rehydrate
        # the rich chart curves (with F_N / pos_m / a_mps2) when reloading a
        # session into the coach view. Live reps store this; xlsx imports too.
        ("samples_json", "samples_json TEXT"),
        # Rep validity flag (§11 of v1 addendum). Default 1 = valid.
        # Reason: free-text or one of {false_start, slip, fall, equipment, other}.
        # Invalid reps are EXCLUDED from FV regressions / aggregates / chase
        # metric, but remain in the DB so coach can re-include retroactively.
        ("valid", "valid INTEGER NOT NULL DEFAULT 1"),
        ("invalid_reason", "invalid_reason TEXT"),
        ("invalid_note", "invalid_note TEXT"),
        # Deceleration time (top speed -> zero) — 1080 AccelDecelStats parity.
        ("decel_time_s", "decel_time_s REAL"),
        # Per-rep coach annotation — 1080 MotionGroup.color / .comment parity.
        ("color", "color TEXT"),
        ("comment", "comment TEXT"),
        # Set grouping — reps belong to a set within the session (default 1).
        ("set_idx", "set_idx INTEGER NOT NULL DEFAULT 1"),
    ]:
        _ensure_column(conn, "reps", col, ddl)
    # Athlete metadata extensions
    _ensure_column(conn, "athletes", "body_mass_kg", "body_mass_kg REAL")
    _ensure_column(conn, "athletes", "position_group", "position_group TEXT")
    _ensure_column(conn, "athletes", "sport", "sport TEXT")
    _ensure_column(conn, "athletes", "level", "level TEXT")
    _ensure_column(conn, "athletes", "dob", "dob TEXT")
    # Squad organisation — 1080 Client.group / .tags parity.
    _ensure_column(conn, "athletes", "squad_group", "squad_group TEXT")
    _ensure_column(conn, "athletes", "tags", "tags TEXT")
    # Multi-rig support (§15.3 v1 addendum) — rig_id on sessions, defaulting
    # to 1 so existing rows back-fill cleanly. Seed a default "PPA-1" rig if
    # the table is empty.
    _ensure_column(conn, "sessions", "rig_id", "rig_id INTEGER DEFAULT 1")
    _seed_default_rig(conn)
    _seed_builtin_curves(conn)
    _recover_orphans(conn)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Idempotent ADD COLUMN. SQLite has no IF NOT EXISTS for ALTER TABLE,
    so we check PRAGMA table_info first."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    with conn:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    log.info("schema migration: added %s.%s (%s)", table, column, ddl)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _add_ms(iso: str, ms: int) -> str:
    return (datetime.fromisoformat(iso) + timedelta(milliseconds=ms)).isoformat(
        timespec="milliseconds"
    )


def _recover_orphans(conn: sqlite3.Connection) -> None:
    """Auto-close any session/rep left open by a previous crash.

    `ended_at` is reconstructed from `started_at + max(samples.t_offset_ms)` —
    NEVER from wall-clock now. If no samples exist, `ended_at = started_at`.
    Sets `sessions.recovered_at = now()` so analytics can filter these out.
    Same logic for orphan reps (per the rep's started_t_offset_ms range).
    """
    open_sessions = conn.execute(
        "SELECT id, athlete_id, started_at FROM sessions WHERE ended_at IS NULL"
    ).fetchall()

    if not open_sessions:
        return

    now_iso = _now_iso()

    for sess in open_sessions:
        sid = sess["id"]
        started_at = sess["started_at"]
        row = conn.execute(
            "SELECT MAX(t_offset_ms) AS max_t, COUNT(*) AS n "
            "FROM samples WHERE session_id = ?",
            (sid,),
        ).fetchone()
        max_t = row["max_t"]
        n_samples = row["n"]

        ended_at = _add_ms(started_at, max_t) if max_t is not None else started_at
        duration_s = (max_t / 1000.0) if max_t is not None else 0.0

        with conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, recovered_at = ? WHERE id = ?",
                (ended_at, now_iso, sid),
            )

            # Close any orphan reps in this session
            orphan_reps = conn.execute(
                "SELECT id, started_t_offset_ms FROM reps "
                "WHERE session_id = ? AND ended_at IS NULL",
                (sid,),
            ).fetchall()

            for rep in orphan_reps:
                rep_id = rep["id"]
                rep_start = rep["started_t_offset_ms"]
                # End the rep at the last sample at-or-after its start; if none,
                # collapse it to a zero-duration rep at its start time.
                r = conn.execute(
                    "SELECT MAX(t_offset_ms) AS max_t FROM samples "
                    "WHERE session_id = ? AND t_offset_ms >= ?",
                    (sid, rep_start),
                ).fetchone()
                ended_t = r["max_t"] if r["max_t"] is not None else rep_start
                aggs = _compute_rep_aggregates(conn, sid, rep_start, ended_t)
                rep_ended_at = _add_ms(started_at, ended_t)
                conn.execute(
                    """UPDATE reps SET
                          ended_at = ?, ended_t_offset_ms = ?,
                          peak_speed_rpm = ?, peak_torque_pct = ?,
                          total_distance_counts = ?, net_displacement_counts = ?,
                          peak_decel_rpm_per_s = ?
                       WHERE id = ?""",
                    (
                        rep_ended_at,
                        ended_t,
                        aggs["peak_speed_rpm"],
                        aggs["peak_torque_pct"],
                        aggs["total_distance_counts"],
                        aggs["net_displacement_counts"],
                        aggs["peak_decel_rpm_per_s"],
                        rep_id,
                    ),
                )

        log.warning(
            "recovered orphan session id=%d athlete=%d started=%s duration=%ss samples=%d",
            sid,
            sess["athlete_id"],
            started_at,
            f"{duration_s:.3f}",
            n_samples,
        )


# --- aggregate computation ---

def _compute_rep_aggregates(
    conn: sqlite3.Connection, session_id: int, t_start_ms: int, t_end_ms: int
) -> dict:
    """Pure-Python aggregates from samples in [t_start_ms, t_end_ms].

    Distance: path length (sum of |Δposition|).
    Net displacement: end_position - start_position.
    Peak decel: max -d(speed_rpm)/dt across consecutive samples (rpm/s).
    """
    rows = conn.execute(
        """SELECT t_offset_ms, speed_rpm, torque_pct, position_counts
           FROM samples
           WHERE session_id = ? AND t_offset_ms BETWEEN ? AND ?
           ORDER BY t_offset_ms""",
        (session_id, t_start_ms, t_end_ms),
    ).fetchall()

    if not rows:
        return {
            "peak_speed_rpm": None,
            "peak_torque_pct": None,
            "total_distance_counts": 0,
            "net_displacement_counts": 0,
            "peak_decel_rpm_per_s": None,
        }

    speeds = [r["speed_rpm"] for r in rows if r["speed_rpm"] is not None]
    torques = [r["torque_pct"] for r in rows if r["torque_pct"] is not None]
    positions = [r["position_counts"] for r in rows if r["position_counts"] is not None]

    peak_speed = max(abs(v) for v in speeds) if speeds else None
    peak_torque = max(abs(v) for v in torques) if torques else None

    total_distance = (
        sum(abs(positions[i + 1] - positions[i]) for i in range(len(positions) - 1))
        if len(positions) > 1
        else 0
    )
    net_displacement = (positions[-1] - positions[0]) if positions else 0

    decels = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if a["speed_rpm"] is None or b["speed_rpm"] is None:
            continue
        dt_s = (b["t_offset_ms"] - a["t_offset_ms"]) / 1000.0
        if dt_s <= 0:
            continue
        decels.append(-(b["speed_rpm"] - a["speed_rpm"]) / dt_s)
    peak_decel = max(decels) if decels else None

    return {
        "peak_speed_rpm": peak_speed,
        "peak_torque_pct": peak_torque,
        "total_distance_counts": int(total_distance),
        "net_displacement_counts": int(net_displacement),
        "peak_decel_rpm_per_s": peak_decel,
    }


# --- athletes ---

def create_athlete(conn: sqlite3.Connection, name: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("athlete name must be non-empty")
    with conn:
        cur = conn.execute("INSERT INTO athletes(name) VALUES (?)", (name,))
    return cur.lastrowid


def list_athletes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, body_mass_kg, squad_group, tags, created_at "
        "FROM athletes ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


# --- sessions ---

def start_session(
    conn: sqlite3.Connection,
    athlete_id: int,
    notes: Optional[str] = None,
    hmi_load_kg: Optional[float] = None,
) -> int:
    if hmi_load_kg is not None and hmi_load_kg < 0:
        raise ValueError(f"hmi_load_kg must be >= 0, got {hmi_load_kg}")

    open_row = conn.execute(
        "SELECT id FROM sessions WHERE ended_at IS NULL LIMIT 1"
    ).fetchone()
    if open_row:
        raise SessionAlreadyOpenError(
            f"session {open_row['id']} is still open"
        )

    # Validate athlete exists for clearer error than a constraint failure.
    if not conn.execute(
        "SELECT 1 FROM athletes WHERE id = ?", (athlete_id,)
    ).fetchone():
        raise ValueError(f"athlete {athlete_id} does not exist")

    started_at = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO sessions(athlete_id, started_at, notes, hmi_load_kg) "
            "VALUES (?, ?, ?, ?)",
            (athlete_id, started_at, notes, hmi_load_kg),
        )
    return cur.lastrowid


def end_session(conn: sqlite3.Connection, session_id: int) -> dict:
    """Closes the session and any open rep within it. Returns summary."""
    sess = conn.execute(
        "SELECT id, started_at, ended_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not sess:
        raise ValueError(f"session {session_id} does not exist")

    with conn:
        # Close any open rep first, using the last sample's t_offset_ms.
        last_t = _last_t_offset(conn, session_id)
        open_rep = conn.execute(
            "SELECT id FROM reps WHERE session_id = ? AND ended_at IS NULL",
            (session_id,),
        ).fetchone()
        if open_rep:
            _end_rep_locked(conn, open_rep["id"], last_t)

        if sess["ended_at"] is None:
            ended_at = _now_iso()
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (ended_at, session_id),
            )

    summary = conn.execute(
        """SELECT
              (julianday(ended_at) - julianday(started_at)) * 86400 AS duration_seconds,
              (SELECT COUNT(*) FROM reps WHERE session_id = ?) AS rep_count
           FROM sessions WHERE id = ?""",
        (session_id, session_id),
    ).fetchone()
    return {
        "session_id": session_id,
        "rep_count": summary["rep_count"],
        "duration_seconds": summary["duration_seconds"],
    }


def _last_t_offset(conn: sqlite3.Connection, session_id: int) -> int:
    r = conn.execute(
        "SELECT MAX(t_offset_ms) AS m FROM samples WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return r["m"] if r["m"] is not None else 0


def list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.athlete_id, a.name AS athlete_name,
                  s.started_at, s.ended_at, s.notes, s.hmi_load_kg, s.recovered_at,
                  CASE WHEN s.ended_at IS NULL THEN NULL
                       ELSE (julianday(s.ended_at) - julianday(s.started_at)) * 86400 END
                       AS duration_seconds,
                  (SELECT COUNT(*) FROM reps r WHERE r.session_id = s.id) AS rep_count
           FROM sessions s
           JOIN athletes a ON a.id = s.athlete_id
           ORDER BY s.started_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_session(conn: sqlite3.Connection, session_id: int) -> Optional[dict]:
    sess = conn.execute(
        """SELECT s.*, a.name AS athlete_name,
                  CASE WHEN s.ended_at IS NULL THEN NULL
                       ELSE (julianday(s.ended_at) - julianday(s.started_at)) * 86400 END
                       AS duration_seconds
           FROM sessions s
           JOIN athletes a ON a.id = s.athlete_id
           WHERE s.id = ?""",
        (session_id,),
    ).fetchone()
    if not sess:
        return None
    reps = conn.execute(
        "SELECT * FROM reps WHERE session_id = ? ORDER BY started_t_offset_ms",
        (session_id,),
    ).fetchall()
    out = dict(sess)
    out["reps"] = [dict(r) for r in reps]
    return out


def get_open_session(conn: sqlite3.Connection) -> Optional[dict]:
    """Returns the currently-open session if any, else None.
    Useful for the service to find its own state on (re)start."""
    row = conn.execute(
        "SELECT id, athlete_id, started_at, notes, hmi_load_kg "
        "FROM sessions WHERE ended_at IS NULL LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# --- reps ---

def start_rep(conn: sqlite3.Connection, session_id: int, t_offset_ms: int,
              set_idx: int = 1) -> int:
    started_at = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO reps(session_id, started_at, started_t_offset_ms, set_idx) "
            "VALUES (?, ?, ?, ?)",
            (session_id, started_at, t_offset_ms, set_idx),
        )
    return cur.lastrowid


def end_rep(conn: sqlite3.Connection, rep_id: int, t_offset_ms: int) -> dict:
    """Compute aggregates from samples and finalize the rep."""
    with conn:
        return _end_rep_locked(conn, rep_id, t_offset_ms)


def end_rep_with_aggregates(
    conn: sqlite3.Connection,
    rep_id: int,
    t_offset_ms: int,
    agg: dict,
    drill: Optional[str] = None,
) -> dict:
    """Finalise a rep using the full in-memory aggregate dict from the athletic loop.

    Writes the 1080-schema fields (peak_force_n, peak_power_w, work_j, impulse_ns,
    ttpf_ms, ttps_ms, accel/decel phase markers, max_extension_m, drill, splits_s_json,
    is_eccentric) AND the legacy sample-derived columns (peak_speed_rpm,
    peak_torque_pct, total/net distance, peak_decel) for backwards compatibility.
    """
    import json as _json
    rep = conn.execute(
        "SELECT session_id, started_t_offset_ms FROM reps WHERE id = ?",
        (rep_id,),
    ).fetchone()
    if not rep:
        raise ValueError(f"rep {rep_id} not found")
    sid = rep["session_id"]
    t_start = rep["started_t_offset_ms"]
    legacy = _compute_rep_aggregates(conn, sid, t_start, t_offset_ms)
    ended_at = _now_iso()
    splits = agg.get("splits_s")
    splits_json = _json.dumps(splits) if splits else None
    samples = agg.get("samples")
    samples_json = _json.dumps(samples) if samples else None
    with conn:
        conn.execute(
            """UPDATE reps SET
                  ended_at = ?, ended_t_offset_ms = ?,
                  peak_speed_rpm = ?, peak_torque_pct = ?,
                  total_distance_counts = ?, net_displacement_counts = ?,
                  peak_decel_rpm_per_s = ?,
                  peak_speed_mps = ?, avg_speed_mps = ?,
                  peak_force_n = ?, avg_force_n = ?,
                  peak_power_w = ?, avg_power_w = ?,
                  peak_acceleration_mps2 = ?, avg_acceleration_mps2 = ?,
                  work_j = ?, impulse_ns = ?,
                  ttpf_ms = ?, ttps_ms = ?,
                  accel_end_ms = ?, decel_start_ms = ?,
                  max_extension_m = ?, drill = ?, splits_s_json = ?,
                  is_eccentric = ?, samples_json = ?, source = ?,
                  dist_to_max_v_m = ?, decel_time_s = ?
               WHERE id = ?""",
            (
                ended_at, t_offset_ms,
                legacy["peak_speed_rpm"], legacy["peak_torque_pct"],
                legacy["total_distance_counts"], legacy["net_displacement_counts"],
                legacy["peak_decel_rpm_per_s"],
                agg.get("peak_speed_mps"), agg.get("avg_speed_mps"),
                agg.get("peak_force_n"), agg.get("avg_force_n"),
                agg.get("peak_power_w"), agg.get("avg_power_w"),
                agg.get("peak_acceleration_mps2"), agg.get("avg_acceleration_mps2"),
                agg.get("work_j"), agg.get("impulse_ns"),
                agg.get("ttpf_ms"), agg.get("ttps_ms"),
                agg.get("accel_end_ms"), agg.get("decel_start_ms"),
                agg.get("max_extension_m"), drill, splits_json,
                1 if agg.get("is_eccentric") else 0,
                samples_json, "live",
                agg.get("dist_to_max_v_m"), agg.get("decel_time_s"),
                rep_id,
            ),
        )
    return {"rep_id": rep_id, "ended_at": ended_at, "ended_t_offset_ms": t_offset_ms, **legacy}


def delete_rep(conn: sqlite3.Connection, rep_id: int) -> None:
    """Delete a rep row entirely — used to purge phantom reps."""
    with conn:
        conn.execute("DELETE FROM reps WHERE id = ?", (rep_id,))


def set_rep_validity(conn: sqlite3.Connection, rep_id: int, valid: bool,
                     reason: Optional[str] = None,
                     note: Optional[str] = None) -> dict:
    """Mark a rep valid or invalid. Invalid reps are excluded from FV
    regressions / aggregates / chase metric but remain in the DB.

    `reason` is a short tag from {false_start, slip, fall, equipment, other}
    or any free text; `note` is an optional one-line comment.
    """
    with conn:
        conn.execute(
            "UPDATE reps SET valid = ?, invalid_reason = ?, invalid_note = ? WHERE id = ?",
            (1 if valid else 0, reason if not valid else None,
             note if not valid else None, rep_id),
        )
    row = conn.execute(
        "SELECT id, valid, invalid_reason, invalid_note FROM reps WHERE id = ?",
        (rep_id,),
    ).fetchone()
    return dict(row) if row else {"id": rep_id, "valid": 1}


def annotate_rep(conn: sqlite3.Connection, rep_id: int,
                 color: Optional[str] = None,
                 comment: Optional[str] = None) -> dict:
    """Set a coach annotation on a rep — a colour tag and/or a free-text
    comment (1080 MotionGroup.color / .comment parity). Pass None to leave a
    field unchanged; pass "" to clear it."""
    with conn:
        if color is not None:
            conn.execute("UPDATE reps SET color = ? WHERE id = ?",
                         (color or None, rep_id))
        if comment is not None:
            conn.execute("UPDATE reps SET comment = ? WHERE id = ?",
                         (comment or None, rep_id))
    row = conn.execute(
        "SELECT id, color, comment FROM reps WHERE id = ?", (rep_id,),
    ).fetchone()
    return dict(row) if row else {"id": rep_id}


def import_rep(conn: sqlite3.Connection, athlete_id: int, rep: dict,
               source: str = "xlsx_import",
               session_notes: Optional[str] = None,
               session_id: Optional[int] = None) -> dict:
    """Persist a fully-built rep dict (e.g. from an xlsx parser) into the DB.

    Creates a new session row + rep row + samples rows in one transaction.
    Pass session_id to append the rep to an existing session instead (multi-rep
    imports): the rep is stacked onto the session timeline after the previous
    rep (5 s gap), so per-rep windows into the samples table stay disjoint and
    the on-demand analytics recompute slices the right samples.
    Returns {session_id, rep_id, athlete_id}.
    """
    import json as _json

    drill = rep.get("drill") or rep.get("_meta", {}).get("drill")
    body_mass_kg = rep.get("_meta", {}).get("body_mass_kg")
    duration_s = rep.get("duration_s") or rep.get("total_time_s") or 0.0
    samples = rep.get("samples") or []

    # Open athlete metadata row update if we have new body_mass_kg
    if body_mass_kg is not None:
        conn.execute(
            "UPDATE athletes SET body_mass_kg = ? WHERE id = ? AND (body_mass_kg IS NULL OR body_mass_kg <= 0)",
            (body_mass_kg, athlete_id),
        )

    started_at = _now_iso()
    notes = session_notes or (
        f"{source} | drill={drill or '?'} | source={source}"
    )

    appending = session_id is not None

    with conn:
        # Where this rep sits on the session timeline (0 for a fresh session;
        # after the last rep + 5 s gap when appending).
        base_ms = 0
        if appending:
            row = conn.execute(
                "SELECT MAX(ended_t_offset_ms) AS m FROM reps WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            base_ms = int(row["m"] or 0) + 5000

        if not appending:
            # Close any open session first — import shouldn't bash an in-flight one
            open_row = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL LIMIT 1"
            ).fetchone()
            if open_row:
                conn.execute(
                    "UPDATE sessions SET ended_at = ?, recovered_at = ? WHERE id = ?",
                    (started_at, started_at, open_row["id"]),
                )

            cur = conn.execute(
                "INSERT INTO sessions(athlete_id, started_at, ended_at, notes, hmi_load_kg) "
                "VALUES (?, ?, ?, ?, ?)",
                (athlete_id, started_at, started_at, notes, None),
            )
            session_id = cur.lastrowid

        # End-offset = duration in ms (samples are time-zero relative to rep start)
        t_end_ms = int(duration_s * 1000)

        cur = conn.execute(
            "INSERT INTO reps(session_id, started_at, ended_at, "
            "started_t_offset_ms, ended_t_offset_ms) VALUES (?, ?, ?, ?, ?)",
            (session_id, started_at, started_at, base_ms, base_ms + t_end_ms),
        )
        rep_id = cur.lastrowid

        # Splits + step events + full sample curve as JSON blobs
        splits_json = _json.dumps(rep.get("splits_s_extended") or rep.get("splits_s") or {})
        step_events_json = _json.dumps(rep.get("step_events") or [])
        asymmetry_json = _json.dumps(rep.get("asymmetry")) if rep.get("asymmetry") else None
        samples_json = _json.dumps(samples) if samples else None

        conn.execute(
            """UPDATE reps SET
                  peak_speed_mps = ?, avg_speed_mps = ?,
                  peak_force_n = ?, avg_force_n = ?,
                  peak_power_w = ?, avg_power_w = ?,
                  peak_acceleration_mps2 = ?, avg_acceleration_mps2 = ?,
                  work_j = ?, impulse_ns = ?,
                  ttpf_ms = ?, ttps_ms = ?,
                  accel_end_ms = ?, decel_start_ms = ?,
                  max_extension_m = ?, drill = ?, splits_s_json = ?,
                  is_eccentric = ?,
                  f0_n = ?, f0_rel_nkg = ?, v0_mps = ?,
                  pmax_w_morin = ?, pmax_rel_wkg = ?, fv_slope_per_kg = ?,
                  time_to_max_v_s = ?, dist_to_max_v_m = ?, v_dropoff_pct = ?,
                  total_steps = ?, step_freq_hz = ?,
                  avg_step_length_m = ?, step_length_std_m = ?,
                  flagged_steps = ?, step_confidence = ?, asymmetry_json = ?,
                  step_events_json = ?, samples_json = ?, source = ?
               WHERE id = ?""",
            (
                rep.get("peak_speed_mps"), rep.get("avg_speed_mps"),
                rep.get("peak_force_n"), rep.get("avg_force_n"),
                rep.get("peak_power_w"), rep.get("avg_power_w"),
                rep.get("peak_acceleration_mps2"), rep.get("avg_acceleration_mps2"),
                rep.get("work_j"), rep.get("impulse_ns"),
                rep.get("ttpf_ms"), rep.get("ttps_ms"),
                rep.get("accel_end_ms"), rep.get("decel_start_ms"),
                rep.get("max_extension_m"), drill, splits_json,
                1 if rep.get("is_eccentric") else 0,
                rep.get("f0_n"), rep.get("f0_rel_nkg"), rep.get("v0_mps"),
                rep.get("pmax_w_morin"), rep.get("pmax_rel_wkg"), rep.get("fv_slope_per_kg"),
                rep.get("time_to_max_v_s"), rep.get("dist_to_max_v_m"), rep.get("v_dropoff_pct"),
                rep.get("total_steps"), rep.get("step_freq_hz"),
                rep.get("avg_step_length_m"), rep.get("step_length_std_m"),
                rep.get("flagged_steps"), rep.get("step_confidence"), asymmetry_json,
                step_events_json, samples_json, source,
                rep_id,
            ),
        )

        # Persist samples — each sample's t_ms (rep-relative) lands at
        # base_ms + t_ms so per-rep windows stay disjoint within the session.
        if samples:
            # Unit factors mirror analytics.py (COUNTS_PER_METRE, PCT_PER_KG);
            # local import avoids any module-level cycle.
            from analytics import COUNTS_PER_METRE, PCT_PER_KG
            rows = []
            for s in samples:
                t_ms = s.get("t_ms")
                if t_ms is None: continue
                speed_rpm = None
                if "v_mps" in s:
                    # 0.00576 m/s per RPM → speed_rpm = v_mps / 0.00576
                    speed_rpm = int(round(s["v_mps"] / 0.00576))
                pos_counts = None
                if s.get("pos_m") is not None:
                    pos_counts = int(round(s["pos_m"] * COUNTS_PER_METRE))
                torque_pct = None
                if s.get("F_N") is not None:
                    torque_pct = round((s["F_N"] / 9.81) * PCT_PER_KG, 2)
                rows.append((
                    session_id, base_ms + int(t_ms),
                    None,  # status — not present in imports
                    speed_rpm,
                    torque_pct,
                    pos_counts,
                    None,  # bus_voltage_v
                ))
            if rows:
                conn.executemany(
                    """INSERT OR IGNORE INTO samples
                         (session_id, t_offset_ms, status, speed_rpm, torque_pct,
                          position_counts, bus_voltage_v)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )

    return {"session_id": session_id, "rep_id": rep_id, "athlete_id": athlete_id}


def athlete_history(conn: sqlite3.Connection, athlete_id: int, limit: int = 50) -> dict:
    """Return an athlete's session history with rep summaries.

    Each session row contains the count of reps + best speed/force/power
    across reps in that session, ready for an overview list.
    """
    a = conn.execute(
        "SELECT id, name, body_mass_kg, position_group, created_at "
        "FROM athletes WHERE id = ?",
        (athlete_id,),
    ).fetchone()
    if not a:
        return {"athlete": None, "sessions": []}
    athlete = dict(a)
    sessions = conn.execute(
        """SELECT s.id, s.started_at, s.ended_at, s.notes, s.hmi_load_kg,
                  COUNT(r.id) AS rep_count,
                  SUM(CASE WHEN COALESCE(r.valid, 1) = 0 THEN 1 ELSE 0 END) AS invalid_count,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.peak_speed_mps END) AS best_speed_mps,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.peak_force_n   END) AS best_force_n,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.peak_power_w   END) AS best_power_w,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.f0_rel_nkg     END) AS best_f0_rel_nkg,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.v0_mps         END) AS best_v0_mps,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.pmax_rel_wkg   END) AS best_pmax_rel_wkg,
                  MIN(CASE WHEN COALESCE(r.valid,1)=1 THEN json_extract(r.splits_s_json,'$."10"') END) AS best_split_10m_s,
                  MIN(CASE WHEN COALESCE(r.valid,1)=1 THEN json_extract(r.splits_s_json,'$."40"') END) AS best_split_40m_s,
                  MAX(r.drill) AS drill
           FROM sessions s
           LEFT JOIN reps r ON r.session_id = s.id
           WHERE s.athlete_id = ?
           GROUP BY s.id
           ORDER BY s.started_at DESC
           LIMIT ?""",
        (athlete_id, limit),
    ).fetchall()
    return {"athlete": athlete, "sessions": [dict(s) for s in sessions]}


def athlete_profile(conn: sqlite3.Connection, athlete_id: int,
                    recent_window: int = 10) -> dict:
    """Derived athlete-centric profile: PRs, latest L-V profile, recent loads.

    Computed on demand from the athlete's rep history — no stored table, so
    it never goes stale on a rep insert / validity toggle / weight edit.
    Feeds the coach UI's load-suggestion advisor.
    """
    athlete = get_athlete(conn, athlete_id)
    if athlete is None:
        return {"athlete": None, "prs": {}, "lv_profile": {},
                "lv_profile_quality": "insufficient", "recent_loads": {},
                "session_count": 0, "last_session_at": None}

    pr = conn.execute(
        """SELECT MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.peak_speed_mps END) AS pr_speed_mps,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.peak_force_n   END) AS pr_force_n,
                  MAX(CASE WHEN COALESCE(r.valid,1)=1 THEN r.peak_power_w   END) AS pr_power_w,
                  MIN(CASE WHEN COALESCE(r.valid,1)=1 THEN json_extract(r.splits_s_json,'$."10"') END) AS pr_split_10m_s,
                  MIN(CASE WHEN COALESCE(r.valid,1)=1 THEN json_extract(r.splits_s_json,'$."40"') END) AS pr_split_40m_s,
                  COUNT(CASE WHEN COALESCE(r.valid,1)=1 THEN r.id END) AS valid_rep_count
           FROM reps r JOIN sessions s ON s.id = r.session_id
           WHERE s.athlete_id = ?""",
        (athlete_id,),
    ).fetchone()
    prs = dict(pr) if pr else {}

    sc = conn.execute(
        "SELECT COUNT(*) AS session_count, MAX(started_at) AS last_session_at "
        "FROM sessions WHERE athlete_id = ?",
        (athlete_id,),
    ).fetchone()

    recent = conn.execute(
        """SELECT r.f0_rel_nkg, r.v0_mps, r.pmax_rel_wkg, r.fv_slope_per_kg,
                  s.hmi_load_kg
           FROM reps r JOIN sessions s ON s.id = r.session_id
           WHERE s.athlete_id = ? AND COALESCE(r.valid,1) = 1
           ORDER BY s.started_at DESC, r.id DESC
           LIMIT ?""",
        (athlete_id, recent_window),
    ).fetchall()

    def _median(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        n = len(vals)
        mid = n // 2
        return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    lv = {
        "f0_rel_nkg": _median([r["f0_rel_nkg"] for r in recent]),
        "v0_mps": _median([r["v0_mps"] for r in recent]),
        "pmax_rel_wkg": _median([r["pmax_rel_wkg"] for r in recent]),
        "fv_slope_per_kg": _median([r["fv_slope_per_kg"] for r in recent]),
    }
    try:
        from insights import fv_orientation
        ori = fv_orientation(lv["fv_slope_per_kg"])
        lv["orientation"] = ori["tag"] if ori else None
    except Exception:
        lv["orientation"] = None

    usable = [r for r in recent
              if r["fv_slope_per_kg"] is not None and r["v0_mps"] is not None]
    loaded = [r for r in usable if r["hmi_load_kg"] is not None]
    distinct_loads = {round(r["hmi_load_kg"], 1) for r in loaded}
    # When sessions carry load data, require a spread of ≥2 loads; when no
    # load data exists at all, gate on the valid-rep count alone.
    load_ok = len(distinct_loads) >= 2 if loaded else True
    quality = "ok" if len(usable) >= 3 and load_ok else "insufficient"

    loads = conn.execute(
        """SELECT r.drill AS drill, s.hmi_load_kg AS load_kg, MAX(s.started_at)
           FROM reps r JOIN sessions s ON s.id = r.session_id
           WHERE s.athlete_id = ? AND r.drill IS NOT NULL
           GROUP BY r.drill""",
        (athlete_id,),
    ).fetchall()
    recent_loads = {r["drill"]: r["load_kg"] for r in loads
                    if r["load_kg"] is not None}

    return {
        "athlete": athlete,
        "prs": prs,
        "lv_profile": lv,
        "lv_profile_quality": quality,
        "recent_loads": recent_loads,
        "session_count": sc["session_count"] if sc else 0,
        "last_session_at": sc["last_session_at"] if sc else None,
    }


def load_session_reps(conn: sqlite3.Connection, session_id: int) -> dict:
    """Return a session's reps in the in-memory rep-dict shape (with samples
    rehydrated from samples_json). Designed for /api/sessions/{id}/load to
    bring a historical session back into state.athletic_reps.
    """
    import json as _json
    sess = conn.execute(
        "SELECT s.id, s.athlete_id, s.started_at, s.ended_at, s.notes, "
        "       a.name AS athlete_name, a.body_mass_kg, a.position_group "
        "FROM sessions s JOIN athletes a ON a.id = s.athlete_id "
        "WHERE s.id = ?", (session_id,)
    ).fetchone()
    if not sess:
        return {"session": None, "reps": []}
    rows = conn.execute(
        "SELECT * FROM reps WHERE session_id = ? ORDER BY started_t_offset_ms",
        (session_id,),
    ).fetchall()
    reps = []
    for i, row in enumerate(rows):
        d = dict(row)
        # Coerce validity to bool for the in-memory shape
        d["valid"] = bool(d.get("valid", 1))
        # Hydrate JSON blobs
        for json_col, target_key in [("splits_s_json", "splits_s"),
                                      ("step_events_json", "step_events"),
                                      ("asymmetry_json", "asymmetry"),
                                      ("samples_json", "samples")]:
            raw = d.pop(json_col, None)
            if raw:
                try: d[target_key] = _json.loads(raw)
                except Exception: d[target_key] = None
        # Re-shape into the in-memory rep dict the UI expects
        d["rep_idx"] = i + 1
        d["_meta"] = {
            "source": d.pop("source", None) or "db",
            "athlete_name": sess["athlete_name"],
            "body_mass_kg": sess["body_mass_kg"],
            "athlete_id": sess["athlete_id"],
            "session_id": session_id,
        }
        # Provide convenient extended-splits alias for UI
        if d.get("splits_s") and isinstance(d["splits_s"], dict):
            d["splits_s_extended"] = d["splits_s"]
        # is_eccentric stored as 0/1 → bool
        if d.get("is_eccentric") is not None:
            d["is_eccentric"] = bool(d["is_eccentric"])
        # Compute duration_s from started/ended offsets if missing
        if d.get("ended_t_offset_ms") is not None and d.get("started_t_offset_ms") is not None:
            d.setdefault("duration_s",
                         round((d["ended_t_offset_ms"] - d["started_t_offset_ms"]) / 1000.0, 3))
        reps.append(d)
    return {"session": dict(sess), "reps": reps}


def previous_rep_for_athlete(conn: sqlite3.Connection, athlete_id: int,
                              before_session_id: Optional[int] = None) -> Optional[dict]:
    """Most recent rep persisted for `athlete_id` BEFORE `before_session_id`.
    Used by the synthesis layer to compute vs-previous-session deltas.
    Returns the rep dict (rehydrated) or None if no prior rep exists.
    """
    sql = ("SELECT r.*, s.started_at, a.name AS athlete_name, "
           "       a.body_mass_kg, a.position_group "
           "FROM reps r "
           "JOIN sessions s ON s.id = r.session_id "
           "JOIN athletes a ON a.id = s.athlete_id "
           "WHERE s.athlete_id = ? ")
    params: list = [athlete_id]
    if before_session_id is not None:
        sql += "AND s.id < ? "
        params.append(before_session_id)
    sql += "ORDER BY s.started_at DESC, r.id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    d = dict(row)
    # Light hydration — synthesis only reads scalar metrics, not samples
    import json as _json
    if d.get("splits_s_json"):
        try: d["splits_s"] = _json.loads(d["splits_s_json"])
        except Exception: d["splits_s"] = {}
        d["splits_s_extended"] = d["splits_s"]
    return d


def _seed_default_rig(conn: sqlite3.Connection) -> None:
    """Ensure at least one rig row exists. Idempotent."""
    row = conn.execute("SELECT COUNT(*) AS n FROM rigs").fetchone()
    if row and row["n"] == 0:
        with conn:
            conn.execute("INSERT INTO rigs(id, name, location) VALUES (1, ?, ?)",
                         ("PPA-1", "Workshop"))


# Built-in resistance-curve library, seeded once when the table is empty.
# Each curve is six points expressed as % of the working resistance, so a
# curve scales with whatever load the coach sets.
_BUILTIN_CURVES = [
    ("Flat",         [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]),
    ("Sprint-accel", [150.0, 150.0, 130.0, 100.0, 70.0, 60.0]),
    ("Plateau-drop", [100.0, 100.0, 100.0, 100.0, 70.0, 40.0]),
    ("Pyramid",      [50.0, 90.0, 130.0, 130.0, 90.0, 50.0]),
    ("Block start",  [200.0, 160.0, 120.0, 100.0, 90.0, 80.0]),
    ("Light-Heavy",  [20.0, 40.0, 70.0, 100.0, 130.0, 150.0]),
    ("Late-load",    [30.0, 50.0, 70.0, 140.0, 150.0, 110.0]),
]
_CURVE_SCHEMA_VERSION = "2"  # bump to reseed builtins (e.g. units change)


def _seed_builtin_curves(conn: sqlite3.Connection) -> None:
    """Seed the resistance-curve library with the built-in shapes. Reseeds
    (wiping the table) when the schema version changes — e.g. the v1->v2
    move from absolute kg to % of working resistance."""
    if get_setting(conn, "curve_schema") == _CURVE_SCHEMA_VERSION:
        return
    with conn:
        conn.execute("DELETE FROM resistance_curves")
        for name, pts in _BUILTIN_CURVES:
            conn.execute(
                "INSERT INTO resistance_curves(name, points_json) VALUES (?, ?)",
                (name, json.dumps(pts)))
    set_setting(conn, "curve_schema", _CURVE_SCHEMA_VERSION)


def list_curves(conn: sqlite3.Connection) -> list[dict]:
    """List all saved resistance curves, oldest first (built-ins lead)."""
    rows = conn.execute(
        "SELECT id, name, points_json, created_at, updated_at "
        "FROM resistance_curves ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["points"] = json.loads(d.pop("points_json") or "[]")
        except Exception: d["points"] = []
        out.append(d)
    return out


def save_curve(conn: sqlite3.Connection, name: str, points: list,
               curve_id: Optional[int] = None) -> dict:
    """Upsert a resistance curve. With curve_id, updates that row (rename
    allowed). Without, updates by name if it exists, else inserts."""
    name = (name or "").strip()
    if not name:
        raise ValueError("curve name required")
    pts = [float(p) for p in points]
    pts_json = json.dumps(pts)
    now = _now_iso()
    with conn:
        if curve_id is not None:
            conn.execute(
                "UPDATE resistance_curves SET name=?, points_json=?, updated_at=? "
                "WHERE id=?", (name, pts_json, now, curve_id))
            cid = curve_id
        else:
            existing = conn.execute(
                "SELECT id FROM resistance_curves WHERE name=?", (name,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE resistance_curves SET points_json=?, updated_at=? "
                    "WHERE id=?", (pts_json, now, existing["id"]))
                cid = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO resistance_curves(name, points_json) VALUES (?, ?)",
                    (name, pts_json))
                cid = cur.lastrowid
    return {"id": cid, "name": name, "points": pts}


def delete_curve(conn: sqlite3.Connection, curve_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM resistance_curves WHERE id=?", (curve_id,))


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now')",
            (key, str(value)))


def list_rigs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, location, serial, notes, created_at FROM rigs ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def save_rig(conn: sqlite3.Connection, name: str,
             location: Optional[str] = None,
             serial: Optional[str] = None,
             notes: Optional[str] = None,
             rig_id: Optional[int] = None) -> dict:
    """Insert a new rig or upsert by id."""
    name = (name or "").strip()
    if not name:
        raise ValueError("rig name required")
    with conn:
        if rig_id is not None:
            conn.execute(
                "UPDATE rigs SET name = ?, location = ?, serial = ?, notes = ? WHERE id = ?",
                (name, location, serial, notes, rig_id),
            )
            return {"id": rig_id, "name": name, "location": location, "serial": serial, "notes": notes}
        cur = conn.execute(
            "INSERT INTO rigs(name, location, serial, notes) VALUES (?, ?, ?, ?)",
            (name, location, serial, notes),
        )
        return {"id": cur.lastrowid, "name": name, "location": location, "serial": serial, "notes": notes}


def list_templates(conn: sqlite3.Connection) -> list[dict]:
    """List all session-config templates, newest first."""
    import json as _json
    rows = conn.execute(
        "SELECT id, name, position_group, sport, config_json, notes, "
        "       created_at, updated_at FROM templates ORDER BY updated_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["config"] = _json.loads(d.pop("config_json") or "{}")
        except Exception: d["config"] = {}
        out.append(d)
    return out


def save_template(conn: sqlite3.Connection, name: str, config: dict,
                  position_group: Optional[str] = None,
                  sport: Optional[str] = None,
                  notes: Optional[str] = None) -> dict:
    """Upsert a template by name. Overwrites config + tags if name exists."""
    import json as _json
    name = (name or "").strip()
    if not name:
        raise ValueError("template name required")
    cfg_json = _json.dumps(config)
    now = _now_iso()
    with conn:
        existing = conn.execute(
            "SELECT id FROM templates WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE templates SET config_json = ?, position_group = ?, "
                "sport = ?, notes = ?, updated_at = ? WHERE id = ?",
                (cfg_json, position_group, sport, notes, now, existing["id"]),
            )
            tid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO templates(name, position_group, sport, config_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, position_group, sport, cfg_json, notes),
            )
            tid = cur.lastrowid
    return {"id": tid, "name": name, "config": config,
            "position_group": position_group, "sport": sport, "notes": notes}


def delete_template(conn: sqlite3.Connection, template_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


def update_athlete(conn: sqlite3.Connection, athlete_id: int, **fields) -> dict:
    """Partial update of an athlete row. Whitelisted fields only."""
    allowed = {"name", "body_mass_kg", "position_group", "sport", "level",
               "dob", "external_id", "squad_group", "tags"}
    sets = []
    vals: list = []
    for k, v in fields.items():
        if k not in allowed: continue
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return get_athlete(conn, athlete_id) or {}
    vals.append(athlete_id)
    with conn:
        conn.execute(f"UPDATE athletes SET {', '.join(sets)} WHERE id = ?", vals)
    return get_athlete(conn, athlete_id) or {}


def get_athlete(conn: sqlite3.Connection, athlete_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, name, body_mass_kg, position_group, sport, level, dob, "
        "       external_id, squad_group, tags, created_at FROM athletes WHERE id = ?",
        (athlete_id,),
    ).fetchone()
    return dict(row) if row else None


def athlete_progression(conn: sqlite3.Connection, athlete_id: int,
                        metric: str, agg: str = "MAX") -> list[dict]:
    """Time-series of a metric across the athlete's sessions.

    Aggregates across reps within each session (default MAX = best of session).
    Returns [{date, value, session_id, rep_count}, ...] sorted oldest-first.
    """
    safe_cols = {
        "peak_speed_mps", "peak_force_n", "peak_power_w",
        "peak_acceleration_mps2", "f0_rel_nkg", "v0_mps", "pmax_rel_wkg",
        "fv_slope_per_kg", "max_extension_m", "work_j", "impulse_ns",
        "time_to_max_v_s", "v_dropoff_pct", "step_freq_hz",
        "avg_step_length_m", "step_length_std_m",
    }
    if metric not in safe_cols:
        raise ValueError(f"unknown metric '{metric}'")
    if agg not in ("MAX", "MIN", "AVG"):
        agg = "MAX"
    rows = conn.execute(
        f"""SELECT s.id AS session_id,
                   DATE(s.started_at) AS date,
                   {agg}(CASE WHEN COALESCE(r.valid,1)=1 THEN r.{metric} END) AS value,
                   COUNT(CASE WHEN COALESCE(r.valid,1)=1 THEN r.id END) AS rep_count
           FROM sessions s
           LEFT JOIN reps r ON r.session_id = s.id
           WHERE s.athlete_id = ?
           GROUP BY s.id
           HAVING value IS NOT NULL
           ORDER BY s.started_at""",
        (athlete_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _end_rep_locked(conn: sqlite3.Connection, rep_id: int, t_offset_ms: int) -> dict:
    rep = conn.execute(
        "SELECT session_id, started_t_offset_ms FROM reps WHERE id = ?",
        (rep_id,),
    ).fetchone()
    if not rep:
        raise ValueError(f"rep {rep_id} not found")

    sid = rep["session_id"]
    t_start = rep["started_t_offset_ms"]
    aggs = _compute_rep_aggregates(conn, sid, t_start, t_offset_ms)
    ended_at = _now_iso()

    conn.execute(
        """UPDATE reps SET
              ended_at = ?, ended_t_offset_ms = ?,
              peak_speed_rpm = ?, peak_torque_pct = ?,
              total_distance_counts = ?, net_displacement_counts = ?,
              peak_decel_rpm_per_s = ?
           WHERE id = ?""",
        (
            ended_at,
            t_offset_ms,
            aggs["peak_speed_rpm"],
            aggs["peak_torque_pct"],
            aggs["total_distance_counts"],
            aggs["net_displacement_counts"],
            aggs["peak_decel_rpm_per_s"],
            rep_id,
        ),
    )
    return {
        "rep_id": rep_id,
        "ended_at": ended_at,
        "ended_t_offset_ms": t_offset_ms,
        **aggs,
    }


# --- samples ---

def insert_samples(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Bulk insert. rows: (session_id, t_offset_ms, status, speed_rpm, torque_pct,
    position_counts, bus_voltage_v). Idempotent on the (session_id, t_offset_ms)
    primary key via INSERT OR IGNORE — duplicate samples from a retry are dropped."""
    if not rows:
        return
    with conn:
        conn.executemany(
            """INSERT OR IGNORE INTO samples
                 (session_id, t_offset_ms, status, speed_rpm, torque_pct,
                  position_counts, bus_voltage_v)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def get_samples(
    conn: sqlite3.Connection,
    session_id: int,
    from_ms: int = 0,
    to_ms: Optional[int] = None,
) -> list[dict]:
    if to_ms is None:
        rows = conn.execute(
            """SELECT t_offset_ms, status, speed_rpm, torque_pct,
                      position_counts, bus_voltage_v
               FROM samples WHERE session_id = ? AND t_offset_ms >= ?
               ORDER BY t_offset_ms""",
            (session_id, from_ms),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT t_offset_ms, status, speed_rpm, torque_pct,
                      position_counts, bus_voltage_v
               FROM samples WHERE session_id = ? AND t_offset_ms BETWEEN ? AND ?
               ORDER BY t_offset_ms""",
            (session_id, from_ms, to_ms),
        ).fetchall()
    return [dict(r) for r in rows]
