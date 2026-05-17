-- PPA migration 002 — v2 storage schema (Trainitest Motion-table superset).
--
-- Adds new v2_* tables mirroring ppa/models/storage.py. The existing
-- athletes / sessions / reps tables are LEFT UNTOUCHED for backward
-- compatibility. NOT YET APPLIED to ppa.db — applied in a later phase.
--
-- Every table carries the Trainitest sync envelope:
--   row_version, local_db_row_version, is_locally_edited, is_locally_deleted,
--   created_utc, created_offset_minutes, edited_*, deleted_*.
-- guids are TEXT (UUID strings); booleans are INTEGER 0/1.

PRAGMA foreign_keys = ON;

-- ── Identity / organisation ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS v2_instructors (
    guid                   TEXT PRIMARY KEY,
    username               TEXT NOT NULL,
    display_name           TEXT,
    mail_adress            TEXT,
    kinde_organization_id  TEXT,
    use_set_data           INTEGER NOT NULL DEFAULT 0,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

-- ── People ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS v2_clients (
    guid                   TEXT PRIMARY KEY,
    instructor_guid        TEXT NOT NULL,
    display_name           TEXT,
    first_name             TEXT,
    last_name              TEXT,
    date_of_birth          TEXT,
    is_male                INTEGER,
    length                 REAL NOT NULL DEFAULT 0,
    weight                 REAL NOT NULL DEFAULT 0,
    mail_adress            TEXT,
    last_activated         TEXT,
    external_id            TEXT,
    tags                   TEXT,
    image_bytes            BLOB,
    ppa_position           TEXT,
    ppa_dominant_foot      TEXT,
    ppa_squad_tier         TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_client_measurements (
    guid                   TEXT PRIMARY KEY,
    client_guid            TEXT NOT NULL,
    entry_date             TEXT NOT NULL,
    length                 REAL NOT NULL DEFAULT 0,
    weight                 REAL NOT NULL DEFAULT 0,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_client_performance_measurements (
    guid                    TEXT PRIMARY KEY,
    client_guid             TEXT NOT NULL,
    exercise_type_guid      TEXT,
    exercise_arch_type_guid TEXT,
    measurement_date        TEXT NOT NULL,
    payload                 TEXT,
    side                    INTEGER,
    row_version             INTEGER NOT NULL DEFAULT 0,
    local_db_row_version    INTEGER NOT NULL DEFAULT 0,
    is_locally_edited       INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted      INTEGER NOT NULL DEFAULT 0,
    created_utc             TEXT NOT NULL,
    created_offset_minutes  INTEGER NOT NULL,
    edited_utc              TEXT,
    edited_offset_minutes   INTEGER,
    deleted_utc             TEXT,
    deleted_offset_minutes  INTEGER
);

CREATE TABLE IF NOT EXISTS v2_groups (
    guid                   TEXT PRIMARY KEY,
    instructor_guid        TEXT NOT NULL,
    name                   TEXT NOT NULL,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_group_x_clients (
    guid                   TEXT PRIMARY KEY,
    group_guid             TEXT NOT NULL,
    client_guid            TEXT NOT NULL,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

-- ── Equipment ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS v2_machines (
    guid                   TEXT PRIMARY KEY,
    serial_no              INTEGER NOT NULL,
    serial_no2             TEXT,
    name                   TEXT NOT NULL,
    location               TEXT,
    comment                TEXT,
    plc_mac_adress         TEXT NOT NULL,
    chassi                 INTEGER NOT NULL DEFAULT 0,
    current_version        TEXT,
    machine_type           INTEGER NOT NULL DEFAULT 0,
    is_light_flywheel      INTEGER NOT NULL DEFAULT 0,
    node_no                INTEGER NOT NULL DEFAULT 1,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_control_devices (
    guid                   TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    type                   TEXT,
    unique_guid            TEXT,
    last_online            TEXT NOT NULL,
    team_viewer_id         TEXT,
    team_viewer_name       TEXT,
    software_version       TEXT,
    comment                TEXT,
    windows_key            TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

-- ── Exercise catalogue ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS v2_exercise_arch_types (
    guid                   TEXT PRIMARY KEY,
    name                   TEXT,
    friendly_name          TEXT,
    presentation_type      INTEGER NOT NULL DEFAULT 0,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_exercise_types (
    guid                     TEXT PRIMARY KEY,
    exercise_arch_type_guid  TEXT NOT NULL,
    instructor_guid          TEXT,
    name                     TEXT NOT NULL,
    instructions             TEXT,
    hyperlink                TEXT,
    is_bilateral             INTEGER NOT NULL DEFAULT 0,
    is_start_with_concentric INTEGER NOT NULL DEFAULT 0,
    is_test                  INTEGER NOT NULL DEFAULT 0,
    machine_type             INTEGER NOT NULL DEFAULT 0,
    num_of_turns             INTEGER,
    flying_distance          REAL,
    stroke                   REAL,
    settings                 TEXT,
    tags                     TEXT,
    start_image_bytes        BLOB,
    stop_image_bytes         BLOB,
    row_version              INTEGER NOT NULL DEFAULT 0,
    local_db_row_version     INTEGER NOT NULL DEFAULT 0,
    is_locally_edited        INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted       INTEGER NOT NULL DEFAULT 0,
    created_utc              TEXT NOT NULL,
    created_offset_minutes   INTEGER NOT NULL,
    edited_utc               TEXT,
    edited_offset_minutes    INTEGER,
    deleted_utc              TEXT,
    deleted_offset_minutes   INTEGER
);

CREATE TABLE IF NOT EXISTS v2_protocols (
    guid                   TEXT PRIMARY KEY,
    instructor_guid        TEXT,
    name                   TEXT,
    description            TEXT,
    summary                TEXT,
    image_data             BLOB,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_protocol_x_exercise_types (
    guid                   TEXT PRIMARY KEY,
    protocol_guid          TEXT NOT NULL,
    exercise_type_guid     TEXT NOT NULL,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

-- ── Training-data hierarchy ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS v2_group_sessions (
    guid                   TEXT PRIMARY KEY,
    instructor_guid        TEXT NOT NULL,
    title                  TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_sessions (
    guid                   TEXT PRIMARY KEY,
    client_guid            TEXT NOT NULL,
    group_session_guid     TEXT,
    is_test                INTEGER NOT NULL DEFAULT 0,
    title                  TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_exercises (
    guid                   TEXT PRIMARY KEY,
    session_guid           TEXT NOT NULL,
    exercise_type_guid     TEXT NOT NULL,
    notes                  TEXT,
    is_test                INTEGER NOT NULL DEFAULT 0,
    sort_index             INTEGER NOT NULL DEFAULT 0,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_sets (
    guid                          TEXT PRIMARY KEY,
    exercise_guid                 TEXT NOT NULL,
    bargraph_visibility           INTEGER NOT NULL DEFAULT 0,
    is_average_visible            INTEGER NOT NULL DEFAULT 0,
    is_split_bar_sum_or_avg_active INTEGER NOT NULL DEFAULT 0,
    external_load                 INTEGER NOT NULL DEFAULT 0,
    phase                         INTEGER,
    unit                          INTEGER NOT NULL DEFAULT 0,
    unit_x                        INTEGER,
    children_row_version_sum      INTEGER NOT NULL DEFAULT 0,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_set_data (
    guid                   TEXT PRIMARY KEY,
    set_guid               TEXT NOT NULL,
    data                   BLOB,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_motion_groups (
    guid                   TEXT PRIMARY KEY,
    set_guid               TEXT NOT NULL,
    is_curve_selected      INTEGER NOT NULL DEFAULT 0,
    is_fv_selected         INTEGER NOT NULL DEFAULT 0,
    side                   INTEGER NOT NULL DEFAULT 0,
    color_rgb              TEXT,
    comment                TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_motions (
    guid                   TEXT PRIMARY KEY,
    motion_group_guid      TEXT NOT NULL,
    machine_guid           TEXT NOT NULL,
    is_eccentric           INTEGER NOT NULL DEFAULT 0,
    node_no                INTEGER NOT NULL DEFAULT 1,
    trim_type              INTEGER NOT NULL DEFAULT 0,
    start_index            INTEGER,
    stop_index             INTEGER,
    con_mass               REAL NOT NULL DEFAULT 0,
    ecc_mass               REAL NOT NULL DEFAULT 0,
    mode                   INTEGER NOT NULL DEFAULT 0,
    gear                   INTEGER NOT NULL DEFAULT 1,
    con_speed              REAL NOT NULL DEFAULT 0,
    ecc_speed              REAL NOT NULL DEFAULT 0,
    is_boost_active        INTEGER NOT NULL DEFAULT 0,
    boost_load             REAL NOT NULL DEFAULT 0,
    mass_at_v0             REAL,
    mass_at_v1             REAL,
    speed_v1               REAL,
    peak_speed             REAL,
    avg_speed              REAL,
    peak_force             REAL,
    avg_force              REAL,
    peak_power             REAL,
    avg_power              REAL,
    peak_acceleration      REAL,
    avg_acceleration       REAL,
    top_speed              REAL,
    work                   REAL,
    distance               REAL,
    total_distance         REAL,
    duration               REAL,
    total_duration         REAL,
    start_position         REAL,
    stop_position          REAL,
    data                   BLOB,
    ppa_environment        TEXT,
    ppa_wind_mps           REAL,
    ppa_surface_temp_c     REAL,
    ppa_video_url          TEXT,
    ppa_rpe                INTEGER,
    ppa_direction          TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

-- ── Live group-session orchestration ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS v2_active_sessions (
    guid                   TEXT PRIMARY KEY,
    instructor_guid        TEXT NOT NULL,
    session_state          INTEGER NOT NULL DEFAULT 0,
    title                  TEXT,
    planned_start_utc      TEXT,
    planned_start_offset   TEXT,
    completed_utc          INTEGER,
    completed_offset       INTEGER,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_active_session_exercises (
    guid                   TEXT PRIMARY KEY,
    active_session_guid    TEXT NOT NULL,
    exercise_type_guid     TEXT NOT NULL,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_active_session_participants (
    guid                   TEXT PRIMARY KEY,
    active_session_guid    TEXT NOT NULL,
    client_guid            TEXT NOT NULL,
    group_assignment       TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_active_session_results (
    guid                   TEXT PRIMARY KEY,
    active_session_guid    TEXT NOT NULL,
    client_guid            TEXT NOT NULL,
    exercise_type_guid     TEXT NOT NULL,
    result_data            BLOB,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS v2_active_session_targets (
    guid                   TEXT PRIMARY KEY,
    active_session_guid    TEXT NOT NULL,
    client_guid            TEXT NOT NULL,
    exercise_type_guid     TEXT NOT NULL,
    target_data            TEXT,
    row_version            INTEGER NOT NULL DEFAULT 0,
    local_db_row_version   INTEGER NOT NULL DEFAULT 0,
    is_locally_edited      INTEGER NOT NULL DEFAULT 0,
    is_locally_deleted     INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT NOT NULL,
    created_offset_minutes INTEGER NOT NULL,
    edited_utc             TEXT,
    edited_offset_minutes  INTEGER,
    deleted_utc            TEXT,
    deleted_offset_minutes INTEGER
);

-- ── Indexes ───────────────────────────────────────────────────────────────
-- Foreign-key guid columns
CREATE INDEX IF NOT EXISTS ix_v2_clients_instructor       ON v2_clients(instructor_guid);
CREATE INDEX IF NOT EXISTS ix_v2_client_meas_client       ON v2_client_measurements(client_guid);
CREATE INDEX IF NOT EXISTS ix_v2_client_perf_client       ON v2_client_performance_measurements(client_guid);
CREATE INDEX IF NOT EXISTS ix_v2_groups_instructor        ON v2_groups(instructor_guid);
CREATE INDEX IF NOT EXISTS ix_v2_gxc_group                ON v2_group_x_clients(group_guid);
CREATE INDEX IF NOT EXISTS ix_v2_gxc_client               ON v2_group_x_clients(client_guid);
CREATE INDEX IF NOT EXISTS ix_v2_exercise_types_archtype  ON v2_exercise_types(exercise_arch_type_guid);
CREATE INDEX IF NOT EXISTS ix_v2_exercise_types_instr     ON v2_exercise_types(instructor_guid);
CREATE INDEX IF NOT EXISTS ix_v2_protocols_instructor     ON v2_protocols(instructor_guid);
CREATE INDEX IF NOT EXISTS ix_v2_pxet_protocol            ON v2_protocol_x_exercise_types(protocol_guid);
CREATE INDEX IF NOT EXISTS ix_v2_pxet_exercise_type       ON v2_protocol_x_exercise_types(exercise_type_guid);
CREATE INDEX IF NOT EXISTS ix_v2_group_sessions_instr     ON v2_group_sessions(instructor_guid);
CREATE INDEX IF NOT EXISTS ix_v2_sessions_group_session   ON v2_sessions(group_session_guid);
CREATE INDEX IF NOT EXISTS ix_v2_exercises_session        ON v2_exercises(session_guid);
CREATE INDEX IF NOT EXISTS ix_v2_exercises_exercise_type  ON v2_exercises(exercise_type_guid);
CREATE INDEX IF NOT EXISTS ix_v2_sets_exercise            ON v2_sets(exercise_guid);
CREATE INDEX IF NOT EXISTS ix_v2_set_data_set             ON v2_set_data(set_guid);
CREATE INDEX IF NOT EXISTS ix_v2_motions_machine          ON v2_motions(machine_guid);
CREATE INDEX IF NOT EXISTS ix_v2_ase_active_session       ON v2_active_session_exercises(active_session_guid);
CREATE INDEX IF NOT EXISTS ix_v2_asp_active_session       ON v2_active_session_participants(active_session_guid);
CREATE INDEX IF NOT EXISTS ix_v2_asr_active_session       ON v2_active_session_results(active_session_guid);
CREATE INDEX IF NOT EXISTS ix_v2_ast_active_session       ON v2_active_session_targets(active_session_guid);

-- "Recent sessions" query
CREATE INDEX IF NOT EXISTS ix_v2_sessions_client_created
    ON v2_sessions(client_guid, created_utc DESC);

-- Motion group lookups
CREATE INDEX IF NOT EXISTS ix_v2_motions_motion_group
    ON v2_motions(motion_group_guid);
CREATE INDEX IF NOT EXISTS ix_v2_motion_groups_set
    ON v2_motion_groups(set_guid);

-- Sync queue scans (dirty / deleted rows) on the high-churn tables
CREATE INDEX IF NOT EXISTS ix_v2_sessions_sync   ON v2_sessions(is_locally_edited, is_locally_deleted);
CREATE INDEX IF NOT EXISTS ix_v2_motions_sync    ON v2_motions(is_locally_edited, is_locally_deleted);
CREATE INDEX IF NOT EXISTS ix_v2_clients_sync    ON v2_clients(is_locally_edited, is_locally_deleted);
CREATE INDEX IF NOT EXISTS ix_v2_motion_groups_sync ON v2_motion_groups(is_locally_edited, is_locally_deleted);
CREATE INDEX IF NOT EXISTS ix_v2_sets_sync       ON v2_sets(is_locally_edited, is_locally_deleted);
CREATE INDEX IF NOT EXISTS ix_v2_exercises_sync  ON v2_exercises(is_locally_edited, is_locally_deleted);
