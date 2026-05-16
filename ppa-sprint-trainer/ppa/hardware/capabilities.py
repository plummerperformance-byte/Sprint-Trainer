"""Hardware capability profiles.

One :class:`HardwareCapabilities` instance per machine type drives feature
gating across the codebase — the same way Trainitest's ``MachineCapabilities``
does. A single codebase then scales from Sprint Pro to Sprint Lite to the
conversion kit with no forks: a feature simply checks its flag.

All PPA hardware is single-cable (``max_cables == 1``) — Synchro is impossible
by construction.

Australian English throughout.
"""
from __future__ import annotations

from pydantic import BaseModel

from ppa.models.storage import ResistanceMode


class HardwareCapabilities(BaseModel):
    """Capability flags for one machine type. Every gated feature checks here."""

    machine_id: str
    machine_name: str

    # Load limits
    max_cables: int = 1                       # PPA hardware is always single-cable
    max_concentric_load_kg: float
    max_eccentric_load_kg: float
    min_load_increment_kg: float = 0.1

    # Speed limits
    max_concentric_speed_mps: float
    max_eccentric_speed_mps: float
    min_speed_limit_mps: float = 0.05

    # Gear
    has_gear_2: bool = False
    gear_2_load_multiplier: float = 2.0

    # Resistance features
    supports_variable_load: bool = False      # requires RS485
    supports_eccentric_boost: bool = False
    supports_pause_resume_load: bool = True
    supports_synchro: bool = False            # False for all PPA hardware
    supports_vibration: bool = False

    # Modes available
    available_resistance_modes: list[ResistanceMode]

    # Connectivity
    has_wifi: bool = True
    has_bluetooth: bool = False
    has_usb_c: bool = True
    has_rs485: bool = True

    # Sample rate
    sample_rate_hz: int = 333


PPA_SPRINT_PRO = HardwareCapabilities(
    machine_id="ppa_sprint_pro_v1",
    machine_name="PPA Sprint Pro",
    max_concentric_load_kg=30.0,
    max_eccentric_load_kg=30.0,
    max_concentric_speed_mps=14.0,
    max_eccentric_speed_mps=14.0,
    has_gear_2=True,
    supports_variable_load=False,             # flip to True when RS485 lands
    available_resistance_modes=[
        ResistanceMode.NORMAL, ResistanceMode.NFW, ResistanceMode.ISOTONIC,
    ],
)

PPA_SPRINT_LITE = HardwareCapabilities(
    machine_id="ppa_sprint_lite_v1",
    machine_name="PPA Sprint Lite",
    max_concentric_load_kg=15.0,
    max_eccentric_load_kg=15.0,
    max_concentric_speed_mps=10.0,
    max_eccentric_speed_mps=10.0,
    has_gear_2=False,
    supports_variable_load=False,
    available_resistance_modes=[ResistanceMode.NFW],
)

PPA_CONVERSION_KIT = HardwareCapabilities(
    machine_id="ppa_conversion_kit_v1",
    machine_name="PPA Conversion Kit",
    max_concentric_load_kg=20.0,              # depends on host vehicle
    max_eccentric_load_kg=20.0,
    max_concentric_speed_mps=12.0,
    max_eccentric_speed_mps=0.0,              # no powered retract
    supports_variable_load=False,
    supports_eccentric_boost=False,
    available_resistance_modes=[ResistanceMode.NORMAL],
)

#: All shipped profiles, keyed by machine_id.
PROFILES: dict[str, HardwareCapabilities] = {
    p.machine_id: p for p in (PPA_SPRINT_PRO, PPA_SPRINT_LITE, PPA_CONVERSION_KIT)
}
