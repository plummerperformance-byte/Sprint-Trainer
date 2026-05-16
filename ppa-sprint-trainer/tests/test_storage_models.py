"""Tests for the v2 storage models and hardware capability profiles."""
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from ppa.hardware import capabilities as caps
from ppa.models import storage
from ppa.models.storage import MotionStorage, SyncedModel, SyncEnvelope

# Every concrete storage entity.
STORAGE_MODELS = [
    getattr(storage, name)
    for name in storage.__all__
    if name.endswith("Storage") and isinstance(getattr(storage, name), type)
    and issubclass(getattr(storage, name), SyncedModel)
]


def _sync() -> SyncEnvelope:
    return SyncEnvelope(
        created_utc=datetime(2026, 5, 16, 10, 0, 0),
        created_offset_minutes=600,
    )


def _value_for(annotation):
    """A valid value for a required field of the given annotation."""
    return {
        UUID: uuid4(),
        str: "x",
        int: 0,
        float: 0.0,
        bool: False,
        bytes: b"",
        datetime: datetime(2026, 5, 16, 10, 0, 0),
        SyncEnvelope: _sync(),
    }[annotation]


def _build(model_cls):
    """Construct a model_cls with every required field populated."""
    kwargs = {}
    for name, field in model_cls.model_fields.items():
        if field.is_required():
            kwargs[name] = _value_for(field.annotation)
    return model_cls(**kwargs)


def test_every_storage_model_discovered():
    # Sanity: the brief calls for 14+ entities.
    assert len(STORAGE_MODELS) >= 14


@pytest.mark.parametrize("model_cls", STORAGE_MODELS,
                         ids=[m.__name__ for m in STORAGE_MODELS])
def test_round_trip(model_cls):
    obj = _build(model_cls)
    parsed = model_cls.model_validate_json(obj.model_dump_json())
    assert parsed == obj


@pytest.mark.parametrize("model_cls", STORAGE_MODELS,
                         ids=[m.__name__ for m in STORAGE_MODELS])
def test_carries_sync_envelope(model_cls):
    obj = _build(model_cls)
    assert isinstance(obj.sync, SyncEnvelope)


def test_sync_flag_clean_on_construction():
    m = _build(MotionStorage)
    assert m.sync.is_locally_edited is False


def test_sync_flag_flips_on_mutation():
    m = _build(MotionStorage)
    m.con_mass = 9.0
    assert m.sync.is_locally_edited is True


def test_sync_flag_flips_on_any_storage_model():
    c = _build(storage.ClientStorage)
    assert c.sync.is_locally_edited is False
    c.display_name = "Adam P"
    assert c.sync.is_locally_edited is True


def test_motion_blob_round_trips():
    m = _build(MotionStorage)
    m.data = b"\x01\x02\x03\x04binary-blob"
    reparsed = MotionStorage.model_validate_json(m.model_dump_json())
    assert reparsed.data == b"\x01\x02\x03\x04binary-blob"


# --- hardware capability profiles -----------------------------------------

def test_three_profiles_load():
    for profile in (caps.PPA_SPRINT_PRO, caps.PPA_SPRINT_LITE,
                    caps.PPA_CONVERSION_KIT):
        assert isinstance(profile, caps.HardwareCapabilities)
    assert set(caps.PROFILES) == {
        "ppa_sprint_pro_v1", "ppa_sprint_lite_v1", "ppa_conversion_kit_v1",
    }


def test_all_ppa_hardware_is_single_cable():
    for profile in caps.PROFILES.values():
        assert profile.max_cables == 1, f"{profile.machine_id} is not single-cable"
        assert profile.supports_synchro is False
