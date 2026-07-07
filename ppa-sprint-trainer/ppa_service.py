"""ppa_service - FastAPI control plane for the HCFA SV-X3E rig.

Owns the single COM6 connection, polls the drive at 10 Hz in a background
task, and exposes:
  - GET  /api/state           -> latest snapshot (cached)
  - GET  /api/recent?n=N      -> last N samples from the in-memory ring buffer
  - WS   /api/stream          -> live updates (10 Hz)
  - POST /api/arm             -> arm / disarm writes (typed confirmation required)
  - POST /api/control/torque_limit       -> P03.11/.12 (writes only when armed)
  - POST /api/control/torque_limit_source -> P03.08
  - POST /api/control/control_mode        -> P00.01
  - GET  /                    -> the static dashboard
  - POST /api/athletes                    -> create athlete
  - GET  /api/athletes                    -> list athletes
  - POST /api/sessions/start              -> start a session
  - POST /api/sessions/{id}/end           -> end a session
  - GET  /api/sessions                    -> list sessions
  - GET  /api/sessions/{id}               -> session + reps
  - GET  /api/sessions/{id}/samples       -> raw samples

Safety:
  - Service starts with armed=False.  No write endpoints can fire until armed.
  - Arming requires posting {"confirm": "ARM"} as the body.
  - All writes also need armed=true on the underlying PPADrive instance.
  - Torque limit capped at 300% (the factory ceiling) by ppa_drive.

Persistence:
  - SQLite at ppa.db alongside persistence.py (override with --db).  Schema in persistence.py.
  - Sessions/reps/samples persist across process restart.
  - Polling loop continues to run + serve WS even if DB writes fail.

Network:
  - Default bind 127.0.0.1:8765 (loopback only).
  - For phone-on-LAN access, run with --host 0.0.0.0; only safe on a private LAN.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ppa_drive import PPADrive, TORQUE_LIMIT_MAX_PERCENT
import persistence
import analytics
from ppa.analytics import load_advisor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ppa_service")

POLL_HZ = 10
PERIOD = 1.0 / POLL_HZ
IDLE_POLL_HZ = 3  # slower drive polling when phase_c is idle (live view only)
IDLE_PERIOD = 1.0 / IDLE_POLL_HZ
COLD_REFRESH_EVERY = 10  # refresh slow-changing fields (temp/fault/mode/torque_limit) every Nth cycle
# Drive serial baud — bumped from 9600 → 115200 on 2026-05-20 via P09.01 write.
# Override at launch with PPA_DRIVE_BAUD env var if recovery is needed.
DRIVE_BAUD = int(os.environ.get("PPA_DRIVE_BAUD", "115200"))
RING_SIZE = 6000  # ~10 minutes at 10 Hz
VELOCITY_CAP_GAIN = 10.0  # kg of extra resistance per m/s over the velocity cap
CURVE_POINTS = 6          # resistance-curve control points (evenly spaced on x)
CURVE_PCT_MAX = 300       # max curve point — % of working resistance
CURVE_VELOCITY_MAX = 8.0  # m/s — x-axis span for a velocity-based resistance curve
STATIC_DIR = Path(__file__).parent / "static"


def detect_drive_port(default: str = "COM6") -> str:
    """Find the USB-RS485 adapter by its FTDI vendor ID (0x0403)."""
    try:
        from serial.tools import list_ports
    except Exception as e:
        log.warning("could not enumerate serial ports (%s) — falling back to %s", e, default)
        return default
    ftdi = [p for p in list_ports.comports() if p.vid == 0x0403]
    if not ftdi:
        log.warning("no FTDI USB-RS485 adapter found — falling back to %s", default)
        return default
    if len(ftdi) > 1:
        log.warning("multiple FTDI ports %s — using %s", [p.device for p in ftdi], ftdi[0].device)
    log.info("drive port auto-detected: %s (%s)", ftdi[0].device, ftdi[0].description)
    return ftdi[0].device
DB_BUFFER_FLUSH_SIZE = 10  # flush buffered samples every ~1 s

# ---- Phase C (direct-drive control) constants ----
P00_01 = (0 << 8) | 1
P03_25 = (3 << 8) | 25
P03_27 = (3 << 8) | 27
P03_28 = (3 << 8) | 28
P04_01 = (4 << 8) | 1
P09_05 = (9 << 8) | 5
P21_00 = (21 << 8) | 0
P21_41 = (21 << 8) | 41
SON_REG = 0x3607
SON_CMD = 0x02
PCT_PER_KG = 5.64        # locked-in 2026-05-10 4-point fit
SPLIT_LENGTH_M = 5.0     # 1080-style uniform split bucket (metres)
HEARTBEAT_INTERVAL = 1.5  # s; well under 5s drive watchdog
SPEED_LIMIT_ARMED = 560   # RPM cap during Phase C ops (~3.2 m/s). Sized for the
                          # return-phase reel-in (up to 3 m/s) with headroom. This
                          # is a torque-mode limit — it bounds motor-DRIVEN speed
                          # only; athlete-driven (back-driven) sprint speed is
                          # unaffected, so resisted sprints are not braked.
RETURN_SPEED_GAIN = 4.0   # kg of retract load per m/s of return-speed error (P-gain)
RETURN_KG_MAX = 12.0      # cap on the return-phase reel-in load
FLYWHEEL_TAU = 0.25       # s — flywheel-mode velocity-tracking time constant. The
                          # virtual flywheel speed is a low-pass of cable speed with
                          # this constant; it smooths the v→a derivative so the
                          # inertial reaction is stable at the current 10 Hz loop.
                          # See docs/flywheel-loop-rate-plan.md §6.
JOG_KG = 4.0              # maintenance-jog torque level (kg-equivalent)
JOG_KG_MAX = 10.0         # hard cap on any jog torque magnitude
JOG_DEADLINE_S = 0.7      # jog auto-zeros if no keepalive within this window
CPM_DEFAULT = analytics.COUNTS_PER_METRE  # locked-in default position calibration
KG_LIMIT_HARD = 53.0      # absolute ceiling — empirical 300% torque-clip limit, never exceed
KG_LIMIT_DEFAULT = 30.0   # default for the configurable max-resistance setting
WATCHDOG_GRACE = 7.0      # s to wait after heartbeat stop for drive auto-disable


def _to_u16(v): return v if v >= 0 else v + 65536
def _s16(v): return v - 65536 if v >= 32768 else v


def _build_split_report(samples: list, split_length_m: float) -> dict:
    """Build a 1080-shaped SplitReport from time-series samples.
    Each split is a uniform `split_length_m` window of cable extension.
    Returns: {splits: [...], splitLength, isYards}
    Each split: {start, end, time, averages, peaks} matching 1080 SplitData.
    """
    if not samples or split_length_m <= 0:
        return {"splits": [], "splitLength": split_length_m, "isYards": False}
    max_pos = max(s["pos_m"] for s in samples)
    n_splits = int(max_pos // split_length_m)
    if n_splits <= 0:
        return {"splits": [], "splitLength": split_length_m, "isYards": False}
    splits = []
    for i in range(n_splits):
        start_m = i * split_length_m
        end_m = (i + 1) * split_length_m
        bucket = [s for s in samples if start_m <= s["pos_m"] < end_m]
        if not bucket:
            continue
        n = len(bucket)
        t_first = bucket[0]["t_ms"] / 1000.0
        t_last = bucket[-1]["t_ms"] / 1000.0
        speeds = [s["v_mps"] for s in bucket]
        forces = [s["F_N"] for s in bucket]
        powers = [s["P_W"] for s in bucket]
        accs = [s.get("a_mps2", 0.0) for s in bucket]
        splits.append({
            "start": round(start_m, 2),
            "end": round(end_m, 2),
            "time": round(t_last - t_first, 3),
            "averages": {
                "avg_speed": round(sum(speeds) / n, 3),
                "avg_force": round(sum(forces) / n, 1),
                "avg_power": round(sum(powers) / n, 1),
                "avg_acceleration": round(sum(accs) / n, 3),
            },
            "peaks": {
                "peak_speed": round(max(speeds), 3),
                "peak_force": round(max(forces), 1),
                "peak_power": round(max(powers), 1),
                "peak_acceleration": round(max(accs), 3),
            },
        })
    return {"splits": splits, "splitLength": split_length_m, "isYards": False}


class DriveState(BaseModel):
    timestamp: str
    elapsed_s: float
    connected: bool
    armed: bool
    status: int | None = None
    speed_rpm: int | None = None
    torque_pct: float | None = None
    position_counts: int | None = None
    pos_delta_counts: int | None = None
    bus_voltage_v: float | None = None
    temp_c: int | None = None
    control_mode: int | None = None
    fault_code: int | None = None
    torque_limit_source: int | None = None
    torque_limit_external_forward_pct: float | None = None
    torque_limit_external_reverse_pct: float | None = None
    error: str | None = None


class ArmRequest(BaseModel):
    confirm: str  # must equal "ARM" to arm; "DISARM" to disarm


class TorqueLimitRequest(BaseModel):
    forward_pct: float | None = None
    reverse_pct: float | None = None
    external: bool = True


class TorqueSourceRequest(BaseModel):
    external: bool


class ControlModeRequest(BaseModel):
    mode: int


class CreateAthleteRequest(BaseModel):
    name: str


class StartSessionRequest(BaseModel):
    athlete_id: int
    notes: Optional[str] = None
    hmi_load_kg: Optional[float] = Field(default=None, ge=0)


class AthleticConfigRequest(BaseModel):
    """Module-level so FastAPI can resolve the type annotation under
    `from __future__ import annotations`. Defining it inside make_app()
    breaks body parsing — FastAPI falls back to query-param mode."""
    resist_kg: Optional[float] = None
    resist_distance_m: Optional[float] = None
    kg_limit: Optional[float] = None         # configurable max resistance
    velocity_cap_mps: Optional[float] = None
    curve_axis: Optional[str] = None
    curve_pct: Optional[list[float]] = None
    chain_kg_per_m: Optional[float] = None
    eccentric_overload_pct: Optional[float] = None
    concentric_dir: Optional[str] = None
    virtual_mass_kg: Optional[float] = None    # flywheel: virtual inertia dial
    viscous_damping: Optional[float] = None    # flywheel: bearing-drag term, kg per m/s
    return_kg: Optional[float] = None
    return_distance_m: Optional[float] = None
    return_speed_mps: Optional[float] = None
    slew_kg_per_s: Optional[float] = None
    drill: Optional[str] = None
    rest_interval_s: Optional[int] = None    # §12 inter-rep rest timer
    countdown_s: Optional[int] = None        # §9 solo-start countdown
    mode: Optional[str] = None               # §16.1 top-level training mode
    cod_prefix: Optional[str] = None         # §16.5 COD entry sub-mode
    gear: Optional[int] = None               # §16.6 1 = no pulley, 2 = pulley engaged
    rig_id: Optional[int] = None             # §15.3 active rig
    auto_stop_enabled: Optional[bool] = None            # §11 Full Auto rep detection
    auto_stop_velocity_threshold: Optional[float] = None
    auto_stop_dwell_s: Optional[float] = None


class ServiceState:
    def __init__(self, port: str = "COM6", db_path: str = persistence.DB_PATH):
        self.port = port
        self.drive: PPADrive | None = None
        self.connected = False
        self.armed = False  # gates parameter writes (Phase B). Phase C uses its own state.
        self.latest: DriveState | None = None
        self.ring: collections.deque[DriveState] = collections.deque(maxlen=RING_SIZE)
        self.subscribers: set[asyncio.Queue] = set()
        self.baseline_pos: int | None = None
        self.start_t = time.time()
        self.poll_task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        # ---- Modbus serialisation (shared by poll loop + Phase C heartbeat) ----
        self.modbus_lock = threading.Lock()
        # ---- Cycle-time telemetry (added 2026-05-20 to measure FTDI 1ms latency) ----
        self.last_lag_ms: float = 0.0
        self._lag_ema_ms: float = 0.0
        self.lag_min_ms: float = float("inf")
        self.lag_max_ms: float = 0.0
        self.lag_samples: int = 0
        # ---- Cold-cache for slow-changing reads (refresh every COLD_REFRESH_EVERY cycles) ----
        self._cold_cycle: int = 0
        self._cold_cache: dict = {}
        self._block_read_failures: int = 0
        self._block_read_disabled: bool = False  # set True after persistent block-read errors
        # ---- persistence ----
        self.db_path = db_path
        self.db = None  # sqlite3.Connection
        self.db_lock = threading.Lock()
        self.current_session_id: int | None = None
        self.session_start_t: float | None = None  # wall-clock anchor for t_offset_ms
        self.current_rep_id: int | None = None
        self.prev_status: int | None = None
        self.sample_buffer: list[tuple] = []
        # ---- Phase C (direct drive control) ----
        self.phase_c: str = "idle"   # idle | arming | armed | disarming | error
        self.phase_c_error: Optional[str] = None
        self.phase_c_originals: dict = {}
        self.phase_c_setpoint_pct: float = 0.0
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.heartbeat_writes = 0
        self.heartbeat_errs = 0
        # ---- Athletic state machine (Resist → Return → Ready) ----
        self.athletic_mode: bool = False
        self.athletic_phase: str = "off"   # off | ready | resist | return
        self.athletic_config = {
            "resist_kg": 5.0,
            "resist_distance_m": 15.0,  # distance the athlete is resisted for
            "velocity_cap_mps": 0.0,    # 0 = off; >0 caps cable speed (resisted mode)
            "curve_axis": "off",        # off | distance | velocity — resistance curve
            # 6 evenly-spaced curve points, as % of the working resistance.
            "curve_pct": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            "kg_limit": KG_LIMIT_DEFAULT,  # configurable max resistance (capped at KG_LIMIT_HARD)
            "return_kg": 1.0,
            "return_distance_m": 0.5,
            "return_speed_mps": 2.0,  # cable reel-in speed for the return phase
            "slew_kg_per_s": 12.0,  # max rate of change of commanded kg
            # Drill catalogue (lifted from T-APEX TrainingMode):
            "drill": "FreeTest",  # FreeTest, ASkip, BSkip, Ankling, SkippingJumps,
            # ApproachJump, HighKnee, FastLeg, Acceleration, Sprint, StraightLegRun
            "rest_interval_s": 180,   # §12 inter-rep rest timer (3 min default)
            "countdown_s": 5,         # §9 solo-start countdown (5 s default)
            "mode": "resisted",       # resisted | assisted | cod | gym | flywheel
            "chain_kg_per_m": 0.0,          # gym: extra kg per metre of extension
            "eccentric_overload_pct": 0.0,  # gym + flywheel: % boost on the eccentric phase
            "concentric_dir": "extend",     # gym: which cable direction is concentric
            "virtual_mass_kg": 25.0,        # flywheel: virtual inertia (resistance dial)
            "viscous_damping": 0.0,         # flywheel: bearing-drag term, kg per m/s
            "cod_prefix": "auto",     # §16.5 — auto | resisted | assisted (only used when mode=cod)
            "gear": 1,                # §16.6 — 1 (no pulley) | 2 (pulley engaged, 2× capacity)
            "rig_id": 1,              # §15.3 — which rig produced this data
            # §11 Full Auto rep detection — end a rep when the cable goes still
            "auto_stop_enabled": True,
            "auto_stop_velocity_threshold": 0.2,  # m/s — below this counts as "still"
            "auto_stop_dwell_s": 1.0,             # s of stillness before the rep ends
        }
        self.athletic_athlete_id: Optional[int] = None
        self.athletic_session_id: Optional[int] = None  # DB session row id
        self.athletic_start_pos: Optional[int] = None
        self.athletic_peak_rpm: int = 0
        self.athletic_current_set: int = 1   # reps are grouped into sets
        self.athletic_task: Optional[asyncio.Task] = None
        # ---- Athletic-loop diagnostics (added 2026-05-20 — start_rep bug hunt) ----
        self.athletic_loop_ticks: int = 0
        self.athletic_loop_last_exc: Optional[str] = None
        self.athletic_loop_last_exc_at: Optional[float] = None
        self.athletic_loop_exc_count: int = 0
        self.athletic_loop_last_phase_seen: Optional[str] = None
        self.athletic_loop_last_start_req_seen: Optional[bool] = None
        self.athletic_current_kg: float = 0.0  # current commanded kg (slew-limited)
        self.flywheel_v: float = 0.0  # flywheel mode: virtual wheel speed (m/s), reset per rep
        self._return_log_t: float = 0.0  # throttle for return-phase tuning logs
        self._jog_deadline: float = 0.0  # maintenance-jog auto-zero deadline
        self._cal_zero_counts: Optional[int] = None  # calibration zero datum
        # Rep tracking inside athletic mode: a "rep" is one resist phase
        self.athletic_reps: list[dict] = []
        self._rep_in_progress: Optional[dict] = None
        self.athletic_rep_start_requested: bool = False
        self.athletic_rep_stop_requested: bool = False

    # ---- DB helpers ----
    async def db_call(self, fn, *args, **kwargs):
        """Run a persistence.* function in the default executor with the lock held.
        Caller can pass the connection or rely on this helper to provide it."""
        loop = asyncio.get_running_loop()
        def _locked():
            with self.db_lock:
                return fn(self.db, *args, **kwargs)
        return await loop.run_in_executor(None, _locked)

    def _flush_buffer_locked(self) -> int:
        """Flush sample_buffer to DB. Caller must hold db_lock. Returns count flushed.
        Failures are logged but never raised."""
        if not self.sample_buffer:
            return 0
        rows = self.sample_buffer
        self.sample_buffer = []
        try:
            persistence.insert_samples(self.db, rows)
            return len(rows)
        except Exception as e:
            log.error("DB flush failed (%d samples dropped from this batch): %s", len(rows), e)
            return 0

    async def flush_buffer(self) -> int:
        loop = asyncio.get_running_loop()
        def _locked():
            with self.db_lock:
                return self._flush_buffer_locked()
        return await loop.run_in_executor(None, _locked)

    async def connect_drive(self):
        # Open in a thread because pymodbus's serial open is blocking
        loop = asyncio.get_running_loop()
        self.drive = PPADrive(port=self.port, baudrate=DRIVE_BAUD, armed=False)
        await loop.run_in_executor(None, self.drive.connect)
        self.connected = True
        log.info("connected to drive on %s", self.port)

    async def disconnect_drive(self):
        if self.drive:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.drive.disconnect)
        self.connected = False
        log.info("disconnected from drive")

    def _read_snapshot(self) -> DriveState:
        """Synchronous Modbus read — runs in executor so it doesn't block the loop.
        Holds modbus_lock for the duration so the heartbeat thread can't race."""
        now = time.time()
        ts = datetime.now().isoformat(timespec="milliseconds")
        elapsed = now - self.start_t
        if not self.connected or self.drive is None:
            return DriveState(timestamp=ts, elapsed_s=elapsed, connected=False, armed=self.armed,
                              error="not connected")
        try:
            d = self.drive
            with self.modbus_lock:
                # ---- HOT block: 1 read replaces 5 individual round-trips ----
                if not self._block_read_disabled:
                    try:
                        hot = d.read_p21_fast_block()
                        status = hot["status"]
                        speed = hot["speed_rpm"]
                        torque = hot["torque_pct"]
                        bus = hot["bus_voltage_v"]
                        pos = hot["position_counts"]
                    except IOError as be:
                        self._block_read_failures += 1
                        if self._block_read_failures >= 3:
                            log.warning(
                                "block read failed %d× — falling back to per-field reads: %s",
                                self._block_read_failures, be)
                            self._block_read_disabled = True
                        # fall through to per-field path on this cycle
                        status = d.read_status()
                        speed = d.read_speed()
                        torque = d.read_torque()
                        bus = d.read_bus_voltage()
                        pos = d.read_position()
                else:
                    status = d.read_status()
                    speed = d.read_speed()
                    torque = d.read_torque()
                    bus = d.read_bus_voltage()
                    pos = d.read_position()
                # ---- COLD cache: refresh once per second at 10 Hz ----
                if (self._cold_cycle % COLD_REFRESH_EVERY == 0) or not self._cold_cache:
                    try:
                        self._cold_cache = {
                            "temp_c": d.read_temperature(),
                            "fault_code": d.read_fault_code(),
                            "control_mode": d.get_control_mode(),
                            "torque_limit": d.get_torque_limit(),
                        }
                    except IOError as ce:
                        # keep last good values; never let cold-read errors crash the loop
                        log.warning("cold-cache refresh failed: %s", ce)
                self._cold_cycle += 1
                temp = self._cold_cache.get("temp_c")
                fault = self._cold_cache.get("fault_code")
                mode = self._cold_cache.get("control_mode")
                tl = self._cold_cache.get("torque_limit") or {
                    "source": None,
                    "external_forward_pct": None,
                    "external_reverse_pct": None,
                }
            if self.baseline_pos is None:
                self.baseline_pos = pos
            return DriveState(
                timestamp=ts, elapsed_s=elapsed, connected=True, armed=self.armed,
                status=status, speed_rpm=speed, torque_pct=torque,
                position_counts=pos, pos_delta_counts=pos - self.baseline_pos,
                bus_voltage_v=bus, temp_c=temp,
                control_mode=mode, fault_code=fault,
                torque_limit_source=tl["source"],
                torque_limit_external_forward_pct=tl["external_forward_pct"],
                torque_limit_external_reverse_pct=tl["external_reverse_pct"],
            )
        except Exception as e:
            return DriveState(timestamp=ts, elapsed_s=elapsed, connected=self.connected,
                              armed=self.armed, error=str(e))

    # ---- Phase C: direct-drive control (heartbeat + setpoint + state machine) ----

    def _modbus_read_one(self, addr: int) -> Optional[int]:
        """Read one register. Caller must hold modbus_lock."""
        r = self.drive.client.read_holding_registers(
            address=addr, count=1, device_id=self.drive.device_id)
        if r.isError():
            return None
        return r.registers[0]

    def _modbus_write_one(self, addr: int, value: int) -> bool:
        """Write one register. Caller must hold modbus_lock."""
        if value < 0:
            value = _to_u16(value)
        r = self.drive.client.write_register(
            address=addr, value=value, device_id=self.drive.device_id)
        return not r.isError()

    def _heartbeat_run(self):
        while not self.heartbeat_stop.is_set():
            with self.modbus_lock:
                try:
                    r = self.drive.client.write_register(
                        address=SON_REG, value=SON_CMD, device_id=self.drive.device_id)
                    if r.isError():
                        self.heartbeat_errs += 1
                    else:
                        self.heartbeat_writes += 1
                except Exception:
                    self.heartbeat_errs += 1
            self.heartbeat_stop.wait(HEARTBEAT_INTERVAL)

    def _heartbeat_start(self):
        self.heartbeat_stop.clear()
        self.heartbeat_writes = 0
        self.heartbeat_errs = 0
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_run, daemon=True)
        self.heartbeat_thread.start()

    def _heartbeat_stop_wait(self):
        self.heartbeat_stop.set()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=HEARTBEAT_INTERVAL + 1)
            self.heartbeat_thread = None

    def phase_c_arm(self) -> dict:
        """Synchronous: must run in executor. Snapshots, writes prep params,
        switches mode, starts heartbeat, waits for status=2."""
        if self.phase_c != "idle":
            raise RuntimeError(f"phase_c already {self.phase_c}")
        if not self.connected or self.drive is None:
            raise RuntimeError("drive not connected")
        self.phase_c = "arming"
        self.phase_c_error = None
        try:
            with self.modbus_lock:
                # Snapshot originals
                self.phase_c_originals = {
                    "p04_01": self._modbus_read_one(P04_01),
                    "p09_05": self._modbus_read_one(P09_05),
                    "p00_01": self._modbus_read_one(P00_01),
                    "p03_25": self._modbus_read_one(P03_25),
                    "p03_27": self._modbus_read_one(P03_27),
                    "p03_28": self._modbus_read_one(P03_28),
                }
                if self.phase_c_originals["p00_01"] != 7:
                    # Mode 2 with phase_c idle = a previous session was
                    # interrupted (crash / force-kill) before restoring the
                    # drive. Safe to reclaim: recover to EtherCAT mode 7 and
                    # proceed. Any other mode is genuinely unexpected.
                    if self.phase_c_originals["p00_01"] == 2:
                        log.warning("phase_c_arm: drive stranded in mode 2 — recovering to 7")
                        recovered = False
                        for _ in range(5):
                            self._modbus_write_one(P00_01, 7)
                            time.sleep(0.1)
                            if self._modbus_read_one(P00_01) == 7:
                                recovered = True
                                break
                        if not recovered:
                            raise RuntimeError("could not recover drive to EtherCAT mode 7")
                        self.phase_c_originals["p00_01"] = 7
                    else:
                        raise RuntimeError(
                            f"drive not in EtherCAT mode "
                            f"(P00.01={self.phase_c_originals['p00_01']})")
                if self.phase_c_originals["p03_25"] != 0:
                    # Leftover torque setpoint from an interrupted session.
                    # Zeroing torque is always the safe direction — clear it
                    # and proceed rather than dead-ending the arm.
                    log.warning("phase_c_arm: P03.25 stranded at %s — zeroing",
                                _s16(self.phase_c_originals["p03_25"]))
                    self._modbus_write_one(P03_25, 0)
                    time.sleep(0.05)
                    if self._modbus_read_one(P03_25) != 0:
                        raise RuntimeError("could not zero P03.25 torque setpoint")
                    self.phase_c_originals["p03_25"] = 0
                fault = self._modbus_read_one(P21_41)
                if fault:
                    raise RuntimeError(f"drive fault {fault} before arm")
                # Speed limits low
                self._modbus_write_one(P03_27, SPEED_LIMIT_ARMED)
                self._modbus_write_one(P03_28, SPEED_LIMIT_ARMED)
                # Comm-DI prep
                self._modbus_write_one(P04_01, 0)
                self._modbus_write_one(P09_05, 0x02)
                # Mode 2
                self._modbus_write_one(P00_01, 2)
            time.sleep(0.2)
            self._heartbeat_start()
            t0 = time.time()
            while time.time() - t0 < 2.0:
                with self.modbus_lock:
                    s = self._modbus_read_one(P21_00)
                if s == 2:
                    self.phase_c = "armed"
                    self.phase_c_setpoint_pct = 0.0
                    return {"phase_c": self.phase_c}
                time.sleep(0.1)
            raise RuntimeError("motor failed to enable within 2s")
        except Exception as e:
            self.phase_c_error = repr(e)
            self._safe_disarm_phase_c()
            self.phase_c = "error"
            raise

    def phase_c_set_kg(self, kg: float) -> dict:
        if self.phase_c != "armed":
            raise RuntimeError(f"cannot set kg in state {self.phase_c}")
        if kg < 0:
            raise ValueError("only positive kg (retract direction) supported")
        if kg > KG_LIMIT_HARD:
            raise ValueError(f"kg > {KG_LIMIT_HARD} hard cap")
        pct = -kg * PCT_PER_KG  # negative torque = retract direction
        raw = int(round(pct * 10))
        with self.modbus_lock:
            ok = self._modbus_write_one(P03_25, raw)
        if not ok:
            raise RuntimeError("setpoint write failed")
        self.phase_c_setpoint_pct = pct
        return {"kg": kg, "torque_pct": round(pct, 2), "p03_25_raw": raw}

    def phase_c_jog_kg(self, signed_kg: float) -> dict:
        """Maintenance jog. signed_kg > 0 retracts (reels in), < 0 extends
        (pays out). The only path that commands extend-direction torque.
        Magnitude is hard-capped; callers must keepalive or it auto-zeros."""
        if self.phase_c != "armed":
            raise RuntimeError(f"cannot jog in state {self.phase_c}")
        if abs(signed_kg) > JOG_KG_MAX:
            raise ValueError(f"jog kg magnitude > {JOG_KG_MAX} hard cap")
        pct = -signed_kg * PCT_PER_KG  # +kg -> negative torque -> retract
        raw = int(round(pct * 10))
        with self.modbus_lock:
            ok = self._modbus_write_one(P03_25, raw)
        if not ok:
            raise RuntimeError("jog setpoint write failed")
        self.phase_c_setpoint_pct = pct
        return {"kg": signed_kg, "torque_pct": round(pct, 2)}

    def phase_c_jog_stop(self) -> dict:
        """Zero the jog setpoint. Safe to call from any state."""
        self._jog_deadline = 0.0
        if self.phase_c == "armed":
            with self.modbus_lock:
                self._modbus_write_one(P03_25, 0)
        self.phase_c_setpoint_pct = 0.0
        return {"jog": "stopped"}

    def phase_c_disarm(self) -> dict:
        if self.phase_c == "idle":
            return {"phase_c": "idle"}
        self.phase_c = "disarming"
        self._safe_disarm_phase_c()
        self.phase_c = "idle"
        self.phase_c_setpoint_pct = 0.0
        self.phase_c_error = None
        return {"phase_c": "idle"}

    def _safe_disarm_phase_c(self):
        """Best-effort restore. Verifies critical writes (mode) and retries.
        Logs failures rather than swallowing them silently."""
        # Step 1: zero setpoint immediately
        try:
            with self.modbus_lock:
                self._modbus_write_one(P03_25, 0)
        except Exception as e:
            log.warning("_safe_disarm: zero setpoint failed: %s", e)
        # Step 2: stop heartbeat
        self._heartbeat_stop_wait()
        # Step 3: wait for drive to actually go to status=1 (poll, don't sleep blind).
        # Watchdog should take ≤5s; we give it up to WATCHDOG_GRACE seconds.
        t0 = time.time()
        while time.time() - t0 < WATCHDOG_GRACE:
            try:
                with self.modbus_lock:
                    s = self._modbus_read_one(P21_00)
                if s == 1:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        # Step 4: restore mode 7 with verify-and-retry (the critical one — if
        # this is left at 2, every future arm precondition check fails).
        target_mode = self.phase_c_originals.get("p00_01")
        if target_mode is not None:
            for attempt in range(5):
                try:
                    with self.modbus_lock:
                        ok = self._modbus_write_one(P00_01, target_mode)
                        time.sleep(0.05)
                        readback = self._modbus_read_one(P00_01)
                    if readback == target_mode:
                        break
                    log.warning("_safe_disarm: P00.01 readback %s != %s (attempt %d)",
                                readback, target_mode, attempt + 1)
                except Exception as e:
                    log.warning("_safe_disarm: P00.01 write attempt %d: %s", attempt + 1, e)
                time.sleep(0.5)
            else:
                log.error("_safe_disarm: FAILED to restore P00.01 to %s after 5 attempts. "
                          "Drive left in non-EtherCAT mode!", target_mode)
        # Step 5: best-effort restore of the rest (less critical)
        try:
            with self.modbus_lock:
                if "p09_05" in self.phase_c_originals:
                    self._modbus_write_one(P09_05, self.phase_c_originals["p09_05"])
                if "p04_01" in self.phase_c_originals:
                    self._modbus_write_one(P04_01, self.phase_c_originals["p04_01"])
                if "p03_27" in self.phase_c_originals:
                    self._modbus_write_one(P03_27, self.phase_c_originals["p03_27"])
                if "p03_28" in self.phase_c_originals:
                    self._modbus_write_one(P03_28, self.phase_c_originals["p03_28"])
        except Exception as e:
            log.warning("_safe_disarm: param restore failed: %s", e)

    async def poll_loop(self):
        loop = asyncio.get_running_loop()
        log.info("starting poll loop @ %d Hz (idle %d Hz)", POLL_HZ, IDLE_POLL_HZ)
        try:
            while not self.stop_event.is_set():
                t0 = time.time()
                # Poll the drive at all phase_c states so the live view works.
                # Idle uses a slower cadence; full rate only during Phase C.
                idle = self.phase_c == "idle"
                state = await loop.run_in_executor(None, self._read_snapshot)
                self.latest = state
                self.ring.append(state)
                # Jog-watchdog — auto-zero the maintenance jog if the UI stops
                # sending keepalives (button released, page hidden, drop-out).
                if self._jog_deadline and time.time() > self._jog_deadline:
                    self._jog_deadline = 0.0
                    try:
                        await loop.run_in_executor(None, self.phase_c_jog_stop)
                    except Exception as e:
                        log.warning("jog-watchdog stop failed: %s", e)
                # fan-out to WS subscribers (best-effort, drop if queue full)
                for q in list(self.subscribers):
                    if q.qsize() < 8:
                        q.put_nowait(state)
                # ---- persistence: only when a session is open ----
                if self.current_session_id is not None and state.connected and not state.error:
                    await self._handle_sample(state, t0)
                lag = time.time() - t0
                # cycle-time telemetry (FTDI 1ms-latency measurement, 2026-05-20)
                lag_ms = lag * 1000.0
                self.last_lag_ms = lag_ms
                if self.lag_samples == 0:
                    self._lag_ema_ms = lag_ms
                else:
                    self._lag_ema_ms = 0.9 * self._lag_ema_ms + 0.1 * lag_ms
                if lag_ms < self.lag_min_ms:
                    self.lag_min_ms = lag_ms
                if lag_ms > self.lag_max_ms:
                    self.lag_max_ms = lag_ms
                self.lag_samples += 1
                await asyncio.sleep(max(0.0, (IDLE_PERIOD if idle else PERIOD) - lag))
        except asyncio.CancelledError:
            pass
        finally:
            log.info("poll loop stopped")

    async def _handle_sample(self, state: "DriveState", t_now: float):
        """Append sample to buffer + handle status transitions.
        DB failures are caught and logged; never raised back to the loop.
        """
        try:
            sid = self.current_session_id
            if sid is None or self.session_start_t is None:
                return

            t_offset_ms = int((t_now - self.session_start_t) * 1000)
            self.sample_buffer.append((
                sid, t_offset_ms,
                state.status, state.speed_rpm, state.torque_pct,
                state.position_counts, state.bus_voltage_v,
            ))

            # Status transition handling.
            prev = self.prev_status
            now_s = state.status
            self.prev_status = now_s

            if prev == 1 and now_s == 2:
                # rep start
                try:
                    rep_id = await self.db_call(persistence.start_rep, sid, t_offset_ms)
                    self.current_rep_id = rep_id
                    log.info("session=%d rep=%d started at t+%dms", sid, rep_id, t_offset_ms)
                except Exception as e:
                    log.error("start_rep failed: %s", e)
            elif prev == 2 and now_s == 1 and self.current_rep_id is not None:
                # rep end — FORCE FLUSH BUFFER FIRST so aggregates have complete data
                rep_id = self.current_rep_id
                self.current_rep_id = None
                try:
                    await self.flush_buffer()
                    summary = await self.db_call(persistence.end_rep, rep_id, t_offset_ms)
                    log.info(
                        "session=%d rep=%d ended at t+%dms peak_speed=%s peak_torque=%s "
                        "path=%s net=%s decel=%s",
                        sid, rep_id, t_offset_ms,
                        summary.get("peak_speed_rpm"), summary.get("peak_torque_pct"),
                        summary.get("total_distance_counts"),
                        summary.get("net_displacement_counts"),
                        summary.get("peak_decel_rpm_per_s"),
                    )
                except Exception as e:
                    log.error("end_rep failed: %s", e)

            # Periodic flush
            if len(self.sample_buffer) >= DB_BUFFER_FLUSH_SIZE:
                await self.flush_buffer()
        except Exception as e:
            # Never let persistence kill the polling loop
            log.exception("unhandled error in _handle_sample (continuing): %s", e)


def _finalise_in_progress_rep(rip: Optional[dict], period: float) -> Optional[dict]:
    """Finalise an in-progress rep dict — compute averages from accumulators,
    derive sample-based metrics (impulse, TTPF/TTPS, accel/decel phase markers,
    splits, 1080 split_report), and strip private accumulator fields.

    Returns the finalised dict, or None if rip is empty / has no started_at.
    Does not persist — caller decides phantom filtering and DB write.
    """
    if not rip or rip.get("started_at") is None:
        return None
    rip["ended_at"] = time.time()
    rip["duration_s"] = round(rip["ended_at"] - rip["started_at"], 2)
    n = max(rip.get("_sample_count", 0), 1)
    rip["avg_speed_mps"] = round(rip.get("_sum_speed", 0.0) / n, 3)
    rip["avg_force_n"] = round(rip.get("_sum_force", 0.0) / n, 1)
    rip["avg_power_w"] = round(rip.get("_sum_power", 0.0) / n, 1)
    rip["avg_acceleration_mps2"] = round(rip.get("_sum_acc", 0.0) / n, 3)
    rip["work_j"] = round(rip.get("_sum_work", 0.0), 1)
    # 1080-style aliases
    rip["top_speed_mps"] = rip.get("peak_speed_mps", 0.0)
    rip["total_distance_m"] = rip.get("max_extension_m", 0.0)
    rip["total_time_s"] = rip["duration_s"]
    samples = rip.get("samples", [])
    if samples:
        rip["impulse_ns"] = round(sum(s["F_N"] for s in samples) * period, 1)
        peak_f_idx = max(range(len(samples)), key=lambda i: samples[i]["F_N"])
        peak_v_idx = max(range(len(samples)), key=lambda i: samples[i]["v_mps"])
        rip["ttpf_ms"] = samples[peak_f_idx]["t_ms"]
        rip["ttps_ms"] = samples[peak_v_idx]["t_ms"]
        peak_v = max(s["v_mps"] for s in samples)
        accel_end_ms = None
        decel_start_ms = None
        past_peak = False
        for s in samples:
            if accel_end_ms is None and s["v_mps"] >= 0.90 * peak_v:
                accel_end_ms = s["t_ms"]
            if s["v_mps"] >= peak_v:
                past_peak = True
            if past_peak and decel_start_ms is None and s["v_mps"] < 0.95 * peak_v:
                decel_start_ms = s["t_ms"]
        rip["accel_end_ms"] = accel_end_ms
        rip["decel_start_ms"] = decel_start_ms
        # 1080 AccelDecelStats parity: where top speed was reached, and how
        # long from top speed to the end of the rep (deceleration time).
        rip["dist_to_max_v_m"] = round(samples[peak_v_idx]["pos_m"], 2)
        rip["decel_time_s"] = round(
            (samples[-1]["t_ms"] - samples[peak_v_idx]["t_ms"]) / 1000.0, 2)
        splits = {}
        for marker in (5, 10, 20):
            for s in samples:
                if s["pos_m"] >= marker:
                    splits[str(marker)] = s["t_ms"] / 1000.0
                    break
        rip["splits_s"] = splits
        rip["split_report"] = _build_split_report(samples, SPLIT_LENGTH_M)
    for k in ("_sum_speed", "_sum_force", "_sum_power", "_sum_acc",
              "_sum_work", "_sample_count", "_prev_v_mps", "_left_start",
              "_low_v_since"):
        rip.pop(k, None)
    return rip


def _resistance_curve(curve_pct, x, x_max):
    """Linear-interpolate the resistance curve. curve_pct is N evenly-spaced
    points (% of working resistance) spanning x in [0, x_max]; returns the
    percentage at x. 100.0 if no curve is defined."""
    n = len(curve_pct) if curve_pct else 0
    if n == 0:
        return 100.0
    if n == 1 or x_max <= 0:
        return curve_pct[0]
    frac = min(max(x / x_max, 0.0), 1.0)
    pos = frac * (n - 1)
    i = int(pos)
    if i >= n - 1:
        return curve_pct[-1]
    t = pos - i
    return curve_pct[i] * (1.0 - t) + curve_pct[i + 1] * t


async def _athletic_loop(state: "ServiceState"):
    """Background task. When state.athletic_mode is True and phase_c is armed,
    drive the setpoint through a manually-started rep state machine.

    A rep starts only when the coach requests it (the Start-rep button sets
    state.athletic_rep_start_requested). While a rep is in progress the working
    resistance is gated purely by cable position from the rep-start point:
      ext < return_distance_m                  -> recovery_kg (home corridor)
      return_distance_m .. +resist_distance_m  -> resist_kg   (working band)
      ext beyond the band                      -> recovery_kg
    Because it is position-gated it reverses automatically on the way back in.
    athletic_phase reports "resist" while in the working band, "return"
    otherwise (corridor or beyond), "ready" between reps. A rep ends when the
    cable retracts back to the start position.
    """
    log.info("athletic loop started")
    LOOP_HZ = 10
    PERIOD = 1.0 / LOOP_HZ
    POSITION_TOLERANCE_COUNTS = 5000  # ~13mm of slop near start (used for return→ready)
    PHANTOM_DURATION_S = 0.5          # reps shorter than this are dropped
    AUTO_STOP_MIN_S = 2.0             # §11 — min rep age before auto-stop can fire
    try:
        while not state.stop_event.is_set():
            await asyncio.sleep(PERIOD)
            state.athletic_loop_ticks += 1
            state.athletic_loop_last_phase_seen = state.athletic_phase
            state.athletic_loop_last_start_req_seen = state.athletic_rep_start_requested
            if not state.athletic_mode or state.phase_c != "armed":
                continue
            if state.latest is None or state.latest.position_counts is None:
                continue
            tel = state.latest
            cfg = state.athletic_config
            COUNTS_PER_M = analytics.COUNTS_PER_METRE
            MPS_PER_RPM = analytics.MPS_PER_RPM
            speed_mps = abs((tel.speed_rpm or 0) * MPS_PER_RPM)
            if (tel.speed_rpm or 0) > state.athletic_peak_rpm:
                state.athletic_peak_rpm = tel.speed_rpm
            current_pos = tel.position_counts
            rip = state._rep_in_progress
            # Corridor/band reference point. Resisted reps start at home so the
            # rep-start position IS home. Assisted reps start far out and run in
            # toward the rig, so the corridor is measured from the drill-start
            # home position instead.
            if cfg.get("mode") == "assisted" and state.athletic_start_pos is not None:
                home_pos = state.athletic_start_pos
            elif rip is not None:
                home_pos = rip["start_pos_counts"]
            else:
                home_pos = current_pos
            rep_ext_counts = abs(current_pos - home_pos)
            rep_ext_m = rep_ext_counts / COUNTS_PER_M

            # §11 Full Auto rep detection — once a rep has run a minimum time and
            # the athlete has left the start, end it when the cable stays slower
            # than the threshold for the dwell period. Sprint modes only; gym and
            # flywheel reps are short out-and-back cycles that pause legitimately.
            auto_stop_now = False
            if (cfg.get("auto_stop_enabled", True) and cfg.get("mode") not in ("gym", "flywheel")
                    and rip is not None and rip.get("_left_start")
                    and (time.time() - rip["started_at"]) >= AUTO_STOP_MIN_S):
                if speed_mps < cfg.get("auto_stop_velocity_threshold", 0.2):
                    if rip.get("_low_v_since") is None:
                        rip["_low_v_since"] = time.time()
                    elif (time.time() - rip["_low_v_since"]) >= cfg.get("auto_stop_dwell_s", 1.0):
                        auto_stop_now = True
                else:
                    rip["_low_v_since"] = None

            new_phase = state.athletic_phase
            target_kg = 0.0

            if state.athletic_phase == "ready":
                # Idle between reps — light recovery tension. A rep begins only
                # on an explicit Start-rep request from the coach.
                target_kg = cfg["return_kg"]
                if state.athletic_rep_start_requested:
                    new_phase = "return"

            elif state.athletic_phase in ("resist", "return"):
                if cfg.get("mode") == "gym":
                    # GYM: resistance = base + chain(extension), with the
                    # eccentric phase overloaded by a set %. A rep is one
                    # out-and-back cycle (counted, auto-chained — see rep-end).
                    loaded = cfg["resist_kg"] + cfg["chain_kg_per_m"] * rep_ext_m
                    spd = tel.speed_rpm or 0
                    cur_dir = "extend" if spd > 0 else "retract"
                    is_ecc = abs(spd) > 3 and cur_dir != cfg.get("concentric_dir", "extend")
                    if is_ecc:
                        loaded *= 1.0 + cfg["eccentric_overload_pct"] / 100.0
                    target_kg = min(loaded, cfg.get("kg_limit", KG_LIMIT_DEFAULT))
                    new_phase = "return" if is_ecc else "resist"
                    if state.athletic_rep_stop_requested or (
                            rip is not None and rip.get("_left_start")
                            and rep_ext_counts <= POSITION_TOLERANCE_COUNTS):
                        new_phase = "ready"
                elif cfg.get("mode") == "flywheel":
                    # FLYWHEEL — virtual-inertia training. The cable spins up a
                    # simulated flywheel; the resistance the athlete feels is the
                    # inertial reaction to *accelerating* that wheel (F = M·a), so
                    # constant-speed pulling feels light and hard acceleration
                    # feels heavy — the defining feel of inertial/flywheel work.
                    #
                    # The wheel speed `flywheel_v` is a low-pass of the measured
                    # cable speed (time constant FLYWHEEL_TAU), held as a smooth
                    # state. Differentiating that smooth state — instead of the
                    # raw, jittery telemetry — keeps the model stable at the
                    # current ~10 Hz loop. This is the "virtual-flywheel" variant
                    # from docs/flywheel-loop-rate-plan.md §6; the exact F = M·a
                    # model lands once the loop-rate work reaches ~100 Hz.
                    M = cfg.get("virtual_mass_kg", 25.0)
                    c_visc = cfg.get("viscous_damping", 0.0)
                    spd_rpm = tel.speed_rpm or 0
                    prev_fw = state.flywheel_v
                    # 1st-order low-pass: flywheel_v eases toward cable speed.
                    alpha = PERIOD / (FLYWHEEL_TAU + PERIOD)
                    state.flywheel_v = prev_fw + (speed_mps - prev_fw) * alpha
                    fw_accel = (state.flywheel_v - prev_fw) / PERIOD  # smooth m/s²
                    # Inertial reaction in kg-equivalent: F = M·a, plus a small
                    # viscous (bearing-drag) term proportional to speed.
                    target_kg = M * abs(fw_accel) / 9.81 + c_visc * speed_mps
                    # Eccentric (cable retracting) carries the overload boost.
                    is_ecc = spd_rpm < 0
                    if is_ecc:
                        target_kg *= 1.0 + cfg["eccentric_overload_pct"] / 100.0
                    target_kg = min(target_kg, cfg.get("kg_limit", KG_LIMIT_DEFAULT))
                    new_phase = "return" if is_ecc else "resist"
                    if state.athletic_rep_stop_requested or (
                            rip is not None and rip.get("_left_start")
                            and rep_ext_counts <= POSITION_TOLERANCE_COUNTS):
                        new_phase = "ready"
                else:
                    # SPRINT (resisted / assisted). Working resistance is gated
                    # purely by cable position, the same on the way out and back:
                    #   ext < recovery_distance                -> recovery kg (corridor)
                    #   recovery .. recovery + resist_distance -> working kg (band)
                    #   ext beyond the band                    -> recovery kg
                    recovery_m = cfg["return_distance_m"]
                    band_outer_m = recovery_m + cfg["resist_distance_m"]
                    in_resist_band = recovery_m <= rep_ext_m < band_outer_m
                    if in_resist_band:
                        # Resistance curve shapes the working resistance: each
                        # point is a % of resist_kg, so the same curve scales
                        # with whatever working load the coach sets.
                        c_axis = cfg.get("curve_axis", "off")
                        if c_axis == "distance":
                            pct = _resistance_curve(
                                cfg["curve_pct"], rep_ext_m, cfg["resist_distance_m"])
                            target_kg = cfg["resist_kg"] * pct / 100.0
                        elif c_axis == "velocity":
                            pct = _resistance_curve(
                                cfg["curve_pct"], speed_mps, CURVE_VELOCITY_MAX)
                            target_kg = cfg["resist_kg"] * pct / 100.0
                        else:
                            target_kg = cfg["resist_kg"]
                        kg_lim = cfg.get("kg_limit", KG_LIMIT_DEFAULT)
                        target_kg = min(target_kg, kg_lim)
                        # Velocity cap — hold the athlete near the cap. Resisted:
                        # ADD resistance when over the cap. Assisted: the motor is
                        # towing them, so REDUCE the tow instead (adding would
                        # accelerate an already-too-fast athlete).
                        v_cap = cfg.get("velocity_cap_mps", 0.0)
                        if v_cap > 0 and speed_mps > v_cap:
                            over = VELOCITY_CAP_GAIN * (speed_mps - v_cap)
                            if cfg.get("mode") == "assisted":
                                target_kg = max(0.0, target_kg - over)
                            else:
                                target_kg = min(target_kg + over, kg_lim)
                    else:
                        # Outside the working band — gate on retract direction
                        # rather than on band position:
                        #   Outbound or stopped (spd_rpm >= 0): just hold the
                        #     configured return_kg as a light pre-tension. This
                        #     covers BOTH the pre-resist corridor (before the
                        #     band) AND past-band (athlete coasting past their
                        #     rep distance) — in neither case do we want to
                        #     fight them with load while they're still going.
                        #   Retracting (spd_rpm < 0): close the loop on retract
                        #     speed so the line reels in at return_speed_mps.
                        spd_rpm = tel.speed_rpm or 0
                        retracting = spd_rpm < 0
                        ret_cap = min(RETURN_KG_MAX,
                                      cfg.get("kg_limit", KG_LIMIT_DEFAULT))
                        if not retracting:
                            target_kg = max(0.0,
                                            min(cfg["return_kg"], ret_cap))
                        else:
                            ret_target = cfg.get("return_speed_mps", 2.0)
                            retract_mps = -spd_rpm * MPS_PER_RPM
                            err = ret_target - retract_mps
                            target_kg = cfg["return_kg"] + RETURN_SPEED_GAIN * err
                            target_kg = max(0.0, min(target_kg, ret_cap))
                            # Throttled telemetry for tuning RETURN_SPEED_GAIN.
                            _now = time.time()
                            if _now - state._return_log_t >= 1.0:
                                state._return_log_t = _now
                                log.info("return: target=%.2f measured=%.2f m/s "
                                         "-> load=%.1f kg", ret_target, retract_mps,
                                         target_kg)
                    new_phase = "resist" if in_resist_band else "return"
                    if state.athletic_rep_stop_requested or auto_stop_now or (
                            rip is not None and rip.get("_left_start")
                            and rep_ext_counts <= POSITION_TOLERANCE_COUNTS):
                        # Coach ended it, the cable retracted to start, or
                        # Full Auto detected the athlete has stopped.
                        new_phase = "ready"
            else:  # off or unexpected
                target_kg = 0.0

            if new_phase != state.athletic_phase:
                log.info("athletic phase: %s → %s (rep_ext=%.2fm)",
                         state.athletic_phase, new_phase, rep_ext_m)
                if state.athletic_phase == "ready" and new_phase != "ready":
                    # rep start — consume the request, initialise the rep record.
                    state.athletic_rep_start_requested = False
                    state.athletic_rep_stop_requested = False
                    state.flywheel_v = 0.0  # flywheel starts each rep at rest
                    rep_db_id = None
                    if (state.athletic_session_id is not None
                            and state.db is not None
                            and state.session_start_t is not None):
                        try:
                            t_offset_ms = int((time.time() - state.session_start_t) * 1000)
                            rep_db_id = await asyncio.to_thread(
                                lambda: persistence.start_rep(
                                    state.db, state.athletic_session_id, t_offset_ms,
                                    state.athletic_current_set))
                        except Exception as e:
                            log.warning("athletic start_rep persist failed: %s", e)
                    state._rep_in_progress = {
                        "rep_idx": len(state.athletic_reps) + 1,
                        "set_idx": state.athletic_current_set,
                        "rep_db_id": rep_db_id,
                        "started_at": time.time(),
                        "start_pos_counts": current_pos,
                        # peaks (matches 1080 AggregatedValues fields)
                        "peak_speed_mps": 0.0,
                        "peak_force_n": 0.0,
                        "peak_power_w": 0.0,
                        "peak_acceleration_mps2": 0.0,
                        "peak_torque_pct": 0.0,
                        "max_extension_m": 0.0,
                        # accumulators for averages + work integration
                        "_sum_speed": 0.0,
                        "_sum_force": 0.0,
                        "_sum_power": 0.0,
                        "_sum_acc": 0.0,
                        "_sum_work": 0.0,  # ∫ F * v dt
                        "_sample_count": 0,
                        "_prev_v_mps": 0.0,
                        # set True once the athlete extends past the start corridor;
                        # gates the "retracted to start" rep-end test.
                        "_left_start": False,
                        "_low_v_since": None,   # §11 auto-stop dwell tracker
                        "is_eccentric": False,
                        "samples": [],   # {t_ms, v_mps, F_N, P_W, pos_m, a_mps2}
                    }
                    log.info("athletic rep %d started",
                             state._rep_in_progress["rep_idx"])
                if new_phase == "ready" and state._rep_in_progress is not None:
                    gym_autochain = (cfg.get("mode") in ("gym", "flywheel")
                                     and not state.athletic_rep_stop_requested)
                    # rep end — finalise aggregates + apply phantom filter + persist to DB
                    fin = _finalise_in_progress_rep(state._rep_in_progress, PERIOD)
                    if fin is not None:
                        if fin["duration_s"] < PHANTOM_DURATION_S:
                            # Phantom rep (cable jiggle, brief excursion). Drop it.
                            log.info("dropping phantom rep idx=%s duration=%.2fs",
                                     fin.get("rep_idx"), fin["duration_s"])
                            if (fin.get("rep_db_id") is not None
                                    and state.db is not None):
                                try:
                                    await asyncio.to_thread(
                                        lambda: persistence.delete_rep(
                                            state.db, fin["rep_db_id"]))
                                except Exception as e:
                                    log.warning("phantom delete_rep failed: %s", e)
                        else:
                            # Real rep — persist with rich aggregates and surface in UI list.
                            if (fin.get("rep_db_id") is not None
                                    and state.db is not None
                                    and state.session_start_t is not None):
                                try:
                                    t_end_ms = int((fin["ended_at"] - state.session_start_t) * 1000)
                                    drill_name = state.athletic_config.get("drill")
                                    await asyncio.to_thread(
                                        lambda: persistence.end_rep_with_aggregates(
                                            state.db, fin["rep_db_id"], t_end_ms,
                                            fin, drill=drill_name))
                                except Exception as e:
                                    log.warning("athletic end_rep_with_aggregates failed: %s", e)
                            state.athletic_reps.append(fin)
                    state._rep_in_progress = None
                    state.athletic_rep_stop_requested = False
                    if gym_autochain:
                        # Gym counted-reps: one out-and-back cycle finished —
                        # auto-start the next rep so the set keeps counting
                        # until the coach taps Stop.
                        state.athletic_rep_start_requested = True
                state.athletic_phase = new_phase

            # Update in-progress rep peaks + samples across the whole rep
            # (resist and return phases).
            if state._rep_in_progress is not None:
                rip = state._rep_in_progress
                # Convert torque % → force in N (using locked calibration 5.64 %/kg, g=9.81)
                torque_abs = abs(tel.torque_pct or 0.0)
                force_kgf = torque_abs / PCT_PER_KG
                force_n = force_kgf * 9.81
                power_w = force_n * speed_mps
                # Acceleration: (v - v_prev) / dt
                acc_mps2 = (speed_mps - rip["_prev_v_mps"]) / PERIOD
                rip["_prev_v_mps"] = speed_mps
                # Peaks
                if speed_mps > rip["peak_speed_mps"]:
                    rip["peak_speed_mps"] = round(speed_mps, 3)
                if torque_abs > rip["peak_torque_pct"]:
                    rip["peak_torque_pct"] = round(torque_abs, 1)
                if force_n > rip["peak_force_n"]:
                    rip["peak_force_n"] = round(force_n, 1)
                if power_w > rip["peak_power_w"]:
                    rip["peak_power_w"] = round(power_w, 1)
                if acc_mps2 > rip["peak_acceleration_mps2"]:
                    rip["peak_acceleration_mps2"] = round(acc_mps2, 3)
                # Distance for THIS rep: absolute from rep-start position
                rep_extension_m = abs(current_pos - rip["start_pos_counts"]) / COUNTS_PER_M
                if rep_extension_m > rip["max_extension_m"]:
                    rip["max_extension_m"] = round(rep_extension_m, 3)
                if abs(current_pos - rip["start_pos_counts"]) > POSITION_TOLERANCE_COUNTS:
                    rip["_left_start"] = True
                # Averages + work
                rip["_sum_speed"] += speed_mps
                rip["_sum_force"] += force_n
                rip["_sum_power"] += power_w
                rip["_sum_acc"] += acc_mps2
                rip["_sum_work"] += force_n * speed_mps * PERIOD  # F·v·dt
                rip["_sample_count"] += 1
                # Time-series sample (capped at 600 to avoid runaway memory ≈ 60s @10Hz)
                if len(rip["samples"]) < 600:
                    t_ms = int((time.time() - rip["started_at"]) * 1000)
                    rip["samples"].append({
                        "t_ms": t_ms,
                        "v_mps": round(speed_mps, 3),
                        "F_N": round(force_n, 1),
                        "P_W": round(power_w, 1),
                        "a_mps2": round(acc_mps2, 3),
                        "pos_m": round(rep_extension_m, 3),
                    })

            # Slew-rate-limit the commanded kg so phase transitions ease in
            # rather than snap. PERIOD = 1/LOOP_HZ s per cycle.
            slew = cfg.get("slew_kg_per_s", 12.0) * PERIOD
            cur = state.athletic_current_kg
            delta = target_kg - cur
            if abs(delta) <= slew:
                cur = target_kg
            else:
                cur = cur + (slew if delta > 0 else -slew)
            state.athletic_current_kg = cur

            # write setpoint via the same gating
            try:
                with state.modbus_lock:
                    raw = int(round(-cur * PCT_PER_KG * 10))
                    state.drive.client.write_register(
                        address=P03_25,
                        value=raw if raw >= 0 else _to_u16(raw),
                        device_id=state.drive.device_id,
                    )
                    state.phase_c_setpoint_pct = -cur * PCT_PER_KG
            except Exception as e:
                log.warning("athletic setpoint write failed: %s", e)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        # Added 2026-05-20: capture the exception that kills the loop instead of
        # letting it disappear into the void. /api/diag/athletic surfaces this.
        state.athletic_loop_exc_count += 1
        state.athletic_loop_last_exc = f"{type(e).__name__}: {e}"
        state.athletic_loop_last_exc_at = time.time()
        log.exception("athletic loop died with exception: %s", e)
    finally:
        log.info("athletic loop stopped (ticks=%d, exc_count=%d)",
                 state.athletic_loop_ticks, state.athletic_loop_exc_count)


ATHLETE_HTML = """<!doctype html>
<html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,user-scalable=no,viewport-fit=cover">
<title>PPA · Athlete</title>
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#5b8bff">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PPA">
<link rel="apple-touch-icon" sizes="192x192" href="/static/icon-192.png">
<link rel="icon" type="image/png" href="/static/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0a1020; --fg:#eef2fb; --muted:#818eae; --accent:#5b8bff;
          --bad:#ff5a5f; --good:#33d17a; --warn:#ff9440;
          --card:#121a33; --line:#243157; }
  *{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
    -webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;padding:0;height:100%;width:100%;background:var(--bg);color:var(--fg);overflow:hidden}
  body{display:flex;flex-direction:column;justify-content:center;align-items:center;padding:24px}
  .pill{position:fixed;top:18px;right:18px;padding:8px 18px;border-radius:999px;
        font-size:14px;font-weight:600;letter-spacing:0.08em;z-index:10}
  .pill.ready{background:rgba(255,148,64,0.16);color:var(--warn);border:1px solid rgba(255,148,64,0.35)}
  .pill.resist{background:rgba(255,90,95,0.16);color:var(--bad);border:1px solid rgba(255,90,95,0.35)}
  .pill.return{background:rgba(51,209,122,0.16);color:var(--good);border:1px solid rgba(51,209,122,0.35)}
  .pill.standby{background:rgba(129,142,174,0.12);color:var(--muted);border:1px solid rgba(129,142,174,0.28)}
  .head-num{font-size:28vw;font-weight:700;line-height:1;text-align:center;font-variant-numeric:tabular-nums}
  .head-unit{font-size:7vw;font-weight:600;color:var(--muted);text-align:center;margin-top:-4px}
  .secondary{display:flex;gap:36px;margin-top:34px;justify-content:center}
  .secondary .v{font-size:8vw;font-weight:600;font-variant-numeric:tabular-nums;text-align:center}
  .secondary .l{font-size:11px;color:var(--muted);letter-spacing:0.12em;text-align:center;margin-top:4px}
  .last-rep{position:fixed;bottom:24px;left:0;right:0;text-align:center;font-size:14px;color:var(--muted);
            opacity:0;transition:opacity 400ms ease}
  .last-rep.show{opacity:1}
  .last-rep b{color:var(--fg);font-weight:600}
  .offline{position:fixed;bottom:24px;left:0;right:0;text-align:center;font-size:13px;color:var(--bad);opacity:0.85}
</style>
</head><body>

<div id="pill" class="pill standby">STANDBY</div>

<div id="head" class="head-num">–</div>
<div class="head-unit">m/s</div>

<div class="secondary">
  <div><div class="v" id="force">0</div><div class="l">FORCE · N</div></div>
  <div><div class="v" id="power">0</div><div class="l">POWER · W</div></div>
  <div><div class="v" id="ext">0.00</div><div class="l">CABLE OUT · m</div></div>
</div>

<div id="lastRep" class="last-rep"></div>
<div id="offline" class="offline" style="display:none">Connection lost — retrying…</div>

<script>
const head=document.getElementById('head');
const force=document.getElementById('force');
const power=document.getElementById('power');
const ext=document.getElementById('ext');
const pill=document.getElementById('pill');
const lastRep=document.getElementById('lastRep');
const offline=document.getElementById('offline');

const phaseLabels={ready:'READY',resist:'PULLING',return:'RECOVER',off:'STANDBY'};
const phaseClass ={ready:'ready',resist:'resist',return:'return',off:'standby'};

let prevPhase='off';
let lastRepTimer=null;
let lastRepCount=0;

function showLastRep(rep){
  if(!rep)return;
  lastRep.innerHTML='Last rep · <b>'+(rep.peak_speed_mps||0).toFixed(2)+' m/s</b> · <b>'+(rep.peak_power_w||0).toFixed(0)+' W</b>';
  lastRep.classList.add('show');
  if(lastRepTimer)clearTimeout(lastRepTimer);
  lastRepTimer=setTimeout(()=>lastRep.classList.remove('show'),3000);
}

async function refresh(){
  let c,s,r;
  try{
    [c,s,r]=await Promise.all([
      fetch('/api/c/state').then(x=>x.json()),
      fetch('/api/state').then(x=>x.json()),
      fetch('/api/c/athletic/reps').then(x=>x.json()),
    ]);
    offline.style.display='none';
  }catch(e){
    offline.style.display='block';
    return;
  }
  const ph=c.athletic_mode?c.athletic_phase:'off';
  pill.textContent=phaseLabels[ph]||'STANDBY';
  pill.className='pill '+(phaseClass[ph]||'standby');

  // Headline number: peak speed of current rep while pulling, else last rep's peak, else live live
  const ip=r.in_progress;
  const reps=r.reps||[];
  let headline=0;
  if(ph==='resist' && ip){
    headline=ip.peak_speed_mps||0;
  } else if(reps.length){
    headline=reps[reps.length-1].peak_speed_mps||0;
  } else {
    const rpm=Math.abs(s.speed_rpm||0);
    headline=rpm*0.00576;
  }
  head.textContent=headline.toFixed(2);

  // Secondary: live force + power
  const torquePct=Math.abs(s.torque_pct||0);
  const forceN=(torquePct/5.64)*9.81;
  const speedMps=Math.abs((s.speed_rpm||0)*0.00576);
  force.textContent=forceN.toFixed(0);
  power.textContent=(forceN*speedMps).toFixed(0);
  const extM=(c.extension_m!=null)?c.extension_m:0;
  ext.textContent=extM.toFixed(2);

  // last-rep fade-in: trigger when rep count increments
  if(reps.length>lastRepCount){
    showLastRep(reps[reps.length-1]);
    lastRepCount=reps.length;
  }
  // also trigger on phase transition pull→recover (covers the case where reps array is delayed)
  if(prevPhase==='resist' && ph==='return' && reps.length){
    showLastRep(reps[reps.length-1]);
  }
  prevPhase=ph;
}

refresh();
setInterval(refresh,250);

if('serviceWorker' in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(()=>{});});}
</script>
</body></html>
"""


PHASE_C_HTML = """<!doctype html>
<html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PPA · Sprint Trainer</title>
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#5b8bff">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PPA Coach">
<link rel="apple-touch-icon" sizes="192x192" href="/static/icon-192.png">
<link rel="icon" type="image/png" href="/static/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0a1020; --fg:#eef2fb; --muted:#818eae; --accent:#5b8bff;
          --accent-soft:rgba(91,139,255,0.16);
          --bad:#ff5a5f; --good:#33d17a; --warn:#ff9440;
          --card:#121a33; --line:#243157;
          --raised:#182244; --line-strong:#33436b; --ink-faint:#56618a;
          --warm:#ff9440; --warm-soft:rgba(255,148,64,.16);
          --cool:#2bd0e2; --cool-soft:rgba(43,208,226,.16);
          --state:var(--warm); --state-soft:var(--warm-soft); }
  *{box-sizing:border-box;font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif}
  body{margin:0;background:var(--bg);color:var(--fg);font-size:16px;
       min-height:100vh;display:flex;flex-direction:column}
  .wrap{flex:1;width:100%;max-width:1500px;margin:0 auto;padding:18px;
        display:flex;flex-direction:column}
  /* Keep the topbar + mode bar clear of the fixed e-stop button.
     Only needed when the sheet isn't open (an open desktop sheet already
     reserves its own column and shifts the e-stop left). */
  @media (max-width:1363px){
    body:not(.sheet-open) .topbar,
    body:not(.sheet-open) .mode-bar{padding-right:90px}
  }
  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;
          gap:10px;flex-wrap:wrap}
  h1{margin:0;font-size:22px;letter-spacing:0.05em;font-weight:700}
  h1 .accent{color:var(--accent)}
  .brand-logo{height:52px;width:auto;border-radius:0;display:inline-block;vertical-align:middle;margin-right:12px}
  .state-tag{font-size:13px;color:var(--muted)}
  .topbar-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
  .gear-toggle{padding:8px 12px;font-size:12px;font-weight:700;background:var(--card);
               color:var(--fg);border:1px solid var(--line);border-radius:8px;cursor:pointer;min-height:40px}
  .gear-toggle.g2{border-color:var(--warn);color:var(--warn)}
  .gear-warn-chip{font-size:10px;color:var(--warn);font-weight:600}
  .athlete-chip{position:relative}
  #athlete-chip-btn{display:flex;align-items:center;gap:8px;padding:4px 12px 4px 4px;
                    background:var(--card);border:1px solid var(--line);border-radius:999px;
                    cursor:pointer;color:var(--fg);min-height:40px}
  .ath-avatar{width:30px;height:30px;border-radius:50%;background:var(--accent);color:#0a0a0c;
              display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;flex:0 0 auto}
  .ath-chip-text{display:flex;flex-direction:column;align-items:flex-start;line-height:1.15}
  #ath-chip-name{font-size:13px;font-weight:600}
  #ath-chip-mass{font-size:10px;color:var(--muted)}
  .athlete-menu{position:absolute;top:46px;right:0;background:var(--card);border:1px solid var(--line);
                border-radius:10px;padding:6px;min-width:210px;z-index:80;
                box-shadow:0 8px 24px rgba(0,0,0,0.5);max-height:340px;overflow-y:auto}
  .athlete-menu[hidden]{display:none}
  .am-item{display:block;width:100%;text-align:left;padding:10px;background:transparent;border:0;
           color:var(--fg);font-size:13px;cursor:pointer;border-radius:6px;min-height:40px}
  .am-item:hover{background:var(--accent-soft)}
  .am-item.active{color:var(--accent);font-weight:700}
  .am-add{display:block;padding:10px;margin-top:4px;border-top:1px solid var(--line);
          color:var(--accent);text-decoration:none;font-size:13px;font-weight:600}
  .header-setup{text-decoration:none;color:var(--fg);border:1px solid var(--line);border-radius:8px;
                width:40px;height:40px;display:flex;align-items:center;justify-content:center;font-size:18px}
  .auto-indicator{display:none;font-size:10px;font-weight:700;color:var(--good);
                  letter-spacing:0.08em;border:1px solid var(--good);border-radius:6px;padding:3px 7px}
  .auto-indicator::before{content:"● "}

  /* Mode pill bar — four top-level training modes, above the chart grid */
  .mode-bar{display:flex;gap:8px;margin-bottom:14px}
  .mode-pill{flex:1;min-height:48px;padding:12px 8px;border-radius:12px;
             background:var(--card);color:var(--muted);border:1px solid var(--line);
             font-size:14px;font-weight:700;letter-spacing:0.03em;cursor:pointer;
             text-align:center;transition:background 120ms,color 120ms,border-color 120ms}
  .mode-pill:hover{color:var(--fg);border-color:var(--accent)}
  .mode-pill.active{background:var(--accent);color:#0a0a0c;border-color:var(--accent)}

  /* Session presets — one-tap full session setup */
  .preset-row{display:flex;gap:8px;margin-bottom:14px;overflow-x:auto;padding-bottom:4px}
  .preset-btn{flex:0 0 auto;min-height:48px;padding:10px 14px;font-size:13px;font-weight:600;
              background:var(--card);color:var(--fg);border:1px solid var(--line);
              border-radius:10px;cursor:pointer;white-space:nowrap}
  .preset-btn:hover{border-color:var(--accent)}
  .preset-btn.active{border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
  @media (min-width:880px){ .preset-row{flex-wrap:wrap;overflow-x:visible} }
  /* Athlete profile card — derived PRs + L-V profile */
  .profile-card{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;
                background:var(--card);border:1px solid var(--line);border-radius:10px;
                padding:10px 14px;margin-bottom:14px}
  .profile-card[hidden]{display:none}
  .pc-stats{display:flex;gap:14px;flex-wrap:wrap}
  .pc-stat{display:flex;flex-direction:column;line-height:1.2}
  .pc-stat .pc-l{font-size:9px;letter-spacing:0.06em;color:var(--muted);text-transform:uppercase}
  .pc-stat .pc-v{font-size:15px;font-weight:700;color:var(--fg)}
  .pc-meta{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;font-size:11px;color:var(--muted)}
  .pc-orient{color:var(--accent);font-weight:600}
  /* Auto-apply load advisor */
  .autoload{margin-top:8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  .autoload select,.autoload input{min-height:40px}
  .autoload input[hidden]{display:none}
  #cfg-autoload-param{width:80px}
  .resist-suggest{flex:1 1 100%;font-size:11px;color:var(--muted);min-height:14px}
  .resist-suggest.has{color:var(--accent);font-weight:600}
  .preset-target{background:#0a0a0c;border:1px solid var(--line);border-radius:8px;
                 padding:10px 12px;margin-bottom:10px;font-size:12px}
  .preset-target[hidden]{display:none}
  .pt-label{color:var(--fg);font-weight:600;margin-bottom:6px}
  .pt-dots{display:flex;gap:5px;flex-wrap:wrap}
  .pt-dot{width:16px;height:16px;border-radius:50%;border:1px solid var(--line);background:transparent}
  .pt-dot.hit{background:var(--good);border-color:var(--good)}
  .pt-dot.miss{background:var(--bad);border-color:var(--bad)}
  .pt-summary{margin-top:6px;color:var(--muted)}

  @media (max-width:414px){
    .mode-bar{flex-wrap:wrap}
    .mode-pill{flex:1 1 calc(50% - 4px);font-size:13px}
  }
  /* Small phones — tighten type one notch so nothing forces a horizontal scroll */
  @media (max-width:414px){
    h1{font-size:18px}
    .stat-line .v.primary{font-size:46px}
    .stat-line .v.secondary{font-size:26px}
  }

  /* Drill picker — dropdown + description line */
  .drill-desc{font-size:11px;color:var(--muted);line-height:1.3;margin-top:4px;min-height:14px}
  /* Stepper — − [value] + for numeric fields */
  .stepper{display:flex;align-items:stretch;gap:6px}
  .stepper button{flex:0 0 46px;padding:0;font-size:22px;font-weight:700;line-height:1;
                  min-height:46px;background:#0a0a0c;color:var(--accent);
                  border:1px solid var(--line);border-radius:8px;cursor:pointer}
  .stepper button:hover{border-color:var(--accent)}
  .stepper button:active{background:var(--accent-soft)}
  .sheet .field .stepper input{flex:1 1 auto;width:auto;text-align:center;
                  font-size:20px;font-weight:700;min-height:46px}
  .stepper.off{opacity:0.4;pointer-events:none}
  /* On/off switch */
  .switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
  .switch input{position:absolute;opacity:0;width:0;height:0}
  .switch .track{width:42px;height:24px;border-radius:999px;background:var(--line);
                 position:relative;transition:background 140ms;flex:0 0 auto}
  .switch .track::after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;
                 border-radius:50%;background:#fff;transition:transform 140ms}
  .switch input:checked + .track{background:var(--accent)}
  .switch input:checked + .track::after{transform:translateX(18px)}
  .switch .sw-label{font-size:11px;font-weight:700;letter-spacing:0.08em;
                 text-transform:uppercase;color:var(--muted)}
  .field-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .field-head label{margin:0}
  /* Variable-resistance template picker */
  .vr-presets{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
  .vr-preset{padding:6px 10px;font-size:11px;font-weight:600;min-height:32px;
             background:#0a0a0c;color:var(--muted);border:1px solid var(--line);
             border-radius:7px;cursor:pointer}
  .vr-preset:hover{color:var(--fg);border-color:var(--accent)}
  .vr-preset.active{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
  .vr-preview{display:block;width:100%;height:90px;background:#0a0a0c;
             border:1px solid var(--line);border-radius:8px}
  .card{background:var(--card);border:1px solid var(--line);
        border-radius:14px;padding:18px;margin-bottom:14px}
  .meta{color:var(--muted);font-size:11px;letter-spacing:0.12em;text-transform:uppercase}
  .err{color:var(--bad)} .ok{color:var(--good)} .warn{color:var(--warn)} .muted{color:var(--muted)}
  button{appearance:none;padding:12px 18px;font-size:15px;
         background:var(--accent);color:#0a0a0c;border:0;border-radius:9px;
         font-weight:600;cursor:pointer}
  button.disarm{background:var(--bad);color:#fff}
  button.on{background:var(--good);color:#fff}
  button.secondary{background:transparent;color:var(--accent);
         border:1px solid var(--accent)}
  button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
  button:disabled{opacity:0.4;cursor:not-allowed}
  input[type=number],input[type=text],select{padding:9px;font-size:14px;background:#0a0a0c;color:var(--fg);
         border:1px solid var(--line);border-radius:6px}
  label{display:block;color:var(--muted);font-size:11px;letter-spacing:0.12em;text-transform:uppercase;margin:0 0 4px}

  /* Top grid: chart 60% + stats 40% */
  /* Top grid grows to fill the viewport so the chart uses the screen. */
  .top-grid{display:grid;grid-template-columns:3fr 2fr;grid-template-rows:1fr;gap:14px;
            flex:1 1 auto;min-height:360px;max-height:64vh;margin-bottom:14px}
  @media (max-width:767px){
    .top-grid{grid-template-columns:1fr;grid-template-rows:auto auto;
              flex:0 0 auto;max-height:none}
  }

  /* Chart card */
  .chart-card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;
              min-height:280px;display:flex;flex-direction:column;justify-content:center;
              align-items:stretch;position:relative}
  .chart-card.hidden{display:none}
  /* Per-mode intro card (§12) — first-time-per-mode overlay on the chart */
  .mode-intro{position:absolute;inset:0;background:var(--card);border-radius:14px;z-index:6;
              display:flex;flex-direction:column;justify-content:center;align-items:center;
              text-align:center;padding:24px;gap:10px}
  .mode-intro[hidden]{display:none}
  .mi-title{font-size:15px;font-weight:700;color:var(--accent);letter-spacing:0.06em;text-transform:uppercase}
  .mi-diagram svg{display:block}
  .mi-text{font-size:13px;color:var(--fg);line-height:1.55;max-width:44ch}
  .mi-actions{display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;justify-content:center}
  .mi-got{padding:10px 18px;font-size:14px;font-weight:700;background:var(--accent);
          color:#0a0a0c;border:0;border-radius:9px;cursor:pointer;min-height:44px}
  .mi-never{padding:10px 16px;font-size:13px;background:transparent;color:var(--muted);
            border:1px solid var(--line);border-radius:9px;cursor:pointer;min-height:44px}
  .mi-never:hover{color:var(--fg)}
  .chart-empty{color:var(--muted);text-align:center;font-size:14px;padding:60px 0}
  #live-chart{width:100%;height:auto;flex:1 1 auto;min-height:220px;
              background:#0a0a0c;border:1px solid var(--line);border-radius:6px}

  /* Stats card */
  .stats-card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;
              display:flex;flex-direction:column;gap:14px}
  .stat-line{display:flex;justify-content:space-between;align-items:baseline}
  .stat-line .l{font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted)}
  .stat-line .v{display:inline-flex;align-items:baseline;gap:4px;justify-content:flex-end}
  .stat-line .v.primary{font-size:72px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}
  .stat-line .v.secondary{font-size:32px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}
  .stat-line .v .unit{color:var(--muted);font-weight:600}
  .stat-line .v.primary .unit{font-size:20px}
  .stat-line .v.secondary .unit{font-size:14px}
  @media (max-width:767px){ .stat-line .v.primary{font-size:56px} }
  /* Current / Peak / Avg toggle above the headline Speed metric */
  .speed-toggle{display:inline-flex;align-self:flex-start;gap:2px;padding:3px;
                background:#0a0a0c;border:1px solid var(--line);border-radius:8px}
  .sp-tog{padding:5px 12px;font-size:11px;font-weight:600;letter-spacing:0.06em;
          text-transform:uppercase;background:transparent;color:var(--muted);
          border:0;border-radius:6px;cursor:pointer;min-height:30px}
  .sp-tog.active{background:var(--accent);color:#0a0a0c}
  .stats-foot{margin-top:auto;padding-top:14px;border-top:1px solid var(--line)}
  .session-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
  .session-stats > div{text-align:center}
  .session-stats .ss-l{font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
  .session-stats .ss-v{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--fg)}
  .session-stats .ss-v.muted{color:var(--muted);font-weight:400}
  @media (max-width:767px){ .session-stats{grid-template-columns:repeat(3,1fr)} .session-stats > div:nth-child(n+4){display:none} }

  /* Solo countdown overlay (§9) — fullscreen takeover with huge number */
  .cd-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:90;
              display:none;flex-direction:column;justify-content:center;align-items:center}
  .cd-overlay.show{display:flex}
  .cd-num{font-size:38vw;font-weight:800;line-height:1;color:var(--accent);
          font-variant-numeric:tabular-nums;text-shadow:0 0 60px rgba(91,139,255,0.5)}
  .cd-num.go{color:var(--good);font-size:30vw}
  .cd-label{font-size:18px;letter-spacing:0.16em;text-transform:uppercase;color:var(--muted);margin-top:24px}

  /* Inter-rep rest timer (§12) — fixed bottom-centre */
  .rest-timer{position:fixed;bottom:120px;left:50%;transform:translateX(-50%);
              background:var(--card);border:1px solid var(--line);border-radius:14px;
              padding:14px 20px;display:none;align-items:center;gap:14px;z-index:55;
              box-shadow:0 8px 24px rgba(0,0,0,0.4)}
  .rest-timer.show{display:flex}
  .rest-timer.warn{border-color:var(--warn)}
  .rt-label{font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
  .rt-num{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--fg);min-width:80px;text-align:center}
  .rt-actions{display:flex;gap:6px}
  .rt-add,.rt-skip{padding:6px 12px;font-size:12px;font-weight:600;border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--muted);cursor:pointer}
  .rt-skip:hover{color:var(--accent);border-color:var(--accent)}
  .rt-add:hover{color:var(--good);border-color:var(--good)}

  /* Tap-to-cycle metric tiles (§16.7) */
  .stat-line.tappable{cursor:pointer;border-radius:6px;padding:4px 6px;margin:0 -6px;
                      transition:background 120ms}
  .stat-line.tappable:hover{background:rgba(91,139,255,0.08)}
  .stat-line.tappable .l::after{content:"  ⇄";color:var(--muted);opacity:0;transition:opacity 120ms}
  .stat-line.tappable:hover .l::after{opacity:0.5}

  /* Universal Emergency Stop (§2.5) — every screen, every depth, one tap, no
     confirmation. Sized for sweaty thumb on a phone. Sits below phase pill. */
  .estop-btn{position:fixed;top:80px;right:18px;width:74px;height:74px;
             border-radius:50%;border:none;cursor:pointer;z-index:100;
             display:flex;align-items:center;justify-content:center;
             background:radial-gradient(circle at 50% 35%,#e84a4a 0%,#cc1f1f 38%,#9c0f0f 72%,#6b0808 100%);
             box-shadow:0 0 0 5px #7a0c0c,0 0 16px 6px rgba(255,80,80,0.35),
                        0 10px 22px rgba(0,0,0,0.55),
                        inset 0 5px 13px rgba(255,255,255,0.5),
                        inset 0 -13px 22px rgba(0,0,0,0.5);
             color:#fff;font-size:16px;font-weight:800;letter-spacing:0.05em;
             text-shadow:0 1px 3px rgba(0,0,0,0.6);
             -webkit-tap-highlight-color:transparent;transition:transform 100ms}
  .estop-btn::before{content:"";position:absolute;left:17%;top:11%;width:66%;height:44%;
             border-radius:50%;pointer-events:none;
             background:linear-gradient(rgba(255,255,255,0.6),rgba(255,255,255,0.04))}
  .estop-btn span{position:relative;z-index:1}
  .estop-btn:hover,.estop-btn:active{transform:scale(1.06)}
  .estop-btn:active{box-shadow:0 0 0 5px #7a0c0c,0 0 16px 6px rgba(255,80,80,0.35),
                        0 4px 10px rgba(0,0,0,0.55),
                        inset 0 5px 13px rgba(255,255,255,0.35),
                        inset 0 -13px 22px rgba(0,0,0,0.55)}
  .estop-btn.firing{animation:estop-pulse 0.4s ease-out 3}
  @keyframes estop-pulse{0%,100%{transform:scale(1)} 50%{transform:scale(1.15)}}

  /* Pre-drill confirmation card (§7) — modal overlay, intercepts Start drill */
  .prerep-mask{position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:80;
               opacity:0;pointer-events:none;transition:opacity 180ms}
  .prerep-mask.show{opacity:1;pointer-events:auto}
  .prerep-card{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(0.95);
               background:var(--card);border:1px solid var(--line);border-radius:16px;
               padding:24px;width:min(90vw,460px);z-index:81;
               opacity:0;pointer-events:none;transition:opacity 180ms,transform 180ms}
  .prerep-card.show{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1)}
  .prerep-title{font-size:14px;letter-spacing:0.14em;text-transform:uppercase;
                color:var(--accent);font-weight:600;margin-bottom:16px}
  .prerep-grid{display:grid;grid-template-columns:auto 1fr;gap:8px 14px;font-size:14px;margin-bottom:18px}
  .prerep-grid .l{color:var(--muted);font-size:11px;letter-spacing:0.12em;text-transform:uppercase;align-self:center}
  .prerep-grid .v{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}
  .prerep-actions{display:flex;gap:10px;align-items:stretch}
  .prerep-actions .ghost{flex:0 0 auto}
  .prerep-go{flex:1;padding:18px;font-size:24px;font-weight:800;letter-spacing:0.15em;
             background:var(--accent);color:#0a0a0c;border:0;border-radius:10px;cursor:pointer}
  .prerep-go:hover{background:#e89647}
  .prerep-skip{display:block;margin-top:14px;font-size:11px;color:var(--muted);cursor:pointer}
  .prerep-skip input{margin-right:6px}

  /* Rep validity controls (§11) */
  .rep-row.invalid{opacity:0.45;border-style:dashed}
  .rep-row .invalid-tag{font-size:9px;letter-spacing:0.12em;text-transform:uppercase;
                        color:var(--bad);margin-left:6px}
  .rep-row .v-toggle{background:transparent;color:var(--muted);border:0;font-size:14px;
                     padding:0 4px;cursor:pointer;line-height:1}
  .rep-row .v-toggle:hover{color:var(--bad)}
  .rep-row .v-toggle.invalid{color:var(--bad)}

  /* Run detail card */
  .rd-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px}
  .rd-tabs{display:flex;gap:4px}
  .rd-tab{padding:6px 14px;font-size:12px;font-weight:600;letter-spacing:0.05em;
          background:transparent;color:var(--muted);border:1px solid var(--line);
          border-radius:6px;cursor:pointer;text-transform:uppercase}
  .rd-tab:hover{color:var(--fg)}
  .rd-tab.active{background:var(--accent);color:#0a0a0c;border-color:var(--accent)}
  .rd-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
  @media (max-width:767px){ .rd-grid{grid-template-columns:1fr} .rd-tabs{flex-wrap:wrap} }
  .rd-section .rd-h{font-size:10px;letter-spacing:0.12em;text-transform:uppercase;color:var(--accent);margin-bottom:8px;font-weight:600}
  .rd-row{display:flex;justify-content:space-between;align-items:baseline;padding:4px 0;font-size:13px;border-bottom:1px solid #1f1f26}
  .rd-row:last-child{border-bottom:0}
  .rd-row .rd-l{color:var(--muted)}
  .rd-row .rd-v{color:var(--fg);font-weight:500;font-variant-numeric:tabular-nums}
  /* Insights tab — toolbar + view toggle */
  .ins-toolbar{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:18px}
  .ins-view-toggle{display:inline-flex;background:#0a0a0c;border:1px solid var(--line);border-radius:8px;padding:3px}
  .ins-view-btn{padding:6px 16px;font-size:12px;font-weight:600;letter-spacing:0.05em;text-transform:uppercase;background:transparent;color:var(--muted);border:0;cursor:pointer;border-radius:6px}
  .ins-view-btn.active{background:var(--accent);color:#0a0a0c}
  /* Athlete history drawer */
  .hist-meta{margin-bottom:18px;font-size:13px;color:var(--muted)}
  .hist-section{margin-bottom:22px}
  .hist-chart-controls{display:flex;gap:8px;flex-wrap:wrap}
  .hist-chart-controls select{flex:1;min-width:140px}
  .hist-list{display:flex;flex-direction:column;gap:8px;max-height:340px;overflow-y:auto}
  .hist-row{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;
            padding:10px 12px;background:#0a0a0c;border:1px solid var(--line);border-radius:6px;
            font-size:13px;cursor:pointer;transition:border-color 120ms}
  .hist-row:hover{border-color:var(--accent)}
  .hist-row .hist-date{color:var(--muted);font-weight:500;font-variant-numeric:tabular-nums;font-size:12px}
  .hist-row .hist-summary{color:var(--fg);font-variant-numeric:tabular-nums}
  .hist-row .hist-summary .sep{color:#3a3a44;margin:0 6px}
  .hist-row .hist-reps{color:var(--accent);font-weight:600;font-size:11px}
  .hist-empty{color:var(--muted);text-align:center;padding:22px;font-size:13px}

  /* Athlete view layout */
  .ath-headline{font-size:30px;font-weight:700;line-height:1.15;margin-bottom:10px;color:var(--fg)}
  .ath-verdict{font-size:15px;line-height:1.55;color:var(--muted);margin-bottom:24px;max-width:60ch}
  .ath-section{margin-bottom:22px}
  .ath-section-h{font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:var(--accent);margin-bottom:10px;font-weight:600}
  .ath-rx-list{display:flex;flex-direction:column;gap:10px}
  .ath-rx{background:#0a0a0c;border:1px solid var(--line);border-radius:8px;padding:14px}
  .ath-rx-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
  .ath-rx-num{font-size:11px;color:var(--muted);font-weight:600;letter-spacing:0.08em}
  .ath-rx-day{font-size:11px;color:var(--accent);font-weight:600}
  .ath-rx-label{font-size:15px;font-weight:600;color:var(--fg);margin-bottom:4px}
  .ath-rx-text{font-size:13px;color:var(--muted);line-height:1.5}
  .ath-avoid-list{display:flex;flex-direction:column;gap:8px}
  .ath-avoid{background:rgba(232,90,90,0.06);border:1px solid rgba(232,90,90,0.25);border-radius:8px;padding:12px;font-size:13px;color:var(--fg);line-height:1.5}
  .ath-avoid::before{content:"✗  ";color:var(--bad);font-weight:700}
  .ath-chase{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
  .ath-chase .name{font-size:14px;color:var(--muted);min-width:140px}
  .ath-chase .current{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--fg)}
  .ath-chase .arrow{color:var(--muted);font-size:18px}
  .ath-chase .target{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--accent)}
  .ath-chase-delta{flex-basis:100%;margin-top:6px;font-size:12px;color:var(--muted)}
  .ath-chase-delta b{color:var(--fg);font-weight:600}
  .ath-chase-delta .good{color:var(--good);font-weight:600}
  .ath-chase-delta .bad{color:var(--bad);font-weight:600}
  /* Athlete editor grid in history drawer */
  .hist-editor-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .hist-editor-grid .field label{font-size:10px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
  .hist-editor-grid .field input,.hist-editor-grid .field select{width:100%}

  /* Coach view (existing) */
  .ins-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:14px}
  .ins-headline{font-size:22px;font-weight:700;line-height:1.2;margin-top:2px}
  .ins-controls{display:flex;gap:10px}
  .ins-controls label{display:flex;flex-direction:column;gap:3px;font-size:10px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted)}
  .ins-controls select{margin-top:0}
  .ins-list{display:flex;flex-direction:column;gap:8px;margin-top:8px}
  .ins-card{background:#0a0a0c;border:1px solid var(--line);border-left-width:3px;border-radius:6px;padding:12px}
  .ins-card.high{border-left-color:var(--bad)}
  .ins-card.warn{border-left-color:var(--warn)}
  .ins-card.info{border-left-color:var(--accent)}
  .ins-card.ok{border-left-color:var(--good)}
  .ins-card .ins-tag{font-size:11px;letter-spacing:0.12em;text-transform:uppercase;font-weight:600;margin-bottom:4px}
  .ins-card.high .ins-tag{color:var(--bad)}
  .ins-card.warn .ins-tag{color:var(--warn)}
  .ins-card.info .ins-tag{color:var(--accent)}
  .ins-card.ok .ins-tag{color:var(--good)}
  .ins-card .ins-detail{font-size:13px;line-height:1.5;color:var(--fg)}
  .ins-card ul{margin:6px 0 0 0;padding-left:18px;font-size:13px;color:var(--fg)}
  .ins-card .ins-do-not{color:var(--bad)}
  .ins-card .ins-evidence{font-size:10px;color:var(--muted);margin-top:6px;font-style:italic}

  /* Steps tab meta strip */
  .rd-meta-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  .rd-meta-strip > div{text-align:center;padding:8px;background:#0a0a0c;border:1px solid var(--line);border-radius:6px}
  .rd-meta-strip .rd-l{display:block;font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
  .rd-meta-strip .rd-v{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
  /* Splits table */
  .rd-splits-table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
  .rd-splits-table th{text-align:left;color:var(--muted);font-size:10px;letter-spacing:0.12em;text-transform:uppercase;font-weight:600;padding:8px 4px;border-bottom:1px solid var(--line)}
  .rd-splits-table td{padding:10px 4px;color:var(--fg);border-bottom:1px solid #1f1f26}
  .rd-splits-table td:last-child,.rd-splits-table th:last-child{text-align:right}

  /* Recent reps list */
  .reps-list{display:flex;flex-direction:column;gap:6px}
  .rep-row{display:grid;grid-template-columns:44px 1fr auto;gap:10px;align-items:center;
           padding:10px 12px;background:#0a0a0c;border:1px solid var(--line);
           border-left:3px solid transparent;border-radius:8px;cursor:pointer;
           font-size:15px;font-variant-numeric:tabular-nums;
           transition:background 120ms,border-color 120ms;-webkit-tap-highlight-color:transparent}
  .rep-row:hover,.rep-row:focus-visible{border-left-color:var(--accent);
           background:rgba(91,139,255,0.07);outline:none}
  .rep-row.active{border-left-color:var(--accent);background:rgba(91,139,255,0.05)}
  .rep-row .num{color:var(--muted);font-weight:600}
  .rep-row .stats{color:var(--fg);font-weight:500}
  .rep-row .stats .sep{color:#3a3a44;margin:0 8px}
  .rep-row .chev{color:var(--muted);font-size:20px;line-height:1}
  .rep-row:hover .chev,.rep-row.active .chev{color:var(--accent)}
  .reps-empty{color:var(--muted);text-align:center;padding:14px;font-size:14px}
  /* Per-rep annotation controls + set grouping */
  .rep-color,.rep-note{background:transparent;border:0;cursor:pointer;padding:0 3px;
                       line-height:1;color:var(--muted);font-size:13px}
  .rep-note.has{color:var(--accent)}
  .rep-color:hover,.rep-note:hover{color:var(--fg)}
  .rep-comment{font-size:11px;color:var(--muted);font-style:italic;margin-top:3px;
               font-variant-numeric:normal}
  .set-head{display:flex;justify-content:space-between;align-items:baseline;
            font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
            color:var(--accent);font-weight:700;margin-top:8px;padding-top:8px;
            border-top:1px solid var(--line)}
  .set-head:first-child{margin-top:0;padding-top:0;border-top:0}
  .set-sum{color:var(--muted);font-weight:600;letter-spacing:0.04em;text-transform:none}
  .reps-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
  #new-set-btn{padding:5px 12px;font-size:11px;font-weight:700;letter-spacing:0.04em;
               background:transparent;color:var(--accent);border:1px solid var(--accent);
               border-radius:7px;cursor:pointer;min-height:32px}

  /* Bottom bar (sticky — scrolls with content, rests at the viewport bottom) */
  /* Primary control as a lifted, centred floating pill — off the bottom edge,
     not full-width, still within thumb reach (per PPA HIG brief §5.2). */
  .bottombar{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);
             width:auto;max-width:min(600px,calc(100vw - 36px));
             background:rgba(18,26,51,0.94);border:1px solid var(--line-strong);
             border-radius:16px;padding:10px;z-index:40;
             box-shadow:0 20px 48px -22px rgba(3,7,18,0.9);
             backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
  .bottombar-inner{margin:0;display:flex;align-items:center;gap:10px}
  #adjust-btn{flex:0 0 auto;min-height:56px;padding:0 16px;font-size:13px;font-weight:700;
              background:transparent;color:var(--fg);border:1px solid var(--line);
              border-radius:10px;cursor:pointer}
  #primary-action{flex:1;min-height:56px;font-size:17px;font-weight:700}
  #primary-action.danger{background:var(--bad);color:#fff;min-height:64px}
  #primary-action.warn{background:var(--warn);color:#0a0a0c}

  /* Settings sheet (slides in from right on coach) */
  .sheet-mask{position:fixed;inset:0;background:rgba(0,0,0,0.5);opacity:0;pointer-events:none;
              transition:opacity 200ms ease;z-index:60}
  .sheet-mask.show{opacity:1;pointer-events:auto}
  .sheet{position:fixed;top:0;right:0;height:100%;width:380px;max-width:100vw;background:var(--card);
         border-left:1px solid var(--line);transform:translateX(100%);transition:transform 240ms ease;
         z-index:70;padding:22px;overflow-y:auto;display:flex;flex-direction:column;gap:14px}
  .sheet.show{transform:translateX(0)}
  .sheet h2{margin:0 0 8px;font-size:18px;font-weight:600}
  .sheet .field{display:flex;flex-direction:column;gap:4px}
  .sheet .field input,.sheet .field select{width:100%}
  .sheet-actions{display:flex;gap:10px;margin-top:10px}
  .sheet-close{position:absolute;top:14px;right:14px;background:transparent;color:var(--muted);
               border:0;font-size:22px;cursor:pointer}
  /* Desktop: the sheet is open by default and gets its own column — the main
     content reflows into the space beside it instead of sitting underneath. */
  @media (min-width:1100px){
    body.sheet-open .wrap{max-width:1500px;margin:0 auto;padding-right:418px}
    body.sheet-open .bottombar{transform:translateX(calc(-50% - 209px))}
    body.sheet-open #estop-btn{right:398px}
  }
  @media (min-width:1280px){
    body.sheet-open .wrap{max-width:1800px}
  }

  /* Reports (collapsible) */
  details.report{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:14px}
  details.report > summary{cursor:pointer;font-weight:600;font-size:15px;list-style:none}
  details.report > summary::-webkit-details-marker{display:none}
  details.report > summary::before{content:"›";display:inline-block;margin-right:8px;color:var(--muted);
                                  transition:transform 150ms;font-size:18px}
  details.report[open] > summary::before{transform:rotate(90deg)}
  details.report .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  details.report .row select,details.report .row button{}
  #cmp-chart,#fv-chart{width:100%;background:#0a0a0c;border:1px solid var(--line);border-radius:6px;margin-top:8px}

  /* ===== Visual calm pass — rounder cards, soft depth, more air (2026-07) ===== */
  .chart-card,.stats-card{border-radius:20px;padding:22px}
  .card,#run-detail,.profile-card,.rest-timer,.prerep-card{border-radius:18px}
  .chart-card,.stats-card,.card,#run-detail{
    box-shadow:0 1px 2px rgba(3,7,18,.4), 0 20px 46px -28px rgba(3,7,18,.75)}
  .top-grid{gap:16px}
  .mode-bar,.preset-row{gap:10px}
  .mode-pill{border-radius:14px}
  /* active mode = outlined warm/cool state accent (mockup), not a solid blue fill */
  .mode-pill.active{background:var(--state-soft);color:var(--state);border-color:var(--state)}
  .mode-pill:hover{border-color:var(--state)}
  /* E-stop as a flat red pill matching the Arm/Adjust button family — still fixed
     top-right, always visible, red = danger (label carries the meaning) */
  .estop-btn{position:static;top:auto;right:auto;flex:0 0 auto;width:auto;height:auto;
    min-height:56px;padding:0 22px;border-radius:11px;display:inline-flex;align-items:center;
    background:var(--bad);border:1px solid var(--bad);box-shadow:none;font-size:15px;
    font-weight:800;letter-spacing:.06em;text-shadow:none}
  .estop-btn::before{display:none}
  .estop-btn:hover,.estop-btn:active{transform:none;filter:brightness(1.08)}
  .bottombar{max-width:min(680px,calc(100vw - 36px))}
  .preset-btn{border-radius:12px}
  .rd-tab{border-radius:10px}
  .speed-toggle,.ins-view-toggle{background:rgba(4,8,20,.5)}
  .rd-row{border-bottom-color:var(--line)}
  .prerep-go:hover{background:#7ea6ff}
  .sheet{box-shadow:-24px 0 60px -30px rgba(3,7,18,.85)}
  /* room under the floating primary-control pill so content isn't obscured */
  .wrap{padding-bottom:118px}
  /* relocated session presets — compact 2-col grid inside the Adjust sheet */
  .sheet-presets{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .sheet-presets .preset-btn{flex:none;min-height:44px;font-size:12.5px;padding:8px 10px;
    text-align:center;line-height:1.15}
  /* Mode selector inside the Adjust panel (relocated from the top bar) */
  .sheet .mode-bar{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:0}
  .sheet .mode-pill{flex:none;min-height:44px;font-size:13px;padding:9px 6px;letter-spacing:.02em}
  /* Variable-resistance editor (mockup): draggable curve + axis toggle */
  #vr-preview{height:110px;cursor:ns-resize;touch-action:none;border-radius:10px}
  .vr-axis{display:flex;gap:4px;margin-bottom:8px;border:1px solid var(--line);border-radius:9px;
    padding:3px;background:rgba(4,8,20,.5)}
  .vr-axis button{flex:1;border:0;background:transparent;color:var(--muted);font-size:12px;
    font-weight:700;padding:6px;border-radius:6px;cursor:pointer}
  .vr-axis button.on{background:var(--state);color:#08122a}
  .vr-presets{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  /* Few bullet-graph — load vs 53 kg cap. Greyscale bands (working <30 / high 30-45 /
     near-cap 45-53 faint warn); accent measure bar carries the colour; red cap tick. */
  .load-bullet{margin-top:12px}
  .lb-track{position:relative;height:22px;border-radius:7px;border:1px solid var(--line);overflow:hidden;
    background:linear-gradient(90deg,rgba(129,142,174,.13) 0 57%,rgba(129,142,174,.22) 57% 85%,
      rgba(255,148,64,.20) 85% 100%)}
  .lb-fill{position:absolute;left:0;top:6px;height:10px;width:9%;border-radius:5px;
    background:var(--accent);box-shadow:0 0 0 2px var(--card);transition:width .15s ease}
  .lb-cap{position:absolute;top:0;bottom:0;right:0;width:2px;background:var(--bad)}
  .lb-scale{display:flex;justify-content:space-between;margin-top:5px;font-size:10px;
    color:var(--ink-faint);letter-spacing:.03em}
  .lb-scale span:nth-child(2){color:var(--fg);font-weight:600}
  /* Hero emphasis (Few, Information Dashboard Design — mistake #10 "highlight the
     important data"; data-ink: enhance the hero, recede the rest). Speed is the
     one number; Force/Power/Cable-out stay readable but subordinate. */
  .stat-line .v.primary{font-size:88px;font-weight:700;letter-spacing:-.02em;line-height:.92}
  @media (max-width:767px){ .stat-line .v.primary{font-size:60px} }
  @media (max-width:414px){ .stat-line .v.primary{font-size:52px} }
  .stat-line .v.secondary{font-size:28px;font-weight:500}
  .stat-line[data-tile]:not([data-tile="speed"]) .l{color:var(--ink-faint)}
  .stat-line[data-tile]:not([data-tile="speed"]) .v.secondary{opacity:.82}
  /* Hero merge — one card: header · number · live trace · tiles · session */
  .top-grid{grid-template-columns:1fr}
  .hero-card{display:flex;flex-direction:column;gap:12px}
  .hero-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:2px}
  .phase-badge{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:800;
    letter-spacing:.09em;text-transform:uppercase;color:var(--state);background:var(--state-soft);
    padding:6px 13px;border-radius:999px}
  .phase-badge::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--state)}
  .hero-speed{margin:2px 0}
  .hero-chart{position:relative}
  .hero-chart .chart-empty{padding:16px 0;font-size:13px}
  .hero-chart #live-chart{min-height:150px}
  .hero-tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;
    border-top:1px solid var(--line);padding-top:14px}
  .hero-tiles .stat-line{flex-direction:column;align-items:flex-start;gap:3px;margin:0;padding:8px 10px;border-radius:10px}
  .hero-tiles .stat-line .l{font-size:10px}
  .hero-tiles .stat-line .v{justify-content:flex-start}
  .hero-tiles .stat-line .v.secondary{font-size:26px;opacity:1}
</style>
</head><body>

<!-- e-stop relocated into the floating control pill (with Adjust + Arm rig) -->

<div class="wrap">

  <div class="topbar">
    <h1><img class="brand-logo" src="/static/ppa-logo.png" alt="Plummer Performance Academy"><span class="accent">·</span> Sprint Trainer</h1>
    <div class="topbar-right">
      <select id="rig-select" title="Active rig" style="padding:6px 10px;font-size:12px"></select>
      <button type="button" id="gear-toggle" class="gear-toggle">Gear 1</button>
      <span id="gear-warn-chip" class="gear-warn-chip" style="display:none">Pulley attached?</span>
      <div class="athlete-chip" id="athlete-chip">
        <button type="button" id="athlete-chip-btn">
          <span class="ath-avatar" id="ath-avatar">–</span>
          <span class="ath-chip-text">
            <span id="ath-chip-name">No athlete</span>
            <span id="ath-chip-mass"></span>
          </span>
        </button>
        <div class="athlete-menu" id="athlete-menu" hidden></div>
        <select id="athlete-select" hidden><option value="">— Athlete —</option></select>
      </div>
      <span class="auto-indicator" id="auto-indicator" title="Full Auto rep detection on">AUTO</span>
      <div class="state-tag" id="state-tag">Connecting…</div>
      <a class="header-setup" href="/setup" title="Setup" aria-label="Setup">⚙</a>
    </div>
  </div>

  <!-- mode selector relocated into the Adjust panel (per mockup) -->

  <div class="profile-card" id="athlete-profile-card" hidden>
    <div class="pc-stats" id="pc-stats"></div>
    <div class="pc-meta" id="pc-meta"></div>
  </div>

  <!-- session presets relocated into the Adjust sheet ("Quick start") to keep the
       live screen to one hero + minimal chrome (Few #11; HIG progressive disclosure) -->

  <div class="top-grid">
    <div class="stats-card hero-card">
      <div class="hero-head">
        <span class="phase-badge" id="phase-badge">Resist</span>
        <div class="speed-toggle" id="speed-toggle" role="group" aria-label="Speed metric">
          <button class="sp-tog active" data-idx="0" aria-pressed="true">Current</button>
          <button class="sp-tog" data-idx="1" aria-pressed="false">Peak</button>
          <button class="sp-tog" data-idx="2" aria-pressed="false">Avg</button>
        </div>
      </div>
      <div class="stat-line tappable hero-speed" data-tile="speed" title="Tap to cycle metric"><span class="l" id="stat-speed-l">Speed</span><span class="v primary" id="stat-speed"><span class="num">0.00</span><span class="unit">m/s</span></span></div>
      <div class="hero-chart">
        <div class="chart-empty" id="chart-empty">Start a drill to see the live trace</div>
        <svg id="live-chart" viewBox="0 0 600 240" preserveAspectRatio="none" style="display:none"></svg>
        <div class="mode-intro" id="mode-intro" hidden>
          <div class="mi-title" id="mi-title"></div>
          <div class="mi-diagram" id="mi-diagram"></div>
          <div class="mi-text" id="mi-text"></div>
          <div class="mi-actions">
            <button type="button" class="mi-got" id="mi-got">Got it</button>
            <button type="button" class="mi-never" id="mi-never">Don't show again</button>
          </div>
        </div>
      </div>
      <div class="hero-tiles">
        <div class="stat-line tappable" data-tile="force" title="Tap to cycle metric"><span class="l" id="stat-force-l">Force</span><span class="v secondary" id="stat-force"><span class="num">0</span><span class="unit">N</span></span></div>
        <div class="stat-line tappable" data-tile="power" title="Tap to cycle metric"><span class="l" id="stat-power-l">Power</span><span class="v secondary" id="stat-power"><span class="num">0</span><span class="unit">W</span></span></div>
        <div class="stat-line tappable" data-tile="ext" title="Tap to cycle metric"><span class="l" id="stat-ext-l">Cable out</span><span class="v secondary" id="stat-ext"><span class="num">0.00</span><span class="unit">m</span></span></div>
      </div>
      <div class="stats-foot">
        <div class="session-stats" id="session-stats">
          <div><div class="ss-l">REPS</div><div class="ss-v" id="ss-reps">0</div></div>
          <div><div class="ss-l">BEST SPEED</div><div class="ss-v" id="ss-best-speed">–</div></div>
          <div><div class="ss-l">BEST POWER</div><div class="ss-v" id="ss-best-power">–</div></div>
          <div><div class="ss-l">TOTAL WORK</div><div class="ss-v" id="ss-work">–</div></div>
          <div><div class="ss-l">TIMER</div><div class="ss-v" id="ss-timer">–</div></div>
        </div>
      </div>
    </div>
  </div>

  <div class="card" id="run-detail" style="display:none">
    <div class="rd-head">
      <div class="meta">Run detail · <span id="rd-rep-label">–</span></div>
      <div class="rd-tabs" role="tablist">
        <button class="rd-tab active" data-tab="profile">Profile</button>
        <button class="rd-tab" data-tab="insights">Insights</button>
        <button class="rd-tab" data-tab="steps">Steps</button>
        <button class="rd-tab" data-tab="splits">Splits</button>
        <button class="rd-tab" data-tab="fv">F · V</button>
      </div>
    </div>

    <!-- Profile tab -->
    <div class="rd-panel" data-tab="profile">
      <div class="rd-grid">
        <div class="rd-section">
          <div class="rd-h">Sprint profile</div>
          <div class="rd-row"><span class="rd-l">Peak speed</span><span class="rd-v" id="rd-peak-v">–</span></div>
          <div class="rd-row"><span class="rd-l">Time to peak v</span><span class="rd-v" id="rd-ttv">–</span></div>
          <div class="rd-row"><span class="rd-l">Distance to peak v</span><span class="rd-v" id="rd-dtv">–</span></div>
          <div class="rd-row"><span class="rd-l">Time to 90% v</span><span class="rd-v" id="rd-t90">–</span></div>
          <div class="rd-row"><span class="rd-l">Distance to 90% v</span><span class="rd-v" id="rd-d90">–</span></div>
          <div class="rd-row"><span class="rd-l">V dropoff</span><span class="rd-v" id="rd-drop">–</span></div>
          <div class="rd-row"><span class="rd-l">Decel time</span><span class="rd-v" id="rd-decelt">–</span></div>
        </div>
        <div class="rd-section">
          <div class="rd-h">Cadence</div>
          <div class="rd-row"><span class="rd-l">Strides</span><span class="rd-v" id="rd-strides">–</span></div>
          <div class="rd-row"><span class="rd-l">Stride freq</span><span class="rd-v" id="rd-sf">–</span></div>
          <div class="rd-row"><span class="rd-l">Avg stride length</span><span class="rd-v" id="rd-sl">–</span></div>
          <div class="rd-row"><span class="rd-l">Length consistency</span><span class="rd-v" id="rd-slstd">–</span></div>
        </div>
        <div class="rd-section">
          <div class="rd-h">Power & work</div>
          <div class="rd-row"><span class="rd-l">Peak force</span><span class="rd-v" id="rd-pf">–</span></div>
          <div class="rd-row"><span class="rd-l">Peak power</span><span class="rd-v" id="rd-pwk">–</span></div>
          <div class="rd-row"><span class="rd-l">Avg power</span><span class="rd-v" id="rd-avgp">–</span></div>
          <div class="rd-row"><span class="rd-l">Work</span><span class="rd-v" id="rd-work">–</span></div>
          <div class="rd-row"><span class="rd-l">Impulse</span><span class="rd-v" id="rd-imp">–</span></div>
        </div>
      </div>
    </div>

    <!-- Insights tab -->
    <div class="rd-panel" data-tab="insights" style="display:none">
      <div class="ins-toolbar">
        <div class="ins-view-toggle" role="tablist">
          <button class="ins-view-btn active" data-view="athlete">Athlete</button>
          <button class="ins-view-btn" data-view="coach">Coach</button>
        </div>
        <div class="ins-controls">
          <label>Position
            <select id="ins-position">
              <option value="back" selected>Back</option>
              <option value="forward">Forward</option>
            </select>
          </label>
          <label>Planned modality
            <select id="ins-modality">
              <option value="">—</option>
              <option value="heavy_resisted">Heavy resisted</option>
              <option value="assisted">Assisted / overspeed</option>
            </select>
          </label>
        </div>
      </div>

      <!-- Athlete view -->
      <div class="ins-athlete-view" id="ins-athlete-view">
        <div class="ath-headline" id="ath-headline">–</div>
        <div class="ath-verdict" id="ath-verdict"></div>
        <div class="ath-section">
          <div class="ath-section-h">This week</div>
          <div id="ath-prescriptions" class="ath-rx-list"></div>
        </div>
        <div class="ath-section" id="ath-avoid-section">
          <div class="ath-section-h">Avoid this week</div>
          <div id="ath-avoids" class="ath-avoid-list"></div>
        </div>
        <div class="ath-section">
          <div class="ath-section-h">Chase this number</div>
          <div class="ath-chase" id="ath-chase">–</div>
        </div>
      </div>

      <!-- Coach view (verbose, all insights) -->
      <div class="ins-coach-view" id="ins-coach-view" style="display:none">
        <div class="ins-head">
          <div>
            <div class="meta">Verdict</div>
            <div class="ins-headline" id="ins-headline">–</div>
            <div class="meta" id="ins-limiter"></div>
          </div>
        </div>
        <div id="ins-primary" class="ins-list"></div>
        <div class="meta" style="margin-top:14px;font-size:11px;letter-spacing:0.05em">Context</div>
        <div id="ins-context" class="ins-list"></div>
        <div class="meta" id="ins-caveat" style="margin-top:14px;font-size:10px;font-style:italic;color:var(--muted)"></div>
      </div>
    </div>

    <!-- Steps tab -->
    <div class="rd-panel" data-tab="steps" style="display:none">
      <div class="rd-meta-strip">
        <div><span class="rd-l">Strides</span><span class="rd-v" id="st-strides">–</span></div>
        <div><span class="rd-l">Stride freq</span><span class="rd-v" id="st-sf">–</span></div>
        <div><span class="rd-l">Avg length</span><span class="rd-v" id="st-sl">–</span></div>
        <div><span class="rd-l">Length consistency</span><span class="rd-v" id="st-slstd">–</span></div>
      </div>
      <svg id="steps-chart" viewBox="0 0 600 220" preserveAspectRatio="none" height="220"
           style="width:100%;background:#0a0a0c;border:1px solid var(--line);border-radius:6px;margin-top:10px"></svg>
      <div id="steps-empty" class="meta" style="display:none;text-align:center;padding:30px 0">No stride data — needs cable-force peaks (1 kHz xlsx import)</div>
    </div>

    <!-- Splits tab -->
    <div class="rd-panel" data-tab="splits" style="display:none">
      <table class="rd-splits-table" id="splits-table"></table>
    </div>

    <!-- F-V tab -->
    <div class="rd-panel" data-tab="fv" style="display:none">
      <div class="meta">Force vs velocity, sampled across this rep · one dot per sample, coloured by time</div>
      <svg id="fv-rep-chart" viewBox="0 0 600 240" preserveAspectRatio="none" height="240"
           style="width:100%;background:#0a0a0c;border:1px solid var(--line);border-radius:6px;margin-top:10px"></svg>
    </div>

    <div class="meta" id="rd-note" style="margin-top:10px;font-size:10px"></div>
  </div>

  <div class="card">
    <div class="reps-head">
      <span class="meta">Recent reps</span>
      <button type="button" id="new-set-btn" title="Group following reps into a new set">+ New set</button>
    </div>
    <div class="preset-target" id="preset-target" hidden></div>
    <div class="reps-list" id="reps-list"></div>
  </div>

  <details class="report" id="cmp-card">
    <summary>Compare two reps</summary>
    <div class="meta" style="margin-top:8px">Speed (orange) + force (blue) overlaid</div>
    <div class="row">
      <select id="cmp-a" style="flex:1"><option value="">Rep A</option></select>
      <select id="cmp-b" style="flex:1"><option value="">Rep B</option></select>
      <button id="cmp-go" class="secondary">Compare</button>
    </div>
    <svg id="cmp-chart" viewBox="0 0 600 140" preserveAspectRatio="none" height="140"></svg>
  </details>

  <details class="report" id="fv-card">
    <summary>Force-Velocity profile</summary>
    <div class="meta" style="margin-top:8px">All reps in current session</div>
    <div class="row">
      <button id="fv-go" class="secondary">Build F-V profile</button>
    </div>
    <div id="fv-result" class="meta" style="margin-top:8px">Need at least 2 reps at different loads</div>
    <svg id="fv-chart" viewBox="0 0 600 200" preserveAspectRatio="none" height="200"></svg>
  </details>

</div>

<!-- Countdown overlay (§9) — fullscreen black, huge number -->
<div class="cd-overlay" id="cd-overlay">
  <div class="cd-num" id="cd-num">5</div>
  <div class="cd-label">drill starting</div>
</div>

<!-- Inter-rep rest timer (§12) — fixed bottom-centre, dismissible -->
<div class="rest-timer" id="rest-timer">
  <div class="rt-label">REST</div>
  <div class="rt-num" id="rt-num">3:00</div>
  <div class="rt-actions">
    <button class="rt-add" id="rt-add">+30s</button>
    <button class="rt-skip" id="rt-skip">Skip</button>
  </div>
</div>

<!-- Pre-drill confirmation card (modal, intercepts Start drill) -->
<div class="prerep-mask" id="prerep-mask"></div>
<div class="prerep-card" id="prerep-card" role="dialog" aria-modal="true" aria-labelledby="prerep-title">
  <div class="prerep-title" id="prerep-title">Confirm drill</div>
  <div class="prerep-grid" id="prerep-grid"></div>
  <div class="prerep-actions">
    <button class="ghost" id="prerep-edit">Edit</button>
    <button class="prerep-go" id="prerep-go">GO</button>
  </div>
  <label class="prerep-skip">
    <input type="checkbox" id="prerep-skip-cb"> Skip confirmation for the rest of this session
  </label>
</div>


<!-- Sticky bottom bar -->
<div class="bottombar">
  <div class="bottombar-inner" id="bottombar-inner">
    <button type="button" id="adjust-btn" title="Adjust drill settings">Adjust</button>
    <button id="primary-action">Arm rig</button>
    <button id="estop-btn" class="estop-btn" title="Emergency stop — one tap, no confirmation" aria-label="Emergency stop"><span>STOP</span></button>
  </div>
</div>

<!-- Settings sheet (slides in from right) -->
<div class="sheet-mask" id="sheet-mask"></div>
<aside class="sheet" id="sheet">
  <button class="sheet-close" id="sheet-close" aria-label="Close">×</button>
  <h2>Adjust</h2>

  <div class="field">
    <label>Mode</label>
    <div class="mode-bar" id="mode-bar" role="tablist" aria-label="Training mode">
      <button class="mode-pill active" data-mode="resisted" role="tab" aria-selected="true">Resisted</button>
      <button class="mode-pill" data-mode="assisted" role="tab" aria-selected="false">Assisted</button>
      <button class="mode-pill" data-mode="cod" role="tab" aria-selected="false">C.O.D.</button>
      <button class="mode-pill" data-mode="gym" role="tab" aria-selected="false">Gym</button>
      <button class="mode-pill" data-mode="flywheel" role="tab" aria-selected="false">Flywheel</button>
    </div>
  </div>

  <!-- Quick-start presets cut (Purpose/Simplicity: Mode + Drill + Load already cover
       setup; presets overlapped both). applyPreset() kept dormant for easy restore. -->

  <div class="field">
    <label>Drill</label>
    <input type="hidden" id="cfg-drill" value="Sprint">
    <select id="cfg-drill-sel" aria-label="Drill"></select>
    <div class="drill-desc" id="cfg-drill-desc"></div>
  </div>
  <div class="field" data-modes="resisted assisted cod gym">
    <label id="cfg-resist-l">Working resistance (kg)</label>
    <div class="stepper">
      <button type="button" data-step="-1" data-target="cfg-resist" aria-label="Decrease">−</button>
      <input type="number" id="cfg-resist" value="5" step="0.5" min="0.5" max="53" inputmode="decimal">
      <button type="button" data-step="1" data-target="cfg-resist" aria-label="Increase">+</button>
    </div>
    <div class="autoload">
      <label class="switch">
        <input type="checkbox" id="cfg-autoload-toggle">
        <span class="track"></span><span class="sw-label">Auto-load</span>
      </label>
      <select id="cfg-autoload" aria-label="Auto-apply load" hidden>
        <option value="previous">Match previous</option>
        <option value="bodyweight_pct">Body weight %</option>
        <option value="vdec">Velocity decrement</option>
      </select>
      <input type="number" id="cfg-autoload-param" hidden step="1" min="1" max="100" placeholder="%">
      <div class="resist-suggest" id="cfg-resist-suggested"></div>
    </div>
    <!-- Few bullet-graph: current load vs the 53 kg hard cap, against working/high/near-cap bands -->
    <div class="load-bullet" aria-hidden="true">
      <div class="lb-track"><div class="lb-fill" id="lb-fill"></div><div class="lb-cap" title="53 kg hard cap"></div></div>
      <div class="lb-scale"><span>0</span><span id="lb-val">5 kg</span><span>53 kg cap</span></div>
    </div>
  </div>
  <div class="field" data-modes="resisted assisted cod">
    <label id="cfg-resist-dist-l">Resist distance (m)</label>
    <div class="stepper">
      <button type="button" data-step="-5" data-target="cfg-resist-dist" aria-label="Decrease">−</button>
      <input type="number" id="cfg-resist-dist" value="15" step="5" min="1" max="50" inputmode="numeric">
      <button type="button" data-step="5" data-target="cfg-resist-dist" aria-label="Increase">+</button>
    </div>
  </div>
  <div class="field" data-modes="resisted assisted">
    <div class="field-head">
      <label>Velocity cap (m/s)</label>
      <label class="switch">
        <input type="checkbox" id="cfg-vcap-toggle">
        <span class="track"></span>
      </label>
    </div>
    <div class="stepper" id="cfg-vcap-stepper">
      <button type="button" data-step="-0.5" data-target="cfg-vcap" aria-label="Decrease">−</button>
      <input type="number" id="cfg-vcap" value="6" step="0.5" min="0.5" max="15" inputmode="decimal">
      <button type="button" data-step="0.5" data-target="cfg-vcap" aria-label="Increase">+</button>
    </div>
    <div class="meta" id="cfg-vcap-conflict" style="font-size:10px;margin-top:4px" hidden>Off — a velocity-axis variable-resistance curve is active, and that already shapes load by cable speed.</div>
  </div>
  <div class="field" data-modes="resisted assisted">
    <label>Variable resistance</label>
    <div class="vr-axis" id="vr-axis">
      <button type="button" data-axis="off" class="on">Off</button>
      <button type="button" data-axis="distance">Distance</button>
      <button type="button" data-axis="velocity">Velocity</button>
    </div>
    <svg class="vr-preview" id="vr-preview" viewBox="0 0 300 110" preserveAspectRatio="none"></svg>
    <div class="vr-presets" id="vr-presets">
      <button type="button" class="vr-preset" data-shape="flat">Flat</button>
      <button type="button" class="vr-preset" data-shape="ascending">Ascending</button>
      <button type="button" class="vr-preset" data-shape="descending">Descending</button>
      <button type="button" class="vr-preset" data-shape="bell">Bell</button>
      <button type="button" class="vr-preset" data-shape="steps">Steps</button>
    </div>
    <div class="meta" id="vr-meta" style="font-size:10px;margin-top:6px"></div>
  </div>
  <div class="meta" id="resist-pipeline-summary" style="font-size:11px;margin:2px 0 6px;color:var(--accent)"></div>
  <!-- Cable OUT is always the concentric (lifting) phase for gym movements. -->
  <input type="hidden" id="cfg-condir" value="extend">
  <div class="field" data-modes="gym">
    <label>Eccentric weight (kg)</label>
    <input type="number" id="cfg-ecc-kg" value="5" step="0.5" min="0.5" max="20">
    <div class="meta" id="ecc-overload-note" style="font-size:10px;margin-top:4px"></div>
  </div>
  <div class="field" data-modes="gym">
    <label>Chains (kg/m) — 0 = off</label>
    <input type="number" id="cfg-chain" value="0" step="0.5" min="0" max="10">
  </div>
  <div class="field" data-modes="flywheel">
    <label>Virtual mass (kg)</label>
    <div class="stepper">
      <button type="button" data-step="-5" data-target="cfg-fw-mass" aria-label="Decrease">−</button>
      <input type="number" id="cfg-fw-mass" value="25" step="5" min="1" max="100" inputmode="numeric">
      <button type="button" data-step="5" data-target="cfg-fw-mass" aria-label="Increase">+</button>
    </div>
    <div class="meta" style="font-size:10px;margin-top:4px">Simulated flywheel inertia — heavier resists acceleration harder. Tune on the rig.</div>
  </div>
  <div class="field" data-modes="flywheel">
    <label>Eccentric overload (%)</label>
    <input type="number" id="cfg-fw-ecc-pct" value="0" step="5" min="0" max="100">
    <div class="meta" style="font-size:10px;margin-top:4px">Extra resistance on the cable-in (eccentric) phase. 0 = matched.</div>
  </div>
  <div class="field" data-modes="flywheel">
    <label>Viscous damping (kg per m/s)</label>
    <input type="number" id="cfg-fw-visc" value="0" step="0.5" min="0" max="10">
    <div class="meta" style="font-size:10px;margin-top:4px">Small speed-proportional drag — adds stability. 0 = off.</div>
  </div>

  <div style="height:1px;background:var(--line);margin:6px 0"></div>

  <div class="sheet-actions">
    <button id="sheet-done" style="flex:1">Done</button>
  </div>
</aside>

<script>
const stateTag=document.getElementById('state-tag');
const statSpeed=document.getElementById('stat-speed');
const statForce=document.getElementById('stat-force');
const statPower=document.getElementById('stat-power');
const statExt=document.getElementById('stat-ext');
const ssReps=document.getElementById('ss-reps');
const ssBestSpeed=document.getElementById('ss-best-speed');
const ssBestPower=document.getElementById('ss-best-power');
const ssWork=document.getElementById('ss-work');
const ssTimer=document.getElementById('ss-timer');
const repsList=document.getElementById('reps-list');
const liveChart=document.getElementById('live-chart');
const chartEmpty=document.getElementById('chart-empty');

let prevPhase='off';
let cachedSamples=[];   // last full curve to keep showing during 'recover'
let lastCorridor=0;     // current Recovery distance from athletic config (for chart threshold + corridor highlight)
let drillStartedAt=null; // ms timestamp when athletic_mode flipped True (for session timer)
let currentMode='resisted'; // active training-mode pill (resisted|assisted|cod|gym)
// Variable-resistance curve, mirrored from the Adjust panel template picker.
const vrState={axis:'off',pts:[100,100,100,100,100,100]};  // curve points, % of load

// Gym shows concentric + eccentric as plain kg inputs; the backend wants an
// overload percentage, so convert here: pct = (ecc-conc)/conc, clamped 0-100.
function eccOverloadPct(){
  const concKg = parseFloat(document.getElementById('cfg-resist').value)||0;
  const eccEl = document.getElementById('cfg-ecc-kg');
  const eccKg = eccEl ? (parseFloat(eccEl.value)||0) : 0;
  if(concKg <= 0) return 0;
  return Math.max(0, Math.min(100, ((eccKg-concKg)/concKg)*100));
}
function buildAthleticCfg(){
  // Live-screen fields only. Recovery / slew / rest / countdown are managed
  // on /setup and pushed straight to the backend config, so they're omitted
  // here — the backend keeps its last value for any key not sent.
  const vcapOn=document.getElementById('cfg-vcap-toggle').checked;
  return {
    mode:currentMode,
    resist_kg:parseFloat(document.getElementById('cfg-resist').value),
    resist_distance_m:parseFloat(document.getElementById('cfg-resist-dist').value),
    velocity_cap_mps:vcapOn?(parseFloat(document.getElementById('cfg-vcap').value)||0):0,
    chain_kg_per_m:parseFloat(document.getElementById('cfg-chain').value)||0,
    // Eccentric overload: gym derives it from the concentric/eccentric kg pair;
    // flywheel takes a direct percentage.
    eccentric_overload_pct:(currentMode==='flywheel')
      ?(parseFloat(document.getElementById('cfg-fw-ecc-pct').value)||0)
      :eccOverloadPct(),
    concentric_dir:document.getElementById('cfg-condir').value,
    virtual_mass_kg:parseFloat(document.getElementById('cfg-fw-mass').value)||25,
    viscous_damping:parseFloat(document.getElementById('cfg-fw-visc').value)||0,
    drill:document.getElementById('cfg-drill').value,
    curve_axis:vrState.axis,
    curve_pct:vrState.pts,
  };
}
async function armRig(){
  const r=await fetch('/api/c/arm',{method:'POST'});
  if(!r.ok){const j=await r.json().catch(()=>({}));alert("Couldn't start motor: "+(j.detail||JSON.stringify(j)));return;}
  await fetch('/api/c/athletic/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildAthleticCfg())});
  const aid=document.getElementById('athlete-select').value;
  await fetch('/api/c/athletic/start'+(aid?('?athlete_id='+encodeURIComponent(aid)):''),{method:'POST'});
}
async function repAction(){
  const c=await(await fetch('/api/c/state')).json();
  const url=(c.athletic_phase==='ready')?'/api/c/athletic/start_rep':'/api/c/athletic/stop_rep';
  const r=await fetch(url,{method:'POST'});
  if(!r.ok){const j=await r.json().catch(()=>({}));alert('Rep: '+(j.detail||JSON.stringify(j)));}
}
// Single phase-aware primary action: Arm rig → Start/Next rep → Stop → Reset.
// Powering the rig off is handled by the red E-stop button (safe + idempotent).
const primaryBtn=document.getElementById('primary-action');
primaryBtn.onclick=async()=>{
  primaryBtn.disabled=true;
  try{
    const c=await(await fetch('/api/c/state')).json();
    if(c.phase_c==='error'){ await fetch('/api/c/disarm',{method:'POST'}); await armRig(); }
    else if(c.phase_c!=='armed'){ await armRig(); }
    else{ await repAction(); }
  }catch(e){}
  primaryBtn.disabled=false;
};

// ---- Athlete chip (header) ----
const athleteSel=document.getElementById('athlete-select');
window._athleteMass={};
window._athleteGroup={};
function renderAthleteChip(){
  const opt=athleteSel.options[athleteSel.selectedIndex];
  const name=(athleteSel.value&&opt)?opt.textContent:'No athlete';
  document.getElementById('ath-chip-name').textContent=name;
  document.getElementById('ath-avatar').textContent=
    (athleteSel.value&&name)?name.trim().charAt(0).toUpperCase():'–';
  const m=athleteSel.value?window._athleteMass[athleteSel.value]:null;
  const g=athleteSel.value?window._athleteGroup[athleteSel.value]:null;
  document.getElementById('ath-chip-mass').textContent=
    [m?(m+' kg'):null, g||null].filter(Boolean).join(' · ');
}
document.getElementById('athlete-chip-btn').onclick=(e)=>{
  e.stopPropagation();
  const menu=document.getElementById('athlete-menu');
  if(!menu.hidden){ menu.hidden=true; return; }
  let h='';
  for(const o of athleteSel.options){
    if(!o.value) continue;
    h+='<button type="button" class="am-item'+(o.value===athleteSel.value?' active':'')+
       '" data-aid="'+o.value+'">'+o.textContent+'</button>';
  }
  h+='<a class="am-add" href="/setup">+ Add athlete</a>';
  menu.innerHTML=h;
  menu.querySelectorAll('.am-item').forEach(b=>b.onclick=()=>{
    athleteSel.value=b.getAttribute('data-aid');
    renderAthleteChip(); menu.hidden=true;
    if(typeof loadAthleteProfile==='function') loadAthleteProfile(athleteSel.value);
  });
  menu.hidden=false;
};
document.addEventListener('click',(e)=>{
  const chip=document.getElementById('athlete-chip');
  if(chip&&!chip.contains(e.target)) document.getElementById('athlete-menu').hidden=true;
});

// ---- Athlete profile card + auto-load advisor ----
window._athleteProfile=null;
function _pfNum(v,d){ return (v==null)?'–':Number(v).toFixed(d==null?1:d); }
async function loadAthleteProfile(aid){
  const card=document.getElementById('athlete-profile-card');
  if(!card) return;
  if(!aid){ card.hidden=true; window._athleteProfile=null; refreshSuggestedLoad(); return; }
  try{
    const r=await fetch('/api/athletes/'+encodeURIComponent(aid)+'/profile');
    if(!r.ok){ card.hidden=true; window._athleteProfile=null; refreshSuggestedLoad(); return; }
    const p=await r.json();
    window._athleteProfile=p;
    const prs=p.prs||{}, lv=p.lv_profile||{};
    document.getElementById('pc-stats').innerHTML=[
      ['Top speed', prs.pr_speed_mps!=null?_pfNum(prs.pr_speed_mps,2)+' m/s':'–'],
      ['Peak power', prs.pr_power_w!=null?_pfNum(prs.pr_power_w,0)+' W':'–'],
      ['Best 10 m', prs.pr_split_10m_s!=null?_pfNum(prs.pr_split_10m_s,2)+' s':'–'],
      ['Best 40 m', prs.pr_split_40m_s!=null?_pfNum(prs.pr_split_40m_s,2)+' s':'–'],
    ].map(s=>'<div class="pc-stat"><span class="pc-l">'+s[0]+'</span>'+
            '<span class="pc-v">'+s[1]+'</span></div>').join('');
    const meta=[];
    if(lv.orientation) meta.push('<span class="pc-orient">'+lv.orientation+'</span>');
    const bm=(p.athlete||{}).body_mass_kg;
    if(bm!=null) meta.push(bm+' kg');
    if(p.last_session_at) meta.push('Last '+String(p.last_session_at).split('T')[0]);
    meta.push((p.session_count||0)+' sessions');
    document.getElementById('pc-meta').innerHTML=meta.join(' · ');
    card.hidden=false;
    // Velocity-decrement auto-loading needs a sufficient L-V profile.
    const vopt=document.querySelector('#cfg-autoload option[value="vdec"]');
    if(vopt) vopt.disabled=(p.lv_profile_quality!=='ok');
  }catch(e){ card.hidden=true; }
  refreshSuggestedLoad();
}
async function refreshSuggestedLoad(){
  const out=document.getElementById('cfg-resist-suggested');
  const sel=document.getElementById('cfg-autoload');
  const toggle=document.getElementById('cfg-autoload-toggle');
  if(!out||!sel) return;
  const target=(toggle&&toggle.checked)?sel.value:'off';
  const aid=athleteSel.value;
  if(target==='off'||!aid){ out.textContent=''; out.classList.remove('has'); return; }
  const param=document.getElementById('cfg-autoload-param').value;
  const drill=(document.getElementById('cfg-drill')||{}).value||'';
  let u='/api/athletes/'+encodeURIComponent(aid)+'/load_suggestion?target='+
        encodeURIComponent(target)+'&drill='+encodeURIComponent(drill);
  if(param!=='') u+='&target_param='+encodeURIComponent(param);
  try{
    const r=await fetch(u);
    if(!r.ok){ out.textContent=''; out.classList.remove('has'); return; }
    const j=await r.json();
    if(j.suggestion!=null){
      document.getElementById('cfg-resist').value=j.suggestion;
      out.textContent='Suggested: '+j.suggestion+' kg — applied';
      out.classList.add('has');
    }else{
      out.textContent=j.reason?('No suggestion — '+j.reason):'No suggestion';
      out.classList.remove('has');
    }
  }catch(e){ out.textContent=''; out.classList.remove('has'); }
}
(function(){
  const sel=document.getElementById('cfg-autoload');
  const param=document.getElementById('cfg-autoload-param');
  const toggle=document.getElementById('cfg-autoload-toggle');
  function syncParam(){
    const needsParam=toggle&&toggle.checked&&(sel.value==='bodyweight_pct'||sel.value==='vdec');
    param.hidden=!needsParam;
    if(needsParam&&param.value===''){ param.value=(sel.value==='vdec')?10:30; }
  }
  window._syncAutoload=function(){
    sel.hidden=!(toggle&&toggle.checked);
    syncParam();
    refreshSuggestedLoad();
  };
  if(toggle) toggle.addEventListener('change',window._syncAutoload);
  if(sel) sel.addEventListener('change',()=>{ syncParam(); refreshSuggestedLoad(); });
  if(param) param.addEventListener('input',refreshSuggestedLoad);
})();

// ---- Adjust panel: ± steppers + velocity-cap on/off toggle ----
(function(){
  document.querySelectorAll('.sheet .stepper button[data-step]').forEach(b=>{
    b.addEventListener('click',()=>{
      const inp=document.getElementById(b.getAttribute('data-target'));
      if(!inp) return;
      const step=parseFloat(b.getAttribute('data-step'));
      const min=inp.min!==''?parseFloat(inp.min):-Infinity;
      const max=inp.max!==''?parseFloat(inp.max):Infinity;
      let v=(parseFloat(inp.value)||0)+step;
      v=Math.max(min,Math.min(max,Math.round(v*100)/100));
      inp.value=v;
      inp.dispatchEvent(new Event('input',{bubbles:true}));
      inp.dispatchEvent(new Event('change',{bubbles:true}));
    });
  });
  // Few bullet-graph — live load vs the 53 kg hard cap
  window.updateLoadBullet=function(){
    var inp=document.getElementById('cfg-resist'); if(!inp) return;
    var v=parseFloat(inp.value)||0, pct=Math.max(0,Math.min(100,v/53*100));
    var f=document.getElementById('lb-fill'); if(f) f.style.width=pct.toFixed(1)+'%';
    var l=document.getElementById('lb-val'); if(l) l.textContent=(Math.round(v*10)/10)+' kg';
  };
  var _lbInp=document.getElementById('cfg-resist');
  if(_lbInp){ _lbInp.addEventListener('input',window.updateLoadBullet);
              _lbInp.addEventListener('change',window.updateLoadBullet); }
  window.updateLoadBullet();
  const vcapToggle=document.getElementById('cfg-vcap-toggle');
  const vcapStepper=document.getElementById('cfg-vcap-stepper');
  const vcapConflict=document.getElementById('cfg-vcap-conflict');
  window._syncVcap=function(){
    // Mutual exclusion: a velocity-axis variable-resistance curve already
    // shapes load by cable speed. A velocity cap is a second speed-driven
    // controller — run both and they fight. Velocity curve wins: the cap is
    // forced off and locked out (and cleared on the backend if it was on,
    // so a live session actually stops capping).
    const velCurve=(vrState.axis==='velocity');
    if(vcapToggle){
      if(velCurve){
        if(vcapToggle.checked){
          vcapToggle.checked=false;
          fetch('/api/c/athletic/config',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({velocity_cap_mps:0})}).catch(()=>{});
        }
        vcapToggle.disabled=true;
      }else{
        vcapToggle.disabled=false;
      }
    }
    const on=vcapToggle&&vcapToggle.checked;
    if(vcapStepper) vcapStepper.classList.toggle('off',!on);
    if(vcapConflict) vcapConflict.hidden=!velCurve;
    if(typeof updateResistSummary==='function') updateResistSummary();
  };
  if(vcapToggle) vcapToggle.addEventListener('change',window._syncVcap);
  const vcapInput=document.getElementById('cfg-vcap');
  if(vcapInput) vcapInput.addEventListener('input',function(){
    if(typeof updateResistSummary==='function') updateResistSummary();
  });
  window._syncVcap();
})();

// ---- Resistance pipeline summary (Adjust panel) ----
// One-line readout of how working resistance, the variable-resistance curve
// and the velocity cap compose into the commanded load — keeps the layering
// visible so it never feels like a black box to the coach.
function updateResistSummary(){
  const el=document.getElementById('resist-pipeline-summary');
  if(!el) return;
  if(currentMode==='gym'||currentMode==='flywheel'){ el.hidden=true; return; }
  el.hidden=false;
  const kg=parseFloat(document.getElementById('cfg-resist').value)||0;
  const noun=(currentMode==='assisted')?'Assist':'Load';
  const chip=document.querySelector('#vr-presets .vr-preset.active');
  const curveName=(chip&&chip.getAttribute('data-id')!=='off')?chip.textContent.trim():'';
  const vcapOn=document.getElementById('cfg-vcap-toggle').checked;
  const vcap=parseFloat(document.getElementById('cfg-vcap').value)||0;
  let s=noun+' = '+kg+' kg';
  if(curveName&&vrState.axis!=='off'){
    s+=' × '+curveName+' curve'+(vrState.axis==='velocity'?' (by speed)':'');
  }else{
    s+=' flat';
  }
  if(vcapOn&&vcap>0) s+=' · capped at '+vcap+' m/s';
  el.textContent=s+'.';
}
window.updateResistSummary=updateResistSummary;
(function(){
  const ri=document.getElementById('cfg-resist');
  if(ri) ri.addEventListener('input',updateResistSummary);
})();

// ---- Variable resistance — draggable curve editor + axis toggle (Adjust panel, mockup) ----
(function(){
  const svg=document.getElementById('vr-preview');
  const axisWrap=document.getElementById('vr-axis');
  const presetWrap=document.getElementById('vr-presets');
  const metaEl=document.getElementById('vr-meta');
  if(!svg||!axisWrap) return;
  const N=6, MAXPCT=300;
  const SHAPES={flat:[100,100,100,100,100,100],
                ascending:[50,70,90,115,145,175],
                descending:[175,145,115,90,70,50],
                bell:[60,100,150,150,100,60],
                steps:[60,60,110,110,160,160]};
  const VW=300,VH=110,PL=8,PR=8,PT=10,PB=10,PW=VW-PL-PR,PH=VH-PT-PB;
  const xOf=i=>PL+(i/(N-1))*PW;
  const yOf=v=>PT+(1-Math.max(0,Math.min(MAXPCT,v))/MAXPCT)*PH;
  function cssv(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
  function render(){
    const off=(vrState.axis==='off');
    const col=off?'#4a5578':cssv('--state');
    let poly='';
    for(let i=0;i<N;i++) poly+=xOf(i).toFixed(1)+','+yOf(vrState.pts[i]).toFixed(1)+' ';
    let s='<polyline points="'+poly+'" fill="none" stroke="'+col+'" stroke-width="2.5" stroke-linejoin="round"'+(off?' stroke-dasharray="4 3"':'')+'/>';
    for(let j=0;j<N;j++) s+='<circle cx="'+xOf(j).toFixed(1)+'" cy="'+yOf(vrState.pts[j]).toFixed(1)+'" r="5" fill="'+cssv('--card')+'" stroke="'+col+'" stroke-width="2.5"/>';
    svg.innerHTML=s;
    svg.style.opacity=off?0.5:1;
    axisWrap.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.getAttribute('data-axis')===vrState.axis));
    if(metaEl) metaEl.textContent=off?'Flat working resistance — same load the whole rep.'
      :('Load scales 0–300% of working load across '+(vrState.axis==='velocity'?'cable velocity.':'the resist distance.')+' Drag the 6 points.');
  }
  function post(){
    fetch('/api/c/athletic/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({curve_axis:vrState.axis,curve_pct:vrState.pts})}).catch(()=>{});
    if(window._syncVcap) window._syncVcap();
  }
  let dragIdx=-1;
  function evVal(e){
    const r=svg.getBoundingClientRect();
    const nx=(e.clientX-r.left)/r.width, ny=(e.clientY-r.top)/r.height;
    const idx=Math.max(0,Math.min(N-1,Math.round(nx*(N-1))));
    let v=(1-((ny*VH)-PT)/PH)*MAXPCT;
    v=Math.max(0,Math.min(MAXPCT,Math.round(v/5)*5));
    return {idx:idx,v:v};
  }
  svg.addEventListener('pointerdown',e=>{const p=evVal(e);dragIdx=p.idx;
    try{svg.setPointerCapture(e.pointerId);}catch(_){}
    if(vrState.axis==='off') vrState.axis='distance';
    vrState.pts[dragIdx]=p.v; render();});
  svg.addEventListener('pointermove',e=>{if(dragIdx<0)return;const p=evVal(e);vrState.pts[dragIdx]=p.v;render();});
  svg.addEventListener('pointerup',()=>{if(dragIdx>=0){dragIdx=-1;post();}});
  svg.addEventListener('pointercancel',()=>{dragIdx=-1;});
  axisWrap.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    vrState.axis=b.getAttribute('data-axis'); render(); post();}));
  if(presetWrap) presetWrap.querySelectorAll('.vr-preset').forEach(b=>b.addEventListener('click',()=>{
    const sh=SHAPES[b.getAttribute('data-shape')]; if(!sh) return;
    vrState.pts=sh.slice(); if(vrState.axis==='off') vrState.axis='distance'; render(); post();}));
  fetch('/api/c/state').then(r=>r.json()).then(st=>{
    const cfg=st.config||{};
    if(cfg.curve_pct&&cfg.curve_pct.length===N) vrState.pts=cfg.curve_pct.slice();
    if(cfg.curve_axis) vrState.axis=cfg.curve_axis;
    render();
  }).catch(()=>render());
})();

// ---- Gear toggle (header) ----
let currentGear=1;
const gearToggle=document.getElementById('gear-toggle');
function setGear(g){
  currentGear=(g===2||g==='2')?2:1;
  gearToggle.textContent='Gear '+currentGear;
  gearToggle.classList.toggle('g2',currentGear===2);
  document.getElementById('gear-warn-chip').style.display=currentGear===2?'inline':'none';
}
gearToggle.onclick=()=>{
  setGear(currentGear===1?2:1);
  fetch('/api/c/athletic/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gear:currentGear})}).catch(()=>{});
};
setGear(1);

// ============== UNITS TOGGLE (§15.2) ==============
// Pure-client preference. Conversions applied at render time only — exports
// stay metric. Persisted in localStorage.
function unitMode(){ return localStorage.getItem('ppa.units') || 'metric'; }
function setUnitMode(m){ localStorage.setItem('ppa.units', m); }
const M_TO_FT  = 3.28084;
const KG_TO_LB = 2.20462;
const MPS_TO_MPH = 2.23694;
function convertUnit(value, unit){
  if(value == null || isNaN(value)) return [value, unit];
  if(unitMode() !== 'imperial') return [value, unit];
  switch(unit){
    case 'm':    return [value * M_TO_FT,  'ft'];
    case 'kg':   return [value * KG_TO_LB, 'lb'];
    case 'm/s':  return [value * MPS_TO_MPH, 'mph'];
    case 'N':    return [value * 0.224809, 'lbf'];
    default:     return [value, unit];
  }
}

// Session-config templates are managed on /setup (Drills & Presets tab).

// ============== AUDIO ENGINE (§8) ==============
// Web Audio API tone generator. No asset files — synthesised tones so the
// service stays single-file. Master + per-event mutes persisted in localStorage.
let _audioCtx = null;
function audioCtx(){
  if(!_audioCtx){
    try{ _audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    catch(e){ _audioCtx = null; }
  }
  // Browser autoplay policy: resume on first user gesture
  if(_audioCtx && _audioCtx.state === 'suspended') _audioCtx.resume();
  return _audioCtx;
}
function audioPrefs(){
  return {
    master: localStorage.getItem('ppa.audio.master') !== '0',
    rep:    localStorage.getItem('ppa.audio.rep')    !== '0',
    rest:   localStorage.getItem('ppa.audio.rest')   !== '0',
  };
}
function tone(freq, durMs, type='sine', gain=0.18){
  const prefs = audioPrefs();
  if(!prefs.master) return;
  const ctx = audioCtx();
  if(!ctx) return;
  const o = ctx.createOscillator();
  const g = ctx.createGain();
  o.type = type; o.frequency.value = freq;
  g.gain.value = 0;
  o.connect(g); g.connect(ctx.destination);
  const t = ctx.currentTime;
  g.gain.setValueAtTime(0, t);
  g.gain.linearRampToValueAtTime(gain, t + 0.01);
  g.gain.linearRampToValueAtTime(0, t + durMs/1000);
  o.start(t); o.stop(t + durMs/1000 + 0.05);
}
const SOUNDS = {
  countdown_tick: ()=> tone(660, 120, 'sine', 0.15),
  countdown_go:   ()=> { tone(880, 250, 'sine', 0.22); setTimeout(()=>tone(1320, 200,'sine',0.18), 60); },
  rep_start:      ()=> { if(audioPrefs().rep) tone(880, 120, 'sine', 0.16); },
  rep_end:        ()=> { if(audioPrefs().rep) { tone(660,90,'sine',0.14); setTimeout(()=>tone(880,140,'sine',0.16), 90); } },
  rest_warn:      ()=> { if(audioPrefs().rest) { tone(523,180,'sine',0.16); setTimeout(()=>tone(523,180,'sine',0.16), 220); } },
  rest_ready:     ()=> { if(audioPrefs().rest) { tone(880,200,'sine',0.18); setTimeout(()=>tone(1320,250,'sine',0.20), 200); } },
  estop:          ()=> { tone(220, 180, 'sawtooth', 0.22); setTimeout(()=>tone(180,200,'sawtooth',0.22), 200); setTimeout(()=>tone(220,200,'sawtooth',0.22), 420); },
};

// Wire audio prefs <-> Settings checkboxes
function loadAudioPrefs(){
  const m = document.getElementById('audio-master');
  const r = document.getElementById('audio-rep');
  const t = document.getElementById('audio-rest');
  if(m) m.checked = localStorage.getItem('ppa.audio.master') !== '0';
  if(r) r.checked = localStorage.getItem('ppa.audio.rep')    !== '0';
  if(t) t.checked = localStorage.getItem('ppa.audio.rest')   !== '0';
  if(m) m.onchange = ()=> localStorage.setItem('ppa.audio.master', m.checked ? '1' : '0');
  if(r) r.onchange = ()=> localStorage.setItem('ppa.audio.rep',    r.checked ? '1' : '0');
  if(t) t.onchange = ()=> localStorage.setItem('ppa.audio.rest',   t.checked ? '1' : '0');
}
loadAudioPrefs();

// ============== SOLO COUNTDOWN (§9) ==============
async function runCountdown(seconds){
  const ov = document.getElementById('cd-overlay');
  const num = document.getElementById('cd-num');
  if(seconds <= 0){ return; }
  ov.classList.add('show');
  num.classList.remove('go');
  for(let i = seconds; i >= 1; i--){
    num.textContent = i;
    SOUNDS.countdown_tick();
    await new Promise(r => setTimeout(r, 1000));
  }
  num.classList.add('go');
  num.textContent = 'GO';
  SOUNDS.countdown_go();
  await new Promise(r => setTimeout(r, 600));
  ov.classList.remove('show');
}

// ============== INTER-REP REST TIMER (§12) ==============
let restTimerInterval = null;
let restRemainingS = 0;
let restWarnedAt30 = false;
function startRestTimer(seconds){
  stopRestTimer();
  if(seconds <= 0) return;
  restRemainingS = seconds;
  restWarnedAt30 = false;
  const el = document.getElementById('rest-timer');
  el.classList.add('show');
  el.classList.remove('warn');
  paintRestTimer();
  restTimerInterval = setInterval(()=>{
    restRemainingS--;
    paintRestTimer();
    if(restRemainingS === 30 && !restWarnedAt30){
      SOUNDS.rest_warn();
      restWarnedAt30 = true;
      el.classList.add('warn');
    }
    if(restRemainingS <= 0){
      SOUNDS.rest_ready();
      stopRestTimer();
    }
  }, 1000);
}
function stopRestTimer(){
  if(restTimerInterval){ clearInterval(restTimerInterval); restTimerInterval = null; }
  document.getElementById('rest-timer').classList.remove('show');
}
function paintRestTimer(){
  const m = Math.max(0, Math.floor(restRemainingS / 60));
  const s = Math.max(0, restRemainingS % 60);
  document.getElementById('rt-num').textContent = m + ':' + (s < 10 ? '0'+s : s);
}
document.getElementById('rt-add').onclick = ()=> { restRemainingS += 30; paintRestTimer(); };
document.getElementById('rt-skip').onclick = ()=> stopRestTimer();

// Trigger rest timer on rep-end (resist→return phase transition is the rep-end event).
// Hook is in the main refresh() loop where we already track prevPhase.

// ============== TAP-TO-CYCLE METRIC TILES (§16.7) ==============
// Each tile cycles through a list of (metric_id, label, formatter) tuples.
const TILE_METRICS = {
  speed: [
    {id: 'live_speed',  label: 'Speed',           u: 'm/s', big: true},
    {id: 'peak_speed',  label: 'Peak speed',      u: 'm/s', big: true},
    {id: 'avg_speed',   label: 'Avg speed (rep)', u: 'm/s', big: true},
  ],
  force: [
    {id: 'live_force',  label: 'Force',           u: 'N'},
    {id: 'peak_force',  label: 'Peak force',      u: 'N'},
    {id: 'avg_force',   label: 'Avg force (rep)', u: 'N'},
  ],
  power: [
    {id: 'live_power',  label: 'Power',           u: 'W'},
    {id: 'peak_power',  label: 'Peak power',      u: 'W'},
    {id: 'avg_power',   label: 'Avg power (rep)', u: 'W'},
  ],
  ext: [
    {id: 'cable_out',   label: 'Cable out',       u: 'm'},
    {id: 'max_ext',     label: 'Max distance (rep)', u: 'm'},
    {id: 'duration',    label: 'Duration (rep)',  u: 's'},
  ],
};
// User's per-tile choice (idx into TILE_METRICS list), persisted
function tileIdx(tile){
  const v = parseInt(localStorage.getItem('ppa.tile.'+tile) || '0', 10);
  return isNaN(v) ? 0 : v;
}
function setTileIdx(tile, i){ localStorage.setItem('ppa.tile.'+tile, String(i)); }
function currentTileMetric(tile){ return TILE_METRICS[tile][tileIdx(tile) % TILE_METRICS[tile].length]; }
document.querySelectorAll('.stat-line.tappable').forEach(el => {
  el.addEventListener('click', ()=>{
    const tile = el.getAttribute('data-tile');
    const list = TILE_METRICS[tile] || [];
    setTileIdx(tile, (tileIdx(tile) + 1) % list.length);
    // Update the label immediately for responsiveness; value updates next refresh tick
    const m = currentTileMetric(tile);
    const lbl = document.getElementById('stat-'+tile+'-l');
    if(lbl) lbl.textContent = m.label;
    if(tile === 'speed') syncSpeedToggle();
  });
});
// Initialise labels from stored prefs
['speed','force','power','ext'].forEach(t => {
  const lbl = document.getElementById('stat-'+t+'-l');
  if(lbl) lbl.textContent = currentTileMetric(t).label;
});

// Speed metric Current/Peak/Avg toggle — drives the same per-tile index as
// the tap-cycle so both controls stay in sync. Auto-switches to Peak when a
// rep ends and back to Current when the next rep starts (see refresh loop).
function syncSpeedToggle(){
  const idx = tileIdx('speed') % TILE_METRICS.speed.length;
  document.querySelectorAll('#speed-toggle .sp-tog').forEach(b=>{
    const on = parseInt(b.getAttribute('data-idx'),10) === idx;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}
function setSpeedMetric(idx){
  setTileIdx('speed', idx);
  syncSpeedToggle();
  const lbl = document.getElementById('stat-speed-l');
  if(lbl) lbl.textContent = currentTileMetric('speed').label;
}
document.querySelectorAll('#speed-toggle .sp-tog').forEach(b=>{
  b.addEventListener('click', ()=> setSpeedMetric(parseInt(b.getAttribute('data-idx'),10)));
});
syncSpeedToggle();

// Pre-drill confirmation card state
let preRepSkip = false;          // session-scoped opt-out (§7: always shows on first rep)
let preRepFirstShownThisSession = false;
let preRepLastConfig = null;     // detect parameter changes -> always show

function configChanged(a, b){
  if(!a || !b) return true;
  return ['resist_kg','resist_distance_m','velocity_cap_mps','chain_kg_per_m','eccentric_overload_pct','concentric_dir','virtual_mass_kg','viscous_damping','return_kg','return_distance_m','return_speed_mps','slew_kg_per_s','drill','athlete_id']
    .some(k => a[k] !== b[k]);
}

function buildPreRepGrid(cfg, athleteName){
  let rows;
  if(cfg.mode === 'flywheel'){
    rows = [
      ['Mode',     'Flywheel'],
      ['Athlete',  athleteName || '—'],
      ['Exercise', cfg.drill],
      ['Virtual mass',        (cfg.virtual_mass_kg!=null?cfg.virtual_mass_kg:25) + ' kg'],
      ['Eccentric overload',  (cfg.eccentric_overload_pct||0) + ' %'],
      ['Viscous damping',     (cfg.viscous_damping||0) + ' kg per m/s'],
      ['Ease-in speed',       cfg.slew_kg_per_s + ' kg/s'],
    ];
  }else{
    rows = [
      ['Mode',     'Sprint Mode'],
      ['Athlete',  athleteName || '—'],
      ['Exercise', cfg.drill],
      ['Working resistance', cfg.resist_kg + ' kg'],
      ['Resist distance',     cfg.resist_distance_m + ' m'],
      ['Recovery resistance', cfg.return_kg + ' kg'],
      ['Recovery distance',   cfg.return_distance_m + ' m'],
      ['Return speed',        (cfg.return_speed_mps!=null?cfg.return_speed_mps:2) + ' m/s'],
      ['Ease-in speed',       cfg.slew_kg_per_s + ' kg/s'],
    ];
  }
  return rows.map(([l,v]) => '<div class="l">'+l+'</div><div class="v">'+v+'</div>').join('');
}

function showPreRepCard(cfg, athleteName, onGo){
  document.getElementById('prerep-grid').innerHTML = buildPreRepGrid(cfg, athleteName);
  document.getElementById('prerep-card').classList.add('show');
  document.getElementById('prerep-mask').classList.add('show');
  const goBtn = document.getElementById('prerep-go');
  const editBtn = document.getElementById('prerep-edit');
  const skipCb = document.getElementById('prerep-skip-cb');
  skipCb.checked = preRepSkip;
  const close = ()=>{
    document.getElementById('prerep-card').classList.remove('show');
    document.getElementById('prerep-mask').classList.remove('show');
  };
  goBtn.onclick = async ()=>{
    preRepSkip = skipCb.checked;
    close();
    await onGo();
  };
  editBtn.onclick = ()=>{
    close();
    openSheet();   // settings sheet → coach can edit
  };
}

// Universal E-stop (§2.5) — fires immediately, no confirmation
const estopBtn = document.getElementById('estop-btn');
if(estopBtn){
  estopBtn.onclick = async ()=>{
    estopBtn.classList.add('firing');
    setTimeout(()=>estopBtn.classList.remove('firing'), 1500);
    try{ await fetch('/api/c/estop',{method:'POST'}); }
    catch(e){ /* even if network fails, the local UI animation still gives feedback */ }
  };
}

// ---- Settings sheet ----
// Open by default on desktop, closed on phone; the open/closed choice is
// remembered in localStorage. The dark mask only shows when the sheet
// overlays content (phone) — on desktop the sheet has its own column.
const sheet=document.getElementById('sheet');
const sheetMask=document.getElementById('sheet-mask');
const SHEET_DESKTOP_MIN=1100;
function sheetIsDesktop(){ return window.innerWidth >= SHEET_DESKTOP_MIN; }
function applySheet(open){
  sheet.classList.toggle('show', open);
  document.body.classList.toggle('sheet-open', open);
  sheetMask.classList.toggle('show', open && !sheetIsDesktop());
  localStorage.setItem('ppa.sheet.open', open ? '1' : '0');
}
function openSheet(){ applySheet(true); }
function closeSheet(){ applySheet(false); }
function toggleSheet(){ applySheet(!sheet.classList.contains('show')); }
(function(){
  const stored=localStorage.getItem('ppa.sheet.open');
  const open = stored===null ? sheetIsDesktop() : stored==='1';
  sheet.style.transition='none';   // no slide-in on first paint
  applySheet(open);
  requestAnimationFrame(()=>{ sheet.style.transition=''; });
})();
document.getElementById('adjust-btn').onclick=toggleSheet;
document.getElementById('sheet-close').onclick=closeSheet;
document.getElementById('sheet-done').onclick=closeSheet;
sheetMask.onclick=closeSheet;

// ---- Athletes ----
async function loadAthletes(){
  try{
    const r=await fetch('/api/athletes');
    if(!r.ok){return;}
    const list=await r.json();
    const sel=document.getElementById('athlete-select');
    const cur=sel.value;
    sel.innerHTML='<option value="">— Athlete —</option>'+
      list.map(a=>'<option value="'+a.id+'"'+(String(a.id)===cur?' selected':'')+'>'+a.name+'</option>').join('');
    window._athleteMass={}; window._athleteGroup={};
    list.forEach(a=>{
      if(a.body_mass_kg!=null) window._athleteMass[String(a.id)]=a.body_mass_kg;
      if(a.squad_group) window._athleteGroup[String(a.id)]=a.squad_group;
    });
    if(typeof renderAthleteChip==='function') renderAthleteChip();
    if(athleteSel.value && typeof loadAthleteProfile==='function')
      loadAthleteProfile(athleteSel.value);
  }catch(e){}
}
// Adding athletes is done on /setup (Athletes tab).

// ---- Compare reps ----
function populateRepSelects(reps){
  const opts='<option value="">Rep…</option>'+reps.map(r=>'<option value="'+r.rep_idx+'">Rep '+r.rep_idx+' · '+(r.peak_speed_mps||0).toFixed(2)+' m/s · '+(r.peak_force_n||0).toFixed(0)+' N</option>').join('');
  for(const id of ['cmp-a','cmp-b']){
    const sel=document.getElementById(id);
    const cur=sel.value;
    sel.innerHTML=opts;
    if(cur)sel.value=cur;
  }
}
document.getElementById('cmp-go').onclick=async()=>{
  const a=document.getElementById('cmp-a').value;
  const b=document.getElementById('cmp-b').value;
  if(!a||!b){alert('Pick two reps to compare');return;}
  const [ja,jb]=await Promise.all([
    fetch('/api/c/athletic/rep/'+a+'/samples').then(r=>r.json()),
    fetch('/api/c/athletic/rep/'+b+'/samples').then(r=>r.json()),
  ]);
  renderCompare(ja.samples||[],jb.samples||[],a,b);
};
function renderCompare(sa,sb,la,lb){
  const svg=document.getElementById('cmp-chart');
  if(!sa.length||!sb.length){svg.innerHTML='<text x="300" y="70" fill="#5a5a64" text-anchor="middle">No data for one of the reps</text>';return;}
  const W=600,H=140,PAD=8;
  const all=sa.concat(sb);
  const tMax=Math.max(...all.map(s=>s.t_ms))||1;
  const vMax=Math.max(...all.map(s=>s.v_mps),0.1);
  const fMax=Math.max(...all.map(s=>s.F_N),1);
  const xs=t=>PAD+(t/tMax)*(W-2*PAD);
  const yV=v=>H-PAD-(v/vMax)*(H-2*PAD);
  const yF=f=>H-PAD-(f/fMax)*(H-2*PAD);
  function path(samples,getter){return samples.map((s,i)=>(i?'L':'M')+xs(s.t_ms).toFixed(1)+','+getter(s).toFixed(1)).join(' ');}
  svg.innerHTML=
    '<path d="'+path(sa,s=>yV(s.v_mps))+'" stroke="#5b8bff" stroke-width="2" fill="none"/>'+
    '<path d="'+path(sb,s=>yV(s.v_mps))+'" stroke="#5b8bff" stroke-width="2" stroke-dasharray="4,3" fill="none"/>'+
    '<path d="'+path(sa,s=>yF(s.F_N))+'" stroke="#58a6ff" stroke-width="2" fill="none"/>'+
    '<path d="'+path(sb,s=>yF(s.F_N))+'" stroke="#58a6ff" stroke-width="2" stroke-dasharray="4,3" fill="none"/>'+
    '<text x="6" y="14" fill="#5b8bff" font-size="10">speed · Rep '+la+' solid · Rep '+lb+' dashed</text>'+
    '<text x="6" y="26" fill="#58a6ff" font-size="10">force · same lines</text>'+
    '<text x="'+(W-6)+'" y="'+(H-4)+'" fill="#5a5a64" font-size="10" text-anchor="end">'+(tMax/1000).toFixed(1)+' s</text>';
}

// ---- FV report ----
document.getElementById('fv-go').onclick=async()=>{
  const j=await(await fetch('/api/c/athletic/fv')).json();
  const out=document.getElementById('fv-result');
  if(!j.ok){
    out.innerHTML='<span class="warn">'+j.reason+'</span>';
    document.getElementById('fv-chart').innerHTML='';
    return;
  }
  out.innerHTML='F0 = <b>'+j.F0_n+' N</b> · V0 = <b>'+j.V0_mps+' m/s</b> · Pmax = <b>'+j.Pmax_w+' W</b> · slope '+j.slope+' · R² '+j.r2+' · n='+j.n;
  renderFV(j);
};
function renderFV(j){
  const svg=document.getElementById('fv-chart');
  const W=600,H=200,PAD=24;
  const pts=j.points;
  const vMax=Math.max(j.V0_mps||0,...pts.map(p=>p.v))*1.1||1;
  const fMax=Math.max(j.F0_n||0,...pts.map(p=>p.F))*1.1||1;
  const xs=v=>PAD+(v/vMax)*(W-2*PAD);
  const ys=f=>H-PAD-(f/fMax)*(H-2*PAD);
  const line='M'+xs(0).toFixed(1)+','+ys(j.F0_n).toFixed(1)+' L'+xs(j.V0_mps).toFixed(1)+','+ys(0).toFixed(1);
  const dots=pts.map(p=>'<circle cx="'+xs(p.v).toFixed(1)+'" cy="'+ys(p.F).toFixed(1)+'" r="4" fill="#5b8bff"/>').join('');
  svg.innerHTML=
    '<line x1="'+PAD+'" y1="'+(H-PAD)+'" x2="'+(W-PAD)+'" y2="'+(H-PAD)+'" stroke="#3a3a44"/>'+
    '<line x1="'+PAD+'" y1="'+PAD+'" x2="'+PAD+'" y2="'+(H-PAD)+'" stroke="#3a3a44"/>'+
    '<path d="'+line+'" stroke="#58a6ff" stroke-width="2" fill="none"/>'+
    dots+
    '<text x="'+PAD+'" y="14" fill="#5a5a64" font-size="10">F (N)</text>'+
    '<text x="'+(W-PAD)+'" y="'+(H-6)+'" fill="#5a5a64" font-size="10" text-anchor="end">v (m/s)</text>';
}

// ---- Live chart ----
function renderLiveChart(samples, opts){
  opts = opts || {};
  if(!samples||samples.length<2){
    chartEmpty.style.display='block'; liveChart.style.display='none'; return;
  }
  chartEmpty.style.display='none'; liveChart.style.display='block';
  const W=600,H=240,PADL=42,PADR=42,PADT=22,PADB=26;
  const tMax=Math.max(...samples.map(s=>s.t_ms))||1;
  const vMax=Math.max(...samples.map(s=>s.v_mps),0.1);
  const fMax=Math.max(...samples.map(s=>s.F_N),1);
  // Round axis maxima up to a "nice" value for cleaner ticks
  const niceMax=v=>{const e=Math.pow(10,Math.floor(Math.log10(v)));const m=v/e;return e*(m<=1?1:m<=2?2:m<=5?5:10);};
  const vAxMax=niceMax(vMax*1.1);
  const fAxMax=niceMax(fMax*1.1);
  const tAxMax=tMax/1000;
  const xs=t=>PADL+(t/tMax)*(W-PADL-PADR);
  const yV=v=>H-PADB-(v/vAxMax)*(H-PADT-PADB);
  const yF=f=>H-PADB-(f/fAxMax)*(H-PADT-PADB);

  // Gridlines + ticks (5 horizontal divisions)
  let grid='';
  for(let i=0;i<=4;i++){
    const y=PADT+i*(H-PADT-PADB)/4;
    const v=vAxMax*(1-i/4);
    const f=fAxMax*(1-i/4);
    grid+='<line x1="'+PADL+'" y1="'+y+'" x2="'+(W-PADR)+'" y2="'+y+'" stroke="#1f1f26" stroke-width="1"/>';
    grid+='<text x="'+(PADL-6)+'" y="'+(y+3)+'" fill="#5b8bff" font-size="10" text-anchor="end" opacity="0.7">'+v.toFixed(v<10?1:0)+'</text>';
    grid+='<text x="'+(W-PADR+6)+'" y="'+(y+3)+'" fill="#58a6ff" font-size="10" opacity="0.7">'+f.toFixed(0)+'</text>';
  }
  // X ticks (4 divisions)
  for(let i=0;i<=4;i++){
    const x=PADL+i*(W-PADL-PADR)/4;
    const t=tAxMax*(i/4);
    grid+='<line x1="'+x+'" y1="'+(H-PADB)+'" x2="'+x+'" y2="'+(H-PADB+4)+'" stroke="#3a3a44" stroke-width="1"/>';
    grid+='<text x="'+x+'" y="'+(H-PADB+15)+'" fill="#5a5a64" font-size="10" text-anchor="middle">'+t.toFixed(1)+'s</text>';
  }
  // Axis labels
  grid+='<text x="'+(PADL-30)+'" y="'+(PADT-8)+'" fill="#5b8bff" font-size="10" font-weight="600">m/s</text>';
  grid+='<text x="'+(W-PADR+10)+'" y="'+(PADT-8)+'" fill="#58a6ff" font-size="10" font-weight="600">N</text>';

  // Phase-coloured velocity polyline: orange while above corridor (engaged), muted otherwise
  const corridorM = (opts.corridor_m != null) ? opts.corridor_m : 0;
  function segColor(s){ return (s.pos_m != null && s.pos_m >= corridorM && corridorM > 0) ? '#5b8bff' : '#8a6d4a'; }
  let velSegments='';
  for(let i=1;i<samples.length;i++){
    const a=samples[i-1], b=samples[i];
    const c=segColor(b);
    velSegments+='<line x1="'+xs(a.t_ms).toFixed(1)+'" y1="'+yV(a.v_mps).toFixed(1)+'" x2="'+xs(b.t_ms).toFixed(1)+'" y2="'+yV(b.v_mps).toFixed(1)+'" stroke="'+c+'" stroke-width="2" stroke-linecap="round"/>';
  }
  const pathF=samples.map((s,i)=>(i?'L':'M')+xs(s.t_ms).toFixed(1)+','+yF(s.F_N).toFixed(1)).join(' ');

  // Peak markers (small dots at the peak of each curve)
  const peakV=samples.reduce((a,b)=>b.v_mps>a.v_mps?b:a, samples[0]);
  const peakF=samples.reduce((a,b)=>b.F_N>a.F_N?b:a, samples[0]);

  liveChart.innerHTML=
    grid+
    '<path d="'+pathF+'" stroke="#58a6ff" stroke-width="1.8" fill="none" opacity="0.9"/>'+
    velSegments+
    '<circle cx="'+xs(peakV.t_ms).toFixed(1)+'" cy="'+yV(peakV.v_mps).toFixed(1)+'" r="3.5" fill="#5b8bff"/>'+
    '<circle cx="'+xs(peakF.t_ms).toFixed(1)+'" cy="'+yF(peakF.F_N).toFixed(1)+'" r="3.5" fill="#58a6ff"/>'+
    '<text x="'+(xs(peakV.t_ms)+8).toFixed(1)+'" y="'+(yV(peakV.v_mps)-4).toFixed(1)+'" fill="#5b8bff" font-size="10" font-weight="600">'+peakV.v_mps.toFixed(2)+' m/s</text>'+
    '<text x="'+(xs(peakF.t_ms)+8).toFixed(1)+'" y="'+(yF(peakF.F_N)-4).toFixed(1)+'" fill="#58a6ff" font-size="10" font-weight="600">'+peakF.F_N.toFixed(0)+' N</text>';
}
function hideChart(){chartEmpty.style.display='block'; liveChart.style.display='none';}

function fmtSec(v){return v==null?'–':(v.toFixed(2)+' s');}
function fmtM(v){return v==null?'–':(v.toFixed(2)+' m');}

let currentRep = null;
let activeTab = 'profile';

function renderRunDetail(rep){
  const card=document.getElementById('run-detail');
  if(!rep){ card.style.display='none'; return; }
  card.style.display='block';
  currentRep = rep;
  document.getElementById('rd-rep-label').textContent='Rep '+(rep.rep_idx||'?');

  // Profile tab fields
  document.getElementById('rd-peak-v').textContent=(rep.peak_speed_mps||0).toFixed(2)+' m/s';
  document.getElementById('rd-ttv').textContent=fmtSec(rep.time_to_max_v_s);
  document.getElementById('rd-dtv').textContent=fmtM(rep.dist_to_max_v_m);
  document.getElementById('rd-t90').textContent=fmtSec(rep.time_to_90pct_v_s);
  document.getElementById('rd-d90').textContent=fmtM(rep.dist_to_90pct_v_m);
  document.getElementById('rd-drop').textContent=(rep.v_dropoff_pct!=null?rep.v_dropoff_pct.toFixed(1)+' %':'–');
  document.getElementById('rd-decelt').textContent=fmtSec(rep.decel_time_s);
  // Cadence
  document.getElementById('rd-strides').textContent=(rep.total_steps!=null?rep.total_steps:'–');
  document.getElementById('rd-sf').textContent=(rep.step_freq_hz!=null?rep.step_freq_hz.toFixed(2)+' Hz':'–');
  document.getElementById('rd-sl').textContent=(rep.avg_step_length_m!=null?rep.avg_step_length_m.toFixed(2)+' m':'–');
  document.getElementById('rd-slstd').textContent=(rep.step_length_std_m!=null?'±'+rep.step_length_std_m.toFixed(2)+' m':'–');
  // Power & work
  const pwkg=rep.peak_power_rel_wkg!=null?rep.peak_power_rel_wkg:(rep._meta&&rep._meta.body_mass_kg?(rep.peak_power_w/rep._meta.body_mass_kg):null);
  document.getElementById('rd-pf').textContent=(rep.peak_force_n!=null?rep.peak_force_n.toFixed(0)+' N':'–');
  document.getElementById('rd-pwk').textContent=(rep.peak_power_w!=null?(rep.peak_power_w.toFixed(0)+' W'+(pwkg!=null?' · '+pwkg.toFixed(1)+' W/kg':'')):'–');
  document.getElementById('rd-avgp').textContent=(rep.avg_power_w!=null?rep.avg_power_w.toFixed(0)+' W':'–');
  document.getElementById('rd-work').textContent=(rep.work_j!=null?rep.work_j.toFixed(0)+' J':'–');
  document.getElementById('rd-imp').textContent=(rep.impulse_ns!=null?rep.impulse_ns.toFixed(0)+' N·s':'–');

  // Steps tab meta strip
  document.getElementById('st-strides').textContent=(rep.total_steps!=null?rep.total_steps:'–');
  document.getElementById('st-sf').textContent=(rep.step_freq_hz!=null?rep.step_freq_hz.toFixed(2)+' Hz':'–');
  document.getElementById('st-sl').textContent=(rep.avg_step_length_m!=null?rep.avg_step_length_m.toFixed(2)+' m':'–');
  document.getElementById('st-slstd').textContent=(rep.step_length_std_m!=null?'±'+rep.step_length_std_m.toFixed(2)+' m':'–');

  // Note
  const note=document.getElementById('rd-note');
  if(rep.total_steps){
    note.textContent='Cadence from cable-force peaks (≈ stride rate; true step rate ≈ 2× this). GCT, flight time and L/R asymmetry need a foot sensor.';
  } else { note.textContent=''; }

  // Render the active tab's content (lazy — only what's visible)
  renderTab(activeTab);
}

function renderTab(name){
  if(!currentRep) return;
  if(name==='steps') renderStepsTab(currentRep);
  else if(name==='splits') renderSplitsTab(currentRep);
  else if(name==='fv') renderFVRepTab(currentRep);
  else if(name==='insights') renderInsightsTab(currentRep);
  // 'profile' is static DOM, already populated in renderRunDetail
}

function _insCard(i){
  const sev = i.severity || 'info';
  let body = '<div class="ins-tag">'+sev.toUpperCase()+' · '+i.tag+'</div>';
  body += '<div class="ins-detail">'+i.detail+'</div>';
  if(i.prescribe && i.prescribe.length){
    body += '<ul>'+i.prescribe.map(p=>'<li>'+p+'</li>').join('')+'</ul>';
  }
  if(i.do_not && i.do_not.length){
    body += '<ul class="ins-do-not">'+i.do_not.map(p=>'<li>'+p+'</li>').join('')+'</ul>';
  }
  if(i.evidence) body += '<div class="ins-evidence">'+i.evidence+'</div>';
  return '<div class="ins-card '+sev+'">'+body+'</div>';
}

let insightsView = 'athlete';   // 'athlete' | 'coach'

async function renderInsightsTab(rep){
  if(!rep || !rep.rep_idx) return;
  const pos = (document.getElementById('ins-position')||{value:'back'}).value;
  const mod = (document.getElementById('ins-modality')||{value:''}).value;
  // Show/hide the two view containers
  document.getElementById('ins-athlete-view').style.display = (insightsView==='athlete'?'block':'none');
  document.getElementById('ins-coach-view').style.display   = (insightsView==='coach'  ?'block':'none');
  if(insightsView === 'athlete'){ await renderAthleteInsights(rep, pos, mod); }
  else { await renderCoachInsights(rep, pos, mod); }
}

async function renderAthleteInsights(rep, pos, mod){
  const url = '/api/c/athletic/rep/'+rep.rep_idx+'/athlete_report?position='+pos+(mod?('&planned_modality='+mod):'');
  let r;
  try{ r = await(await fetch(url)).json(); }
  catch(e){ document.getElementById('ath-headline').textContent='Connection lost'; return; }
  document.getElementById('ath-headline').textContent = r.headline || '–';
  document.getElementById('ath-verdict').textContent  = r.verdict_paragraph || '';
  // Prescriptions
  const rxList = document.getElementById('ath-prescriptions');
  rxList.innerHTML = (r.prescriptions || []).map((rx,i)=>(
    '<div class="ath-rx">'+
      '<div class="ath-rx-head">'+
        '<span class="ath-rx-num">'+(i+1)+'.  '+(rx.label||'').toUpperCase()+'</span>'+
        (rx.day_hint?'<span class="ath-rx-day">'+rx.day_hint.toUpperCase()+'</span>':'')+
      '</div>'+
      '<div class="ath-rx-text">'+(rx.athlete_text||'')+'</div>'+
    '</div>'
  )).join('') || '<div class="meta">No specific prescriptions for this profile</div>';
  // Avoids
  const avSection = document.getElementById('ath-avoid-section');
  const avList = document.getElementById('ath-avoids');
  if(r.avoids && r.avoids.length){
    avSection.style.display='block';
    avList.innerHTML = r.avoids.map(a=>'<div class="ath-avoid">'+(a.athlete_text||'')+'</div>').join('');
  } else {
    avSection.style.display='none';
  }
  // Chase metric (with vs-previous-session delta + MDC flag)
  const c = r.chase_metric || {};
  const cur = c.current!=null ? c.current : '–';
  const tgt = c.target!=null  ? c.target  : '–';
  const u   = c.units || '';
  let chaseHtml =
    '<span class="name">'+(c.name||'Target')+'</span>'+
    '<span class="current">'+cur+(u?' '+u:'')+'</span>'+
    '<span class="arrow">→</span>'+
    '<span class="target">'+tgt+(u?' '+u:'')+'</span>';
  if(c.previous!=null){
    // For split-time-style metrics, lower is better. Decide direction colour
    // by whether the delta moved AWAY from the target.
    const movedTowardTarget = c.target!=null
      ? (Math.abs(c.target - c.current) < Math.abs(c.target - c.previous))
      : (c.delta_pct > 0);
    const arrow = c.delta_pct > 0 ? '↑' : (c.delta_pct < 0 ? '↓' : '→');
    const cls   = movedTowardTarget ? 'good' : 'bad';
    const sign  = c.delta_pct > 0 ? '+' : '';
    let mdcLabel = '';
    if(c.exceeds_mdc === true)  mdcLabel = ' · <span class="good">beats MDC ✓</span>';
    if(c.exceeds_mdc === false) mdcLabel = ' · <span class="muted">within noise</span>';
    chaseHtml += '<div class="ath-chase-delta">vs last: <b>'+c.previous+(u?' '+u:'')+
                 '</b> <span class="'+cls+'">'+arrow+' '+sign+c.delta_pct.toFixed(1)+'%</span>'+
                 mdcLabel+'</div>';
  }
  document.getElementById('ath-chase').innerHTML = chaseHtml;
}

async function renderCoachInsights(rep, pos, mod){
  const url = '/api/c/athletic/rep/'+rep.rep_idx+'/insights?position='+pos+(mod?('&planned_modality='+mod):'');
  let r;
  try{ r = await(await fetch(url)).json(); }
  catch(e){ document.getElementById('ins-headline').textContent='Connection lost'; return; }
  document.getElementById('ins-headline').textContent = r.headline || '–';
  document.getElementById('ins-limiter').textContent =
    r.primary_limiter ? ('Primary limiter: '+r.primary_limiter+' · Position: '+r.position_group) : '';
  document.getElementById('ins-primary').innerHTML =
    (r.primary_insights || []).map(_insCard).join('') || '<div class="meta">No primary findings</div>';
  document.getElementById('ins-context').innerHTML =
    (r.context_insights || []).map(_insCard).join('') || '<div class="meta">No additional context</div>';
  document.getElementById('ins-caveat').textContent = r.framing_caveat || '';
}

// Re-render when position/modality change OR when the view toggle is hit
['ins-position','ins-modality'].forEach(id=>{
  const el = document.getElementById(id);
  if(el) el.addEventListener('change',()=>{ if(activeTab==='insights' && currentRep) renderInsightsTab(currentRep); });
});
document.querySelectorAll('.ins-view-btn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.ins-view-btn').forEach(b=>b.classList.toggle('active', b===btn));
    insightsView = btn.getAttribute('data-view');
    if(currentRep) renderInsightsTab(currentRep);
  });
});

function renderStepsTab(rep){
  const svg=document.getElementById('steps-chart');
  const empty=document.getElementById('steps-empty');
  const events=rep.step_events||[];
  if(!events.length || events.length<2){
    svg.style.display='none'; empty.style.display='block'; return;
  }
  svg.style.display='block'; empty.style.display='none';
  const W=600,H=220,PADL=42,PADR=42,PADT=18,PADB=36;
  const valid=events.filter(e=>e.step_length_m!=null);
  const maxLen=Math.max(...valid.map(e=>e.step_length_m||0),0.1);
  // Velocity at strike — use the time-aligned chart sample, or fall back to NaN
  const samples=window._lastSamples||[];
  function velAt(t_s){
    if(!samples.length) return null;
    const t_ms=t_s*1000;
    let best=samples[0],dBest=Math.abs(samples[0].t_ms-t_ms);
    for(const s of samples){const d=Math.abs(s.t_ms-t_ms); if(d<dBest){best=s;dBest=d;}}
    return best.v_mps;
  }
  const vels=valid.map(e=>velAt(e.t_strike_s)).map(v=>v||0);
  const maxV=Math.max(...vels,0.1);
  const niceMax=v=>{const e=Math.pow(10,Math.floor(Math.log10(v)));const m=v/e;return e*(m<=1?1:m<=2?2:m<=5?5:10);};
  const lenAxMax=niceMax(maxLen*1.15);
  const velAxMax=niceMax(maxV*1.15);
  const barWidth=(W-PADL-PADR)/valid.length*0.7;
  const xs=i=>PADL+(i+0.5)*((W-PADL-PADR)/valid.length);
  const yL=v=>H-PADB-(v/lenAxMax)*(H-PADT-PADB);
  const yV=v=>H-PADB-(v/velAxMax)*(H-PADT-PADB);
  // Gridlines + axis labels
  let grid='';
  for(let i=0;i<=4;i++){
    const y=PADT+i*(H-PADT-PADB)/4;
    grid+='<line x1="'+PADL+'" y1="'+y+'" x2="'+(W-PADR)+'" y2="'+y+'" stroke="#1f1f26"/>';
    grid+='<text x="'+(PADL-6)+'" y="'+(y+3)+'" fill="#5b8bff" font-size="10" text-anchor="end" opacity="0.7">'+(lenAxMax*(1-i/4)).toFixed(1)+'</text>';
    grid+='<text x="'+(W-PADR+6)+'" y="'+(y+3)+'" fill="#58a6ff" font-size="10" opacity="0.7">'+(velAxMax*(1-i/4)).toFixed(1)+'</text>';
  }
  grid+='<text x="'+(PADL-30)+'" y="'+(PADT-4)+'" fill="#5b8bff" font-size="10" font-weight="600">m</text>';
  grid+='<text x="'+(W-PADR+10)+'" y="'+(PADT-4)+'" fill="#58a6ff" font-size="10" font-weight="600">m/s</text>';
  // Stride number labels along X
  for(let i=0;i<valid.length;i++){
    grid+='<text x="'+xs(i).toFixed(1)+'" y="'+(H-PADB+15)+'" fill="#5a5a64" font-size="9" text-anchor="middle">'+valid[i].step_number+'</text>';
  }
  grid+='<text x="'+(W/2)+'" y="'+(H-6)+'" fill="#5a5a64" font-size="10" text-anchor="middle">stride number</text>';
  // Bars (stride length)
  let bars='';
  for(let i=0;i<valid.length;i++){
    const x=xs(i)-barWidth/2;
    const y=yL(valid[i].step_length_m);
    const h=H-PADB-y;
    bars+='<rect x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+barWidth.toFixed(1)+'" height="'+h.toFixed(1)+'" fill="#5b8bff" opacity="0.75" rx="2"/>';
  }
  // Velocity overlay line
  let velPath='';
  for(let i=0;i<valid.length;i++){
    velPath+=(i?'L':'M')+xs(i).toFixed(1)+','+yV(vels[i]).toFixed(1);
  }
  // Velocity dots
  let velDots='';
  for(let i=0;i<valid.length;i++){
    velDots+='<circle cx="'+xs(i).toFixed(1)+'" cy="'+yV(vels[i]).toFixed(1)+'" r="3" fill="#58a6ff"/>';
  }
  svg.innerHTML=grid+bars+'<path d="'+velPath+'" stroke="#58a6ff" stroke-width="2" fill="none"/>'+velDots;
}

function renderSplitsTab(rep){
  const tbl=document.getElementById('splits-table');
  const sx=rep.splits_s_extended||rep.splits_s||{};
  const markers=[5,10,20,30,40].filter(m=>sx[String(m)]!=null);
  if(!markers.length){
    tbl.innerHTML='<tr><td class="meta" style="text-align:center;padding:30px 0">No splits — needs reps that cover ≥5 m</td></tr>';
    return;
  }
  let rows='<tr><th>Marker</th><th>Time</th><th>Segment</th><th>Avg speed</th></tr>';
  let prevT=0, prevM=0;
  for(const m of markers){
    const t=Number(sx[String(m)]);
    const segT=t-prevT, segM=m-prevM;
    const segV=segT>0?(segM/segT):0;
    rows+='<tr>'+
      '<td>'+m+' m</td>'+
      '<td>'+t.toFixed(2)+' s</td>'+
      '<td>'+(prevM===0?'0–'+m+' m':prevM+'–'+m+' m')+' · '+segT.toFixed(2)+' s</td>'+
      '<td>'+segV.toFixed(2)+' m/s</td>'+
    '</tr>';
    prevT=t; prevM=m;
  }
  tbl.innerHTML=rows;
}

// Smarter "nice" axis-max: rounds to 1 / 1.5 / 2 / 3 / 5 / 7.5 / 10 within the
// current decade. Avoids the 1->2->5->10 jumps that left huge empty axis space.
function niceAxisMax(v){
  if(v<=0) return 1;
  const e=Math.pow(10,Math.floor(Math.log10(v)));
  const m=v/e;
  const stops=[1,1.5,2,2.5,3,4,5,7.5,10];
  for(const s of stops){ if(m<=s) return e*s; }
  return e*10;
}

function renderFVRepTab(rep){
  const svg=document.getElementById('fv-rep-chart');
  const samples=window._lastSamples||[];
  if(!samples.length){
    svg.innerHTML='<text x="300" y="120" fill="#5a5a64" text-anchor="middle">Click a rep to load samples</text>'; return;
  }
  const W=600,H=240,PADL=46,PADR=22,PADT=18,PADB=32;

  // Morin F-V overlay (from xlsx Dashboard import) — draw line from (0,F0) to (V0,0).
  const f0 = (rep && rep.f0_n) ? rep.f0_n : null;
  const v0 = (rep && rep.v0_mps) ? rep.v0_mps : null;
  const pmax = (rep && rep.pmax_w_morin) ? rep.pmax_w_morin : (f0 && v0 ? (f0*v0/4) : null);

  const sampleVMax = Math.max(...samples.map(s=>s.v_mps),0.1);
  const sampleFMax = Math.max(...samples.map(s=>s.F_N),1);
  // Axes need to fit BOTH the sample cloud and the Morin line endpoints
  const vMax = Math.max(sampleVMax, v0||0);
  const fMax = Math.max(sampleFMax, f0||0);
  const tMax = Math.max(...samples.map(s=>s.t_ms),1);
  const vAx = niceAxisMax(vMax*1.08);
  const fAx = niceAxisMax(fMax*1.08);
  const xs=v=>PADL+(v/vAx)*(W-PADL-PADR);
  const ys=f=>H-PADB-(f/fAx)*(H-PADT-PADB);

  // Gridlines + axis ticks
  let grid='';
  for(let i=0;i<=4;i++){
    const y=PADT+i*(H-PADT-PADB)/4;
    const x=PADL+i*(W-PADL-PADR)/4;
    grid+='<line x1="'+PADL+'" y1="'+y+'" x2="'+(W-PADR)+'" y2="'+y+'" stroke="#1f1f26"/>';
    grid+='<text x="'+(PADL-6)+'" y="'+(y+3)+'" fill="#58a6ff" font-size="10" text-anchor="end" opacity="0.7">'+(fAx*(1-i/4)).toFixed(0)+'</text>';
    grid+='<line x1="'+x+'" y1="'+(H-PADB)+'" x2="'+x+'" y2="'+(H-PADB+4)+'" stroke="#3a3a44"/>';
    grid+='<text x="'+x+'" y="'+(H-PADB+15)+'" fill="#5a5a64" font-size="10" text-anchor="middle">'+(vAx*(i/4)).toFixed(1)+'</text>';
  }
  grid+='<text x="'+(PADL-32)+'" y="'+(PADT-4)+'" fill="#58a6ff" font-size="10" font-weight="600">F (N)</text>';
  grid+='<text x="'+(W-12)+'" y="'+(H-8)+'" fill="#5b8bff" font-size="10" font-weight="600" text-anchor="end">v (m/s)</text>';

  // Per-sample cloud — colour by time (start = warm orange, end = cool blue)
  let dots='';
  for(const s of samples){
    const tt=s.t_ms/tMax;
    const r=Math.round(212+(88-212)*tt);
    const g=Math.round(130+(166-130)*tt);
    const b=Math.round(58+(255-58)*tt);
    dots+='<circle cx="'+xs(s.v_mps).toFixed(1)+'" cy="'+ys(s.F_N).toFixed(1)+'" r="2" fill="rgb('+r+','+g+','+b+')" opacity="0.65"/>';
  }

  // Per-stride mean points (more interpretable than full cloud) — only if step_events present
  let strideDots='';
  const strideFV=[];
  if(rep && rep.step_events && rep.step_events.length>1){
    // Approximate per-stride mean by averaging samples in each stride window.
    // Each stride spans from t_strike[i-1] to t_strike[i].
    for(let i=1;i<rep.step_events.length;i++){
      const tA=rep.step_events[i-1].t_strike_s*1000;
      const tB=rep.step_events[i].t_strike_s*1000;
      const inWin=samples.filter(s=>s.t_ms>=tA && s.t_ms<tB);
      if(inWin.length<2) continue;
      const mv=inWin.reduce((a,b)=>a+b.v_mps,0)/inWin.length;
      const mf=inWin.reduce((a,b)=>a+b.F_N,0)/inWin.length;
      strideFV.push({v:mv,f:mf});
      strideDots+='<circle cx="'+xs(mv).toFixed(1)+'" cy="'+ys(mf).toFixed(1)+'" r="5" fill="#f0f0f3" stroke="#0a0a0c" stroke-width="1.5"/>';
    }
  }

  // Morin F-V regression line + F0 / V0 / Pmax markers
  let morin='';
  if(f0 && v0){
    const x0=xs(0), y0=ys(f0);     // (0, F0)
    const x1=xs(v0), y1=ys(0);     // (V0, 0)
    morin+='<line x1="'+x0.toFixed(1)+'" y1="'+y0.toFixed(1)+'" x2="'+x1.toFixed(1)+'" y2="'+y1.toFixed(1)+'" stroke="#5aa86a" stroke-width="2" stroke-dasharray="6,4" opacity="0.85"/>';
    // F0 endpoint
    morin+='<circle cx="'+x0.toFixed(1)+'" cy="'+y0.toFixed(1)+'" r="4" fill="#5aa86a"/>';
    morin+='<text x="'+(x0+8).toFixed(1)+'" y="'+(y0).toFixed(1)+'" fill="#5aa86a" font-size="11" font-weight="600">F0 = '+f0.toFixed(0)+' N</text>';
    // V0 endpoint
    morin+='<circle cx="'+x1.toFixed(1)+'" cy="'+y1.toFixed(1)+'" r="4" fill="#5aa86a"/>';
    morin+='<text x="'+(x1-4).toFixed(1)+'" y="'+(y1-8).toFixed(1)+'" fill="#5aa86a" font-size="11" font-weight="600" text-anchor="end">V0 = '+v0.toFixed(2)+' m/s</text>';
    // Pmax point at (V0/2, F0/2)
    if(pmax){
      const px=xs(v0/2), py=ys(f0/2);
      morin+='<circle cx="'+px.toFixed(1)+'" cy="'+py.toFixed(1)+'" r="6" fill="none" stroke="#d4a13a" stroke-width="2"/>';
      morin+='<circle cx="'+px.toFixed(1)+'" cy="'+py.toFixed(1)+'" r="2" fill="#d4a13a"/>';
      morin+='<text x="'+(px+10).toFixed(1)+'" y="'+(py+4).toFixed(1)+'" fill="#d4a13a" font-size="11" font-weight="600">Pmax = '+pmax.toFixed(0)+' W</text>';
    }
  }

  // Legend
  const legend = '<g transform="translate('+(W-160)+','+(PADT)+')">'+
    '<rect x="0" y="0" width="155" height="48" fill="#0a0a0c" stroke="#1f1f26" rx="4"/>'+
    '<circle cx="10" cy="12" r="3" fill="#5b8bff"/><text x="18" y="16" fill="#8a8a96" font-size="10">samples (start → end)</text>'+
    (rep && rep.step_events && rep.step_events.length>1 ?
      '<circle cx="10" cy="26" r="4" fill="#f0f0f3" stroke="#0a0a0c"/><text x="18" y="30" fill="#8a8a96" font-size="10">per-stride avg</text>' : '')+
    (f0 && v0 ?
      '<line x1="3" y1="40" x2="17" y2="40" stroke="#5aa86a" stroke-width="2" stroke-dasharray="3,2"/><text x="22" y="43" fill="#8a8a96" font-size="10">Morin F-V line</text>' : '')+
    '</g>';

  // F-V fit confidence (R²) — per-stride points vs the Morin line.
  // 1080 guidance: values below 0.98 are starting to become questionable.
  let r2txt='';
  if(f0 && v0 && strideFV.length>=2){
    const meanF=strideFV.reduce((a,p)=>a+p.f,0)/strideFV.length;
    let ssRes=0, ssTot=0;
    for(const p of strideFV){
      const pred=f0*(1-p.v/v0);
      ssRes+=(p.f-pred)*(p.f-pred);
      ssTot+=(p.f-meanF)*(p.f-meanF);
    }
    const r2=ssTot>0?Math.max(0,1-ssRes/ssTot):0;
    const col=r2>=0.98?'#5aa86a':'#d4a13a';
    r2txt='<text x="'+(PADL+6)+'" y="'+(PADT+14)+'" fill="'+col+'" font-size="11" '+
          'font-weight="700">R² '+r2.toFixed(3)+(r2<0.98?' — questionable fit':'')+'</text>';
  }
  svg.innerHTML=grid+dots+strideDots+morin+r2txt+legend;
}

// Tab switching
document.querySelectorAll('.rd-tab').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.rd-tab').forEach(b=>b.classList.toggle('active', b===btn));
    document.querySelectorAll('.rd-panel').forEach(p=>{
      p.style.display = (p.getAttribute('data-tab')===btn.getAttribute('data-tab')) ? 'block' : 'none';
    });
    activeTab = btn.getAttribute('data-tab');
    renderTab(activeTab);
  });
});

const REP_COLORS=['', '#5b8bff', '#5aa86a', '#e85a5a', '#4a90d4'];
function repEsc(s){ return String(s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
async function patchRepAnnotation(idx, body){
  try{
    const r=await fetch('/api/c/athletic/rep/'+idx+'/annotation',
      {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok){ alert("Couldn't update rep"); return false; }
    return true;
  }catch(e){ alert('Network error'); return false; }
}
function renderRepsList(reps){
  if(!reps.length){
    repsList.innerHTML='<div class="reps-empty">No reps yet — start a drill to log reps here.</div>';
    if(typeof renderPresetTarget==='function') renderPresetTarget();
    return;
  }
  // Per-set summary across all reps (not just the visible slice)
  const setStats={};
  reps.forEach(r=>{
    const s=r.set_idx||1;
    if(!setStats[s]) setStats[s]={count:0,best:0};
    setStats[s].count++;
    if((r.peak_speed_mps||0)>setStats[s].best) setStats[s].best=r.peak_speed_mps||0;
  });
  const recent=reps.slice().reverse().slice(0,8);
  let html='', lastSet=null;
  recent.forEach(r=>{
    const setN=r.set_idx||1;
    if(setN!==lastSet){
      const st=setStats[setN]||{count:0,best:0};
      html+='<div class="set-head">Set '+setN+
            '<span class="set-sum">'+st.count+' rep'+(st.count===1?'':'s')+
            ' · best '+st.best.toFixed(2)+' m/s</span></div>';
      lastSet=setN;
    }
    const isInvalid = r.valid === false;
    const isActive = repsList.getAttribute('data-active-rep') === String(r.rep_idx);
    const colorStyle = r.color ? (' style="border-left-color:'+r.color+'"') : '';
    html+='<div class="rep-row'+(isInvalid?' invalid':'')+(isActive?' active':'')+'"'+colorStyle+
        ' role="button" tabindex="0" data-rep="'+r.rep_idx+'" '+
        'aria-label="Show detail for rep '+r.rep_idx+'">'+
      '<div class="num">Rep '+r.rep_idx+(isInvalid?'<span class="invalid-tag">invalid</span>':'')+'</div>'+
      '<div class="stats">'+
        (r.peak_speed_mps||0).toFixed(2)+' m/s'+
        '<span class="sep">·</span>'+
        (r.peak_force_n||0).toFixed(0)+' N'+
        '<span class="sep">·</span>'+
        (r.peak_power_w||0).toFixed(0)+' W'+
        '<span class="sep">·</span>'+
        (r.duration_s||0).toFixed(2)+' s'+
        (r.comment?('<div class="rep-comment">'+repEsc(r.comment)+'</div>'):'')+
      '</div>'+
      '<div style="display:flex;gap:5px;align-items:center">'+
        '<button class="rep-color" data-rep="'+r.rep_idx+'" title="Colour tag" '+
          'style="color:'+(r.color||'var(--line)')+'">●</button>'+
        '<button class="rep-note'+(r.comment?' has':'')+'" data-rep="'+r.rep_idx+'" '+
          'title="Comment">✎</button>'+
        '<button class="v-toggle'+(isInvalid?' invalid':'')+'" data-rep="'+r.rep_idx+'" '+
          'title="'+(isInvalid?'Mark valid':'Mark invalid (false start, slip, etc.)')+'">'+
          (isInvalid?'↺':'✗')+'</button>'+
        '<span class="chev" aria-hidden="true">›</span>'+
      '</div>'+
    '</div>';
  });
  repsList.innerHTML=html;
  repsList.querySelectorAll('.v-toggle').forEach(b=>{
    b.onclick=async(e)=>{
      e.stopPropagation();
      const idx=parseInt(b.getAttribute('data-rep'),10);
      const cur=(window._lastReps||[]).find(r=>r.rep_idx===idx);
      const isInvalid = cur && cur.valid === false;
      let body = {valid: isInvalid};   // toggle
      if(!isInvalid){
        // Going invalid — capture optional reason
        const reason = prompt('Mark invalid? Optional reason (false_start / slip / fall / equipment / other):', 'other');
        if(reason === null) return;
        body.reason = reason || 'other';
      }
      try{
        const r=await fetch('/api/c/athletic/rep/'+idx+'/validity',
          {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(!r.ok){ alert("Couldn't update validity"); return; }
        if(cur){ cur.valid = body.valid; cur.invalid_reason = body.reason; }
        renderRepsList(window._lastReps || []);
      }catch(e){ alert('Network error'); }
    };
  });
  repsList.querySelectorAll('.rep-color').forEach(b=>{
    b.onclick=async(e)=>{
      e.stopPropagation();
      const idx=parseInt(b.getAttribute('data-rep'),10);
      const cur=(window._lastReps||[]).find(r=>r.rep_idx===idx);
      const ci=REP_COLORS.indexOf((cur&&cur.color)||'');
      const next=REP_COLORS[(ci+1)%REP_COLORS.length];
      if(await patchRepAnnotation(idx,{color:next})){
        if(cur) cur.color=next||null;
        renderRepsList(window._lastReps||[]);
      }
    };
  });
  repsList.querySelectorAll('.rep-note').forEach(b=>{
    b.onclick=async(e)=>{
      e.stopPropagation();
      const idx=parseInt(b.getAttribute('data-rep'),10);
      const cur=(window._lastReps||[]).find(r=>r.rep_idx===idx);
      const note=prompt('Rep comment:', (cur&&cur.comment)||'');
      if(note===null) return;
      if(await patchRepAnnotation(idx,{comment:note})){
        if(cur) cur.comment=note||null;
        renderRepsList(window._lastReps||[]);
      }
    };
  });
  repsList.querySelectorAll('.rep-row').forEach(row=>{
    const activate=async()=>{
      const idx=parseInt(row.getAttribute('data-rep'),10);
      const sj=await(await fetch('/api/c/athletic/rep/'+idx+'/samples')).json();
      cachedSamples=sj.samples||[];
      window._lastSamples = cachedSamples;
      renderLiveChart(cachedSamples, {corridor_m: lastCorridor});
      // Surface the rep's full metric panel on click
      const repObj = (window._lastReps||[]).find(r=>r.rep_idx===idx);
      if(repObj) renderRunDetail(repObj);
      // Persist which rep is expanded so its left-border accent survives re-render
      repsList.setAttribute('data-active-rep', idx);
      repsList.querySelectorAll('.rep-row').forEach(x=>
        x.classList.toggle('active', x.getAttribute('data-rep')===String(idx)));
    };
    row.addEventListener('click', activate);
    row.addEventListener('keydown', e=>{
      if(e.target!==row) return;   // let the v-toggle button handle its own keys
      if(e.key==='Enter'||e.key===' '){ e.preventDefault(); activate(); }
    });
  });
  if(typeof renderPresetTarget==='function') renderPresetTarget();
}

loadAthletes();

// New-set button — groups subsequent reps under a new set index
document.getElementById('new-set-btn').onclick=async()=>{
  try{ await fetch('/api/c/athletic/new_set',{method:'POST'}); }catch(e){}
};

// Pull current athletic config to seed the Settings sheet inputs on first paint
(async ()=>{
  try{
    const r = await fetch('/api/c/state');
    if(!r.ok) return;
    const j = await r.json();
    const cfg = j.config || {};
    const set = (id, val) => { const el = document.getElementById(id); if(el && val != null) el.value = val; };
    set('cfg-resist',      cfg.resist_kg);
    set('cfg-resist-dist', cfg.resist_distance_m);
    const vcapToggle=document.getElementById('cfg-vcap-toggle');
    if(vcapToggle){
      const vc=cfg.velocity_cap_mps;
      if(vc!=null && vc>0){ document.getElementById('cfg-vcap').value=vc; vcapToggle.checked=true; }
      else vcapToggle.checked=false;
      if(typeof window._syncVcap==='function') window._syncVcap();
    }
    set('cfg-chain',       cfg.chain_kg_per_m);
    set('cfg-ecc',         cfg.eccentric_overload_pct);
    set('cfg-fw-mass',     cfg.virtual_mass_kg);
    set('cfg-fw-ecc-pct',  cfg.eccentric_overload_pct);
    set('cfg-fw-visc',     cfg.viscous_damping);
    // cfg-condir is fixed to "extend" (Cable OUT) — gym concentric is always cable-out.
    set('cfg-return',      cfg.return_kg);
    set('cfg-dist',        cfg.return_distance_m);
    set('cfg-slew',        cfg.slew_kg_per_s);
    set('cfg-rest',        cfg.rest_interval_s);
    set('cfg-countdown',   cfg.countdown_s);
    // Clamp the working-resistance stepper to the configured max resistance.
    const kgLim=(j.limits&&j.limits.kg_max)||cfg.kg_limit;
    if(kgLim){
      const ri=document.getElementById('cfg-resist');
      if(ri){ ri.max=kgLim; if((parseFloat(ri.value)||0)>kgLim) ri.value=kgLim; }
    }
    if(cfg.drill){ const sel = document.getElementById('cfg-drill'); if(sel) sel.value = cfg.drill; }
    if(cfg.mode) applyMode(cfg.mode, {push:false});
    if(cfg.cod_prefix){ const sel = document.getElementById('cfg-cod-prefix'); if(sel) sel.value = cfg.cod_prefix; }
    if(cfg.gear != null && typeof setGear === 'function') setGear(cfg.gear);
  }catch(e){}
})();

// E-stop also fires the alert sound
if(estopBtn){
  const __origEstop = estopBtn.onclick;
  estopBtn.onclick = async (e)=>{ try{SOUNDS.estop();}catch(_){} if(__origEstop) await __origEstop.call(estopBtn, e); };
}

// ============== MULTI-RIG (§15.3) ==============
async function loadRigs(){
  const sel = document.getElementById('rig-select');
  if(!sel) return;
  try{
    const list = await(await fetch('/api/rigs')).json();
    const cur = parseInt(localStorage.getItem('ppa.activeRigId') || '1', 10);
    sel.innerHTML = list.map(r => '<option value="'+r.id+'"'+(r.id===cur?' selected':'')+'>'+r.name+'</option>').join('');
    sel.onchange = ()=>{
      localStorage.setItem('ppa.activeRigId', sel.value);
      // Push to backend so subsequent sessions stamp the right rig_id
      fetch('/api/c/athletic/config',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({rig_id: parseInt(sel.value, 10)})}).catch(()=>{});
    };
  }catch(e){}
}
loadRigs();

// ============== MODE / DRILL (§16.1) ==============
// COD sub-mode and gear/pulley state moved to /setup.

// Drill picker — tile grid. Backend values are the enum strings; the labels
// are rugby-friendly display copy. Tiles shown are filtered by the active mode.
const DRILLS = {
  Acceleration:   {label:'Acceleration · 10m', desc:'First 10m drive phase'},
  Sprint:         {label:'Top-end speed',      desc:'Max-velocity sprint',
                   assisted:{label:'Overspeed sprint', desc:'Motor-towed overspeed'}},
  ASkip:          {label:'A-Skip',             desc:'Posture + dorsiflexion'},
  BSkip:          {label:'B-Skip',             desc:'A-Skip plus leg extension'},
  Ankling:        {label:'Ankling',            desc:'Quick low-amplitude steps'},
  HighKnee:       {label:'High knee',          desc:'Knee-drive cadence'},
  FastLeg:        {label:'Fast leg',           desc:'Single-leg cycling action'},
  StraightLegRun: {label:'Straight-leg run',   desc:'Stiff-leg foot strike'},
  ApproachJump:   {label:'Approach jump',      desc:'Run-up into a jump'},
  SkippingJumps:  {label:'Skipping jumps',     desc:'Continuous skip-jumps'},
  FreeTest:       {label:'Free run',           desc:'Unstructured free run'},
};
const DRILLS_BY_MODE = {
  resisted: ['Sprint','Acceleration','ASkip','BSkip','Ankling','HighKnee','FastLeg','StraightLegRun'],
  assisted: ['Sprint','ApproachJump','SkippingJumps'],
  cod:      ['FreeTest'],
  gym:      ['FreeTest','HighKnee','FastLeg'],
  flywheel: ['FreeTest','HighKnee','FastLeg'],
};
const cfgDrill = document.getElementById('cfg-drill');   // hidden input — backend value
const drillSel = document.getElementById('cfg-drill-sel');
const drillDesc = document.getElementById('cfg-drill-desc');
function _drillView(v, mode){
  const d = DRILLS[v] || {label:v, desc:''};
  return (mode === 'assisted' && d.assisted) ? d.assisted : d;
}
function updateDrillDesc(mode){
  if(drillDesc) drillDesc.textContent = _drillView(cfgDrill.value, mode).desc || '';
}
function renderDrillGrid(mode){
  if(!drillSel) return;
  const list = DRILLS_BY_MODE[mode] || DRILLS_BY_MODE.resisted;
  if(list.indexOf(cfgDrill.value) < 0) cfgDrill.value = list[0];
  drillSel.innerHTML = list.map(v=>
    '<option value="'+v+'"'+(v===cfgDrill.value?' selected':'')+'>'+
    _drillView(v, mode).label+'</option>').join('');
  drillSel.value = cfgDrill.value;
  updateDrillDesc(mode);
}
if(drillSel){
  drillSel.addEventListener('change', ()=>{
    cfgDrill.value = drillSel.value;
    updateDrillDesc(currentMode);
    if(typeof refreshSuggestedLoad==='function') refreshSuggestedLoad();
  });
}

// Mode pill bar is the single source of truth for `mode`. Selecting a pill
// shows/hides the settings-sheet fields that apply to that mode and pushes
// the new mode to the backend athletic config.
function applyMode(mode, opts){
  opts = opts || {};
  if(['resisted','assisted','cod','gym','flywheel'].indexOf(mode) < 0) mode = 'resisted';
  currentMode = mode;
  // warm/cool state accent — resisting (resisted/cod/gym) warm; assist/flywheel cool
  var _warm = !(mode==='assisted' || mode==='flywheel');
  document.documentElement.style.setProperty('--state', _warm?'var(--warm)':'var(--cool)');
  document.documentElement.style.setProperty('--state-soft', _warm?'var(--warm-soft)':'var(--cool-soft)');
  var _pb=document.getElementById('phase-badge');
  if(_pb) _pb.textContent=(mode==='assisted')?'Assist':(mode==='gym')?'Gym':(mode==='flywheel')?'Flywheel':(mode==='cod')?'C.O.D.':'Resist';
  document.querySelectorAll('.mode-pill').forEach(p=>{
    const on = p.getAttribute('data-mode') === mode;
    p.classList.toggle('active', on);
    p.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.sheet .field[data-modes]').forEach(f=>{
    const modes = (f.getAttribute('data-modes') || '').split(/\s+/);
    f.style.display = modes.indexOf(mode) >= 0 ? '' : 'none';
  });
  const rl = document.getElementById('cfg-resist-l');
  const dl = document.getElementById('cfg-resist-dist-l');
  if(rl) rl.textContent = (mode === 'assisted') ? 'Assistance (kg)'
                        : (mode === 'gym') ? 'Concentric weight (kg)'
                        : 'Working resistance (kg)';
  if(dl) dl.textContent = (mode === 'assisted') ? 'Tow distance (m)' : 'Resist distance (m)';
  renderDrillGrid(mode);
  if(typeof updateEccNote === 'function') updateEccNote();
  if(opts.push !== false){
    fetch('/api/c/athletic/config',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode: mode})}).catch(()=>{});
  }
  if(typeof refreshSuggestedLoad==='function') refreshSuggestedLoad();
  if(typeof updateResistSummary==='function') updateResistSummary();
}
document.querySelectorAll('.mode-pill').forEach(p=>{
  p.addEventListener('click', ()=>{
    const m=p.getAttribute('data-mode');
    applyMode(m);
    if(typeof maybeShowModeIntro==='function') maybeShowModeIntro(m);
  });
});
applyMode('resisted', {push:false});

// Eccentric-weight note — shows the implied overload % under the input
function updateEccNote(){
  const note=document.getElementById('ecc-overload-note');
  if(!note) return;
  const concKg=parseFloat(document.getElementById('cfg-resist').value)||0;
  const eccEl=document.getElementById('cfg-ecc-kg');
  const eccKg=eccEl?(parseFloat(eccEl.value)||0):0;
  if(concKg<=0||eccKg<=0){ note.textContent=''; note.style.color=''; return; }
  const pct=((eccKg-concKg)/concKg)*100;
  const ratio=eccKg/concKg;
  if(ratio>1.7){
    note.style.color='var(--bad)';
    note.textContent='⚠ Eccentric is '+pct.toFixed(0)+'% above concentric — a very high overload, injury risk.';
  }else if(ratio>1.4){
    note.style.color='var(--warn)';
    note.textContent='⚠ Eccentric is '+pct.toFixed(0)+'% above concentric — high eccentric load.';
  }else{
    note.style.color='';
    note.textContent = pct>0.5 ? ('+'+pct.toFixed(0)+'% eccentric overload')
                     : pct<-0.5 ? (Math.abs(pct).toFixed(0)+'% eccentric under-load')
                     : 'Matched concentric / eccentric';
  }
}
// On commit (blur/enter), an extreme overload (>70%) needs an explicit OK.
function confirmEccOverload(){
  const concKg=parseFloat(document.getElementById('cfg-resist').value)||0;
  const eccEl=document.getElementById('cfg-ecc-kg');
  if(!eccEl||concKg<=0) return;
  const eccKg=parseFloat(eccEl.value)||0;
  if(eccKg/concKg>1.7){
    const pct=((eccKg-concKg)/concKg)*100;
    const ok=confirm('Eccentric is '+pct.toFixed(0)+'% above concentric. '+
      'This is a very high overload.\\n\\nOK = keep the value · Cancel = reset to a safe 40% overload.');
    if(!ok){ eccEl.value=(concKg*1.4).toFixed(1); }
    updateEccNote();
  }
}
(function(){
  const e=document.getElementById('cfg-ecc-kg'), r=document.getElementById('cfg-resist');
  if(e){ e.addEventListener('input',updateEccNote); e.addEventListener('change',confirmEccOverload); }
  if(r) r.addEventListener('input',updateEccNote);
  updateEccNote();
})();

// ============== SESSION PRESETS (§10) ==============
const PRESETS = {
  match_prep:{name:'Match-prep accel',mode:'resisted',drill:'Acceleration',resist_kg:6,resist_distance_m:15,
    target:{reps:5,metric:'peak_speed_mps',threshold:6.0,cmp:'gte'},target_label:'5 reps · peak v ≥ 6.0 m/s'},
  top_end:{name:'Top-end speed',mode:'resisted',drill:'Sprint',resist_kg:3,resist_distance_m:40,
    target:{reps:4,metric:'peak_speed_mps',threshold:8.0,cmp:'gte'},target_label:'4 reps · peak v ≥ 8.0 m/s'},
  decel:{name:'Decel mechanics',mode:'resisted',drill:'Sprint',resist_kg:2,resist_distance_m:20,
    target:{reps:6,metric:'peak_speed_mps',threshold:0,cmp:'gte'},target_label:'6 reps · decel focus'},
  overspeed:{name:'Overspeed sprint',mode:'assisted',drill:'Sprint',resist_kg:2,resist_distance_m:30,
    target:{reps:4,metric:'peak_speed_mps',threshold:9.5,cmp:'gte'},target_label:'4 reps · peak v ≥ 9.5 m/s'},
  power_gym:{name:'Power gym',mode:'gym',drill:'FreeTest',resist_kg:50,ecc_kg:70,chain_kg_per_m:2,resist_distance_m:1,
    target:{reps:15,metric:'peak_power_w',threshold:800,cmp:'gte'},target_label:'15 reps · peak P ≥ 800 W'},
  fv_test:{name:'FV profile',mode:'resisted',drill:'Acceleration',resist_kg:4,resist_distance_m:20,
    target:{reps:4,metric:'peak_speed_mps',threshold:0,cmp:'gte'},target_label:'Capture 4 loads for an FV profile'},
};
let activePreset=null;
function applyPreset(key){
  const p=PRESETS[key]; if(!p) return;
  activePreset=key;
  applyMode(p.mode);
  const setv=(id,v)=>{ const el=document.getElementById(id); if(el&&v!=null) el.value=v; };
  setv('cfg-resist',p.resist_kg);
  setv('cfg-resist-dist',p.resist_distance_m);
  if(p.mode==='gym'){ setv('cfg-ecc-kg',p.ecc_kg); setv('cfg-chain',p.chain_kg_per_m); }
  if(p.drill){ cfgDrill.value=p.drill; renderDrillGrid(p.mode); }
  document.querySelectorAll('.preset-btn').forEach(b=>
    b.classList.toggle('active',b.getAttribute('data-preset')===key));
  localStorage.setItem('ppa.lastPreset',key);
  const ce=document.getElementById('chart-empty');
  if(ce) ce.innerHTML='<div style="font-weight:700;color:var(--accent);font-size:14px">Preset loaded · '+p.name+'</div>'+
    '<div style="margin-top:6px">'+p.target_label+'</div>'+
    '<div style="margin-top:10px;color:var(--muted)">Tap Arm rig to begin.</div>';
  if(typeof updateEccNote==='function') updateEccNote();
  renderPresetTarget();
}
document.querySelectorAll('.preset-btn').forEach(b=>
  b.addEventListener('click',()=>applyPreset(b.getAttribute('data-preset'))));
(function(){
  const last=localStorage.getItem('ppa.lastPreset');
  if(last&&PRESETS[last]) document.querySelectorAll('.preset-btn').forEach(b=>
    b.classList.toggle('active',b.getAttribute('data-preset')===last));
})();
function repHitsTarget(rep,t){
  const v=rep[t.metric];
  if(t.threshold===0) return v!=null;
  if(v==null) return false;
  return t.cmp==='lte' ? v<=t.threshold : v>=t.threshold;
}
function renderPresetTarget(){
  const el=document.getElementById('preset-target');
  if(!el) return;
  const p=activePreset?PRESETS[activePreset]:null;
  if(!p){ el.hidden=true; return; }
  el.hidden=false;
  const reps=(window._lastReps||[]).filter(r=>r.valid!==false);
  const t=p.target;
  let hits=0,dots='';
  for(let i=0;i<t.reps;i++){
    const rep=reps[i];
    let cls='';
    if(rep){ const ok=repHitsTarget(rep,t); cls=ok?'hit':'miss'; if(ok) hits++; }
    dots+='<span class="pt-dot '+cls+'"></span>';
  }
  const done=reps.length>=t.reps;
  el.innerHTML='<div class="pt-label">'+p.name+' · '+p.target_label+'</div>'+
    '<div class="pt-dots">'+dots+'</div>'+
    '<div class="pt-summary">'+hits+' of '+t.reps+' hit'+
    (done ? (' — '+(hits>=Math.ceil(t.reps*0.8)?'session on plan':'short of plan'))
          : (' · '+Math.max(0,t.reps-reps.length)+' reps to go'))+'</div>';
}

// ============== PER-MODE INTRO CARDS (§12) ==============
const MODE_INTRO = {
  resisted:{
    title:'Resisted Sprint',
    text:'Athlete starts about 1 m in front of the rig. On Start rep the motor applies the working load — the athlete sprints away from the rig.',
    svg:'<svg viewBox="0 0 200 60" width="184" height="55" aria-hidden="true">'+
        '<rect x="8" y="14" width="22" height="32" rx="3" fill="#5b8bff"/>'+
        '<line x1="30" y1="30" x2="116" y2="30" stroke="#3a3a44" stroke-width="2" stroke-dasharray="4 3"/>'+
        '<circle cx="124" cy="30" r="9" fill="#f0f0f3"/>'+
        '<line x1="138" y1="30" x2="178" y2="30" stroke="#5aa86a" stroke-width="3"/>'+
        '<path d="M184 30 l-10 -6 v12 z" fill="#5aa86a"/></svg>'},
  assisted:{
    title:'Assisted Sprint',
    text:'Walk the cable out so the athlete is past the end zone. On Start rep the motor tows the athlete back toward the rig — an overspeed run.',
    svg:'<svg viewBox="0 0 200 60" width="184" height="55" aria-hidden="true">'+
        '<rect x="8" y="14" width="22" height="32" rx="3" fill="#5b8bff"/>'+
        '<line x1="30" y1="30" x2="158" y2="30" stroke="#3a3a44" stroke-width="2" stroke-dasharray="4 3"/>'+
        '<circle cx="166" cy="30" r="9" fill="#f0f0f3"/>'+
        '<line x1="150" y1="30" x2="40" y2="30" stroke="#5aa86a" stroke-width="3"/>'+
        '<path d="M34 30 l10 -6 v12 z" fill="#5aa86a"/></svg>'},
  cod:{
    title:'Change of Direction',
    text:'Set the zero point at least 0.15 m from the start. The athlete sprints out and returns — the motor resists both the way out and the way back (eccentric load on the return).',
    svg:'<svg viewBox="0 0 200 60" width="184" height="55" aria-hidden="true">'+
        '<rect x="8" y="14" width="22" height="32" rx="3" fill="#5b8bff"/>'+
        '<line x1="30" y1="30" x2="118" y2="30" stroke="#3a3a44" stroke-width="2" stroke-dasharray="4 3"/>'+
        '<circle cx="124" cy="30" r="9" fill="#f0f0f3"/>'+
        '<line x1="138" y1="22" x2="178" y2="22" stroke="#5b8bff" stroke-width="3"/>'+
        '<path d="M184 22 l-10 -5 v10 z" fill="#5b8bff"/>'+
        '<line x1="178" y1="38" x2="138" y2="38" stroke="#5aa86a" stroke-width="3"/>'+
        '<path d="M132 38 l10 -5 v10 z" fill="#5aa86a"/></svg>'},
  gym:{
    title:'Gym',
    text:'Static position. Direction OUT = the cable extends on the concentric (lifting) phase; Direction IN = it retracts. Set concentric and eccentric weights separately.',
    svg:'<svg viewBox="0 0 200 60" width="184" height="55" aria-hidden="true">'+
        '<rect x="8" y="14" width="22" height="32" rx="3" fill="#5b8bff"/>'+
        '<line x1="30" y1="30" x2="110" y2="30" stroke="#3a3a44" stroke-width="2" stroke-dasharray="4 3"/>'+
        '<circle cx="118" cy="30" r="9" fill="#f0f0f3"/>'+
        '<line x1="118" y1="14" x2="118" y2="6" stroke="#5aa86a" stroke-width="3"/>'+
        '<path d="M118 2 l-5 9 h10 z" fill="#5aa86a"/>'+
        '<line x1="118" y1="46" x2="118" y2="54" stroke="#5b8bff" stroke-width="3"/>'+
        '<path d="M118 58 l-5 -9 h10 z" fill="#5b8bff"/></svg>'},
  flywheel:{
    title:'Flywheel',
    text:'Static position. The cable spins up a simulated flywheel — resistance comes from accelerating the virtual mass, so steady pulling feels light and explosive effort feels heavy. The eccentric (cable-in) phase can be overloaded. Set Virtual mass in Adjust.',
    svg:'<svg viewBox="0 0 200 60" width="184" height="55" aria-hidden="true">'+
        '<rect x="8" y="14" width="22" height="32" rx="3" fill="#5b8bff"/>'+
        '<line x1="30" y1="30" x2="120" y2="30" stroke="#3a3a44" stroke-width="2" stroke-dasharray="4 3"/>'+
        '<circle cx="150" cy="30" r="20" fill="none" stroke="#f0f0f3" stroke-width="3"/>'+
        '<circle cx="150" cy="30" r="3" fill="#f0f0f3"/>'+
        '<line x1="150" y1="10" x2="150" y2="16" stroke="#5aa86a" stroke-width="3"/>'+
        '<line x1="170" y1="30" x2="164" y2="30" stroke="#5aa86a" stroke-width="3"/>'+
        '<line x1="150" y1="50" x2="150" y2="44" stroke="#5aa86a" stroke-width="3"/>'+
        '<line x1="130" y1="30" x2="136" y2="30" stroke="#5aa86a" stroke-width="3"/></svg>'},
};
const _introSeenSession = new Set();
function maybeShowModeIntro(mode){
  const intro=MODE_INTRO[mode];
  if(!intro) return;
  if(localStorage.getItem('ppa.modeIntroSeen.'+mode)==='1') return;
  if(_introSeenSession.has(mode)) return;
  const card=document.getElementById('mode-intro');
  if(!card) return;
  document.getElementById('mi-title').textContent=intro.title;
  document.getElementById('mi-diagram').innerHTML=intro.svg;
  document.getElementById('mi-text').textContent=intro.text;
  card.setAttribute('data-mode',mode);
  card.hidden=false;
}
(function(){
  const card=document.getElementById('mode-intro');
  if(!card) return;
  document.getElementById('mi-got').onclick=()=>{
    _introSeenSession.add(card.getAttribute('data-mode'));
    card.hidden=true;
  };
  document.getElementById('mi-never').onclick=()=>{
    localStorage.setItem('ppa.modeIntroSeen.'+card.getAttribute('data-mode'),'1');
    card.hidden=true;
  };
})();

// ============== BT HID INPUT LAYER (§2.6) ==============
// Abstraction so a coach could pair a presenter remote / footswitch and have
// it act on the rig. We don't ship a remote — just don't paint into a corner.
// Maps:
//   Page Down / right-arrow / 'g'   → Start drill
//   Page Up / left-arrow / 's'      → End drill
//   Escape / 'x'                    → E-stop (one tap, no confirmation)
// Skipped if user is typing in an <input> or <select>.
window.addEventListener('keydown', (ev)=>{
  const tag = (ev.target && ev.target.tagName) || '';
  if(tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if(ev.repeat) return;
  const k = ev.key;
  if(k === 'Escape' || k === 'x' || k === 'X'){
    ev.preventDefault();
    document.getElementById('estop-btn')?.click();
  } else if(k === 'PageDown' || k === 'ArrowRight' || k === 'g' || k === 'G' || k === ' '){
    ev.preventDefault();
    if(!primaryBtn.disabled) primaryBtn.click();
  } else if(k === 'PageUp' || k === 'ArrowLeft' || k === 's' || k === 'S'){
    ev.preventDefault();
    if(!primaryBtn.disabled) primaryBtn.click();
  }
});

// Gamepad API for actual BT HID controllers — poll for button presses
let _gamepadButtonsLast = {};
function pollGamepads(){
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  for(const pad of pads){
    if(!pad) continue;
    const last = _gamepadButtonsLast[pad.index] || [];
    pad.buttons.forEach((btn, i)=>{
      const wasPressed = last[i];
      const isPressed = btn.pressed;
      if(isPressed && !wasPressed){
        // First button on most remotes/presenters
        if(i === 0 && !primaryBtn.disabled) primaryBtn.click();
        else if(i === 9 || i === 8) document.getElementById('estop-btn')?.click();
      }
      last[i] = isPressed;
    });
    _gamepadButtonsLast[pad.index] = last;
  }
  requestAnimationFrame(pollGamepads);
}
if(navigator.getGamepads) requestAnimationFrame(pollGamepads);

// Units toggle (§15.2)
(()=>{
  const sel = document.getElementById('cfg-units');
  if(!sel) return;
  sel.value = unitMode();
  sel.onchange = ()=> setUnitMode(sel.value);
})();

async function refresh(){
  let c,s;
  try{
    c=await(await fetch('/api/c/state')).json();
    s=await(await fetch('/api/state')).json();
  }catch(e){
    stateTag.innerHTML='<span class="err">Connection lost — retrying</span>';
    return;
  }

  // Connection state tag (top right of header)
  const stateLabels={
    idle:'<span class="muted">Standby</span>',
    arming:'<span class="warn">Connecting…</span>',
    armed:'<span class="ok">Ready</span>',
    disarming:'<span class="warn">Stopping…</span>',
    error:'<span class="err">Fault — tap to reset</span>',
  };
  stateTag.innerHTML=stateLabels[c.phase_c]||c.phase_c;
  const autoInd=document.getElementById('auto-indicator');
  if(autoInd) autoInd.style.display=(c.config&&c.config.auto_stop_enabled)?'inline-block':'none';

  const armed=c.phase_c==='armed';
  const repActive=c.athletic_phase==='resist'||c.athletic_phase==='return';

  // Single phase-aware primary action — Arm rig → Start/Next rep → Stop → Reset
  if(primaryBtn){
    primaryBtn.classList.remove('danger','warn');
    if(c.phase_c==='error'){
      primaryBtn.textContent='Reset';
      primaryBtn.classList.add('warn');
      primaryBtn.disabled=false;
    }else if(!armed){
      primaryBtn.textContent='Arm rig';
      primaryBtn.disabled=false;
    }else if(repActive){
      primaryBtn.textContent='Stop';
      primaryBtn.classList.add('danger');
      primaryBtn.disabled=false;
    }else{
      primaryBtn.textContent=((window._lastReps||[]).length>0)?'Next rep':'Start rep';
      primaryBtn.disabled=!(armed && c.athletic_mode);
    }
  }

  // Live big numbers — always live from instantaneous telemetry
  const rpm=Math.abs(s.speed_rpm||0);
  const speedMps=rpm*0.00576;
  const torquePct=Math.abs(s.torque_pct||0);
  const forceN=(torquePct/5.64)*9.81;
  const powerW=forceN*speedMps;
  // Reps + chart data — fetched here so ipForTile (below) can reference rj.
  let rj={reps:[],in_progress:null,count:0};
  if(c.athletic_mode){
    try{rj=await(await fetch('/api/c/athletic/reps')).json();}catch(e){}
  }
  // Tile values driven by the user's per-tile metric choice (§16.7)
  const ipForTile = (rj && rj.in_progress) || (window._lastReps && window._lastReps[window._lastReps.length-1]) || null;
  function tileValue(tile){
    const m = currentTileMetric(tile);
    switch(m.id){
      case 'live_speed': return [speedMps, 2, m.u, 'primary'];
      case 'peak_speed': return [(ipForTile?ipForTile.peak_speed_mps:0)||0, 2, m.u, 'primary'];
      case 'avg_speed':  return [(ipForTile?ipForTile.avg_speed_mps:0)||0, 2, m.u, 'primary'];
      case 'live_force': return [forceN, 0, m.u, 'secondary'];
      case 'peak_force': return [(ipForTile?ipForTile.peak_force_n:0)||0, 0, m.u, 'secondary'];
      case 'avg_force':  return [(ipForTile?ipForTile.avg_force_n:0)||0, 0, m.u, 'secondary'];
      case 'live_power': return [powerW, 0, m.u, 'secondary'];
      case 'peak_power': return [(ipForTile?ipForTile.peak_power_w:0)||0, 0, m.u, 'secondary'];
      case 'avg_power':  return [(ipForTile?ipForTile.avg_power_w:0)||0, 0, m.u, 'secondary'];
      case 'cable_out':  return [(c.extension_m!=null?c.extension_m:0), 2, m.u, 'secondary'];
      case 'max_ext':    return [(ipForTile?ipForTile.max_extension_m:0)||0, 2, m.u, 'secondary'];
      case 'duration':   return [(ipForTile?ipForTile.duration_s:0)||0, 2, m.u, 'secondary'];
      default: return [0, 0, '', 'secondary'];
    }
  }
  for(const tile of ['speed','force','power','ext']){
    const [rawVal, dec, rawUnit, cls] = tileValue(tile);
    const [val, unit] = convertUnit(rawVal, rawUnit);
    document.getElementById('stat-'+tile).innerHTML =
      '<span class="num">'+(val||0).toFixed(dec)+'</span><span class="unit">'+unit+'</span>';
  }
  // (Cable-out tile is now driven by the tap-cycle metric system above.)

  // Reps + chart
  const ip=rj.in_progress;
  const reps=rj.reps||[];

  // Session stats: rep count, best speed/power, total work, drill timer
  // Drill timer tracks elapsed wall-clock since athletic_mode flipped True.
  if(c.athletic_mode){
    if(drillStartedAt===null) drillStartedAt=Date.now();
  } else {
    drillStartedAt=null;
  }
  const liveCount = reps.length + (ip?1:0);
  ssReps.textContent=liveCount;
  if(reps.length || ip){
    const allReps = reps.concat(ip?[ip]:[]);
    const bestV = Math.max(...allReps.map(r=>r.peak_speed_mps||0));
    const bestP = Math.max(...allReps.map(r=>r.peak_power_w||0));
    const totalWork = reps.reduce((s,r)=>s+(r.work_j||0),0);  // exclude in-progress (no work yet)
    ssBestSpeed.classList.remove('muted');
    ssBestPower.classList.remove('muted');
    ssBestSpeed.textContent=bestV.toFixed(2)+' m/s';
    ssBestPower.textContent=bestP.toFixed(0)+' W';
    if(totalWork>0){
      ssWork.classList.remove('muted');
      ssWork.textContent=totalWork.toFixed(0)+' J';
    } else {
      ssWork.classList.add('muted'); ssWork.textContent='–';
    }
  } else {
    ssBestSpeed.classList.add('muted'); ssBestSpeed.textContent='–';
    ssBestPower.classList.add('muted'); ssBestPower.textContent='–';
    ssWork.classList.add('muted'); ssWork.textContent='–';
  }
  if(drillStartedAt!==null){
    const sec=Math.floor((Date.now()-drillStartedAt)/1000);
    const mm=Math.floor(sec/60), ss=sec%60;
    ssTimer.classList.remove('muted');
    ssTimer.textContent=mm+':'+(ss<10?'0':'')+ss;
  } else {
    ssTimer.classList.add('muted'); ssTimer.textContent='–';
  }

  // Track corridor + drill-start time for chart + session stats
  lastCorridor=(c.config && c.config.return_distance_m)||0;
  const chartOpts={corridor_m: lastCorridor};

  // Chart visibility logic
  if(ph==='resist' && ip){
    // Live: show in-progress samples
    try{
      const sj=await(await fetch('/api/c/athletic/rep/'+ip.rep_idx+'/samples?include_in_progress=1')).json();
      if(sj.samples && sj.samples.length>=2){ renderLiveChart(sj.samples, chartOpts); }
    }catch(e){}
  } else if(prevPhase==='resist' && ph==='return' && reps.length){
    // Just finished a rep — show full curve
    try{
      const last=reps[reps.length-1];
      const sj=await(await fetch('/api/c/athletic/rep/'+last.rep_idx+'/samples')).json();
      cachedSamples=sj.samples||[];
      renderLiveChart(cachedSamples, chartOpts);
    }catch(e){}
  } else if(ph==='return' && cachedSamples.length){
    // Stay on the cached curve through recover
    renderLiveChart(cachedSamples, chartOpts);
  } else if(ph==='ready'){
    // Hide chart between reps (athlete is at start, ready to go again)
    if(cachedSamples.length){
      renderLiveChart(cachedSamples, chartOpts);
    } else {
      hideChart();
    }
  } else {
    // off / standby — no chart
    hideChart();
    cachedSamples=[];
  }

  // Recent reps list + compare selects
  renderRepsList(reps);
  populateRepSelects(reps);

  // Cache reps for click-to-render lookup; auto-show latest rep's run detail
  window._lastReps = reps;
  if(reps.length){
    const latest = reps[reps.length-1];
    renderRunDetail(latest);
    // Lazy-load samples for the latest rep so Steps/F-V tabs work without click
    if(!window._lastSamples || window._lastSamplesRepIdx !== latest.rep_idx){
      try{
        const sj = await(await fetch('/api/c/athletic/rep/'+latest.rep_idx+'/samples')).json();
        window._lastSamples = sj.samples||[];
        window._lastSamplesRepIdx = latest.rep_idx;
        renderTab(activeTab);  // re-render in case Steps/F-V is open
      }catch(e){}
    }
  } else {
    renderRunDetail(null);
    window._lastSamples = [];
  }

  // §8 audio cues + §12 rest timer wired into phase transitions
  if(prevPhase !== ph){
    if(ph === 'resist' && (prevPhase === 'ready' || prevPhase === 'return')){
      SOUNDS.rep_start();
      setSpeedMetric(0);   // headline Speed shows live value during a rep
    }
    if(prevPhase === 'resist' && ph === 'return'){
      SOUNDS.rep_end();
      setSpeedMetric(1);   // rep done — headline Speed jumps to the peak

      // Kick off the rest timer if a rest interval is configured
      const restS = (c.config && c.config.rest_interval_s) || 0;
      if(restS > 0) startRestTimer(restS);
    }
    if(ph === 'off' && prevPhase !== 'off'){
      // Drill ended (athletic_mode went False) — clear any active rest timer
      stopRestTimer();
    }
  }
  prevPhase=ph;
}
// ---- Resistance curve editor (draggable 6-point graph) ----
(function(){
  var svg=document.getElementById('curve-svg');
  if(!svg) return;
  var meta=document.getElementById('curve-meta');
  var N=6,VW=300,VH=160,PL=34,PR=12,PT=12,PB=24;
  var PW=VW-PL-PR,PH=VH-PT-PB,KGMAX=10;
  var axis='off';
  var kg=[5,5,5,5,5,5];
  function ptx(i){ return PL+(i/(N-1))*PW; }
  function pty(v){ return PT+(1-v/KGMAX)*PH; }
  function y2kg(y){ var v=(1-(y-PT)/PH)*KGMAX; return Math.max(0,Math.min(KGMAX,v)); }
  function render(){
    var s='';
    s+='<rect x="'+PL+'" y="'+PT+'" width="'+PW+'" height="'+PH+'" fill="none" stroke="#2c2c34"/>';
    for(var k=0;k<=KGMAX;k+=5){
      var gy=pty(k);
      s+='<line x1="'+PL+'" y1="'+gy+'" x2="'+(PL+PW)+'" y2="'+gy+'" stroke="#22222a"/>';
      s+='<text x="'+(PL-5)+'" y="'+(gy+3)+'" fill="#8a8a96" font-size="9" text-anchor="end">'+k+'</text>';
    }
    var xl=(axis==='velocity')?'cable speed (m/s)':(axis==='distance')?'distance (m)':'';
    s+='<text x="'+(PL+PW/2)+'" y="'+(VH-7)+'" fill="#8a8a96" font-size="9" text-anchor="middle">'+xl+'</text>';
    var poly='';
    for(var i=0;i<N;i++){ poly+=ptx(i)+','+pty(kg[i])+' '; }
    s+='<polyline points="'+poly+'" fill="none" stroke="#5b8bff" stroke-width="2"/>';
    for(var j=0;j<N;j++){
      s+='<circle cx="'+ptx(j)+'" cy="'+pty(kg[j])+'" r="7" fill="#5b8bff" stroke="#0a0a0c" stroke-width="1.5"/>';
      s+='<text x="'+ptx(j)+'" y="'+(pty(kg[j])-10)+'" fill="#f0f0f3" font-size="9" text-anchor="middle">'+kg[j].toFixed(1)+'</text>';
    }
    svg.innerHTML=s;
    svg.style.opacity=(axis==='off')?0.4:1;
  }
  function postCurve(){
    fetch('/api/c/athletic/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({curve_axis:axis,curve_kg:kg})}).catch(function(){});
  }
  function svgPt(e){
    var r=svg.getBoundingClientRect();
    return {x:(e.clientX-r.left)/r.width*VW,y:(e.clientY-r.top)/r.height*VH};
  }
  var drag=-1;
  svg.addEventListener('pointerdown',function(e){
    if(axis==='off') return;
    var p=svgPt(e),best=-1,bd=1e9;
    for(var i=0;i<N;i++){ var d=Math.abs(ptx(i)-p.x); if(d<bd){bd=d;best=i;} }
    if(best>=0&&bd<26){ drag=best; try{svg.setPointerCapture(e.pointerId);}catch(_){} }
  });
  svg.addEventListener('pointermove',function(e){
    if(drag<0) return;
    kg[drag]=Math.round(y2kg(svgPt(e).y)*2)/2;
    render();
  });
  function endDrag(){ if(drag>=0){ drag=-1; postCurve(); } }
  svg.addEventListener('pointerup',endDrag);
  svg.addEventListener('pointercancel',endDrag);
  function setBtns(){
    var bs=document.querySelectorAll('.curve-axis-btn');
    for(var i=0;i<bs.length;i++){
      var on=bs[i].getAttribute('data-axis')===axis;
      bs[i].style.background=on?'#5b8bff':'#16161a';
      bs[i].style.color=on?'#fff':'#8a8a96';
    }
    if(meta){
      meta.textContent=(axis==='distance')?'Resistance shaped over distance (0 to resist distance). Drag the points.'
        :(axis==='velocity')?'Resistance shaped over cable speed (0 to 8 m/s). Drag the points.'
        :'Off — flat working resistance is used.';
    }
  }
  var abs=document.querySelectorAll('.curve-axis-btn');
  for(var bi=0;bi<abs.length;bi++){
    abs[bi].onclick=function(){ axis=this.getAttribute('data-axis'); setBtns(); render(); postCurve(); };
  }
  fetch('/api/c/state').then(function(r){return r.json();}).then(function(j){
    var cfg=j.config||{};
    if(cfg.curve_axis) axis=cfg.curve_axis;
    if(cfg.curve_kg&&cfg.curve_kg.length===N) kg=cfg.curve_kg.slice();
    setBtns(); render();
  }).catch(function(){ setBtns(); render(); });
})();

refresh();setInterval(refresh,500);

if('serviceWorker' in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(()=>{});});}
</script>
</body></html>
"""


# ============================================================================
# SETUP_HTML — deep configuration screen (served at /setup). Holds the things
# a coach changes before/between sessions, not during: athlete admin, template
# manager, resistance-curve editor, session-default numbers, units, audio.
# The live /coach screen stays slim and links here via its header cog.
# ============================================================================
SETUP_HTML = """<!doctype html>
<html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PPA · Setup</title>
<link rel="icon" type="image/png" href="/static/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0a1020; --fg:#eef2fb; --muted:#818eae; --accent:#5b8bff;
          --accent-soft:rgba(91,139,255,0.16);
          --bad:#ff5a5f; --good:#33d17a; --warn:#ff9440;
          --card:#121a33; --line:#243157;
          --raised:#182244; --line-strong:#33436b; --ink-faint:#56618a;
          --warm:#ff9440; --warm-soft:rgba(255,148,64,.16);
          --cool:#2bd0e2; --cool-soft:rgba(43,208,226,.16);
          --state:var(--warm); --state-soft:var(--warm-soft); }
  *{box-sizing:border-box;font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif}
  body{margin:0;background:var(--bg);color:var(--fg);font-size:16px}
  .wrap{max-width:1200px;margin:0 auto;padding:18px;padding-bottom:48px}
  .topbar{display:flex;align-items:center;justify-content:space-between;
          margin-bottom:18px;gap:12px;flex-wrap:wrap}
  h1{margin:0;font-size:22px;letter-spacing:0.05em;font-weight:700}
  h1 .accent{color:var(--accent)}
  .brand-logo{height:52px;width:auto;border-radius:0;display:inline-block;vertical-align:middle;margin-right:12px}
  h1 .sub{color:var(--muted);font-weight:600;font-size:14px;letter-spacing:0.12em;
          text-transform:uppercase;margin-left:8px}
  a.livelink{display:inline-flex;align-items:center;gap:6px;text-decoration:none;
             background:var(--accent);color:#0a0a0c;font-weight:700;font-size:14px;
             padding:10px 16px;border-radius:9px;min-height:44px}
  button{appearance:none;padding:11px 16px;font-size:14px;background:var(--accent);
         color:#0a0a0c;border:0;border-radius:9px;font-weight:600;cursor:pointer;min-height:44px}
  button.secondary{background:transparent;color:var(--accent);border:1px solid var(--accent)}
  button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
  input[type=number],input[type=text],select{padding:9px;font-size:14px;background:#0a0a0c;
         color:var(--fg);border:1px solid var(--line);border-radius:6px}
  label{display:block;color:var(--muted);font-size:11px;letter-spacing:0.12em;
        text-transform:uppercase;margin:0 0 4px}
  .setup-tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
  .setup-tab{padding:10px 18px;font-size:13px;font-weight:700;letter-spacing:0.04em;
             background:var(--card);color:var(--muted);border:1px solid var(--line);
             border-radius:10px;cursor:pointer;min-height:44px}
  .setup-tab:hover{color:var(--fg)}
  .setup-tab.active{background:var(--accent);color:#0a0a0c;border-color:var(--accent)}
  section[data-panel][hidden]{display:none}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:18px;margin-bottom:14px}
  .card h2{margin:0 0 14px;font-size:16px;font-weight:600}
  .meta{color:var(--muted);font-size:11px;letter-spacing:0.06em}
  .field{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
  .field input,.field select{width:100%}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .divider{height:1px;background:var(--line);margin:14px 0}
  .ath-section-h{font-size:11px;letter-spacing:0.14em;text-transform:uppercase;
                 color:var(--accent);margin:14px 0 8px;font-weight:600}
  .num-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .checkrow{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .checkrow label{display:flex;align-items:center;gap:6px;font-size:13px;
                  text-transform:none;letter-spacing:0;color:var(--fg);margin:0}
  .curve-axis-btn{flex:1;padding:8px;border:none;border-radius:5px;font-size:11px;
                  cursor:pointer;min-height:36px}
  .seg-btn{flex:1;padding:9px;font-size:12px;font-weight:600;cursor:pointer;
           background:#0a0a0c;color:var(--muted);border:1px solid var(--line);
           border-radius:6px;min-height:38px}
  .seg-btn:hover{color:var(--fg);border-color:var(--accent)}
  .seg-btn.active{background:var(--accent);color:#0a0a0c;border-color:var(--accent)}
  .hist-editor-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .hist-list{display:flex;flex-direction:column;gap:8px;max-height:300px;overflow-y:auto}
  .hist-row{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;
            padding:10px 12px;background:#0a0a0c;border:1px solid var(--line);
            border-radius:6px;font-size:13px;cursor:pointer}
  .hist-row:hover{border-color:var(--accent)}
  .hist-date{color:var(--muted);font-variant-numeric:tabular-nums;font-size:12px}
  .hist-summary{color:var(--fg);font-variant-numeric:tabular-nums}
  .hist-summary .sep{color:#3a3a44;margin:0 6px}
  .hist-reps{color:var(--accent);font-weight:600;font-size:11px}
  .hist-empty{color:var(--muted);text-align:center;padding:18px;font-size:13px}
  .estop-btn{position:fixed;top:14px;right:14px;width:66px;height:66px;border-radius:50%;
             border:none;cursor:pointer;z-index:100;
             display:flex;align-items:center;justify-content:center;
             background:radial-gradient(circle at 50% 35%,#e84a4a 0%,#cc1f1f 38%,#9c0f0f 72%,#6b0808 100%);
             box-shadow:0 0 0 5px #7a0c0c,0 0 16px 6px rgba(255,80,80,0.35),
                        0 10px 22px rgba(0,0,0,0.55),
                        inset 0 5px 13px rgba(255,255,255,0.5),
                        inset 0 -13px 22px rgba(0,0,0,0.5);
             color:#fff;font-size:15px;font-weight:800;letter-spacing:0.05em;
             text-shadow:0 1px 3px rgba(0,0,0,0.6);transition:transform 100ms}
  .estop-btn::before{content:"";position:absolute;left:17%;top:11%;width:66%;height:44%;
             border-radius:50%;pointer-events:none;
             background:linear-gradient(rgba(255,255,255,0.6),rgba(255,255,255,0.04))}
  .estop-btn span{position:relative;z-index:1}
  .estop-btn:hover,.estop-btn:active{transform:scale(1.06)}
</style>
</head><body>

<button id="estop-btn" class="estop-btn" title="Emergency stop" aria-label="Emergency stop"><span>STOP</span></button>

<div class="wrap">
  <div class="topbar">
    <h1><img class="brand-logo" src="/static/ppa-logo.png" alt="Plummer Performance Academy"><span class="accent">·</span> Sprint Trainer<span class="sub">Setup</span></h1>
    <a class="livelink" href="/coach">&larr; Live session</a>
  </div>

  <nav class="setup-tabs" id="setup-tabs">
    <button class="setup-tab active" data-tab="athletes">Athletes</button>
    <button class="setup-tab" data-tab="drills">Drills &amp; Presets</button>
    <button class="setup-tab" data-tab="reports">Reports</button>
    <button class="setup-tab" data-tab="settings">Settings</button>
    <button class="setup-tab" data-tab="calibrate">Calibrate</button>
  </nav>

  <section data-panel="athletes">
      <div class="card">
        <h2>Athletes</h2>
        <div class="field">
          <label>Add athlete</label>
          <div class="row">
            <input type="text" id="new-athlete-name" placeholder="Name" style="flex:1">
            <button id="new-athlete-btn" class="secondary">+ add</button>
          </div>
        </div>
        <div class="divider"></div>
        <div class="field">
          <label>View / edit athlete</label>
          <select id="setup-athlete-select"><option value="">— Pick an athlete —</option></select>
        </div>
        <div id="ath-editor" style="display:none">
          <div class="ath-section-h">Edit details</div>
          <div class="hist-editor-grid">
            <div class="field"><label>Name</label><input type="text" id="edit-name"></div>
            <div class="field"><label>Body mass (kg)</label><input type="number" id="edit-mass" step="0.1" min="20" max="200"></div>
            <div class="field"><label>Position</label>
              <select id="edit-position">
                <option value="">—</option><option value="back">Back</option>
                <option value="forward">Forward</option><option value="outside_back">Outside back</option>
                <option value="other">Other</option>
              </select>
            </div>
            <div class="field"><label>Sport</label>
              <select id="edit-sport">
                <option value="">—</option><option value="rugby_union">Rugby Union</option>
                <option value="rugby_league">Rugby League</option><option value="rugby_sevens">Rugby Sevens</option>
                <option value="afl">AFL</option><option value="soccer">Soccer</option>
                <option value="basketball">Basketball</option><option value="track">Track</option>
                <option value="other">Other</option>
              </select>
            </div>
            <div class="field"><label>Level</label>
              <select id="edit-level">
                <option value="">—</option><option value="developmental">Developmental</option>
                <option value="club">Club</option><option value="semi_pro">Semi-pro</option>
                <option value="pro">Pro</option><option value="international">International</option>
              </select>
            </div>
            <div class="field"><label>Group / squad</label><input type="text" id="edit-group" placeholder="e.g. 1st XV"></div>
            <div class="field"><label>Tags (comma-separated)</label><input type="text" id="edit-tags" placeholder="e.g. winger, return-to-play"></div>
          </div>
          <button class="secondary" id="edit-save">Save athlete</button>

          <div class="ath-section-h">Progression</div>
          <div class="row" style="gap:8px">
            <select id="hist-metric" style="flex:1">
              <option value="peak_speed_mps">Peak speed (m/s)</option>
              <option value="peak_power_w">Peak power (W)</option>
              <option value="pmax_rel_wkg">Pmax / kg (W/kg)</option>
              <option value="f0_rel_nkg">F0 / kg (N/kg)</option>
              <option value="v0_mps">V0 (m/s)</option>
              <option value="time_to_max_v_s">Time to peak v (s)</option>
              <option value="step_freq_hz">Stride freq (Hz)</option>
            </select>
            <select id="hist-agg" style="flex:1">
              <option value="MAX" selected>Best of session</option>
              <option value="AVG">Session avg</option>
              <option value="MIN">Worst of session</option>
            </select>
          </div>
          <svg id="hist-chart" viewBox="0 0 380 140" preserveAspectRatio="none" height="140"
               style="width:100%;background:#0a0a0c;border:1px solid var(--line);border-radius:6px;margin-top:8px"></svg>
          <div class="meta" id="hist-trend" style="margin-top:6px"></div>

          <div class="ath-section-h">Sessions</div>
          <div class="hist-list" id="hist-sessions"></div>
        </div>
      </div>
  </section>

  <section data-panel="drills" hidden>
      <div class="card">
        <h2>Templates</h2>
        <div class="field">
          <label>Saved templates</label>
          <div class="row" style="gap:6px">
            <select id="tpl-select" style="flex:1"><option value="">— Pick a template —</option></select>
            <button class="ghost" id="tpl-load">Load</button>
            <button class="ghost" id="tpl-delete" title="Delete selected" style="color:var(--bad)">×</button>
          </div>
        </div>
        <div class="field">
          <label>Save current rig config as template</label>
          <div class="row" style="gap:6px">
            <input type="text" id="tpl-new-name" placeholder="Template name" style="flex:1">
            <button class="secondary" id="tpl-save">Save</button>
          </div>
        </div>
        <div class="meta">Templates store the rig's current mode, drill, resistance and curve.</div>
      </div>

      <div class="card">
        <h2>Resistance curve</h2>
        <div class="field">
          <label>Curve library</label>
          <div class="row" style="gap:6px">
            <select id="curve-lib-select" style="flex:1"></select>
            <button type="button" class="ghost" id="curve-delete" title="Delete selected curve" style="color:var(--bad)">×</button>
          </div>
        </div>
        <div class="row" style="gap:6px;margin-bottom:6px">
          <button type="button" class="curve-axis-btn" data-axis="off">Off</button>
          <button type="button" class="curve-axis-btn" data-axis="distance">By distance</button>
          <button type="button" class="curve-axis-btn" data-axis="velocity">By velocity</button>
        </div>
        <svg id="curve-svg" width="100%" viewBox="0 0 300 160"
             style="display:block;border:1px solid var(--line);border-radius:6px;touch-action:none"></svg>
        <div class="meta" id="curve-meta" style="margin-top:4px">Off — flat working resistance is used.</div>
        <div class="field" style="margin-top:10px">
          <label>Save edited curve</label>
          <div class="row" style="gap:6px">
            <input type="text" id="curve-name" placeholder="Curve name" style="flex:1">
            <button type="button" class="ghost" id="curve-save">Save</button>
            <button type="button" class="secondary" id="curve-save-as">Save as new</button>
          </div>
        </div>
        <div class="meta" id="curve-save-msg" style="min-height:14px"></div>
      </div>
  </section>

  <section data-panel="reports" hidden>
      <div class="card">
        <h2>Reports</h2>
        <p class="meta" style="line-height:1.7">Rep comparison, Force&ndash;Velocity profiles and longitudinal trends will live here. Wired in a later phase.</p>
      </div>
  </section>

  <section data-panel="settings" hidden>
      <div class="card">
        <h2>Session defaults</h2>
        <div class="num-grid">
          <div class="field"><label>Gear / pulley</label>
            <select id="cfg-gear">
              <option value="1" selected>Gear 1 — no pulley</option>
              <option value="2">Gear 2 — pulley (2×)</option>
            </select>
          </div>
          <div class="field"><label>Recovery resistance (kg)</label>
            <input type="number" id="cfg-return" value="1" step="0.1" min="0" max="5"></div>
          <div class="field"><label>Recovery distance (m)</label>
            <input type="number" id="cfg-dist" value="0.5" step="0.1" min="0.1" max="5"></div>
          <div class="field"><label>Ease-in speed (kg/s)</label>
            <input type="number" id="cfg-slew" value="12" step="1" min="1" max="60"></div>
          <div class="field"><label>Rest interval (s)</label>
            <input type="number" id="cfg-rest" value="180" step="15" min="0" max="600"></div>
          <div class="field"><label>Pre-drill countdown (s)</label>
            <input type="number" id="cfg-countdown" value="5" step="1" min="0" max="10"></div>
          <div class="field"><label>Max resistance (kg)</label>
            <input type="number" id="cfg-kg-limit" value="30" step="1" min="1" max="53"></div>
        </div>
        <div class="meta" style="margin-top:6px">Hard ceiling for any commanded load (working, curve and velocity-cap boost). Capped at 53 kg — the rig's 300% torque limit.</div>
        <div class="field" style="margin-top:12px">
          <label>Return speed</label>
          <div id="return-speed-seg" style="display:flex;gap:6px">
            <button type="button" class="seg-btn" data-speed="1">Slow · 1 m/s</button>
            <button type="button" class="seg-btn" data-speed="2">Medium · 2 m/s</button>
            <button type="button" class="seg-btn" data-speed="3">Fast · 3 m/s</button>
          </div>
        </div>
        <div class="meta" style="margin-top:6px">Speed the cable reels back in once the athlete is past the resist band or inside the recovery zone.</div>
        <div class="divider"></div>
        <div class="checkrow">
          <label><input type="checkbox" id="cfg-auto-stop" checked> Full Auto rep detection</label>
        </div>
        <div class="meta" style="margin-top:6px">Rep auto-ends ~1s after the athlete stops (cable below 0.2 m/s). Turn off for flying sprints with a long coast-down.</div>
        <div class="divider"></div>
        <button type="button" class="ghost" id="reset-tips" style="width:100%">Reset onboarding tips</button>
        <div class="meta" style="margin-top:6px">Brings back the per-mode intro cards on the live screen.</div>
      </div>

      <div class="card">
        <h2>Display &amp; audio</h2>
        <div class="field">
          <label>Display units</label>
          <select id="cfg-units">
            <option value="metric" selected>Metric (m, kg, m/s)</option>
            <option value="imperial">Imperial (ft, lb, mph)</option>
          </select>
          <div class="meta" style="margin-top:4px">Per-display only — exports stay metric</div>
        </div>
        <div class="field">
          <label>Audio</label>
          <div class="checkrow">
            <label><input type="checkbox" id="audio-master" checked> Sound enabled</label>
            <label><input type="checkbox" id="audio-rep" checked> Rep tones</label>
            <label><input type="checkbox" id="audio-rest" checked> Rest cues</label>
          </div>
        </div>
      </div>

  </section>

  <section data-panel="calibrate" hidden>
      <div class="card">
        <h2>Rig maintenance</h2>
        <div class="meta" style="margin-bottom:10px">Cable detached, operator at the rig. The red STOP button kills the motor at any point.</div>
        <div class="field">
          <label>Motor</label>
          <button type="button" class="ghost" id="maint-motor" style="width:100%">Motor: off</button>
        </div>
        <div class="field">
          <label>Jog cable — hold to move</label>
          <div style="display:flex;gap:6px">
            <button type="button" class="seg-btn" id="jog-retract">&#9664; Retract</button>
            <button type="button" class="seg-btn" id="jog-extend">Extend &#9654;</button>
          </div>
          <div class="meta" id="jog-pos" style="margin-top:6px">Position: —</div>
        </div>
        <div class="divider"></div>
        <div class="field">
          <label>Position calibration</label>
          <div class="meta" style="margin-bottom:8px">Jog the cable fully in, tap Mark zero. Pull it out a measured distance, type that distance, tap Set calibration.</div>
          <button type="button" class="ghost" id="cal-zero" style="width:100%;margin-bottom:8px">Mark zero (home)</button>
          <div style="display:flex;gap:6px">
            <input type="number" id="cal-distance" placeholder="Distance pulled out (m)" step="0.1" min="0.5" max="60" style="flex:1">
            <button type="button" class="secondary" id="cal-set">Set calibration</button>
          </div>
          <div class="meta" id="cal-msg" style="margin-top:6px;min-height:14px"></div>
          <div style="display:flex;gap:6px;align-items:center;margin-top:4px">
            <span class="meta" id="cal-current" style="flex:1"></span>
            <button type="button" class="ghost" id="cal-reset" style="color:var(--bad)">Reset to default</button>
          </div>
        </div>
      </div>
  </section>
</div>

<script>
// ---- E-stop (every screen) ----
document.getElementById('estop-btn').onclick=async()=>{
  try{ await fetch('/api/c/estop',{method:'POST'}); }catch(e){}
};

function postConfig(body){
  return fetch('/api/c/athletic/config',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).catch(()=>{});
}

// ---- Athletes ----
async function loadAthletes(){
  try{
    const r=await fetch('/api/athletes');
    if(!r.ok) return;
    const list=await r.json();
    const sel=document.getElementById('setup-athlete-select');
    const cur=sel.value;
    sel.innerHTML='<option value="">— Pick an athlete —</option>'+
      list.map(a=>'<option value="'+a.id+'"'+(String(a.id)===cur?' selected':'')+'>'+a.name+'</option>').join('');
  }catch(e){}
}
document.getElementById('new-athlete-btn').onclick=async()=>{
  const name=document.getElementById('new-athlete-name').value.trim();
  if(!name){ alert('Type a name first'); return; }
  const r=await fetch('/api/athletes',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  if(r.ok){
    document.getElementById('new-athlete-name').value='';
    const j=await r.json();
    await loadAthletes();
    document.getElementById('setup-athlete-select').value=String(j.id);
    loadAthleteHistory();
  }else{ alert("Couldn't add athlete ("+r.status+")"); }
};
document.getElementById('setup-athlete-select').onchange=loadAthleteHistory;

let _currentAthlete=null;
async function loadAthleteHistory(){
  const aid=document.getElementById('setup-athlete-select').value;
  const editor=document.getElementById('ath-editor');
  if(!aid){ editor.style.display='none'; return; }
  let h;
  try{ h=await(await fetch('/api/athletes/'+aid+'/history')).json(); }
  catch(e){ return; }
  if(!h.athlete) return;
  _currentAthlete=h.athlete;
  editor.style.display='block';
  document.getElementById('edit-name').value=h.athlete.name||'';
  document.getElementById('edit-mass').value=h.athlete.body_mass_kg??'';
  document.getElementById('edit-position').value=h.athlete.position_group||'';
  document.getElementById('edit-sport').value=h.athlete.sport||'';
  document.getElementById('edit-level').value=h.athlete.level||'';
  document.getElementById('edit-group').value=h.athlete.squad_group||'';
  document.getElementById('edit-tags').value=h.athlete.tags||'';
  const list=document.getElementById('hist-sessions');
  if(!h.sessions.length){
    list.innerHTML='<div class="hist-empty">No sessions yet</div>';
  }else{
    list.innerHTML=h.sessions.map(s=>{
      const date=(s.started_at||'').split('T')[0];
      const drill=s.drill||'?';
      const peakV=s.best_speed_mps!=null?s.best_speed_mps.toFixed(2)+' m/s':'–';
      const peakP=s.best_power_w!=null?s.best_power_w.toFixed(0)+' W':'–';
      return '<div class="hist-row"><div class="hist-date">'+date+'</div>'+
        '<div class="hist-summary">'+peakV+'<span class="sep">·</span>'+peakP+
        '<span class="sep">·</span>'+drill+'</div>'+
        '<div class="hist-reps">'+s.rep_count+' rep'+(s.rep_count===1?'':'s')+'</div></div>';
    }).join('');
  }
  loadProgressionChart(aid);
}
async function saveAthleteEdit(){
  if(!_currentAthlete) return;
  const body={
    name:document.getElementById('edit-name').value.trim(),
    body_mass_kg:parseFloat(document.getElementById('edit-mass').value)||null,
    position_group:document.getElementById('edit-position').value||null,
    sport:document.getElementById('edit-sport').value||null,
    level:document.getElementById('edit-level').value||null,
    squad_group:document.getElementById('edit-group').value.trim()||null,
    tags:document.getElementById('edit-tags').value.trim()||null,
  };
  Object.keys(body).forEach(k=>{ if(body[k]==null||body[k]==='') delete body[k]; });
  try{
    const r=await fetch('/api/athletes/'+_currentAthlete.id,{method:'PUT',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok){ alert("Couldn't save"); return; }
    await loadAthletes();
    await loadAthleteHistory();
  }catch(e){ alert('Save failed'); }
}
document.getElementById('edit-save').onclick=saveAthleteEdit;

async function loadProgressionChart(aid){
  const metric=document.getElementById('hist-metric').value;
  const agg=document.getElementById('hist-agg').value;
  let p;
  try{ p=await(await fetch('/api/athletes/'+aid+'/progression?metric='+metric+'&agg='+agg)).json(); }
  catch(e){ return; }
  const points=p.points||[];
  const svg=document.getElementById('hist-chart');
  const trend=document.getElementById('hist-trend');
  if(points.length<2){
    svg.innerHTML='<text x="190" y="70" fill="#5a5a64" text-anchor="middle" font-size="12">Need &#8805;2 sessions for a trend line</text>';
    trend.textContent=points.length===1?('Latest: '+(points[0].value!=null?points[0].value.toFixed(2):'–')):'';
    return;
  }
  const W=380,H=140,PADL=36,PADR=12,PADT=14,PADB=22;
  const vals=points.map(pt=>pt.value);
  const vMin=Math.min(...vals),vMax=Math.max(...vals);
  const range=Math.max(vMax-vMin,vMax*0.05,0.01);
  const yMin=vMin-range*0.15,yMax=vMax+range*0.15;
  const xs=i=>PADL+(i/(points.length-1))*(W-PADL-PADR);
  const ys=v=>H-PADB-((v-yMin)/(yMax-yMin))*(H-PADT-PADB);
  let grid='';
  for(let i=0;i<=3;i++){
    const y=PADT+i*(H-PADT-PADB)/3;
    const v=yMax-(yMax-yMin)*(i/3);
    grid+='<line x1="'+PADL+'" y1="'+y+'" x2="'+(W-PADR)+'" y2="'+y+'" stroke="#1f1f26"/>';
    grid+='<text x="'+(PADL-4)+'" y="'+(y+3)+'" fill="#5a5a64" font-size="9" text-anchor="end">'+v.toFixed(2)+'</text>';
  }
  const path=points.map((pt,i)=>(i?'L':'M')+xs(i).toFixed(1)+','+ys(pt.value).toFixed(1)).join(' ');
  const dots=points.map((pt,i)=>'<circle cx="'+xs(i).toFixed(1)+'" cy="'+ys(pt.value).toFixed(1)+'" r="3" fill="#5b8bff"/>').join('');
  svg.innerHTML=grid+'<path d="'+path+'" stroke="#5b8bff" stroke-width="2" fill="none"/>'+dots;
  const first=points[0].value,last=points[points.length-1].value;
  const delta=last-first;
  const pct=first!==0?(delta/Math.abs(first)*100):0;
  const dir=delta>0?'↑':(delta<0?'↓':'→');
  trend.textContent=first.toFixed(2)+' → '+last.toFixed(2)+' ('+dir+' '+Math.abs(delta).toFixed(2)+
    ', '+pct.toFixed(1)+'%) over '+points.length+' sessions';
}
document.getElementById('hist-metric').onchange=()=>{
  const aid=document.getElementById('setup-athlete-select').value;
  if(aid) loadProgressionChart(aid);
};
document.getElementById('hist-agg').onchange=()=>{
  const aid=document.getElementById('setup-athlete-select').value;
  if(aid) loadProgressionChart(aid);
};

// ---- Templates ----
async function loadTemplateList(){
  const sel=document.getElementById('tpl-select');
  try{
    const list=await(await fetch('/api/templates')).json();
    const cur=sel.value;
    sel.innerHTML='<option value="">— Pick a template —</option>'+
      list.map(t=>'<option value="'+t.id+'">'+t.name+'</option>').join('');
    if(cur) sel.value=cur;
    window._templates=list;
  }catch(e){}
}
document.getElementById('tpl-save').onclick=async()=>{
  const name=(document.getElementById('tpl-new-name').value||'').trim();
  if(!name){ alert('Type a template name first'); return; }
  let cfg={};
  try{ cfg=(await(await fetch('/api/c/state')).json()).config||{}; }catch(e){}
  try{
    const r=await fetch('/api/templates',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name,config:cfg})});
    if(!r.ok){ alert("Couldn't save template ("+r.status+')'); return; }
    document.getElementById('tpl-new-name').value='';
    await loadTemplateList();
  }catch(e){ alert('Network error'); }
};
document.getElementById('tpl-load').onclick=async()=>{
  const id=parseInt(document.getElementById('tpl-select').value,10);
  if(!id) return;
  try{
    const r=await fetch('/api/templates/'+id+'/load',{method:'POST'});
    if(!r.ok){ alert('Load failed'); return; }
    alert('Template loaded into the rig config.');
  }catch(e){ alert('Load failed'); }
};
document.getElementById('tpl-delete').onclick=async()=>{
  const id=parseInt(document.getElementById('tpl-select').value,10);
  if(!id) return;
  const name=(window._templates||[]).find(t=>t.id===id)?.name||'this template';
  if(!confirm('Delete template "'+name+'"?')) return;
  try{ await fetch('/api/templates/'+id,{method:'DELETE'}); await loadTemplateList(); }
  catch(e){ alert('Delete failed'); }
};

// ---- Session-default numbers → push to athletic config on change ----
[['cfg-return','return_kg'],['cfg-dist','return_distance_m'],['cfg-slew','slew_kg_per_s'],
 ['cfg-rest','rest_interval_s'],['cfg-countdown','countdown_s'],
 ['cfg-kg-limit','kg_limit']].forEach(([id,key])=>{
  const el=document.getElementById(id);
  el.addEventListener('change',()=>{
    const v=parseFloat(el.value);
    if(!isNaN(v)) postConfig({[key]:v});
  });
});
document.getElementById('cfg-gear').addEventListener('change',e=>
  postConfig({gear:parseInt(e.target.value,10)}));
document.getElementById('cfg-auto-stop').addEventListener('change',e=>
  postConfig({auto_stop_enabled:e.target.checked}));

// ---- Return-speed segmented control ----
(function(){
  const seg=document.getElementById('return-speed-seg');
  if(!seg) return;
  const btns=seg.querySelectorAll('.seg-btn');
  window._markReturnSpeed=function(v){
    btns.forEach(b=>b.classList.toggle('active',parseFloat(b.getAttribute('data-speed'))===v));
  };
  btns.forEach(b=>b.addEventListener('click',()=>{
    const v=parseFloat(b.getAttribute('data-speed'));
    window._markReturnSpeed(v);
    postConfig({return_speed_mps:v});
  }));
})();

// ---- Units + audio (localStorage, shared with /coach) ----
(()=>{
  const u=document.getElementById('cfg-units');
  u.value=localStorage.getItem('ppa.units')||'metric';
  u.onchange=()=>localStorage.setItem('ppa.units',u.value);
  const wire=(id,key)=>{
    const el=document.getElementById(id);
    el.checked=localStorage.getItem(key)!=='0';
    el.onchange=()=>localStorage.setItem(key,el.checked?'1':'0');
  };
  wire('audio-master','ppa.audio.master');
  wire('audio-rep','ppa.audio.rep');
  wire('audio-rest','ppa.audio.rest');
})();

// ---- Resistance curve editor + library (draggable 6-point graph, % of load) ----
(function(){
  var svg=document.getElementById('curve-svg');
  if(!svg) return;
  var meta=document.getElementById('curve-meta');
  var libSel=document.getElementById('curve-lib-select');
  var nameInp=document.getElementById('curve-name');
  var saveMsg=document.getElementById('curve-save-msg');
  var N=6,VW=300,VH=160,PL=40,PR=12,PT=12,PB=24;
  var PW=VW-PL-PR,PH=VH-PT-PB,PCTMAX=250;
  var axis='off';
  var pts=[100,100,100,100,100,100];  // % of working resistance
  var curves=[];        // saved library curves
  var selectedId=null;  // id of the loaded library curve (null = unsaved)
  function ptx(i){ return PL+(i/(N-1))*PW; }
  function pty(v){ return PT+(1-v/PCTMAX)*PH; }
  function y2pct(y){ var v=(1-(y-PT)/PH)*PCTMAX; return Math.max(0,Math.min(PCTMAX,v)); }
  function render(){
    var s='';
    s+='<rect x="'+PL+'" y="'+PT+'" width="'+PW+'" height="'+PH+'" fill="none" stroke="#2c2c34"/>';
    for(var k=0;k<=PCTMAX;k+=50){
      var gy=pty(k),ref=(k===100);
      s+='<line x1="'+PL+'" y1="'+gy+'" x2="'+(PL+PW)+'" y2="'+gy+'" stroke="'+(ref?'#5a5a66':'#22222a')+'"'+(ref?' stroke-dasharray="3 2"':'')+'/>';
      s+='<text x="'+(PL-5)+'" y="'+(gy+3)+'" fill="#8a8a96" font-size="9" text-anchor="end">'+k+'%</text>';
    }
    var xl=(axis==='velocity')?'cable speed (m/s)':(axis==='distance')?'distance (m)':'';
    s+='<text x="'+(PL+PW/2)+'" y="'+(VH-7)+'" fill="#8a8a96" font-size="9" text-anchor="middle">'+xl+'</text>';
    var poly='';
    for(var i=0;i<N;i++){ poly+=ptx(i)+','+pty(pts[i])+' '; }
    s+='<polyline points="'+poly+'" fill="none" stroke="#5b8bff" stroke-width="2"/>';
    for(var j=0;j<N;j++){
      s+='<circle cx="'+ptx(j)+'" cy="'+pty(pts[j])+'" r="7" fill="#5b8bff" stroke="#0a0a0c" stroke-width="1.5"/>';
      s+='<text x="'+ptx(j)+'" y="'+(pty(pts[j])-10)+'" fill="#f0f0f3" font-size="9" text-anchor="middle">'+Math.round(pts[j])+'%</text>';
    }
    svg.innerHTML=s;
    svg.style.opacity=(axis==='off')?0.4:1;
  }
  function postCurve(){
    fetch('/api/c/athletic/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({curve_axis:axis,curve_pct:pts})}).catch(function(){});
  }
  function svgPt(e){
    var r=svg.getBoundingClientRect();
    return {x:(e.clientX-r.left)/r.width*VW,y:(e.clientY-r.top)/r.height*VH};
  }
  var drag=-1;
  svg.addEventListener('pointerdown',function(e){
    if(axis==='off') return;
    var p=svgPt(e),best=-1,bd=1e9;
    for(var i=0;i<N;i++){ var d=Math.abs(ptx(i)-p.x); if(d<bd){bd=d;best=i;} }
    if(best>=0&&bd<26){ drag=best; try{svg.setPointerCapture(e.pointerId);}catch(_){} }
  });
  svg.addEventListener('pointermove',function(e){
    if(drag<0) return;
    pts[drag]=Math.round(y2pct(svgPt(e).y)/5)*5;
    render();
  });
  function endDrag(){ if(drag>=0){ drag=-1; postCurve(); } }
  svg.addEventListener('pointerup',endDrag);
  svg.addEventListener('pointercancel',endDrag);
  function setBtns(){
    var bs=document.querySelectorAll('.curve-axis-btn');
    for(var i=0;i<bs.length;i++){
      var on=bs[i].getAttribute('data-axis')===axis;
      bs[i].style.background=on?'#5b8bff':'#16161a';
      bs[i].style.color=on?'#fff':'#8a8a96';
    }
    if(meta){
      meta.textContent=(axis==='distance')?'Each point is a % of the working resistance, shaped over the resist distance. Drag the points.'
        :(axis==='velocity')?'Each point is a % of the working resistance, shaped over cable speed (0–8 m/s). Drag the points.'
        :'Off — flat working resistance is used.';
    }
  }
  var abs=document.querySelectorAll('.curve-axis-btn');
  for(var bi=0;bi<abs.length;bi++){
    abs[bi].onclick=function(){ axis=this.getAttribute('data-axis'); setBtns(); render(); postCurve(); };
  }
  // ---- Curve library: load / save / save-as / delete ----
  function renderLibOptions(){
    var html='<option value="">— Current (unsaved) —</option>';
    for(var i=0;i<curves.length;i++){
      html+='<option value="'+curves[i].id+'"'+(curves[i].id===selectedId?' selected':'')+'>'+
            curves[i].name+'</option>';
    }
    libSel.innerHTML=html;
  }
  function loadCurve(id){
    var c=null;
    for(var i=0;i<curves.length;i++){ if(curves[i].id===id){ c=curves[i]; break; } }
    if(!c||!c.points||c.points.length!==N) return;
    selectedId=id;
    pts=c.points.slice();
    if(nameInp) nameInp.value=c.name;
    if(axis==='off') axis='distance';
    setBtns(); render(); postCurve();
  }
  function refreshLib(cb){
    fetch('/api/curves').then(function(r){return r.json();}).then(function(list){
      curves=Array.isArray(list)?list:[];
      renderLibOptions();
      if(cb) cb();
    }).catch(function(){ if(cb) cb(); });
  }
  function flash(msg,ok){
    if(!saveMsg) return;
    saveMsg.textContent=msg;
    saveMsg.style.color=ok?'var(--accent)':'var(--bad)';
  }
  libSel.addEventListener('change',function(){
    var v=libSel.value;
    if(v===''){ selectedId=null; return; }
    loadCurve(parseInt(v,10));
    flash('',true);
  });
  function saveCurve(asNew){
    var name=(nameInp.value||'').trim();
    if(!name){ flash('Type a curve name first',false); return; }
    var body={name:name,points:pts};
    if(!asNew && selectedId!=null) body.id=selectedId;
    fetch('/api/curves',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)}).then(function(r){
      if(!r.ok) return r.json().then(function(j){ throw new Error(j.detail||('HTTP '+r.status)); });
      return r.json();
    }).then(function(c){
      selectedId=c.id;
      flash('Saved "'+c.name+'"',true);
      refreshLib();
    }).catch(function(e){ flash("Couldn't save — "+e.message,false); });
  }
  document.getElementById('curve-save').onclick=function(){ saveCurve(false); };
  document.getElementById('curve-save-as').onclick=function(){ saveCurve(true); };
  document.getElementById('curve-delete').onclick=function(){
    if(selectedId==null){ flash('Pick a saved curve to delete',false); return; }
    var c=null;
    for(var i=0;i<curves.length;i++){ if(curves[i].id===selectedId) c=curves[i]; }
    if(!confirm('Delete curve "'+(c?c.name:'')+'"?')) return;
    fetch('/api/curves/'+selectedId,{method:'DELETE'}).then(function(){
      selectedId=null;
      if(nameInp) nameInp.value='';
      flash('Deleted',true);
      refreshLib();
    }).catch(function(){ flash("Couldn't delete",false); });
  };
  // Seed editor from current rig config, then load the library.
  fetch('/api/c/state').then(function(r){return r.json();}).then(function(j){
    var cfg=j.config||{};
    if(cfg.curve_axis) axis=cfg.curve_axis;
    if(cfg.curve_pct&&cfg.curve_pct.length===N) pts=cfg.curve_pct.slice();
    setBtns(); render(); refreshLib();
  }).catch(function(){ setBtns(); render(); refreshLib(); });
})();

// ---- Seed session-default numbers from current rig config ----
(async()=>{
  try{
    const cfg=(await(await fetch('/api/c/state')).json()).config||{};
    const set=(id,v)=>{ const el=document.getElementById(id); if(el&&v!=null) el.value=v; };
    set('cfg-return',cfg.return_kg);
    set('cfg-dist',cfg.return_distance_m);
    set('cfg-slew',cfg.slew_kg_per_s);
    set('cfg-rest',cfg.rest_interval_s);
    set('cfg-countdown',cfg.countdown_s);
    set('cfg-kg-limit',cfg.kg_limit);
    if(cfg.gear!=null) document.getElementById('cfg-gear').value=String(cfg.gear);
    if(cfg.auto_stop_enabled!=null)
      document.getElementById('cfg-auto-stop').checked=!!cfg.auto_stop_enabled;
    if(typeof window._markReturnSpeed==='function')
      window._markReturnSpeed(cfg.return_speed_mps!=null?cfg.return_speed_mps:2);
  }catch(e){}
})();

// ---- Rig maintenance: motor / jog / calibrate ----
(function(){
  const motorBtn=document.getElementById('maint-motor');
  if(!motorBtn) return;
  const jogPos=document.getElementById('jog-pos');
  const calCurrent=document.getElementById('cal-current');
  const calMsg=document.getElementById('cal-msg');
  let armed=false;
  function calFlash(m,ok){
    if(!calMsg) return;
    calMsg.textContent=m; calMsg.style.color=ok?'var(--accent)':'var(--bad)';
  }
  async function refresh(){
    try{
      const j=await(await fetch('/api/c/state')).json();
      armed=(j.phase_c==='armed');
      motorBtn.textContent='Motor: '+(armed?'ON':'off');
      motorBtn.style.color=armed?'var(--accent)':'';
      motorBtn.style.borderColor=armed?'var(--accent)':'';
      if(jogPos) jogPos.textContent='Position: '+
        (j.position_counts!=null?j.position_counts+' counts':'—')+
        (j.extension_m!=null?'  ·  '+Number(j.extension_m).toFixed(2)+' m':'');
      const cpm=j.limits&&j.limits.counts_per_metre;
      if(calCurrent&&cpm) calCurrent.textContent='Current: '+Math.round(cpm)+' counts/m';
    }catch(e){}
  }
  motorBtn.addEventListener('click',async()=>{
    motorBtn.disabled=true;
    try{ await fetch(armed?'/api/c/disarm':'/api/c/arm',{method:'POST'}); }
    catch(e){}
    motorBtn.disabled=false;
    refresh();
  });
  // Jog — hold to move; the backend jog-watchdog auto-zeros if keepalives stop.
  let jogTimer=null;
  function jogStart(dir){
    if(jogTimer) return;
    if(!armed){ calFlash('Turn the motor on before jogging',false); return; }
    const send=()=>fetch('/api/c/jog',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({direction:dir})}).catch(()=>{});
    send();
    jogTimer=setInterval(send,350);
  }
  function jogStop(){
    if(jogTimer){ clearInterval(jogTimer); jogTimer=null; }
    fetch('/api/c/jog/stop',{method:'POST'}).catch(()=>{});
  }
  [['jog-retract','retract'],['jog-extend','extend']].forEach(([id,dir])=>{
    const b=document.getElementById(id);
    if(!b) return;
    b.addEventListener('pointerdown',e=>{ e.preventDefault(); jogStart(dir); });
    ['pointerup','pointerleave','pointercancel'].forEach(ev=>
      b.addEventListener(ev,jogStop));
  });
  window.addEventListener('blur',jogStop);
  document.addEventListener('visibilitychange',()=>{ if(document.hidden) jogStop(); });
  // Calibration.
  document.getElementById('cal-zero').onclick=async()=>{
    try{
      const r=await fetch('/api/c/calibrate/zero',{method:'POST'});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){ calFlash(j.detail||'Failed',false); return; }
      calFlash('Zero set at '+j.zero_counts+' counts. Now pull the cable out and measure it.',true);
    }catch(e){ calFlash('Failed',false); }
  };
  document.getElementById('cal-set').onclick=async()=>{
    const d=parseFloat(document.getElementById('cal-distance').value);
    if(isNaN(d)){ calFlash('Enter the distance you pulled the cable out',false); return; }
    try{
      const r=await fetch('/api/c/calibrate/span',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({distance_m:d})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){ calFlash(j.detail||'Failed',false); return; }
      calFlash('Calibrated: '+Math.round(j.counts_per_metre)+' counts/m ('
        +j.span_counts+' counts over '+j.distance_m+' m)',true);
      refresh();
    }catch(e){ calFlash('Failed',false); }
  };
  document.getElementById('cal-reset').onclick=async()=>{
    if(!confirm('Revert to the default position calibration?')) return;
    try{
      await fetch('/api/c/calibrate/reset',{method:'POST'});
      calFlash('Reverted to the default calibration.',true);
      refresh();
    }catch(e){ calFlash('Failed',false); }
  };
  refresh();
  setInterval(refresh,1500);
})();

// ---- Tab switching ----
(function(){
  const tabs=document.querySelectorAll('.setup-tab');
  const panels=document.querySelectorAll('section[data-panel]');
  function show(name){
    tabs.forEach(t=>t.classList.toggle('active',t.getAttribute('data-tab')===name));
    panels.forEach(p=>{ p.hidden = p.getAttribute('data-panel')!==name; });
    localStorage.setItem('ppa.setup.tab',name);
  }
  tabs.forEach(t=>t.addEventListener('click',()=>show(t.getAttribute('data-tab'))));
  const stored=localStorage.getItem('ppa.setup.tab');
  show((stored && document.querySelector('section[data-panel="'+stored+'"]'))?stored:'athletes');
})();

// Reset per-mode onboarding tips (§12)
document.getElementById('reset-tips').onclick=()=>{
  Object.keys(localStorage).filter(k=>k.indexOf('ppa.modeIntroSeen.')===0)
    .forEach(k=>localStorage.removeItem(k));
  alert('Onboarding tips reset — they will show again on the live screen.');
};

loadAthletes();
loadTemplateList();
</script>
</body></html>
"""


def make_app(port: str = "auto", db_path: str = persistence.DB_PATH) -> FastAPI:
    if port == "auto":
        port = detect_drive_port()
    state = ServiceState(port=port, db_path=db_path)
    app = FastAPI(title="ppa_service")
    app.state.svc = state

    @app.on_event("startup")
    async def _startup():
        # Open DB + run schema/recovery before the poll loop kicks off.
        try:
            state.db = persistence.open_db(state.db_path)
            persistence.init_schema(state.db)
            log.info("DB ready at %s", state.db_path)
            # Apply a stored position calibration, if one has been set.
            cpm = persistence.get_setting(state.db, "counts_per_metre")
            if cpm:
                try:
                    analytics.COUNTS_PER_METRE = float(cpm)
                    log.info("calibration: counts_per_metre = %s (from settings)", cpm)
                except ValueError:
                    log.warning("calibration: bad counts_per_metre setting %r", cpm)
        except Exception as e:
            log.error("DB init failed: %s — service will run without persistence", e)
            state.db = None
        try:
            await state.connect_drive()
        except Exception as e:
            log.warning("could not connect to drive at startup: %s", e)
        state.poll_task = asyncio.create_task(state.poll_loop())
        state.athletic_task = asyncio.create_task(_athletic_loop(state))

    @app.on_event("shutdown")
    async def _shutdown():
        state.stop_event.set()
        # Disarm Phase C if still armed — otherwise the heartbeat thread keeps
        # the drive enabled until the watchdog fires; cleaner to do it ourselves.
        if state.phase_c in ("armed", "arming", "error"):
            log.warning("shutdown: Phase C is %s, disarming", state.phase_c)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, state.phase_c_disarm)
            except Exception as e:
                log.error("phase_c disarm during shutdown failed: %s", e)
        if state.athletic_task:
            state.athletic_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.athletic_task
        if state.poll_task:
            state.poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.poll_task
        # Flush any leftover samples and close any open session before disconnecting.
        if state.db is not None:
            try:
                await state.flush_buffer()
                if state.current_session_id is not None:
                    sid = state.current_session_id
                    state.current_session_id = None
                    state.current_rep_id = None
                    await state.db_call(persistence.end_session, sid)
                    log.info("auto-closed session=%d on shutdown", sid)
            except Exception as e:
                log.error("shutdown DB cleanup failed: %s", e)
            try:
                state.db.close()
            except Exception:
                pass
        await state.disconnect_drive()

    # ---------- read ----------

    @app.get("/api/state")
    async def get_state():
        if state.latest is None:
            return JSONResponse({"connected": state.connected, "armed": state.armed, "error": "no samples yet"})
        return state.latest.model_dump()

    @app.get("/api/recent")
    async def get_recent(n: int = 100):
        n = max(1, min(n, RING_SIZE))
        return [s.model_dump() for s in list(state.ring)[-n:]]

    @app.websocket("/api/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        state.subscribers.add(q)
        try:
            # Send the latest immediately so client doesn't wait
            if state.latest:
                await ws.send_json(state.latest.model_dump())
            while True:
                msg = await q.get()
                await ws.send_json(msg.model_dump())
        except WebSocketDisconnect:
            pass
        finally:
            state.subscribers.discard(q)

    # ---------- arm / disarm ----------

    @app.post("/api/arm")
    async def arm(req: ArmRequest):
        if req.confirm == "ARM":
            state.armed = True
            if state.drive:
                state.drive.armed = True
            log.warning("ARMED — writes are now permitted")
            return {"armed": True}
        if req.confirm == "DISARM":
            state.armed = False
            if state.drive:
                state.drive.armed = False
            log.info("disarmed — writes blocked")
            return {"armed": False}
        raise HTTPException(400, "confirm must be 'ARM' or 'DISARM'")

    # ---------- control writes (require armed) ----------

    def _require_armed_and_connected():
        if not state.connected or state.drive is None:
            raise HTTPException(503, "drive not connected")
        if not state.armed:
            raise HTTPException(403, "service is not armed — POST /api/arm with confirm='ARM' first")

    @app.post("/api/control/torque_limit")
    async def set_torque_limit(req: TorqueLimitRequest):
        _require_armed_and_connected()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: state.drive.set_torque_limit(
                forward_pct=req.forward_pct,
                reverse_pct=req.reverse_pct,
                external=req.external,
            ),
        )
        log.warning("WROTE torque_limit fwd=%s rev=%s external=%s",
                    req.forward_pct, req.reverse_pct, req.external)
        return {"ok": True}

    @app.post("/api/control/torque_limit_source")
    async def set_torque_source(req: TorqueSourceRequest):
        _require_armed_and_connected()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, state.drive.set_torque_limit_source, req.external)
        log.warning("WROTE torque_limit_source external=%s", req.external)
        return {"ok": True}

    @app.post("/api/control/control_mode")
    async def set_control_mode(req: ControlModeRequest):
        _require_armed_and_connected()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, state.drive.set_control_mode, req.mode)
        log.warning("WROTE control_mode = %s", req.mode)
        return {"ok": True}

    # ---------- Phase C: direct-drive control ----------
    # These manage their own state machine (heartbeat + S-ON via 0x3607) and
    # are independent of the parameter-write `armed` gate above. CN3 must be
    # plugged in. Do NOT run while the HMI is in active session.

    @app.post("/api/c/arm")
    async def phase_c_arm():
        if not state.connected or state.drive is None:
            raise HTTPException(503, "drive not connected")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, state.phase_c_arm)
        except Exception as e:
            raise HTTPException(500, f"phase_c arm failed: {e}")

    @app.post("/api/c/disarm")
    async def phase_c_disarm_endpoint():
        if state.drive is None:
            raise HTTPException(503, "drive not connected")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, state.phase_c_disarm)
        except Exception as e:
            raise HTTPException(500, f"phase_c disarm error: {e}")

    @app.post("/api/c/setkg")
    async def phase_c_setkg(kg: float):
        if state.drive is None:
            raise HTTPException(503, "drive not connected")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, state.phase_c_set_kg, kg)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"phase_c setkg failed: {e}")

    @app.post("/api/c/jog")
    async def phase_c_jog(req: dict):
        """Maintenance jog — hold-to-move. The UI re-posts every ~350 ms while
        the button is held; the jog-watchdog auto-zeros it otherwise."""
        if state.drive is None:
            raise HTTPException(503, "drive not connected")
        if state.athletic_mode:
            raise HTTPException(409, "stop the drill before jogging")
        if state.phase_c != "armed":
            raise HTTPException(409, "motor must be on to jog")
        direction = req.get("direction")
        if direction not in ("retract", "extend"):
            raise HTTPException(400, "direction must be retract or extend")
        signed = JOG_KG if direction == "retract" else -JOG_KG
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(None, state.phase_c_jog_kg, signed)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"jog failed: {e}")
        state._jog_deadline = time.time() + JOG_DEADLINE_S
        return r

    @app.post("/api/c/jog/stop")
    async def phase_c_jog_stop_endpoint():
        if state.drive is None:
            raise HTTPException(503, "drive not connected")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, state.phase_c_jog_stop)

    @app.post("/api/c/calibrate/zero")
    async def phase_c_calibrate_zero():
        """Capture the current cable position as the calibration zero datum."""
        if state.latest is None or state.latest.position_counts is None:
            raise HTTPException(503, "no position telemetry")
        state._cal_zero_counts = state.latest.position_counts
        return {"zero_counts": state._cal_zero_counts}

    @app.post("/api/c/calibrate/span")
    async def phase_c_calibrate_span(req: dict):
        """Set counts_per_metre from the encoder delta since the zero datum
        and the measured pull-out distance."""
        if state.db is None:
            raise HTTPException(503, "database not available")
        if state._cal_zero_counts is None:
            raise HTTPException(409, "set the zero datum first")
        if state.latest is None or state.latest.position_counts is None:
            raise HTTPException(503, "no position telemetry")
        try:
            distance_m = float(req.get("distance_m"))
        except (TypeError, ValueError):
            raise HTTPException(400, "distance_m must be a number")
        if not 0.5 <= distance_m <= 60:
            raise HTTPException(400, "distance_m out of range [0.5, 60]")
        delta = abs(state.latest.position_counts - state._cal_zero_counts)
        if delta < 1000:
            raise HTTPException(400, "cable barely moved — pull it further out")
        cpm = delta / distance_m
        if not 5000 <= cpm <= 100000:
            raise HTTPException(400, f"implausible counts_per_metre ({cpm:.0f})")
        analytics.COUNTS_PER_METRE = cpm
        await state.db_call(persistence.set_setting, "counts_per_metre", cpm)
        state._cal_zero_counts = None
        log.warning("calibration: counts_per_metre set to %.1f (%d counts / %.2f m)",
                    cpm, delta, distance_m)
        return {"counts_per_metre": round(cpm, 1), "span_counts": delta,
                "distance_m": distance_m}

    @app.post("/api/c/calibrate/reset")
    async def phase_c_calibrate_reset():
        """Revert to the locked-in default position calibration."""
        if state.db is None:
            raise HTTPException(503, "database not available")
        analytics.COUNTS_PER_METRE = CPM_DEFAULT
        await state.db_call(persistence.set_setting, "counts_per_metre", CPM_DEFAULT)
        state._cal_zero_counts = None
        return {"counts_per_metre": CPM_DEFAULT}

    @app.get("/api/c/state")
    async def phase_c_state():
        # Cable extension from drill-start position (metres). Useful for debugging
        # and for the UI big-number display so coach can see distance live.
        extension_m = None
        if (state.athletic_start_pos is not None
                and state.latest is not None
                and state.latest.position_counts is not None):
            extension_m = round(
                abs(state.latest.position_counts - state.athletic_start_pos)
                / analytics.COUNTS_PER_METRE, 3)
        return {
            "phase_c": state.phase_c,
            "error": state.phase_c_error,
            "setpoint_pct": round(state.phase_c_setpoint_pct, 2),
            "setpoint_kg": round(abs(state.phase_c_setpoint_pct) / PCT_PER_KG, 2),
            "heartbeat_writes": state.heartbeat_writes,
            "heartbeat_errs": state.heartbeat_errs,
            "athletic_mode": state.athletic_mode,
            "athletic_phase": state.athletic_phase,
            "athletic_start_pos": state.athletic_start_pos,
            "extension_m": extension_m,
            "position_counts": (state.latest.position_counts
                                if state.latest is not None else None),
            "config": state.athletic_config,
            "limits": {
                "kg_max": state.athletic_config.get("kg_limit", KG_LIMIT_DEFAULT),
                "kg_max_hard": KG_LIMIT_HARD,
                "speed_limit_rpm_armed": SPEED_LIMIT_ARMED,
                "pct_per_kg": PCT_PER_KG,
                "counts_per_metre": analytics.COUNTS_PER_METRE,
            },
        }

    @app.post("/api/c/athletic/start")
    async def phase_c_athletic_start(athlete_id: Optional[int] = None):
        if state.phase_c != "armed":
            raise HTTPException(409, "phase_c must be armed")
        state.athletic_mode = True
        state.athletic_phase = "ready"
        state.athletic_rep_start_requested = False
        state.athletic_rep_stop_requested = False
        if state.latest and state.latest.position_counts is not None:
            state.athletic_start_pos = state.latest.position_counts
        state.athletic_peak_rpm = 0
        state.athletic_current_set = 1
        state.athletic_reps = []
        state._rep_in_progress = None
        # Optionally open a DB session for this athletic block so reps persist
        state.athletic_athlete_id = athlete_id
        state.athletic_session_id = None
        if athlete_id is not None and state.db is not None:
            try:
                notes = f"phase_c athletic | drill={state.athletic_config.get('drill')} | resist_kg={state.athletic_config.get('resist_kg')}"
                sid = await state.db_call(
                    persistence.start_session, athlete_id, notes,
                    state.athletic_config.get("resist_kg"),
                )
                state.athletic_session_id = sid
                state.session_start_t = time.time()
                state.current_session_id = sid  # so end_session works on shutdown
                log.info("athletic session=%d started athlete=%d", sid, athlete_id)
            except Exception as e:
                log.error("athletic start_session failed: %s", e)
        return {
            "athletic_mode": True,
            "phase": state.athletic_phase,
            "athlete_id": athlete_id,
            "session_id": state.athletic_session_id,
        }

    @app.post("/api/c/athletic/start_rep")
    async def phase_c_athletic_start_rep():
        """Manual rep start. The athletic loop picks up the request on its next
        tick and transitions ready -> resist."""
        if not state.athletic_mode:
            raise HTTPException(409, "athletic mode is not active — start the drill first")
        if state.athletic_phase != "ready":
            raise HTTPException(409, f"a rep is already in progress (phase={state.athletic_phase})")
        state.athletic_rep_start_requested = True
        return {"ok": True}

    @app.post("/api/c/athletic/new_set")
    async def phase_c_athletic_new_set():
        """Begin a new set. Subsequent reps are grouped under the new set index."""
        if not state.athletic_mode:
            raise HTTPException(409, "athletic mode is not active — start the drill first")
        state.athletic_current_set += 1
        return {"ok": True, "set_idx": state.athletic_current_set}

    @app.post("/api/c/athletic/stop_rep")
    async def phase_c_athletic_stop_rep():
        """Manual rep end. The athletic loop ends the current rep on its next
        tick and transitions back to ready."""
        if not state.athletic_mode:
            raise HTTPException(409, "athletic mode is not active")
        if state.athletic_phase not in ("resist", "return"):
            raise HTTPException(409, "no rep in progress")
        state.athletic_rep_stop_requested = True
        return {"ok": True}

    @app.get("/api/c/athletic/reps")
    async def phase_c_athletic_reps(include_samples: bool = False):
        # Strip private accumulators from in-progress rep before sending
        ip = None
        if state._rep_in_progress is not None:
            ip = {k: v for k, v in state._rep_in_progress.items() if not k.startswith("_")}
            if not include_samples:
                ip.pop("samples", None)
        reps_out = []
        for r in state.athletic_reps:
            r2 = dict(r)
            if not include_samples:
                r2.pop("samples", None)
            reps_out.append(r2)
        return {
            "reps": reps_out,
            "in_progress": ip,
            "count": len(state.athletic_reps),
        }

    @app.post("/api/c/dev/load_rep")
    async def phase_c_dev_load_rep(req: dict):
        """Dev tool: inject a fully-built rep dict (e.g. parsed from a 1080 xlsx
        export) into state.athletic_reps for UI testing. If `athlete_id` is
        provided in the body OR in the rep's _meta, ALSO persists the rep to
        the DB so it shows up in athlete history.

        Body: a single rep dict OR {"reps": [...], "athlete_id": N}
        """
        state.athletic_mode = True
        state.athletic_phase = "ready"
        athlete_id = req.get("athlete_id")
        if "reps" in req and isinstance(req["reps"], list):
            payload_reps = req["reps"]
        else:
            payload_reps = [req]
        state.athletic_reps = []
        persisted_ids = []
        for i, rep in enumerate(payload_reps):
            rep = dict(rep)
            rep.setdefault("rep_idx", i + 1)
            rep.setdefault("started_at", time.time() - (len(payload_reps) - i) * 5)
            rep.setdefault("ended_at", rep["started_at"] + (rep.get("duration_s") or 1.0))
            # Persist if athlete_id provided BEFORE appending so we can stamp
            # _meta with the resulting session_id (used by the prev-session
            # delta lookup in the athlete report).
            aid = athlete_id or (rep.get("_meta") or {}).get("athlete_id")
            if aid is not None and state.db is not None:
                try:
                    out = await state.db_call(
                        persistence.import_rep, int(aid), rep, "xlsx_import")
                    persisted_ids.append(out)
                    rep.setdefault("_meta", {})
                    rep["_meta"]["athlete_id"] = out["athlete_id"]
                    rep["_meta"]["session_id"] = out["session_id"]
                    rep["_meta"]["rep_db_id"] = out["rep_id"]
                except Exception as e:
                    log.warning("import_rep failed: %s", e)
            state.athletic_reps.append(rep)
        return {"loaded_reps": len(payload_reps), "persisted": persisted_ids}

    # ---- Athlete history + progression endpoints ----
    @app.get("/api/athletes/{athlete_id}/history")
    async def athlete_history(athlete_id: int, limit: int = 50):
        if state.db is None:
            raise HTTPException(503, "database not available")
        return await state.db_call(persistence.athlete_history, athlete_id, limit)

    @app.get("/api/athletes/{athlete_id}/progression")
    async def athlete_progression(athlete_id: int, metric: str = "peak_speed_mps",
                                  agg: str = "MAX"):
        if state.db is None:
            raise HTTPException(503, "database not available")
        try:
            return {"athlete_id": athlete_id, "metric": metric, "agg": agg,
                    "points": await state.db_call(
                        persistence.athlete_progression, athlete_id, metric, agg)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/athletes/{athlete_id}/profile")
    async def athlete_profile(athlete_id: int):
        if state.db is None:
            raise HTTPException(503, "database not available")
        result = await state.db_call(persistence.athlete_profile, athlete_id)
        if result["athlete"] is None:
            raise HTTPException(404, f"athlete {athlete_id} not found")
        return result

    @app.get("/api/athletes/{athlete_id}/load_suggestion")
    async def athlete_load_suggestion(athlete_id: int, target: str = "off",
                                      target_param: Optional[float] = None,
                                      drill: Optional[str] = None):
        if state.db is None:
            raise HTTPException(503, "database not available")
        profile = await state.db_call(persistence.athlete_profile, athlete_id)
        if profile["athlete"] is None:
            raise HTTPException(404, f"athlete {athlete_id} not found")
        return load_advisor.suggest_load(profile, target, target_param, drill)

    @app.post("/api/c/dev/seed")
    async def phase_c_dev_seed(reps: int = 4):
        """Dev tool: seed fake athletic reps to populate the UI without a drive.
        Generates plausible time-series + aggregates so the chart and rep table light up.
        """
        import math, random
        state.athletic_mode = True
        state.athletic_phase = "ready"
        state.athletic_reps = []
        for i in range(reps):
            duration_s = round(2.0 + random.random() * 1.5, 2)
            distance_m = round(2.0 + random.random() * 6.0, 3)
            peak_v = round(1.0 + random.random() * 4.0, 3)
            peak_F = round(150 + random.random() * 200, 1)  # N
            samples = []
            n = max(int(duration_s * 10), 8)
            prev_v = 0.0
            for j in range(n):
                t_ms = int(j * duration_s * 1000 / n)
                phase = j / n
                # half-sine velocity profile
                v = peak_v * math.sin(math.pi * phase)
                F = peak_F * (0.4 + 0.6 * math.sin(math.pi * phase))
                P = F * v
                a = (v - prev_v) / 0.1
                prev_v = v
                pos_m = distance_m * (phase ** 1.4)
                samples.append({
                    "t_ms": t_ms, "v_mps": round(v, 3), "F_N": round(F, 1),
                    "P_W": round(P, 1), "a_mps2": round(a, 3),
                    "pos_m": round(pos_m, 3),
                })
            avg_v = sum(s["v_mps"] for s in samples) / len(samples)
            avg_F = sum(s["F_N"] for s in samples) / len(samples)
            avg_P = sum(s["P_W"] for s in samples) / len(samples)
            avg_a = sum(s["a_mps2"] for s in samples) / len(samples)
            peak_a = max(s["a_mps2"] for s in samples)
            work = sum(s["F_N"] * s["v_mps"] * 0.1 for s in samples)
            # Derived metrics same as the live path
            impulse = sum(s["F_N"] for s in samples) * 0.1
            peak_f_idx = max(range(len(samples)), key=lambda i: samples[i]["F_N"])
            peak_v_idx = max(range(len(samples)), key=lambda i: samples[i]["v_mps"])
            splits = {}
            for m in (5, 10, 20):
                for s in samples:
                    if s["pos_m"] >= m:
                        splits[str(m)] = s["t_ms"] / 1000.0
                        break
            state.athletic_reps.append({
                "rep_idx": i + 1,
                "started_at": time.time() - (reps - i) * 5,
                "ended_at": time.time() - (reps - i) * 5 + duration_s,
                "duration_s": duration_s,
                "max_extension_m": distance_m,
                "total_distance_m": distance_m,
                "total_time_s": duration_s,
                "peak_speed_mps": peak_v,
                "top_speed_mps": peak_v,
                "peak_force_n": peak_F,
                "peak_power_w": round(max(s["P_W"] for s in samples), 1),
                "peak_acceleration_mps2": round(peak_a, 3),
                "peak_torque_pct": round(peak_F / 9.81 * 5.64, 1),
                "avg_speed_mps": round(avg_v, 3),
                "avg_force_n": round(avg_F, 1),
                "avg_power_w": round(avg_P, 1),
                "avg_acceleration_mps2": round(avg_a, 3),
                "work_j": round(work, 1),
                "impulse_ns": round(impulse, 1),
                "ttpf_ms": samples[peak_f_idx]["t_ms"],
                "ttps_ms": samples[peak_v_idx]["t_ms"],
                "is_eccentric": False,
                "splits_s": splits,
                "split_report": _build_split_report(samples, SPLIT_LENGTH_M),
                "samples": samples,
            })
        return {"seeded_reps": reps}

    @app.post("/api/c/dev/clear")
    async def phase_c_dev_clear():
        state.athletic_reps = []
        state._rep_in_progress = None
        state.athletic_mode = False
        state.athletic_phase = "off"
        return {"ok": True}

    @app.get("/api/c/athletic/fv")
    async def phase_c_athletic_fv():
        """Build a Force-Velocity profile from the current athletic_reps.
        Each rep contributes one (avg_force, avg_speed) point. Linear regression
        gives F0 (force at v=0), V0 (velocity at F=0), Pmax = F0·V0/4 (Samozino).
        Needs ≥3 reps at meaningfully different loads to be usable.
        """
        # Exclude invalid reps from the FV regression per §11 of v1 addendum.
        pts = [(r.get("avg_speed_mps", 0.0), r.get("avg_force_n", 0.0))
               for r in state.athletic_reps
               if r.get("avg_speed_mps", 0) > 0.05
               and r.get("valid", True) is not False]
        if len(pts) < 2:
            return {"ok": False, "reason": f"need ≥2 reps with motion, have {len(pts)}",
                    "points": pts, "n": len(pts)}
        n = len(pts)
        sx = sum(v for v, _ in pts)
        sy = sum(f for _, f in pts)
        sxy = sum(v * f for v, f in pts)
        sxx = sum(v * v for v, _ in pts)
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-9:
            return {"ok": False, "reason": "degenerate fit (no velocity spread)",
                    "points": pts, "n": n}
        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n
        f0 = intercept
        v0 = -intercept / slope if slope else None
        pmax = (f0 * v0) / 4 if v0 else None
        # R²
        mean_y = sy / n
        ss_res = sum((f - (slope * v + intercept)) ** 2 for v, f in pts)
        ss_tot = sum((f - mean_y) ** 2 for _, f in pts)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
        return {
            "ok": True, "n": n,
            "points": [{"v": v, "F": f} for v, f in pts],
            "slope": round(slope, 2),
            "intercept": round(intercept, 1),
            "F0_n": round(f0, 1),
            "V0_mps": round(v0, 3) if v0 else None,
            "Pmax_w": round(pmax, 1) if pmax else None,
            "r2": round(r2, 4),
        }

    def _find_rep(idx: int):
        for r in state.athletic_reps:
            if r.get("rep_idx") == idx:
                return r
        if state._rep_in_progress and state._rep_in_progress.get("rep_idx") == idx:
            return state._rep_in_progress
        return None

    # ---- Multi-rig naming (§15.3) ----
    @app.get("/api/rigs")
    async def rigs_list():
        if state.db is None: raise HTTPException(503, "database not available")
        return await state.db_call(persistence.list_rigs)

    @app.post("/api/rigs")
    async def rigs_save(req: dict):
        if state.db is None: raise HTTPException(503, "database not available")
        try:
            return await state.db_call(
                persistence.save_rig,
                req.get("name", ""), req.get("location"),
                req.get("serial"), req.get("notes"),
                req.get("id"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ---- Session config templates (§13 v1 addendum) ----
    @app.get("/api/templates")
    async def templates_list():
        if state.db is None: raise HTTPException(503, "database not available")
        return await state.db_call(persistence.list_templates)

    @app.post("/api/templates")
    async def templates_save(req: dict):
        if state.db is None: raise HTTPException(503, "database not available")
        name = (req.get("name") or "").strip()
        if not name: raise HTTPException(400, "name required")
        config = req.get("config") or {}
        try:
            return await state.db_call(
                persistence.save_template, name, config,
                req.get("position_group"), req.get("sport"), req.get("notes"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/templates/{template_id}")
    async def templates_delete(template_id: int):
        if state.db is None: raise HTTPException(503, "database not available")
        await state.db_call(persistence.delete_template, template_id)
        return {"deleted": template_id}

    @app.post("/api/templates/{template_id}/load")
    async def templates_load(template_id: int):
        """Load a template's config into state.athletic_config — coach can
        then click Start drill and the pre-rep card shows the loaded values."""
        if state.db is None: raise HTTPException(503, "database not available")
        rows = await state.db_call(persistence.list_templates)
        tpl = next((t for t in rows if t["id"] == template_id), None)
        if not tpl: raise HTTPException(404, f"template {template_id} not found")
        cfg = tpl.get("config", {})
        # Whitelist to avoid stuffing arbitrary keys into state.athletic_config
        for k in ("resist_kg", "return_kg", "return_distance_m",
                  "autostart_speed_mps", "slew_kg_per_s", "drill",
                  "rest_interval_s", "countdown_s"):
            if k in cfg: state.athletic_config[k] = cfg[k]
        return {"loaded": template_id, "name": tpl["name"], "config": state.athletic_config}

    # ---- Resistance-curve library ----
    @app.get("/api/curves")
    async def curves_list():
        if state.db is None: raise HTTPException(503, "database not available")
        return await state.db_call(persistence.list_curves)

    @app.post("/api/curves")
    async def curves_save(req: dict):
        if state.db is None: raise HTTPException(503, "database not available")
        name = (req.get("name") or "").strip()
        if not name: raise HTTPException(400, "curve name required")
        points = req.get("points")
        if not isinstance(points, list) or len(points) != CURVE_POINTS:
            raise HTTPException(400, f"points must be {CURVE_POINTS} numbers")
        try:
            pts = [float(p) for p in points]
        except (TypeError, ValueError):
            raise HTTPException(400, "points must be numbers")
        if not all(0 <= p <= CURVE_PCT_MAX for p in pts):
            raise HTTPException(400, f"points must be in [0, {CURVE_PCT_MAX}] %")
        curve_id = req.get("id")
        try:
            return await state.db_call(persistence.save_curve, name, pts, curve_id)
        except persistence.sqlite3.IntegrityError:
            raise HTTPException(409, f"a curve named '{name}' already exists")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/curves/{curve_id}")
    async def curves_delete(curve_id: int):
        if state.db is None: raise HTTPException(503, "database not available")
        await state.db_call(persistence.delete_curve, curve_id)
        return {"deleted": curve_id}

    @app.post("/api/c/estop")
    async def emergency_stop():
        """Emergency-stop endpoint — fired by the universal red button on
        every screen. Stops any active drill, zeros the cable setpoint
        immediately, then runs the verify-and-retry disarm path. Idempotent
        and safe to call from any state. Per spec §2.5: never behind a
        confirmation, never behind a menu, always one tap.
        """
        # Stop drill first so the athletic loop doesn't re-write setpoint
        state.athletic_mode = False
        state.athletic_phase = "off"
        state._rep_in_progress = None
        # Zero the setpoint immediately, then disarm the drive
        try:
            if state.phase_c == "armed":
                await asyncio.to_thread(state.phase_c_set_kg, 0.0)
        except Exception as e:
            log.warning("estop: zero-setpoint failed: %s", e)
        try:
            if state.phase_c != "idle":
                await asyncio.to_thread(state.phase_c_disarm)
        except Exception as e:
            log.warning("estop: disarm failed: %s", e)
        # Close any open DB session so we don't leave it orphaned
        if state.athletic_session_id is not None and state.db is not None:
            try:
                await state.db_call(persistence.end_session, state.athletic_session_id)
                state.athletic_session_id = None
                state.current_session_id = None
                state.session_start_t = None
            except Exception:
                pass
        log.warning("EMERGENCY STOP fired — phase_c=%s athletic_mode=%s",
                    state.phase_c, state.athletic_mode)
        return {"emergency_stop": True, "phase_c": state.phase_c}

    @app.patch("/api/c/athletic/rep/{idx}/validity")
    async def phase_c_athletic_rep_validity(idx: int, req: dict):
        """Mark a rep valid or invalid. Body: {valid:bool, reason?, note?}.
        Updates both the in-memory rep (so the live UI reflects it
        immediately) AND the DB row (so it survives restart and gets
        excluded from FV regressions, history aggregates, chase metrics).
        """
        rep = _find_rep(idx)
        if rep is None:
            raise HTTPException(404, f"rep {idx} not found")
        valid = bool(req.get("valid", True))
        reason = req.get("reason")
        note = req.get("note")
        rep["valid"] = valid
        rep["invalid_reason"] = reason if not valid else None
        rep["invalid_note"] = note if not valid else None
        # Persist if there's a backing DB row
        rep_db_id = rep.get("rep_db_id") or (rep.get("_meta") or {}).get("rep_db_id")
        if rep_db_id is not None and state.db is not None:
            try:
                await state.db_call(persistence.set_rep_validity,
                                     int(rep_db_id), valid, reason, note)
            except Exception as e:
                log.warning("set_rep_validity failed: %s", e)
        return {"rep_idx": idx, "valid": valid, "reason": reason, "note": note}

    @app.patch("/api/c/athletic/rep/{idx}/annotation")
    async def phase_c_athletic_rep_annotation(idx: int, req: dict):
        """Set a coach annotation on a rep. Body: {color?, comment?}.
        Updates the in-memory rep and the DB row (1080 MotionGroup parity)."""
        rep = _find_rep(idx)
        if rep is None:
            raise HTTPException(404, f"rep {idx} not found")
        color = req.get("color")
        comment = req.get("comment")
        if color is not None:
            rep["color"] = color or None
        if comment is not None:
            rep["comment"] = comment or None
        rep_db_id = rep.get("rep_db_id") or (rep.get("_meta") or {}).get("rep_db_id")
        if rep_db_id is not None and state.db is not None:
            try:
                await state.db_call(persistence.annotate_rep,
                                     int(rep_db_id), color, comment)
            except Exception as e:
                log.warning("annotate_rep failed: %s", e)
        return {"rep_idx": idx, "color": rep.get("color"), "comment": rep.get("comment")}

    @app.get("/api/c/athletic/rep/{idx}/insights")
    async def phase_c_athletic_rep_insights(idx: int, position: str = "back",
                                            planned_modality: Optional[str] = None):
        """Coach view — full ranked insight list, all severities, raw metrics."""
        import insights as _insights
        rep = _find_rep(idx)
        if rep is None:
            raise HTTPException(404, f"rep {idx} not found")
        return _insights.build_report(rep, position_group=position,
                                       planned_modality=planned_modality)

    @app.get("/api/c/athletic/rep/{idx}/athlete_report")
    async def phase_c_athletic_rep_athlete_report(idx: int, position: str = "back",
                                                   planned_modality: Optional[str] = None):
        """Athlete view — synthesised verdict + dosed plan + chase metric.
        If the rep has _meta.athlete_id and there's a prior session for that
        athlete in the DB, fetches the previous rep and computes vs-prev deltas.
        """
        import insights as _insights
        import synthesis as _synth
        rep = _find_rep(idx)
        if rep is None:
            raise HTTPException(404, f"rep {idx} not found")
        coach_report = _insights.build_report(rep, position_group=position,
                                              planned_modality=planned_modality)
        all_insights = (coach_report.get("primary_insights", []) +
                        coach_report.get("context_insights", []))
        # Look up the previous rep for this athlete (if known) to enable deltas
        previous_rep = None
        meta = rep.get("_meta") or {}
        athlete_id = meta.get("athlete_id")
        before_session = meta.get("session_id")
        if athlete_id is not None and state.db is not None:
            try:
                previous_rep = await state.db_call(
                    persistence.previous_rep_for_athlete,
                    int(athlete_id), before_session)
            except Exception as e:
                log.warning("previous_rep lookup failed: %s", e)
        synth = _synth.synthesise(rep, all_insights, position_group=position,
                                  previous_rep=previous_rep)
        return {
            "rep_idx": idx,
            "athlete": meta.get("athlete_name"),
            "position_group": position,
            **synth,
        }

    # ---- Session load (click a history row -> bring it into the coach view) ----
    @app.post("/api/sessions/{session_id}/load")
    async def session_load_into_state(session_id: int):
        """Hydrate a historical session's reps back into state.athletic_reps so
        the coach view renders them as if they'd just been captured."""
        if state.db is None:
            raise HTTPException(503, "database not available")
        out = await state.db_call(persistence.load_session_reps, session_id)
        if not out.get("session"):
            raise HTTPException(404, f"session {session_id} not found")
        state.athletic_mode = True
        state.athletic_phase = "ready"
        state.athletic_reps = []
        for rep in out["reps"]:
            state.athletic_reps.append(rep)
        return {"loaded": len(out["reps"]), "session_id": session_id,
                "athlete_id": out["session"]["athlete_id"]}

    # ---- Athlete metadata (PUT for partial update, GET for single) ----
    @app.get("/api/athletes/{athlete_id}")
    async def athlete_get(athlete_id: int):
        if state.db is None:
            raise HTTPException(503, "database not available")
        a = await state.db_call(persistence.get_athlete, athlete_id)
        if not a:
            raise HTTPException(404, f"athlete {athlete_id} not found")
        return a

    @app.put("/api/athletes/{athlete_id}")
    async def athlete_update(athlete_id: int, req: dict):
        if state.db is None:
            raise HTTPException(503, "database not available")
        # Whitelist + light type coercion
        allowed = {"name", "body_mass_kg", "position_group", "sport", "level",
                   "dob", "external_id", "squad_group", "tags"}
        clean = {k: v for k, v in req.items() if k in allowed}
        if "body_mass_kg" in clean and clean["body_mass_kg"] is not None:
            try: clean["body_mass_kg"] = float(clean["body_mass_kg"])
            except Exception: clean.pop("body_mass_kg", None)
        if not clean:
            raise HTTPException(400, "no updatable fields provided")
        return await state.db_call(persistence.update_athlete, athlete_id, **clean)

    @app.get("/api/c/athletic/rep/{idx}/samples")
    async def phase_c_athletic_rep_samples(idx: int):
        # 1-indexed rep_idx
        for r in state.athletic_reps:
            if r.get("rep_idx") == idx:
                return {"rep_idx": idx, "samples": r.get("samples", [])}
        if state._rep_in_progress and state._rep_in_progress.get("rep_idx") == idx:
            return {"rep_idx": idx, "samples": state._rep_in_progress.get("samples", []),
                    "in_progress": True}
        raise HTTPException(404, f"rep {idx} not found")

    @app.post("/api/c/athletic/stop")
    async def phase_c_athletic_stop():
        state.athletic_mode = False
        state.athletic_phase = "off"
        state.athletic_rep_start_requested = False
        state.athletic_rep_stop_requested = False
        if state.phase_c == "armed":
            try:
                await asyncio.to_thread(state.phase_c_set_kg, 0.0)
            except Exception:
                pass

        # Clean up any in-progress rep before closing the session. Two cases:
        #  - duration < PHANTOM_DURATION_S → delete the open DB row, drop in-memory
        #  - duration >= PHANTOM_DURATION_S → finalise + persist with rich aggregates
        # Without this, persistence.end_session auto-closes the open rep using
        # only legacy sample-derived columns, producing a phantom row with
        # peak_torque locked at the resist setpoint.
        PHANTOM_DURATION_S = 0.5
        PERIOD = 1.0 / 10  # athletic loop is 10 Hz
        rip = state._rep_in_progress
        if rip is not None:
            rip = _finalise_in_progress_rep(rip, PERIOD)
            db_id = rip.get("rep_db_id") if rip else None
            if db_id is not None and state.db is not None:
                if rip["duration_s"] < PHANTOM_DURATION_S:
                    log.info("stop: dropping phantom in-progress rep idx=%s duration=%.2fs",
                             rip.get("rep_idx"), rip["duration_s"])
                    try:
                        await asyncio.to_thread(
                            lambda: persistence.delete_rep(state.db, db_id))
                    except Exception as e:
                        log.warning("stop: phantom delete_rep failed: %s", e)
                elif state.session_start_t is not None:
                    try:
                        t_end_ms = int((rip["ended_at"] - state.session_start_t) * 1000)
                        drill_name = state.athletic_config.get("drill")
                        await asyncio.to_thread(
                            lambda: persistence.end_rep_with_aggregates(
                                state.db, db_id, t_end_ms, rip, drill=drill_name))
                        state.athletic_reps.append(rip)
                    except Exception as e:
                        log.warning("stop: end_rep_with_aggregates failed: %s", e)
            elif rip is not None and rip["duration_s"] >= PHANTOM_DURATION_S:
                # No DB row to update (no athlete linked) but still surface in-memory
                state.athletic_reps.append(rip)
            state._rep_in_progress = None

        # Close DB session if one was opened for this athletic block
        if state.athletic_session_id is not None and state.db is not None:
            try:
                summary = await state.db_call(
                    persistence.end_session, state.athletic_session_id)
                log.info("athletic session=%d closed: %s reps",
                         state.athletic_session_id, summary.get("rep_count"))
            except Exception as e:
                log.error("athletic end_session failed: %s", e)
            state.athletic_session_id = None
            state.current_session_id = None
            state.session_start_t = None
        return {"athletic_mode": False}

    @app.post("/api/c/athletic/config")
    async def phase_c_athletic_config(req: AthleticConfigRequest):
        c = state.athletic_config
        if req.kg_limit is not None:
            if not 1.0 <= req.kg_limit <= KG_LIMIT_HARD:
                raise HTTPException(400, f"kg_limit out of range [1, {KG_LIMIT_HARD}]")
            c["kg_limit"] = float(req.kg_limit)
        if req.resist_kg is not None:
            if not 0 < req.resist_kg <= KG_LIMIT_HARD:
                raise HTTPException(400, f"resist_kg out of range (0, {KG_LIMIT_HARD}]")
            c["resist_kg"] = req.resist_kg
        if req.resist_distance_m is not None:
            if not 0 < req.resist_distance_m <= 50:
                raise HTTPException(400, "resist_distance_m out of range (0, 50]")
            c["resist_distance_m"] = req.resist_distance_m
        if req.velocity_cap_mps is not None:
            if not 0 <= req.velocity_cap_mps <= 15:
                raise HTTPException(400, "velocity_cap_mps out of range [0, 15]")
            c["velocity_cap_mps"] = req.velocity_cap_mps
        if req.chain_kg_per_m is not None:
            if not 0 <= req.chain_kg_per_m <= 10:
                raise HTTPException(400, "chain_kg_per_m out of range [0, 10]")
            c["chain_kg_per_m"] = req.chain_kg_per_m
        if req.eccentric_overload_pct is not None:
            if not 0 <= req.eccentric_overload_pct <= 100:
                raise HTTPException(400, "eccentric_overload_pct out of range [0, 100]")
            c["eccentric_overload_pct"] = req.eccentric_overload_pct
        if req.concentric_dir is not None:
            if req.concentric_dir not in ("extend", "retract"):
                raise HTTPException(400, "concentric_dir must be extend or retract")
            c["concentric_dir"] = req.concentric_dir
        if req.virtual_mass_kg is not None:
            if not 1.0 <= req.virtual_mass_kg <= 200.0:
                raise HTTPException(400, "virtual_mass_kg out of range [1, 200]")
            c["virtual_mass_kg"] = req.virtual_mass_kg
        if req.viscous_damping is not None:
            if not 0.0 <= req.viscous_damping <= 10.0:
                raise HTTPException(400, "viscous_damping out of range [0, 10]")
            c["viscous_damping"] = req.viscous_damping
        if req.curve_axis is not None:
            if req.curve_axis not in ("off", "distance", "velocity"):
                raise HTTPException(400, "curve_axis must be off/distance/velocity")
            c["curve_axis"] = req.curve_axis
        if req.curve_pct is not None:
            if (len(req.curve_pct) != CURVE_POINTS
                    or not all(0 <= v <= CURVE_PCT_MAX for v in req.curve_pct)):
                raise HTTPException(
                    400, f"curve_pct must be {CURVE_POINTS} values in [0, {CURVE_PCT_MAX}]")
            c["curve_pct"] = [float(v) for v in req.curve_pct]
        if req.return_kg is not None:
            if not 0 <= req.return_kg <= 5:
                raise HTTPException(400, "return_kg out of range [0, 5]")
            c["return_kg"] = req.return_kg
        if req.return_distance_m is not None:
            if not 0 < req.return_distance_m <= 5:
                raise HTTPException(400, "return_distance_m out of range (0, 5]")
            c["return_distance_m"] = req.return_distance_m
        if req.return_speed_mps is not None:
            if not 0.5 <= req.return_speed_mps <= 3.0:
                raise HTTPException(400, "return_speed_mps out of range [0.5, 3.0]")
            c["return_speed_mps"] = req.return_speed_mps
        if req.slew_kg_per_s is not None:
            if not 1.0 <= req.slew_kg_per_s <= 60.0:
                raise HTTPException(400, "slew_kg_per_s out of range [1, 60]")
            c["slew_kg_per_s"] = req.slew_kg_per_s
        if req.drill is not None:
            valid_drills = {"FreeTest", "ASkip", "BSkip", "Ankling", "SkippingJumps",
                            "ApproachJump", "HighKnee", "FastLeg", "Acceleration",
                            "Sprint", "StraightLegRun"}
            if req.drill not in valid_drills:
                raise HTTPException(400, f"drill must be one of {sorted(valid_drills)}")
            c["drill"] = req.drill
        if req.rest_interval_s is not None:
            if not 0 <= req.rest_interval_s <= 600:
                raise HTTPException(400, "rest_interval_s out of range [0, 600]")
            c["rest_interval_s"] = req.rest_interval_s
        if req.countdown_s is not None:
            if not 0 <= req.countdown_s <= 10:
                raise HTTPException(400, "countdown_s out of range [0, 10]")
            c["countdown_s"] = req.countdown_s
        if req.mode is not None:
            valid_modes = {"resisted", "assisted", "cod", "gym", "flywheel"}
            if req.mode not in valid_modes:
                raise HTTPException(400, f"mode must be one of {sorted(valid_modes)}")
            c["mode"] = req.mode
        if req.cod_prefix is not None:
            if req.cod_prefix not in ("auto", "resisted", "assisted"):
                raise HTTPException(400, "cod_prefix must be auto/resisted/assisted")
            c["cod_prefix"] = req.cod_prefix
        if req.gear is not None:
            if req.gear not in (1, 2):
                raise HTTPException(400, "gear must be 1 or 2")
            c["gear"] = req.gear
        if req.rig_id is not None:
            c["rig_id"] = req.rig_id
        if req.auto_stop_enabled is not None:
            c["auto_stop_enabled"] = bool(req.auto_stop_enabled)
        if req.auto_stop_velocity_threshold is not None:
            if not 0.0 <= req.auto_stop_velocity_threshold <= 3.0:
                raise HTTPException(400, "auto_stop_velocity_threshold out of range [0, 3]")
            c["auto_stop_velocity_threshold"] = req.auto_stop_velocity_threshold
        if req.auto_stop_dwell_s is not None:
            if not 0.2 <= req.auto_stop_dwell_s <= 10.0:
                raise HTTPException(400, "auto_stop_dwell_s out of range [0.2, 10]")
            c["auto_stop_dwell_s"] = req.auto_stop_dwell_s
        return c

    # ---- Coach (laptop) + Athlete (phone) UI pages ----
    @app.get("/phase-c", response_class=None)
    async def phase_c_page():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/coach", status_code=302)

    @app.get("/coach", response_class=None)
    async def coach_page():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(PHASE_C_HTML)

    @app.get("/setup", response_class=None)
    async def setup_page():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(SETUP_HTML)

    @app.get("/athlete", response_class=None)
    async def athlete_page():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(ATHLETE_HTML)

    @app.get("/sw.js")
    async def service_worker():
        """Serve the service worker from root so it can claim scope over /coach + /athlete."""
        from fastapi.responses import FileResponse
        sw = STATIC_DIR / "sw.js"
        if not sw.exists():
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("// missing sw.js", media_type="application/javascript")
        return FileResponse(sw, media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/"})

    # ---------- static dashboard ----------

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def root():
            return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/diag/athletic")
    async def diag_athletic():
        """Athletic-loop diagnostics — added to hunt the 'Start rep does nothing' bug."""
        t = state.athletic_task
        return {
            "task_exists": t is not None,
            "task_done": t.done() if t is not None else None,
            "task_cancelled": t.cancelled() if t is not None and t.done() else None,
            "task_exception_repr": (repr(t.exception()) if t is not None and t.done()
                                    and not t.cancelled() else None),
            "ticks": state.athletic_loop_ticks,
            "last_phase_seen": state.athletic_loop_last_phase_seen,
            "last_start_req_seen": state.athletic_loop_last_start_req_seen,
            "exc_count": state.athletic_loop_exc_count,
            "last_exc": state.athletic_loop_last_exc,
            "last_exc_at": state.athletic_loop_last_exc_at,
            # live state for cross-reference
            "athletic_mode": state.athletic_mode,
            "athletic_phase": state.athletic_phase,
            "phase_c": state.phase_c,
            "rep_start_requested_now": state.athletic_rep_start_requested,
        }

    @app.get("/api/diag/cycle")
    async def diag_cycle():
        """Poll-loop cycle-time telemetry. Added 2026-05-20 to measure the
        effect of dropping the FTDI USB-serial latency timer from 16 ms → 1 ms."""
        idle = state.phase_c == "idle"
        nominal_ms = (IDLE_PERIOD if idle else PERIOD) * 1000.0
        headroom_ms = nominal_ms - state._lag_ema_ms
        return {
            "samples": state.lag_samples,
            "phase_c_state": state.phase_c,
            "nominal_period_ms": round(nominal_ms, 2),
            "last_lag_ms": round(state.last_lag_ms, 2),
            "ema_lag_ms": round(state._lag_ema_ms, 2),
            "min_lag_ms": round(state.lag_min_ms, 2) if state.lag_samples else None,
            "max_lag_ms": round(state.lag_max_ms, 2),
            "headroom_ms": round(headroom_ms, 2),
            "headroom_pct": round(100.0 * headroom_ms / nominal_ms, 1),
            "ftdi_latency_note": "drive port FTDI latency timer set to 1 ms (was 16 ms default)",
        }

    @app.post("/api/diag/cycle/reset")
    async def diag_cycle_reset():
        state.lag_min_ms = float("inf")
        state.lag_max_ms = 0.0
        state.lag_samples = 0
        state._lag_ema_ms = 0.0
        return {"reset": True}

    @app.get("/api/info")
    async def info():
        return {
            "service": "ppa_service",
            "port": state.port,
            "connected": state.connected,
            "armed": state.armed,
            "poll_hz": POLL_HZ,
            "ring_size": RING_SIZE,
            "torque_limit_max_pct": TORQUE_LIMIT_MAX_PERCENT,
            "db_path": state.db_path,
            "db_ready": state.db is not None,
            "current_session_id": state.current_session_id,
            "current_rep_id": state.current_rep_id,
            "calibration": {
                "encoder_counts_per_rev": analytics.ENCODER_COUNTS_PER_REV,
                "cable_metres_per_rev": analytics.CABLE_METRES_PER_REV,
                "counts_per_metre": analytics.COUNTS_PER_METRE,
                "calibrated": False,  # flip when CALIBRATION.md procedure has been run
            },
            "phase_c": {
                "state": state.phase_c,
                "setpoint_pct": round(state.phase_c_setpoint_pct, 2),
                "kg_max": state.athletic_config.get("kg_limit", KG_LIMIT_DEFAULT),
                "kg_max_hard": KG_LIMIT_HARD,
                "pct_per_kg": PCT_PER_KG,
                "speed_limit_rpm_armed": SPEED_LIMIT_ARMED,
                "heartbeat_interval_s": HEARTBEAT_INTERVAL,
            },
        }

    # ---------- athletes ----------

    def _require_db():
        if state.db is None:
            raise HTTPException(503, "DB not initialised")

    @app.post("/api/athletes")
    async def create_athlete(req: CreateAthleteRequest):
        _require_db()
        try:
            new_id = await state.db_call(persistence.create_athlete, req.name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": new_id, "name": req.name.strip()}

    @app.get("/api/athletes")
    async def list_athletes():
        _require_db()
        return await state.db_call(persistence.list_athletes)

    # ---------- sessions ----------

    @app.post("/api/sessions/start")
    async def start_session(req: StartSessionRequest):
        _require_db()
        if state.current_session_id is not None:
            raise HTTPException(409, f"session {state.current_session_id} is already open")
        try:
            sid = await state.db_call(
                persistence.start_session,
                req.athlete_id, req.notes, req.hmi_load_kg,
            )
        except persistence.SessionAlreadyOpenError as e:
            # DB has a stale open session that we don't know about (shouldn't happen
            # post-recovery, but be defensive).
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Anchor wall-clock for sample timestamps.
        state.session_start_t = time.time()
        state.current_session_id = sid
        state.current_rep_id = None
        state.prev_status = None
        state.sample_buffer.clear()
        log.info("session=%d started athlete=%d hmi_load_kg=%s",
                 sid, req.athlete_id, req.hmi_load_kg)
        return {"session_id": sid, "started_at_wallclock": state.session_start_t}

    @app.post("/api/sessions/{session_id}/end")
    async def end_session(session_id: int):
        _require_db()
        if state.current_session_id != session_id:
            # Allow ending a session that isn't currently active (e.g. cleaning up
            # a stale row). Just delegate to persistence.
            try:
                summary = await state.db_call(persistence.end_session, session_id)
                return summary
            except ValueError as e:
                raise HTTPException(404, str(e))
        # Active session: flush buffer + close any open rep, then call end_session.
        await state.flush_buffer()
        if state.current_rep_id is not None:
            rep_id = state.current_rep_id
            state.current_rep_id = None
            t_offset_ms = int((time.time() - state.session_start_t) * 1000)
            try:
                await state.db_call(persistence.end_rep, rep_id, t_offset_ms)
            except Exception as e:
                log.error("end_rep on end_session failed: %s", e)
        summary = await state.db_call(persistence.end_session, session_id)
        log.info("session=%d ended rep_count=%s duration=%ss",
                 session_id, summary.get("rep_count"), summary.get("duration_seconds"))
        # Reset session state
        state.current_session_id = None
        state.session_start_t = None
        state.prev_status = None
        state.sample_buffer.clear()
        return summary

    @app.get("/api/sessions")
    async def list_sessions(limit: int = 50):
        _require_db()
        limit = max(1, min(limit, 1000))
        return await state.db_call(persistence.list_sessions, limit)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: int):
        _require_db()
        result = await state.db_call(persistence.get_session, session_id)
        if result is None:
            raise HTTPException(404, f"session {session_id} not found")
        return result

    @app.get("/api/sessions/{session_id}/samples")
    async def get_session_samples(session_id: int, from_ms: int = 0, to_ms: Optional[int] = None):
        _require_db()
        return await state.db_call(persistence.get_samples, session_id, from_ms, to_ms)

    @app.get("/api/sessions/{session_id}/reps/{rep_id}/analytics")
    async def get_rep_analytics(session_id: int, rep_id: int):
        _require_db()
        sess = await state.db_call(persistence.get_session, session_id)
        if sess is None:
            raise HTTPException(404, f"session {session_id} not found")
        rep = next((r for r in sess["reps"] if r["id"] == rep_id), None)
        if rep is None:
            raise HTTPException(404, f"rep {rep_id} not found in session {session_id}")
        if rep.get("ended_t_offset_ms") is None:
            raise HTTPException(409, f"rep {rep_id} is still open")
        samples = await state.db_call(
            persistence.get_samples, session_id,
            rep["started_t_offset_ms"], rep["ended_t_offset_ms"],
        )
        loop = asyncio.get_running_loop()
        metrics = await loop.run_in_executor(None, analytics.compute_rep_analytics, samples)
        # Decorate with rep id + raw aggregates for convenience
        metrics["rep_id"] = rep_id
        metrics["session_id"] = session_id
        return metrics

    return app


def main():
    p = argparse.ArgumentParser()
    # Default to 0.0.0.0 for phone-on-LAN access. Workshop LAN only — do not
    # expose to a public network.
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port-http", type=int, default=8765)
    p.add_argument("--com", default="auto")
    p.add_argument("--db", default=persistence.DB_PATH)
    args = p.parse_args()

    import uvicorn
    app = make_app(port=args.com, db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port_http, log_level="info")


if __name__ == "__main__":
    main()
