"""Pydantic v2 models mirroring the canonical 1080 Motion proto schema.

Vendored from ``1080Motion/API`` @ commit
``3dc07c4a58fc0c3dfe066e9f7fd03cd654301281`` (see ``vendor/1080-api/``).

Each model is a faithful, verbatim mirror of a proto **data** message — proto
field names and enum members are kept exactly as written (so the JSON wire
shape matches 1080). The gRPC ``Get*Request`` / ``Get*Response`` envelopes are
deliberately NOT modelled (plumbing, not schema). Australian English used in
the docstrings.

Model → source proto file:
    BaseProto                                  -> Protos/base.proto
    Client, Gender                             -> Protos/client.proto
    Exercise                                   -> Protos/exercise.proto
    ExerciseType, ArchType, PresentationType   -> Protos/exercise_type.proto
    Instructor                                 -> Protos/instructor.proto
    Session                                    -> Protos/session.proto
    Set, MotionGroup, Motion, Resistance,
      AggregatedValues, DataSamples, DataSample,
      SetViewSetting, Phase, Unit,
      BargraphVisibility, Side                 -> Protos/set.proto

Type mapping: ``google.protobuf.Timestamp`` -> ``datetime | None``;
proto ``NullableInt32`` -> ``int | None``. Proto3 scalar defaults are used
(numbers 0, strings "", bools False, repeated []).
"""
from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field

VENDOR_COMMIT = "3dc07c4a58fc0c3dfe066e9f7fd03cd654301281"


class _Proto(BaseModel):
    """Base for every 1080 proto model — allows building by field name or by
    proto alias, so verbatim wire names survive even where a proto field name
    is not a legal Python identifier."""

    model_config = ConfigDict(populate_by_name=True)


# --------------------------------------------------------------------------
# Enums (Protos/set.proto, Protos/client.proto, Protos/exercise_type.proto)
# --------------------------------------------------------------------------

class Phase(IntEnum):
    NONE = 0
    CONCENTRIC = 1
    ECCENTRIC = 2


class Unit(IntEnum):
    POSITION = 0
    SPEED = 1
    ACCELERATION = 2
    FORCE = 3
    POWER = 4
    TIME = 5


class BargraphVisibility(IntEnum):
    PEAK_ONLY = 0
    AVERAGE_ONLY = 1
    BOTH = 2


class Side(IntEnum):
    LEFT = 0
    RIGHT = 1
    BILATERAL = 2


class Gender(IntEnum):
    Unspecified = 0
    Male = 1
    Female = 2


class PresentationType(IntEnum):
    REPETITIONS = 0
    LINEAR = 1
    CHANGE_OF_DIRECTION = 2


# --------------------------------------------------------------------------
# Protos/base.proto
# --------------------------------------------------------------------------

class BaseProto(_Proto):
    """Common header embedded in most messages."""

    Guid: str = ""
    Created: datetime | None = None
    Edited: datetime | None = None
    Deleted: datetime | None = None


# --------------------------------------------------------------------------
# Protos/set.proto — training-data messages
# --------------------------------------------------------------------------

class AggregatedValues(_Proto):
    """Calculated peak/average values for a motion (full 13-field proto form)."""

    peak_speed: float = 0.0          # m/s
    avg_speed: float = 0.0           # m/s
    peak_force: float = 0.0          # N
    avg_force: float = 0.0           # N
    peak_power: float = 0.0          # W
    avg_power: float = 0.0           # W
    peak_acceleration: float = 0.0   # m/s^2
    avg_acceleration: float = 0.0    # m/s^2
    start_position: float = 0.0      # m
    stop_position: float = 0.0       # m
    work: float = 0.0                # J (integral of force over position)
    distance: float = 0.0            # m (accumulated)
    duration: float = 0.0            # s


class DataSample(_Proto):
    """A raw sample as measured from the machine.

    ``power`` is the proto's sixth field; on the binary wire only the first
    five are present and power is derived (see ``ppa.codecs.sample_bytes``)."""

    position: float = 0.0       # m
    time: float = 0.0           # s, accumulated from 0
    speed: float = 0.0          # m/s
    acceleration: float = 0.0   # m/s^2
    force: float = 0.0          # N
    power: float = 0.0          # W


class DataSamples(_Proto):
    """A list of samples plus optional trim indices (proto ``NullableInt32``
    -> ``int | None``)."""

    data: list[DataSample] = Field(default_factory=list)
    start_index: int | None = None
    stop_index: int | None = None


class Resistance(_Proto):
    """Machine resistance settings used during a motion."""

    con_mass: float = 0.0          # concentric load, kg
    ecc_mass: float = 0.0          # eccentric load, kg
    mode: int = 0                  # 0 Normal, 1 No flying weight (NFW), 2 Isotonic
    gear: int = 0                  # 1 or 2
    con_speed_limit: float = 0.0   # concentric/resisted speed limit, m/s
    ecc_speed_limit: float = 0.0   # eccentric/assisted speed limit, m/s


class SetViewSetting(_Proto):
    """Controls how a set's data is presented."""

    phase: Phase = Phase.NONE
    unit: Unit = Unit.POSITION
    bargraph_visibility: BargraphVisibility = BargraphVisibility.PEAK_ONLY
    is_average_visible: bool = False
    is_average_bargraphs_visible: bool = False


class Motion(_Proto):
    """A single movement from one machine."""

    common: BaseProto | None = None
    motion_group_guid: str = ""
    resistance: Resistance | None = None
    samples: DataSamples | None = None
    values: AggregatedValues | None = None
    node_no: int = 0                 # 1 = left/single machine, 2 = right machine
    comment: str = ""                # not used — see MotionGroup.comment
    is_eccentric: bool = False       # True = eccentric/assisted
    is_curve_selected: bool = False


class MotionGroup(_Proto):
    """A container of motions — one rep (or one run for a linear sprint)."""

    common: BaseProto | None = None
    set_guid: str = ""
    motions: list[Motion] = Field(default_factory=list)
    # Proto field is literally ``_is_curve_selected``; a leading underscore is
    # not a legal Pydantic field name, so it is aliased to keep the wire name.
    is_curve_selected: bool = Field(default=False, alias="_is_curve_selected")
    side: Side = Side.LEFT
    color_rgb: str = ""              # RGB hex
    comment: str = ""                # user-entered comment


class Set(_Proto):
    """A container of motion groups (reps) within an exercise."""

    common: BaseProto | None = None
    exercise_guid: str = ""
    motion_groups: list[MotionGroup] = Field(default_factory=list)
    external_load: int = 0           # total external load in kg (smith-press syncro)
    view_settings: SetViewSetting | None = None


# --------------------------------------------------------------------------
# Protos/client.proto
# --------------------------------------------------------------------------

class Client(_Proto):
    """A 1080 client (the athlete). ``firstname``/``lastname``/``is_male`` are
    proto-deprecated but kept verbatim for a faithful mirror."""

    common: BaseProto | None = None
    instructor_guid: str = ""
    firstname: str = ""              # deprecated — use display_name
    lastname: str = ""               # deprecated — use display_name
    is_male: bool = False            # deprecated — use gender
    length: float = 0.0              # height, cm
    weight: float = 0.0              # body weight, kg
    mail_address: str = ""
    display_name: str = ""
    gender: Gender = Gender.Unspecified
    group_name: str = ""
    tags: list[str] = Field(default_factory=list)
    external_id: str = ""


# --------------------------------------------------------------------------
# Protos/exercise.proto
# --------------------------------------------------------------------------

class Exercise(_Proto):
    """An exercise within a session — an instance of an exercise type."""

    common: BaseProto | None = None
    session_guid: str = ""
    exercise_type_guid: str = ""
    notes: str = ""


# --------------------------------------------------------------------------
# Protos/exercise_type.proto
# --------------------------------------------------------------------------

class ArchType(_Proto):
    """The arch type of an exercise type — drives data presentation."""

    name: str = ""
    presentation_type: PresentationType = PresentationType.REPETITIONS


class ExerciseType(_Proto):
    """An entry in the exercise library."""

    common: BaseProto | None = None
    instructor_guid: str = ""        # empty -> public exercise type
    name: str = ""
    instructions: str = ""
    is_bilateral: bool = False
    is_start_with_concentric: bool = False
    hyperlink: str = ""
    arch_type: ArchType | None = None


# --------------------------------------------------------------------------
# Protos/instructor.proto
# --------------------------------------------------------------------------

class Instructor(_Proto):
    """The instructor / account owner. ``mail_adress`` keeps the proto's
    spelling verbatim."""

    common: BaseProto | None = None
    firstname: str = ""
    lastname: str = ""
    date_of_birth: datetime | None = None
    is_male: bool = False
    mail_adress: str = ""


# --------------------------------------------------------------------------
# Protos/session.proto
# --------------------------------------------------------------------------

class Session(_Proto):
    """A training session for a client."""

    common: BaseProto | None = None
    client_guid: str = ""
