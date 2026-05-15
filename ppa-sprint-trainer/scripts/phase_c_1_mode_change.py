"""Phase C.1 — Mode change test.

Writes P00.01 from 7 (EtherCAT) to 2 (Torque), holds for 2 seconds confirming
it sticks and motor stays disabled, then restores P00.01 = 7. No S-ON. No
setpoint writes. Worst case: drive faults and we restore mode 7.

Pre-conditions (operator must confirm):
  - Cable physically detached or guaranteed clear of any human
  - HMI Motor Off (HMI not commanding via EtherCAT)
  - CN3 mini-USB plugged in (so Modbus on COM6 works)
  - Operator at the rig, eyes on motor, Ctrl+C ready

Stop criteria (script aborts and restores):
  - Any unexpected fault (P21.41 != 0)
  - P00.01 not retaining the written value (means EtherCAT is fighting back)
  - P21.00 transitioning to 2 (motor enabling unexpectedly)

Even if every check fails, the worst this can do is leave mode 2 set with
motor disabled and zero torque setpoint — no motion possible.
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pymodbus.client import ModbusSerialClient

PORT = "COM6"
DEV = 1
P00_01 = (0 << 8) | 1
P21_00 = (21 << 8) | 0
P21_04 = (21 << 8) | 4
P21_41 = (21 << 8) | 41


def s16(v):
    return v - 65536 if v >= 32768 else v


def read_one(c, addr):
    r = c.read_holding_registers(address=addr, count=1, device_id=DEV)
    if r.isError():
        return None, f"err: {r}"
    return r.registers[0], "ok"


def write_one(c, addr, value):
    r = c.write_register(address=addr, value=value, device_id=DEV)
    if r.isError():
        return False, f"err: {r}"
    return True, "ok"


def status_snapshot(c, label):
    mode, _ = read_one(c, P00_01)
    status, _ = read_one(c, P21_00)
    torque, _ = read_one(c, P21_04)
    fault, _ = read_one(c, P21_41)
    if torque is not None:
        torque = s16(torque) * 0.1
    print(f"  [{label}] P00.01={mode}  P21.00={status}  "
          f"P21.04={torque}%  P21.41={fault}")
    return mode, status, fault


def main():
    print("=" * 64)
    print(" Phase C.1 — Mode change test (P00.01 7 → 2 → 7)")
    print("=" * 64)
    print()
    print("Pre-conditions checklist:")
    print("  [ ] Cable physically detached or clear of humans")
    print("  [ ] HMI Motor Off")
    print("  [ ] CN3 plugged in (you're running this script, so probably yes)")
    print()
    print("This script will:")
    print("  1. Snapshot current state")
    print("  2. Write P00.01 = 2 (torque mode)")
    print("  3. Read P00.01 + status 5 times over 2s, watch for instability")
    print("  4. ALWAYS restore P00.01 = 7 at the end (even on error)")
    print()
    ans = input("Type GO to proceed (anything else aborts): ").strip()
    if ans != "GO":
        print("aborted.")
        return 0

    c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {PORT}")
        return 1

    restore_needed = False
    try:
        # Snapshot
        print()
        print("[1] snapshot")
        mode_before, status_before, fault_before = status_snapshot(c, "before")
        if mode_before != 7:
            print(f"  WARNING: P00.01 is not 7 ({mode_before}). Aborting to be safe.")
            return 2
        if status_before != 1:
            print(f"  WARNING: P21.00 is not 1 ({status_before}). Motor not "
                  "in standby. Aborting.")
            return 2
        if fault_before != 0:
            print(f"  WARNING: P21.41 fault present ({fault_before}). Aborting.")
            return 2

        # Write
        print()
        print("[2] writing P00.01 = 2 (torque mode)")
        ok, msg = write_one(c, P00_01, 2)
        if not ok:
            print(f"  WRITE FAILED: {msg}")
            return 3
        restore_needed = True
        print(f"  write: {msg}")

        # Watch
        print()
        print("[3] watching for 2 seconds (5 reads, 0.4s apart)")
        for i in range(5):
            mode, status, fault = status_snapshot(c, f"t={i*0.4:.1f}s")
            if mode != 2:
                print(f"  ABORT: P00.01 reverted to {mode}, EtherCAT fighting back")
                return 4
            if status != 1:
                print(f"  ABORT: P21.00 changed to {status}, motor enabling unexpectedly")
                return 4
            if fault != 0:
                print(f"  ABORT: fault {fault}")
                return 4
            time.sleep(0.4)

        print()
        print("[4] hold checks passed — mode 2 is stable, motor disabled, no fault")

    finally:
        if restore_needed:
            print()
            print("[5] restoring P00.01 = 7 (EtherCAT mode)")
            ok, msg = write_one(c, P00_01, 7)
            print(f"  restore: {msg}")
            mode_after, _ = read_one(c, P00_01)
            print(f"  P00.01 after restore = {mode_after}  "
                  f"({'OK' if mode_after == 7 else 'MISMATCH — manual restore needed!'})")
            status_snapshot(c, "after")
        c.close()

    print()
    print("=" * 64)
    print(" RESULT: Phase C.1 PASSED — mode change works, no fault")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
