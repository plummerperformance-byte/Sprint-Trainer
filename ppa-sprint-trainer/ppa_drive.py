"""ppa_drive — clean read/write wrapper for HCFA SV-X3E over Modbus RTU.

Connection: Mini-USB → CN3 → STM32 virtual COM port (default COM6).
Addressing: P[group].[idx] -> holding register (group << 8) | idx.

Writes are gated by an `armed` flag. Construct with `armed=True` only after
explicit confirmation; otherwise every write method raises RuntimeError.
"""
from pymodbus.client import ModbusSerialClient


# Scaling constants (0.1-units on this drive, confirmed for torque limits + bus voltage)
TORQUE_PERCENT_PER_COUNT = 0.1
BUS_VOLT_PER_COUNT = 0.1
TORQUE_LIMIT_MAX_PERCENT = 300.0   # factory ceiling — refuse writes above this


class PPADrive:
    def __init__(self, port="COM6", device_id=1, baudrate=9600, timeout=0.5,
                 armed=False):
        self.port = port
        self.device_id = device_id
        self.baudrate = baudrate
        self.timeout = timeout
        self.armed = armed
        self.client = None

    # ---------- lifecycle ----------
    def connect(self):
        self.client = ModbusSerialClient(
            port=self.port, baudrate=self.baudrate,
            parity="N", stopbits=1, bytesize=8, timeout=self.timeout,
        )
        if not self.client.connect():
            raise IOError(f"could not open serial port {self.port}")
        return self

    def disconnect(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.disconnect()

    # ---------- low-level helpers ----------
    @staticmethod
    def _addr(group, idx):
        return (group << 8) | idx

    @staticmethod
    def _s16(v):
        return v - 65536 if v >= 32768 else v

    @staticmethod
    def _s32(lo, hi):
        v = (hi << 16) | lo
        return v - (1 << 32) if v >= (1 << 31) else v

    def _read(self, group, idx, count=1):
        if self.client is None:
            raise IOError("not connected — call connect() first")
        addr = self._addr(group, idx)
        r = self.client.read_holding_registers(
            address=addr, count=count, device_id=self.device_id
        )
        if r.isError():
            raise IOError(f"Modbus read error at P{group:02d}.{idx:02d} "
                          f"(0x{addr:04X}): {r}")
        return r.registers

    def _read_u16(self, group, idx):
        return self._read(group, idx, 1)[0]

    def _read_s16(self, group, idx):
        return self._s16(self._read(group, idx, 1)[0])

    def _read_s32(self, group, idx):
        regs = self._read(group, idx, 2)
        return self._s32(regs[0], regs[1])

    def _write(self, group, idx, value):
        if not self.armed:
            raise RuntimeError(
                f"PPADrive is not armed — refused write to P{group:02d}.{idx:02d}. "
                f"Construct with armed=True only after explicit confirmation."
            )
        if self.client is None:
            raise IOError("not connected — call connect() first")
        addr = self._addr(group, idx)
        r = self.client.write_register(
            address=addr, value=value, device_id=self.device_id
        )
        if r.isError():
            raise IOError(f"Modbus write error at P{group:02d}.{idx:02d} "
                          f"(0x{addr:04X}): {r}")

    # ---------- live readback (P21 monitoring group) ----------
    def read_status(self):
        """P21.00 — servo status. 1 = ready (unpowered/standby)."""
        return self._read_u16(21, 0)

    def read_speed(self):
        """P21.01 — motor speed in RPM (signed)."""
        return self._read_s16(21, 1)

    def read_torque(self):
        """P21.04 — internal torque instruction, % of rated.
        Scaling assumed 0.1%/count (matches P03.11 limit-side scaling).
        Verify when motor is energised."""
        return self._read_s16(21, 4) * TORQUE_PERCENT_PER_COUNT

    def read_position(self):
        """P21.07 + P21.08 — cumulative absolute position counter, signed 32-bit."""
        return self._read_s32(21, 7)

    def read_bus_voltage(self):
        """P21.06 — DC bus voltage in volts (raw count is 0.1 V)."""
        return self._read_u16(21, 6) * BUS_VOLT_PER_COUNT

    def read_temperature(self):
        """P21.31 — module temperature, °C (raw)."""
        return self._read_s16(21, 31)

    def read_fault_code(self):
        """P21.41 — current fault code (0 = no fault)."""
        return self._read_u16(21, 41)

    def read_p21_fast_block(self):
        """One-shot read of the contiguous P21.00–P21.08 fast block.

        Replaces 5 individual reads (status / speed / torque / bus_v / position)
        with a single round-trip. P21.02, .03, .05 are undocumented on this drive
        and discarded; the drive returns 0 for unmapped registers in a block read.

        Raises IOError on Modbus failure — caller can fall back to per-field reads
        if the drive ever rejects block reads over the unmapped gaps.
        """
        regs = self._read(21, 0, 9)  # P21.00..P21.08
        return {
            "status":          regs[0],
            "speed_rpm":       self._s16(regs[1]),
            "torque_pct":      self._s16(regs[4]) * TORQUE_PERCENT_PER_COUNT,
            "bus_voltage_v":   regs[6] * BUS_VOLT_PER_COUNT,
            "position_counts": self._s32(regs[7], regs[8]),
        }

    # ---------- control mode ----------
    def get_control_mode(self):
        """P00.01 — current control-mode selection.
        Documented modes 0–6 (0=Pos 1=Spd 2=Torque 3=P/S 4=P/T 5=S/T 6=Full-closed).
        Mode 7 has been observed on this rig — likely EtherCAT/CIA402 override."""
        return self._read_u16(0, 1)

    def set_control_mode(self, mode):
        if not 0 <= mode <= 7:
            raise ValueError(f"control mode out of range: {mode}")
        self._write(0, 1, mode)

    # ---------- torque limits (the variable-resistance lever) ----------
    def get_torque_limit(self):
        """Read the full P03.08–P03.12 block.
        Returns dict with 'source' (0=internal, 1=external),
        and forward/reverse limits in % for both internal and external."""
        regs = self._read(3, 8, 5)
        return {
            "source": regs[0],
            "internal_forward_pct": regs[1] * TORQUE_PERCENT_PER_COUNT,
            "internal_reverse_pct": regs[2] * TORQUE_PERCENT_PER_COUNT,
            "external_forward_pct": regs[3] * TORQUE_PERCENT_PER_COUNT,
            "external_reverse_pct": regs[4] * TORQUE_PERCENT_PER_COUNT,
        }

    def set_torque_limit(self, forward_pct=None, reverse_pct=None, external=True):
        """Set torque limit value(s). Does NOT change the source (P03.08).
        forward_pct / reverse_pct in % (e.g. 100.0). Capped at 300.0%."""
        for v in (forward_pct, reverse_pct):
            if v is not None and not 0.0 <= v <= TORQUE_LIMIT_MAX_PERCENT:
                raise ValueError(
                    f"torque limit {v}% outside [0, {TORQUE_LIMIT_MAX_PERCENT}]"
                )
        f_idx = 11 if external else 9
        r_idx = 12 if external else 10
        if forward_pct is not None:
            self._write(3, f_idx, int(round(forward_pct / TORQUE_PERCENT_PER_COUNT)))
        if reverse_pct is not None:
            self._write(3, r_idx, int(round(reverse_pct / TORQUE_PERCENT_PER_COUNT)))

    def set_torque_limit_source(self, external):
        """P03.08 — flip torque-limit source between internal (0) and external (1)."""
        self._write(3, 8, 1 if external else 0)


def _self_test():
    """Read-only self-test against the live drive."""
    print("ppa_drive self-test (read-only, armed=False)")
    print("-" * 60)
    with PPADrive() as drive:
        print(f"  status       : {drive.read_status()}  (1 = ready)")
        print(f"  speed (RPM)  : {drive.read_speed()}")
        print(f"  torque (%)   : {drive.read_torque():.1f}")
        print(f"  position     : {drive.read_position()}")
        print(f"  bus voltage  : {drive.read_bus_voltage():.1f} V")
        print(f"  temp (°C)    : {drive.read_temperature()}")
        print(f"  fault code   : {drive.read_fault_code()}")
        print(f"  control mode : {drive.get_control_mode()}")
        print(f"  torque limit : {drive.get_torque_limit()}")
        print()
        print("  write-guard check: trying set_torque_limit(forward_pct=100) "
              "on an unarmed drive...")
        try:
            drive.set_torque_limit(forward_pct=100.0)
            print("  FAIL — write was not blocked!")
        except RuntimeError as e:
            print(f"  OK — blocked: {e}")


if __name__ == "__main__":
    _self_test()
