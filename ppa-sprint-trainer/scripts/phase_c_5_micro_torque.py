"""Phase C.5 — first non-zero torque command (microcommand).

Builds on C.3+C.4 success. Adds:
  - Lower speed limits P03.27 = P03.28 = 100 RPM (~0.6 m/s) BEFORE S-ON
  - After motor enabled at zero torque (proven safe in C.4), write
    P03.25 = -50 (-5.0%, ~0.9 kg-equivalent, retract direction)
  - Hold 3s observing motor reaction
  - Zero out P03.25, stop heartbeat, wait for watchdog disable

Cable MUST be detached. With cable detached the motor will spin freely
under torque command, capped by the 100 RPM speed limit.

Restore order (always, even on Ctrl+C / fault):
  P03.25 = 0  (defang setpoint immediately if not already)
  stop heartbeat (drive disables within ~5s watchdog)
  P00.01 = 7  (back to EtherCAT mode)
  P09.05 = 0
  P04.01 = original
  P03.27 = original
  P03.28 = original

--autogo flag bypasses interactive GO gates.
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
P03_27 = (3 << 8) | 27
P03_28 = (3 << 8) | 28
P04_01 = (4 << 8) | 1
P09_05 = (9 << 8) | 5
P09_11 = (9 << 8) | 11
P21_00 = (21 << 8) | 0
P21_01 = (21 << 8) | 1   # speed RPM
P21_04 = (21 << 8) | 4   # commanded torque
P21_41 = (21 << 8) | 41  # fault
SON_REG = 0x3607
SON_CMD = 0x02

# Test parameters
SPEED_LIMIT_TEST = 100      # RPM cap during test (≈0.58 m/s)
TORQUE_TEST_RAW = -50        # 0.1%/count → -5.0%, signed
HEARTBEAT_INTERVAL = 1.5
ZERO_HOLD_SECONDS = 2.0      # hold at zero torque after S-ON
TORQUE_HOLD_SECONDS = 3.0    # hold the actual non-zero command
WATCHDOG_GRACE = 7.0


def s16(v): return v - 65536 if v >= 32768 else v


def to_u16(v):
    return v if v >= 0 else v + 65536


def read1(c, addr):
    r = c.read_holding_registers(address=addr, count=1, device_id=DEV)
    if r.isError():
        return None
    return r.registers[0]


def write1(c, addr, value):
    if value < 0:
        value = to_u16(value)
    r = c.write_register(address=addr, value=value, device_id=DEV)
    return not r.isError()


def snap(c, label):
    mode = read1(c, P00_01)
    status = read1(c, P21_00)
    rpm = read1(c, P21_01)
    torque = read1(c, P21_04)
    fault = read1(c, P21_41)
    if rpm is not None:
        rpm = s16(rpm)
    if torque is not None:
        torque = s16(torque) * 0.1
    print(f"  [{label}] mode={mode} status={status} "
          f"rpm={rpm} torque={torque}% fault={fault}")
    return {"mode": mode, "status": status, "rpm": rpm,
            "torque": torque, "fault": fault}


class Heartbeat:
    def __init__(self, client, lock):
        self.client = client
        self.lock = lock
        self.stop_event = threading.Event()
        self.thread = None
        self.write_count = 0
        self.error_count = 0

    def _run(self):
        while not self.stop_event.is_set():
            with self.lock:
                try:
                    r = self.client.write_register(
                        address=SON_REG, value=SON_CMD, device_id=DEV)
                    if r.isError():
                        self.error_count += 1
                    else:
                        self.write_count += 1
                except Exception:
                    self.error_count += 1
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
    print(" Phase C.5 — first NON-ZERO torque command")
    print("=" * 64)
    print()
    print("Pre-conditions:")
    print("  - Cable physically DETACHED (motor will spin freely)")
    print("  - HMI Motor Off")
    print("  - CN3 plugged in")
    print("  - Operator at rig, eyes/ears on motor, Ctrl+C ready")
    print()
    print(f"Test command: P03.25 = {TORQUE_TEST_RAW} (= {TORQUE_TEST_RAW*0.1}%, "
          f"≈ {abs(TORQUE_TEST_RAW*0.1)/5.64:.2f} kg-equivalent, retract direction)")
    print(f"Speed cap during test: {SPEED_LIMIT_TEST} RPM (~{SPEED_LIMIT_TEST*0.00576:.2f} m/s)")
    print()
    if not autogo:
        if input("Type GO-C5 to proceed: ").strip() != "GO-C5":
            print("aborted.")
            return 0
    else:
        print("--autogo: proceeding")

    c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {PORT}")
        return 1
    lock = threading.Lock()

    # Originals
    orig = {}
    heartbeat = None
    setpoint_was_set = False
    speed_limits_changed = False

    try:
        print()
        print("[snap] pre-state")
        pre = snap(c, "pre")
        if pre["mode"] != 7 or pre["status"] != 1 or pre["fault"] != 0:
            print("ABORT: drive not in clean pre-state")
            return 2

        for label, reg in [("p04_01", P04_01), ("p09_05", P09_05),
                           ("p00_01", P00_01), ("p03_25", P03_25),
                           ("p03_27", P03_27), ("p03_28", P03_28)]:
            orig[label] = read1(c, reg)
        print(f"  originals: {orig}")
        if orig["p03_25"] != 0:
            print(f"  ABORT: P03.25 not zero ({s16(orig['p03_25'])})")
            return 2

        # ---- Set low speed limits BEFORE S-ON ----
        print()
        print(f"[1] setting speed limits = {SPEED_LIMIT_TEST} RPM")
        write1(c, P03_27, SPEED_LIMIT_TEST)
        write1(c, P03_28, SPEED_LIMIT_TEST)
        speed_limits_changed = True
        a = read1(c, P03_27); b = read1(c, P03_28)
        print(f"  P03.27={a} P03.28={b}  ({'OK' if a==b==SPEED_LIMIT_TEST else 'MISMATCH'})")

        # ---- Comm-DI prep (same as C.3) ----
        print()
        print("[2] comm-DI prep: P04.01=0, P09.05=0x02")
        write1(c, P04_01, 0)
        write1(c, P09_05, 0x02)
        time.sleep(0.2)

        # ---- Mode change ----
        print()
        print("[3] P00.01 = 2 (torque mode)")
        write1(c, P00_01, 2)
        time.sleep(0.2)
        if read1(c, P00_01) != 2:
            print("  ABORT: mode 2 not stuck")
            return 3

        # Sanity check setpoint is still 0
        if read1(c, P03_25) != 0:
            print("  ABORT: P03.25 changed!")
            return 4

        # ---- Heartbeat → motor enable at zero ----
        print()
        print(f"[4] starting heartbeat. Hold {ZERO_HOLD_SECONDS}s at zero torque...")
        heartbeat = Heartbeat(c, lock)
        heartbeat.start()
        t0 = time.time()
        enabled = False
        while time.time() - t0 < ZERO_HOLD_SECONDS:
            with lock:
                st = snap(c, f"zero+{time.time()-t0:.1f}s")
            if st["status"] == 2 and not enabled:
                print(f"  motor enabled at t={time.time()-t0:.1f}s")
                enabled = True
            if st["fault"]:
                print(f"  FAULT during zero-hold: {st['fault']}")
                return 5
            if st["torque"] is not None and abs(st["torque"]) > 1.0:
                print(f"  unexpected torque {st['torque']}% during zero-hold")
                return 5
            time.sleep(0.4)

        if not enabled:
            print("  ABORT: motor never enabled during zero-hold")
            return 5

        # ---- Apply non-zero torque ----
        print()
        print(f"[5] writing P03.25 = {TORQUE_TEST_RAW}  ({TORQUE_TEST_RAW*0.1}%)")
        with lock:
            write1(c, P03_25, TORQUE_TEST_RAW)
        setpoint_was_set = True
        rb = read1(c, P03_25)
        rb_signed = s16(rb)
        print(f"  readback: {rb_signed}  "
              f"({'OK' if rb_signed == TORQUE_TEST_RAW else 'MISMATCH'})")

        print(f"  holding for {TORQUE_HOLD_SECONDS}s, watching motor...")
        t0 = time.time()
        peak_rpm = 0
        peak_torque = 0
        while time.time() - t0 < TORQUE_HOLD_SECONDS:
            with lock:
                st = snap(c, f"trq+{time.time()-t0:.1f}s")
            if st["fault"]:
                print(f"  FAULT during torque hold: {st['fault']}")
                break
            if st["rpm"] is not None and abs(st["rpm"]) > abs(peak_rpm):
                peak_rpm = st["rpm"]
            if st["torque"] is not None and abs(st["torque"]) > abs(peak_torque):
                peak_torque = st["torque"]
            time.sleep(0.3)

        # ---- Zero out the setpoint immediately ----
        print()
        print("[6] writing P03.25 = 0")
        with lock:
            write1(c, P03_25, 0)
        setpoint_was_set = False
        time.sleep(0.5)

        # ---- Stop heartbeat, watch for watchdog disable ----
        print()
        print("[7] stopping heartbeat, waiting for watchdog disable...")
        heartbeat.stop()
        heartbeat = None
        t0 = time.time()
        while time.time() - t0 < WATCHDOG_GRACE:
            with lock:
                status = read1(c, P21_00)
            print(f"  t+{time.time()-t0:.1f}s status={status}")
            if status == 1:
                print(f"  watchdog disabled at t={time.time()-t0:.1f}s — OK")
                break
            time.sleep(0.5)

        print()
        print("=" * 50)
        print(" C.5 RESULTS")
        print("=" * 50)
        print(f"  Setpoint commanded: {TORQUE_TEST_RAW*0.1}%")
        print(f"  Peak measured RPM : {peak_rpm}  (cap was {SPEED_LIMIT_TEST})")
        print(f"  Peak P21.04       : {peak_torque}%")

    except KeyboardInterrupt:
        print("\nCtrl+C received, running cleanup")
    finally:
        # Always: zero setpoint first, then stop heartbeat, then restore params
        print()
        print("[cleanup] zeroing P03.25 if needed")
        try:
            with lock:
                write1(c, P03_25, 0)
        except Exception:
            pass
        if heartbeat is not None:
            print("[cleanup] stopping heartbeat")
            heartbeat.stop()
            time.sleep(6)  # ensure watchdog has fired before mode-change
        else:
            time.sleep(0.5)
        if "p00_01" in orig:
            print("[cleanup] restoring P00.01 = 7")
            with lock:
                write1(c, P00_01, 7)
        if "p09_05" in orig:
            print("[cleanup] restoring P09.05")
            with lock:
                write1(c, P09_05, orig["p09_05"])
        if "p04_01" in orig:
            print(f"[cleanup] restoring P04.01 = {orig['p04_01']}")
            with lock:
                write1(c, P04_01, orig["p04_01"])
        if speed_limits_changed and "p03_27" in orig:
            print(f"[cleanup] restoring P03.27 = {orig['p03_27']}, "
                  f"P03.28 = {orig['p03_28']}")
            with lock:
                write1(c, P03_27, orig["p03_27"])
                write1(c, P03_28, orig["p03_28"])
        time.sleep(0.3)
        print()
        snap(c, "final")
        c.close()


if __name__ == "__main__":
    main()
