"""PPA persistence-layer models — a strict superset of Trainitest's storage schema.

Trainitest (the 1080 desktop client) keeps a local EF Core SQLite cache at
``C:\\ProgramData\\1080Motion\\Databases\\LocalDb.sqlite``. That schema — not the
proto wire format and not the public REST API — is the canonical *storage*
reference: the ``Motion`` table alone carries 30+ columns (all aggregates, the
resistance settings used, the eccentric boost, the 3-parameter variable-load
profile, and the raw sample BLOB).

These models mirror that schema (snake_case Python names for the PascalCase
SQL columns) and add PPA-only fields, every one ``ppa_``-prefixed so the
superset claim stays auditable. Every entity carries a :class:`SyncEnvelope` —
Trainitest's two-way-sync pattern — so PPA is multi-device-sync-ready from day
one.

Source: ``vendor/1080-api`` proto reference + the LocalDb.sqlite DDL
(``1080_LocalDb_schema.sql``, 28 tables). Australian English throughout.

Three model layers exist in ``ppa.models``:
  * ``onezeroeightzero`` — the proto wire/export format.
  * ``ppa``               — the proto superset (wire-level PPA additions).
  * ``storage`` (this)    — the persistence layer (Trainitest table superset).
"""
from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ppa.models.onezeroeightzero import (  # reused 1080 enums
    BargraphVisibility,
    Phase,
    PresentationType,
    Side,
    Unit,
)

__all__ = [
    "ResistanceMode", "BargraphVisibility", "Phase", "PresentationType",
    "Side", "Unit", "SyncEnvelope", "SyncedModel",
    "InstructorStorage", "ClientStorage", "ClientMeasurementStorage",
    "ClientPerformanceMeasurementStorage", "GroupStorage", "GroupXClientStorage",
    "MachineStorage", "ControlDeviceStorage", "ExerciseArchTypeStorage",
    "ExerciseTypeStorage", "ProtocolStorage", "ProtocolXExerciseTypeStorage",
    "GroupSessionStorage", "SessionStorage", "ExerciseStorage", "SetStorage",
    "SetDataStorage", "MotionGroupStorage", "MotionStorage",
    "ActiveSessionStorage", "ActiveSessionExerciseStorage",
    "ActiveSessionParticipantStorage", "ActiveSessionResultStorage",
    "ActiveSessionTargetStorage",
]


class ResistanceMode(IntEnum):
    """Trainitest ``Motion.Mode`` — the machine's resistance mode."""

    NORMAL = 0
    NFW = 1       # "no flying weight"
    ISOTONIC = 2


# --------------------------------------------------------------------------
# Sync envelope — present on every Trainitest table; PPA adopts it verbatim.
# --------------------------------------------------------------------------

class SyncEnvelope(BaseModel):
    """Mirrors Trainitest's sync columns. Required on every PPA entity so the
    local DB can two-way-sync against a server later."""

    row_version: int = 0
    local_db_row_version: int = 0
    is_locally_edited: bool = False
    is_locally_deleted: bool = False
    created_utc: datetime
    created_offset_minutes: int          # e.g. 600 for AEST (UTC+10)
    edited_utc: datetime | None = None
    edited_offset_minutes: int | None = None
    deleted_utc: datetime | None = None
    deleted_offset_minutes: int | None = None


class SyncedModel(BaseModel):
    """Base for every storage entity.

    Mutating any field after construction automatically flips
    ``sync.is_locally_edited`` to True — the local DB then knows the row is
    dirty and must be pushed on the next sync. Population during construction
    does not trip the flag.
    """

    # base64 keeps binary BLOB fields round-tripping cleanly through JSON.
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name != "sync" and not name.startswith("_"):
            sync = self.__dict__.get("sync")
            if isinstance(sync, SyncEnvelope):
                sync.is_locally_edited = True


class _Entity(SyncedModel):
    """A Trainitest entity — a GUID primary key plus the sync envelope."""

    guid: UUID
    sync: SyncEnvelope


# --------------------------------------------------------------------------
# Identity / organisation
# --------------------------------------------------------------------------

class InstructorStorage(_Entity):
    """The coach account. Mirrors ``Instructor``."""

    username: str
    display_name: str | None = None
    mail_adress: str | None = None       # Trainitest's spelling, kept verbatim
    kinde_organization_id: str | None = None
    use_set_data: bool = False


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

class ClientStorage(_Entity):
    """An athlete. Mirrors ``Client`` + PPA additions."""

    instructor_guid: UUID
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: str | None = None
    is_male: bool | None = None
    length: float = 0.0                  # height, cm
    weight: float = 0.0                  # body weight, kg
    mail_adress: str | None = None
    last_activated: str | None = None
    external_id: str | None = None
    tags: str | None = None
    image_bytes: bytes | None = None
    # PPA-only additions
    ppa_position: str | None = None      # rugby position
    ppa_dominant_foot: str | None = None  # left | right
    ppa_squad_tier: str | None = None    # e.g. 1st XV, academy


class ClientMeasurementStorage(_Entity):
    """A dated height/weight measurement. Mirrors ``ClientMeasurement``."""

    client_guid: UUID
    entry_date: str
    length: float = 0.0
    weight: float = 0.0


class ClientPerformanceMeasurementStorage(_Entity):
    """A dated performance measurement. Mirrors ``ClientPerformanceMeasurement``."""

    client_guid: UUID
    exercise_type_guid: UUID | None = None
    exercise_arch_type_guid: UUID | None = None
    measurement_date: str
    payload: str | None = None
    side: Side | None = None


class GroupStorage(_Entity):
    """An athlete group / squad. Mirrors ``Group``."""

    instructor_guid: UUID
    name: str


class GroupXClientStorage(_Entity):
    """Group↔client join row. Mirrors ``GroupXClient``."""

    group_guid: UUID
    client_guid: UUID


# --------------------------------------------------------------------------
# Equipment
# --------------------------------------------------------------------------

class MachineStorage(_Entity):
    """A physical machine. Mirrors ``Machine``."""

    serial_no: int
    serial_no2: str | None = None
    name: str
    location: str | None = None
    comment: str | None = None
    plc_mac_adress: str                  # Trainitest's spelling, kept verbatim
    chassi: int = 0
    current_version: str | None = None
    machine_type: int = 0
    is_light_flywheel: bool = False
    node_no: int = 1                     # always 1 on PPA single-cable hardware


class ControlDeviceStorage(_Entity):
    """A control device (tablet / PC). Mirrors ``ControlDevice``."""

    name: str
    type: str | None = None
    unique_guid: str | None = None
    last_online: str
    team_viewer_id: str | None = None
    team_viewer_name: str | None = None
    software_version: str | None = None
    comment: str | None = None
    windows_key: str | None = None


# --------------------------------------------------------------------------
# Exercise catalogue
# --------------------------------------------------------------------------

class ExerciseArchTypeStorage(_Entity):
    """An exercise archetype. Mirrors ``ExerciseArchType``."""

    name: str | None = None
    friendly_name: str | None = None
    presentation_type: PresentationType = PresentationType.REPETITIONS


class ExerciseTypeStorage(_Entity):
    """A library exercise type. Mirrors ``ExerciseType``.

    ``settings`` is the raw 1080 exercise-config JSON string
    (``ResistanceSettings`` / ``LinearSprintSettings`` / ``KpiSettings`` …);
    kept as text here, parsed by a higher layer.
    """

    exercise_arch_type_guid: UUID
    instructor_guid: UUID | None = None  # empty -> public exercise type
    name: str
    instructions: str | None = None
    hyperlink: str | None = None
    is_bilateral: bool = False
    is_start_with_concentric: bool = False
    is_test: bool = False
    machine_type: int = 0
    num_of_turns: int | None = None
    flying_distance: float | None = None
    stroke: float | None = None
    settings: str | None = None          # raw 1080 config JSON
    tags: str | None = None
    start_image_bytes: bytes | None = None
    stop_image_bytes: bytes | None = None


class ProtocolStorage(_Entity):
    """A protocol — an ordered exercise-type collection. Mirrors ``Protocol``."""

    instructor_guid: UUID | None = None
    name: str | None = None
    description: str | None = None
    summary: str | None = None
    image_data: bytes | None = None


class ProtocolXExerciseTypeStorage(_Entity):
    """Protocol↔exercise-type join row. Mirrors ``ProtocolXExerciseType``."""

    protocol_guid: UUID
    exercise_type_guid: UUID


# --------------------------------------------------------------------------
# Training-data hierarchy: GroupSession > Session > Exercise > Set >
#                          MotionGroup > Motion  (+ SetData)
# --------------------------------------------------------------------------

class GroupSessionStorage(_Entity):
    """A group training session. Mirrors ``GroupSession``."""

    instructor_guid: UUID
    title: str | None = None


class SessionStorage(_Entity):
    """One athlete's training session. Mirrors ``Session``."""

    client_guid: UUID
    group_session_guid: UUID | None = None
    is_test: bool = False
    title: str | None = None


class ExerciseStorage(_Entity):
    """An exercise within a session. Mirrors ``Exercise``."""

    session_guid: UUID
    exercise_type_guid: UUID
    notes: str | None = None
    is_test: bool = False
    sort_index: int = 0


class SetStorage(_Entity):
    """A set within an exercise. Mirrors ``Set``."""

    exercise_guid: UUID
    bargraph_visibility: BargraphVisibility = BargraphVisibility.PEAK_ONLY
    is_average_visible: bool = False
    is_split_bar_sum_or_avg_active: bool = False
    external_load: int = 0
    phase: Phase | None = None
    unit: Unit = Unit.POSITION
    unit_x: Unit | None = None
    children_row_version_sum: int = 0


class SetDataStorage(_Entity):
    """The whole-set BLOB form of a set. Mirrors ``SetData``."""

    set_guid: UUID
    data: bytes | None = None            # BLOB — decode via ppa.codecs


class MotionGroupStorage(_Entity):
    """A rep (or a run, for a linear sprint). Mirrors ``MotionGroup``."""

    set_guid: UUID
    is_curve_selected: bool = False
    is_fv_selected: bool = False
    side: Side = Side.LEFT
    color_rgb: str | None = None
    comment: str | None = None


class MotionStorage(_Entity):
    """One movement from one machine — the richest entity. Mirrors ``Motion``.

    All aggregate fields are derived from the sample stream by ``analytics.py``
    (acceleration included — it is a finite difference, not a sensor reading).
    """

    motion_group_guid: UUID
    machine_guid: UUID

    is_eccentric: bool = False
    node_no: int = 1                     # always 1 on PPA hardware
    trim_type: int = 0
    start_index: int | None = None
    stop_index: int | None = None

    # Resistance settings used at the time of the rep
    con_mass: float = 0.0                # concentric load, kg
    ecc_mass: float = 0.0                # eccentric load, kg
    mode: ResistanceMode = ResistanceMode.NORMAL
    gear: int = 1
    con_speed: float = 0.0               # concentric speed limit, m/s
    ecc_speed: float = 0.0               # eccentric speed limit, m/s
    is_boost_active: bool = False
    boost_load: float = 0.0              # eccentric boost load, kg

    # Variable-load profile (load interpolated standstill -> target speed)
    mass_at_v0: float | None = None      # load at standstill, kg
    mass_at_v1: float | None = None      # load at target speed, kg
    speed_v1: float | None = None        # target speed, m/s

    # Aggregates (all derived from samples)
    peak_speed: float | None = None
    avg_speed: float | None = None
    peak_force: float | None = None
    avg_force: float | None = None
    peak_power: float | None = None
    avg_power: float | None = None
    peak_acceleration: float | None = None
    avg_acceleration: float | None = None
    top_speed: float | None = None

    # Totals / geometry
    work: float | None = None            # J
    distance: float | None = None        # m, this rep
    total_distance: float | None = None  # m, cumulative session
    duration: float | None = None        # s, this rep
    total_duration: float | None = None  # s, cumulative session
    start_position: float | None = None  # m
    stop_position: float | None = None   # m

    # Raw samples — binary BLOB, encoded via ppa.codecs.sample_bytes
    data: bytes | None = None

    # PPA-only additions
    ppa_environment: str | None = None       # track | grass | turf | treadmill
    ppa_wind_mps: float | None = None
    ppa_surface_temp_c: float | None = None
    ppa_video_url: str | None = None
    ppa_rpe: int | None = None               # 1-10
    # Sprint context — explicit, not implied from is_eccentric (which is about
    # the gym concentric/eccentric phase). Kept ppa_-prefixed per the hard
    # constraint (the brief's 4.6 wrote it unprefixed).
    ppa_direction: Literal["resisted", "assisted"] | None = None


# --------------------------------------------------------------------------
# Live group-session orchestration — Trainitest separates live training state
# from permanent storage; PPA mirrors that separation.
# --------------------------------------------------------------------------

class ActiveSessionStorage(_Entity):
    """A live (in-progress) group session. Mirrors ``ActiveSession``."""

    instructor_guid: UUID
    session_state: int = 0
    title: str | None = None
    planned_start_utc: str | None = None
    planned_start_offset: str | None = None
    completed_utc: int | None = None
    completed_offset: int | None = None


class ActiveSessionExerciseStorage(_Entity):
    """An exercise in a live session. Mirrors ``ActiveSessionExercise``."""

    active_session_guid: UUID
    exercise_type_guid: UUID


class ActiveSessionParticipantStorage(_Entity):
    """A participant in a live session. Mirrors ``ActiveSessionParticipant``."""

    active_session_guid: UUID
    client_guid: UUID
    group_assignment: str | None = None


class ActiveSessionResultStorage(_Entity):
    """A live-session result. Mirrors ``ActiveSessionResult``."""

    active_session_guid: UUID
    client_guid: UUID
    exercise_type_guid: UUID
    result_data: bytes | None = None     # BLOB


class ActiveSessionTargetStorage(_Entity):
    """A live-session target. Mirrors ``ActiveSessionTarget``."""

    active_session_guid: UUID
    client_guid: UUID
    exercise_type_guid: UUID
    target_data: str | None = None
