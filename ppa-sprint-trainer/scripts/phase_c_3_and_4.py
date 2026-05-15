"""Phase C.3 + C.4 combined runner.

C.3: prep comm-DI (P04.01=0 to free DI1, P09.05=0x02 to enable comm-DI
     bit-1=S_ON), verify P09.11 watchdog = 5. Restore at end.

C.4: enter mode 2 (P00.01=2), start heartbeat thread writing 0x02 to
     0x3607 every 1.5s, watch P21.00 transition 1→2 (motor enabled at
     zero torque), hold ~8s monitoring, stop heartbeat, watch P21.00
     2→1 transition within 5s watchdog. Restore mode 7.

The motor WILL energise during C.4 but with P03.25=0 there is no torque
command — should sit silent. P03.27/P03.28 = 3000 RPM speed cap.

Safety:
- Cable must be detached (cannot move anything).
- Verifies P03.25=0 BEFORE starting heartbeat. Aborts if not.
- Heartbeat thread checks a stop Event, exits cleanly on Ctrl+C.
- finally block always restores all writes. Restore order:
  stop heartbeat → mode 7 → P09.05=0 → P04.01=original.
- Two GO gates: one before C.3, one before C.4.
"""
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pymodbus.client import ModbusSerialClient

PORT = "COM6"
DEV = 1

# Registers
P00_01 = (0 << 8) | 1
P03_25 = (3 << 8) | 25
P04_01 = (4 << 8) | 1
P09_05 = (9 << 8) | 5
P09_11 = (9 << 8) | 11
P21_00 = (21 << 8) | 0
P21_04 = (21 << 8) | 4
P21_41 = (21 << 8) | 41
SON_REG = 0x3607
SON_CMD = 0x02

# Timings
HEARTBEAT_INTERVAL = 1.5  # seconds between writes (well under 5s watchdog)
HOLD_SECONDS = 8.0
WATCHDOG_GRACE = 7.0  # how long after heartbeat stops to wait for disable


def s16(v): return v - 65536 if v >= 32768 else v


def read1(c, address):
    r = c.read_holding_registers(address=address, count=1, device_id=DEV)
    if r.isError():
        return None
    return r.registers[0]


def write1(c, address, value):
    r = c.write_register(address=address, value=value, device_id=DEV)
    return not r.isError()


def snap(c, label):
    mode = read1(c, P00_01)
    status = read1(c, P21_00)
    torque = read1(c, P21_04)
    fault = read1(c, P21_41)
    if torque is not None:
        torque = s16(torque) * 0.1
    print(f"  [{label}] P00.01={mode} P21.00={status} "
          f"P21.04={torque}% P21.41={fault}")
    return {"mode": mode, "status": status, "fault": fault}


class Heartbeat:
    """Background thread that writes SON_CMD to SON_REG every interval."""
    def __init__(self, client, lock):
        self.client = client
        self.lock = lock
        self.stop_event = threading.Event()
        self.thread = None
        self.write_count = 0
        self.error_count = 0
        self.last_error = None

    def _run(self):
        while not self.stop_event.is_set():
            with self.lock:
                try:
                    r = self.client.write_register(
                        address=SON_REG, value=SON_CMD, device_id=DEV)
                    if r.isError():
                        self.error_count += 1
                        self.last_error = str(r)
                    else:
                        self.write_count += 1
                except Exception as e:
                    self.error_count += 1
                    self.last_error = repr(e)
            self.stop_event.wait(HEARTBEAT_INTERVAL)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=HEARTBEAT_INTERVAL + 1)


def main():
    autogo = "--autogo" in sys.argv
    print("=" * 64)
    print(" Phase C.3 + C.4 — comm-DI prep + first S-ON via Modbus")
    print("=" * 64)
    print()
    if autogo:
        print(" --autogo flag set: assuming user has authorized externally.")
        print(" Cable must already be detached, HMI Motor Off, operator at rig.")
        print()
    else:
        print("Pre-conditions YOU must confirm:")
        print("  1. Cable physically detached (not connected to anything)")
        print("  2. HMI Motor Off")
        print("  3. CN3 plugged in")
        print("  4. You're at the rig with eyes on the motor")
        print()
        print("During C.4 the motor WILL be ENABLED (energised). It should sit")
        print("silent because P03.25=0. If it makes ANY noise or any movement,")
        print("hit Ctrl+C immediately. Watchdog (5s) is the secondary backstop.")
        print()
        if input("Type GO-C3 to start Phase C.3: ").strip() != "GO-C3":
            print("aborted.")
            return 0

    c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {PORT}")
        return 1
    lock = threading.Lock()

    # Save originals so we can restore even on error
    original_p04_01 = None
    original_p09_05 = None
    original_p00_01 = None
    heartbeat = None

    try:
        # ---------- Phase C.3 ----------
        print()
        print("=" * 50)
        print("Phase C.3 — comm-DI prep")
        print("=" * 50)
        snap(c, "pre-C.3")

        # Snapshot P09.11 (must be reasonable watchdog)
        p09_11 = read1(c, P09_11)
        if p09_11 is None or p09_11 == 0 or p09_11 > 30:
            print(f"  ABORT: P09.11 watchdog = {p09_11} (need 1..30s)")
            return 2
        print(f"  P09.11 watchdog = {p09_11}s  (OK)")

        # Snapshot originals
        original_p04_01 = read1(c, P04_01)
        original_p09_05 = read1(c, P09_05)
        original_p00_01 = read1(c, P00_01)
        print(f"  originals: P04.01={original_p04_01}  P09.05={original_p09_05}  "
              f"P00.01={original_p00_01}")

        if original_p00_01 != 7:
            print(f"  ABORT: drive not in EtherCAT mode (P00.01={original_p00_01})")
            return 2

        # Verify setpoint is zero before we even start
        p03_25 = read1(c, P03_25)
        if p03_25 != 0:
            print(f"  ABORT: P03.25 torque setpoint is {s16(p03_25)} (= "
                  f"{s16(p03_25)*0.1}%), expected 0")
            return 2
        print(f"  P03.25 setpoint = 0  (OK)")

        # Write P04.01 = 0
        print()
        print("  writing P04.01 = 0 (free DI1 from physical S-ON)")
        if not write1(c, P04_01, 0):
            print("  WRITE FAILED")
            return 3
        rb = read1(c, P04_01)
        print(f"    readback: {rb}  ({'OK' if rb == 0 else 'MISMATCH'})")

        # Write P09.05 = 0x02
        print("  writing P09.05 = 0x02 (enable comm-DI bit 1 = S_ON)")
        if not write1(c, P09_05, 0x02):
            print("  WRITE FAILED")
            return 3
        rb = read1(c, P09_05)
        print(f"    readback: {rb}  ({'OK' if rb == 0x02 else 'MISMATCH'})")

        time.sleep(0.5)
        print()
        print("  state after C.3 writes (motor must STILL be disabled):")
        st = snap(c, "after-C.3")
        if st["status"] != 1:
            print(f"  ABORT: motor enabled unexpectedly after C.3 prep!")
            return 4
        if st["fault"] != 0:
            print(f"  ABORT: fault code {st['fault']}")
            return 4
        print()
        print("  C.3 PASSED — comm-DI prepared, motor still disabled, no fault")

        # ---------- Phase C.4 gate ----------
        print()
        print("Next: Phase C.4 — write 0x3607 heartbeat, motor will ENABLE.")
        print("Final check: cable detached, HMI Motor Off, eyes on motor.")
        if autogo:
            print("  --autogo: proceeding directly to C.4")
        elif input("Type GO-C4 to proceed (anything else triggers restore): ").strip() != "GO-C4":
            print("  user aborted before C.4. Will restore C.3 writes and exit.")
            return 0

        # ---------- Phase C.4 ----------
        print()
        print("=" * 50)
        print("Phase C.4 — first 0x3607 write with heartbeat")
        print("=" * 50)

        # Switch to mode 2
        print("  writing P00.01 = 2 (torque mode)")
        if not write1(c, P00_01, 2):
            print("  WRITE FAILED")
            return 5
        time.sleep(0.2)
        m = read1(c, P00_01)
        print(f"    readback: {m}  ({'OK' if m == 2 else 'MISMATCH'})")
        if m != 2:
            return 5

        # Final zero-torque check
        if read1(c, P03_25) != 0:
            print("  ABORT: P03.25 changed from 0!")
            return 6

        # Start heartbeat
        print()
        print(f"  starting heartbeat (writing {SON_CMD:#x} to "
              f"0x{SON_REG:04X} every {HEARTBEAT_INTERVAL}s)...")
        heartbeat = Heartbeat(c, lock)
        heartbeat.start()

        # Monitor for transition 1→2
        print(f"  monitoring P21.00 for {HOLD_SECONDS}s "
              f"(should go 1→2 within ~2s)...")
        t0 = time.time()
        last_status = None
        while time.time() - t0 < HOLD_SECONDS:
            with lock:
                status = read1(c, P21_00)
                fault = read1(c, P21_41)
                torque = read1(c, P21_04)
            if torque is not None:
                torque = s16(torque) * 0.1
            now = time.time() - t0
            if status != last_status:
                print(f"    t={now:.1f}s  P21.00={status} P21.04={torque}% "
                      f"P21.41={fault}  hb_writes={heartbeat.write_count}")
                last_status = status
            if fault and fault != 0:
                print(f"  FAULT during hold ({fault}). Stopping.")
                break
            if torque is not None and abs(torque) > 1.0:
                print(f"  UNEXPECTED TORQUE ({torque}%)! Stopping.")
                break
            time.sleep(0.3)

        # Stop heartbeat, watch for watchdog disable
        print()
        print(f"  stopping heartbeat. Drive should auto-disable within "
              f"{p09_11}s (watchdog).")
        heartbeat.stop()
        heartbeat = None

        t0 = time.time()
        while time.time() - t0 < WATCHDOG_GRACE:
            with lock:
                status = read1(c, P21_00)
            now = time.time() - t0
            print(f"    t={now:.1f}s after heartbeat stop  P21.00={status}")
            if status == 1:
                print(f"  watchdog disabled the drive at t={now:.1f}s — OK")
                break
            time.sleep(0.5)
        else:
            print(f"  WARNING: watchdog did not disable within {WATCHDOG_GRACE}s")

        print()
        print("  C.4 done")

    except KeyboardInterrupt:
        print("\n  Ctrl+C — running cleanup")
    finally:
        # Always: stop heartbeat, restore mode, restore params
        if heartbeat is not None:
            print("  [cleanup] stopping heartbeat")
            heartbeat.stop()
            time.sleep(p09_11 + 1 if 'p09_11' in dir() else 6)
        print("  [cleanup] restoring P00.01 = 7")
        if original_p00_01 == 7:
            with lock:
                write1(c, P00_01, 7)
        print("  [cleanup] restoring P09.05 = 0")
        if original_p09_05 is not None:
            with lock:
                write1(c, P09_05, original_p09_05)
        print(f"  [cleanup] restoring P04.01 = {original_p04_01}")
        if original_p04_01 is not None:
            with lock:
                write1(c, P04_01, original_p04_01)
        time.sleep(0.3)
        print()
        snap(c, "final")
        c.close()

    print()
    print("=" * 64)
    print(" Done. Verify final state above shows mode 7, status 1, no fault.")
    print("=" * 64)


if __name__ == "__main__":
    main()
