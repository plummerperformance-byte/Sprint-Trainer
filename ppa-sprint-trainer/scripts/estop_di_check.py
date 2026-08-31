"""estop_di_check.py — read-only: confirm the rig's E-stop is wired + configured
as a real drive-level emergency stop.

Context (2026-08): the physical E-stop traces to the SV-X3E drive's CN1 I/O
terminal. CN1's digital inputs get their function from P04.01..P04.09 (DI1..DI9),
where function code 30 == ESTOP. Each DI's active logic is P04.11..P04.19.

This script reads those parameters over Modbus/RS485 (the same CN4 path the app
uses) and reports:
  1. which DI, if any, is assigned ESTOP (function 30), and
  2. that DI's logic setting, with the fail-safe caveat.

READ-ONLY. No writes, no arming — safe to run anytime the drive is powered and
the USB-RS485 adapter is connected. It does NOT move the motor.

The drive honours the ESTOP DI in firmware regardless of command source, so a
DI-30 e-stop stops a Modbus-driven rep too (independent of the mode 7->2 switch).
But parameter config is not proof — finish with the behavioural tests printed at
the end before trusting it with an athlete.

Usage:
  python scripts/estop_di_check.py            # auto-detect FTDI port, else COM6
  python scripts/estop_di_check.py --port COM7
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ppa_drive import PPADrive  # noqa: E402

ESTOP_FUNC = 30
# Minimal decode — 30 is the one that matters here; others print as raw codes.
KNOWN_FUNCS = {0: "(none/unassigned)", 29: "STHOME (homing start)",
               30: "ESTOP (emergency stop)", 31: "STEP (step enable)"}


def find_ftdi_port():
    try:
        from serial.tools import list_ports
    except Exception:
        return None
    for p in list_ports.comports():
        if (getattr(p, "vid", None) == 0x0403) or ("FTDI" in (p.manufacturer or "")):
            return p.device
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None, help="serial port (default: auto-detect FTDI, else COM6)")
    ap.add_argument("--id", type=int, default=1, help="Modbus device id (default 1)")
    args = ap.parse_args()

    port = args.port or find_ftdi_port() or "COM6"
    print(f"Connecting to drive on {port} @ 9600 8N1, id={args.id} ...")
    try:
        drive = PPADrive(port=port, device_id=args.id).connect()
    except Exception as e:
        print(f"  FAILED to open the drive: {e}")
        print("  Is the USB-RS485 adapter plugged into CN4, the drive powered, and no")
        print("  other program holding the port? Try --port COMx.")
        return 2

    try:
        # DI function assignments: P04.01..P04.09  (DI_n function = idx n)
        funcs = {}
        for n in range(1, 10):
            try:
                funcs[n] = drive._read_u16(4, n)
            except Exception as e:
                funcs[n] = None
                print(f"  read P04.{n:02d} failed: {e}")

        estop_dis = [n for n, v in funcs.items() if v == ESTOP_FUNC]

        print("\n--- CN1 digital-input function map (P04.01..P04.09) ---")
        for n in range(1, 10):
            v = funcs[n]
            label = "?" if v is None else KNOWN_FUNCS.get(v, f"code {v}")
            mark = "  <-- ESTOP" if v == ESTOP_FUNC else ""
            print(f"  DI{n}: P04.{n:02d} = {v}   {label}{mark}")

        print("\n--- Verdict ---")
        if not estop_dis:
            print("  ** No CN1 DI is assigned ESTOP (function 30). **")
            print("  The e-stop wire may be on an unconfigured/other-function DI, or on a")
            print("  different circuit than expected. As-is, pressing it would NOT trigger")
            print("  the drive's emergency stop. Confirm wiring + assign function 30 to the")
            print("  DI the e-stop lands on (needs a justified write — not done here).")
            return 1

        for n in estop_dis:
            logic = None
            try:
                logic = drive._read_u16(4, 10 + n)  # DI_n logic = P04.1(n)
            except Exception as e:
                print(f"  read DI{n} logic P04.{10 + n} failed: {e}")
            print(f"  DI{n} is the ESTOP input (function 30). Logic P04.{10 + n} = {logic}.")
            print("  FAIL-SAFE CHECK: with a normally-closed e-stop button, the DI logic must")
            print("  make an OPEN contact = ESTOP active (default is 'valid when connected',")
            print("  which is the WRONG way round for an NC button). Confirm the exact 0/1")
            print("  meaning in the manual's P04.1x table, or prove it with the wire-out test")
            print("  below — do not assume from the value alone.")

        print("\n--- Prove it before any athlete (behavioural, at the rig, cable OFF) ---")
        print("  1. Powered, idle: drive should NOT already be in ESTOP/AL.095.")
        print("  2. Press e-stop -> drive stops + faults AL.095. Release/reset -> clears.")
        print("  3. FAIL-SAFE: with the drive idle, DISCONNECT the e-stop wire (simulate a")
        print("     broken wire). It SHOULD go to AL.095. If nothing happens, it is wired")
        print("     NO / wrong logic and is NOT fail-safe.")
        print("  4. MODE-2 (the real one): drive a rep via the laptop (Phase C, mode 2), hit")
        print("     the e-stop, confirm the drive actually stops. This proves the ESTOP DI")
        print("     is honoured while WE control it, not just in vendor EtherCAT mode.")
        return 0
    finally:
        drive.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
