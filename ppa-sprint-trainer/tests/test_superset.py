"""The auditable superset gate.

Every PPA model must carry every field of its 1080 proto parent with the same
type, and every extra field must be ``ppa_``-prefixed. If this passes, PPA
data is provably a strict superset of the 1080 schema.
"""
import pytest

from ppa.models import onezeroeightzero as o
from ppa.models import ppa as p

# (PpaModel, 1080 proto parent)
PAIRS = [
    (p.PpaClient, o.Client),
    (p.PpaMotion, o.Motion),
    (p.PpaSession, o.Session),
    (p.PpaBaseProto, o.BaseProto),
    (p.PpaExercise, o.Exercise),
    (p.PpaExerciseType, o.ExerciseType),
    (p.PpaArchType, o.ArchType),
    (p.PpaInstructor, o.Instructor),
    (p.PpaSet, o.Set),
    (p.PpaMotionGroup, o.MotionGroup),
    (p.PpaResistance, o.Resistance),
    (p.PpaAggregatedValues, o.AggregatedValues),
    (p.PpaDataSamples, o.DataSamples),
    (p.PpaDataSample, o.DataSample),
    (p.PpaSetViewSetting, o.SetViewSetting),
]


@pytest.mark.parametrize("ppa_model,base_model", PAIRS)
def test_every_1080_field_is_preserved(ppa_model, base_model):
    base_fields = base_model.model_fields
    ppa_fields = ppa_model.model_fields
    for name, info in base_fields.items():
        assert name in ppa_fields, (
            f"{ppa_model.__name__} drops 1080 field '{name}'"
        )
        assert ppa_fields[name].annotation == info.annotation, (
            f"{ppa_model.__name__}.{name} type drifts from 1080"
        )


@pytest.mark.parametrize("ppa_model,base_model", PAIRS)
def test_extra_fields_are_ppa_prefixed(ppa_model, base_model):
    extra = set(ppa_model.model_fields) - set(base_model.model_fields)
    for name in extra:
        assert name.startswith("ppa_"), (
            f"{ppa_model.__name__}.{name} is a non-1080 field and must be "
            f"'ppa_'-prefixed for the superset claim to be auditable"
        )
