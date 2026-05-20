"""One-shot: write P09.01 = 6 (115200) via Modbus at 9600, then verify at 115200.
Authorised in session 2026-05-20. CLAUDE.md hard rule #3 override for this op only.
Read-only after the single write; armed=False on PPADrive (we use raw client.write_register).
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pymodbus.client import ModbusSerialClient

PORT = "COM5"
DEV = 1
OLD_BAUD = 9600
NEW_BAUD = 115200
NEW_CODE = 6  # per HCFA SV-X3E manual V5.1 page 124: 0:2400 1:4800 2:9600 3:19200 4:38400 5:57600 6:115200


def main():
    print(f"--- A. connect @ {OLD_BAUD}, verify P09.01 == 2 ---")
    c = ModbusSerialClient(port=PORT, baudrate=OLD_BAUD, parity="N", stopbits=1, bytesize=8, timeout=0.5)
    if not c.connect():
        print(f"FAIL: cannot open {PORT} at {OLD_BAUD}")
        return 2
    try:
        r = c.read_holding_registers(address=(9 << 8) | 1, count=1, device_id=DEV)
        if r.isError() or r.registers[0] != 2:
            print(f"ABORT: P09.01 not at expected default 2 — got {r.registers if not r.isError() else r}")
            return 3
        print("  P09.01 currently = 2 (9600), confirmed")
        print(f"--- B. write P09.01 := {NEW_CODE} ({NEW_BAUD}) ---")
        w = c.write_register(address=(9 << 8) | 1, value=NEW_CODE, device_id=DEV)
        if w.isError():
            print(f"WRITE FAILED: {w}")
            return 4
        print(f"  write OK at {OLD_BAUD}. Drive UART now switching to {NEW_BAUD}.")
    finally:
        c.close()

    time.sleep(0.3)

    print(f"--- C. reopen @ {NEW_BAUD} ---")
    c2 = ModbusSerialClient(port=PORT, baudrate=NEW_BAUD, parity="N", stopbits=1, bytesize=8, timeout=0.5)
    if not c2.connect():
        print(f"FAIL: cannot open {PORT} at {NEW_BAUD}")
        return 5
    try:
        r = c2.read_holding_registers(address=(9 << 8) | 1, count=1, device_id=DEV)
        if r.isError():
            print(f"NO RESPONSE at {NEW_BAUD}: {r}")
            print("RECOVERY:")
            print("  1. Try stopbits=2 (drive may now be picky about P09.02=0=8N2 default)")
            print("  2. Plug CN3 USB at 9600 (separate bridge — unaffected by P09.01)")
            print("  3. HMI keypad reset P09.01 := 2")
            return 6
        print(f"  P09.01 read back = {r.registers[0]}  (expect 6)")
        s = c2.read_holding_registers(address=(21 << 8) | 0, count=1, device_id=DEV)
        print(f"  P21.00 status    = {s.registers[0]}  (1=ready, 2=running)")
        # Timed block reads (9 regs each = the snapshot's hot block)
        t0 = time.time()
        n = 0
        for _ in range(100):
            rr = c2.read_holding_registers(address=(21 << 8) | 0, count=9, device_id=DEV)
            if not rr.isError():
                n += 1
        dt = (time.time() - t0) * 1000
        print(f"  100x 9-reg block read = {n}/100 OK in {dt:.1f} ms  ({dt/100:.2f} ms/read)")
    finally:
        c2.close()
    print("--- BAUD BUMP COMPLETE ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
