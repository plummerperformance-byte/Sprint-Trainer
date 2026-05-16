"""PPA superset models.

Each ``PpaX`` subclasses the corresponding 1080 proto model from
``onezeroeightzero`` and adds only PPA-specific fields, every one prefixed
``ppa_`` so the "strict superset of 1080" claim stays auditable
(``tests/test_superset.py`` enforces it). Models with no PPA additions yet are
pass-through subclasses — placeholders for future fields.

Australian English used in the docstrings.
"""
from __future__ import annotations

from . import onezeroeightzero as o


# --- models with PPA additions --------------------------------------------

class PpaClient(o.Client):
    """1080 Client plus PPA athlete metadata."""

    ppa_position: str = ""        # rugby position — e.g. prop, halfback, fullback
    ppa_dominant_foot: str = ""   # left | right
    ppa_squad_tier: str = ""      # e.g. 1st XV, academy, development


class PpaMotion(o.Motion):
    """1080 Motion plus PPA environmental context."""

    ppa_environment: str = ""              # track | grass | turf | treadmill
    ppa_wind_mps: float | None = None      # wind speed, m/s (positive = tailwind)
    ppa_surface_temp_c: float | None = None  # surface temperature, deg C
    ppa_video_url: str = ""                # link to a synced video of the rep


class PpaSession(o.Session):
    """1080 Session plus PPA session-level context."""

    ppa_rpe: int | None = None    # rating of perceived exertion, 1-10
    ppa_session_type: str = ""    # max-velo | accel | resisted | contrast


# --- pass-through placeholders (no PPA additions yet) ---------------------

class PpaBaseProto(o.BaseProto):
    pass


class PpaExercise(o.Exercise):
    pass


class PpaExerciseType(o.ExerciseType):
    pass


class PpaArchType(o.ArchType):
    pass


class PpaInstructor(o.Instructor):
    pass


class PpaSet(o.Set):
    pass


class PpaMotionGroup(o.MotionGroup):
    pass


class PpaResistance(o.Resistance):
    pass


class PpaAggregatedValues(o.AggregatedValues):
    pass


class PpaDataSamples(o.DataSamples):
    pass


class PpaDataSample(o.DataSample):
    pass


class PpaSetViewSetting(o.SetViewSetting):
    pass
