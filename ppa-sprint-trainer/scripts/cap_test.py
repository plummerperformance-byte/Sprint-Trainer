"""Torque-limit cap test.

Lower the reverse torque limit below a known steady-pull commanded torque
and verify that P21.04 clamps at the new limit. This is the "limit-modulator"
proof-of-concept: if the cap takes effect, Modbus can act as a real-time
limit clamp on the EtherCAT-driven torque setpoint.

Phases:
  A. Read-only snapshot of P03.08 (limit source) + P03.09/10 (internal fwd/rev)
     + P03.11/12 (external fwd/rev). Read steady-state P21.04 under a 5 kg HMI
     pull (~14% commanded torque per the 2.8%/kg calibration).
  B. ARM, lower the active reverse limit to ~half the steady pull torque.
     Re-pull and confirm clamp.
  C. Restore original value, verify readback.

Safety: only ever LOWERS the limit, never raises. Always restores in finally.
Writes require typed 'YES' confirmation per write.
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import time
from pymodbus.client import ModbusSerialClient
from pymodbus.pdu import ExceptionResponse

PORT = "COM6"
DEVICE_ID = 1
BAUDRATE = 9600
TIMEOUT = 0.5

# Registers
P03_08 = (3 << 8) | 8   # 0x0308 — limit source (0=internal, 1=external)
P03_09 = (3 << 8) | 9   # 0x0309 — internal forward limit (0.1%/count)
P03_10 = (3 << 8) | 10  # 0x030A — internal reverse limit
P03_11 = (3 << 8) | 11  # 0x030B — external forward limit
P03_12 = (3 << 8) | 12  # 0x030C — external reverse limit
P21_00 = (21 << 8) | 0  # status
P21_04 = (21 << 8) | 4  # commanded torque (signed s16, 0.1%/count)

ERROR_CODES = {
    1: "Wrong command", 3: "Invalid parameter", 4: "CRC error",
    16: "Parameter group number data overflow", 17: "Register quantity is 0",
    18: "32-bit data only reading 16-bit (H or L)",
    19: "Parameter exceeding upper/lower limit",
    20: "Not input password or password expired",
    22: "Parameter not editable or restricted",
    24: "Password parameter not to be edited with others",
    25: "Wrong password input",
    26: "Wrong password input 5 times in a row",
}


def describe(resp):
    if isinstance(resp, ExceptionResponse):
        code = getattr(resp, "exception_code", None)
        return f"ExceptionResponse code={code} ({ERROR_CODES.get(code, '?')}) | {resp}"
    if hasattr(resp, "isError") and resp.isError():
        code = getattr(resp, "exception_code", None)
        return f"isError code={code} ({ERROR_CODES.get(code, '?')}) | {resp}"
    return f"OK | {resp}"


def s16(v):
    return v - 65536 if v >= 32768 else v


def read_u16(client, addr):
    r = client.read_holding_registers(address=addr, count=1, device_id=DEVICE_ID)
    if r.isError():
        raise IOError(f"read 0x{addr:04X} failed: {describe(r)}")
    return r.registers[0]


def read_s16(client, addr):
    return s16(read_u16(client, addr))


def write_u16(client, addr, value):
    return client.write_register(address=addr, value=value, device_id=DEVICE_ID)


def confirm(prompt):
    ans = input(f"{prompt} (type YES to proceed): ").strip()
    return ans == "YES"


def sample_torque(client, seconds=5.0, hz=10):
    """Return (mean_pct, min_pct, max_pct, n) of |P21.04| over the window."""
    samples = []
    end = time.monotonic() + seconds
    period = 1.0 / hz
    while time.monotonic() < end:
        v = read_s16(client, P21_04) * 0.1  # %
        samples.append(v)
        time.sleep(period)
    if not samples:
        return 0.0, 0.0, 0.0, 0
    mags = [abs(s) for s in samples]
    return sum(mags) / len(mags), min(mags), max(mags), len(mags)


def main():
    print("=" * 64)
    print(" HCFA SV-X3E — torque-limit cap test")
    print("=" * 64)

    client = ModbusSerialClient(port=PORT, baudrate=BAUDRATE, parity="N",
                                stopbits=1, bytesize=8, timeout=TIMEOUT)
    if not client.connect():
        print(f"FAIL — could not open {PORT}. Is ppa_service holding the port?")
        return 1

    target_addr = None
    target_name = None
    original_value = None
    write_done = False

    try:
        # ---------- Phase A: snapshot ----------
        print("\n[A] reading P03.08–P03.12 snapshot...")
        source = read_u16(client, P03_08)
        int_fwd = read_u16(client, P03_09)
        int_rev = read_u16(client, P03_10)
        ext_fwd = read_u16(client, P03_11)
        ext_rev = read_u16(client, P03_12)
        status = read_u16(client, P21_00)
        print(f"      P03.08 limit source     = {source}  ({'external' if source else 'internal'})")
        print(f"      P03.09 internal fwd lim = {int_fwd:5d}  ({int_fwd*0.1:.1f}%)")
        print(f"      P03.10 internal rev lim = {int_rev:5d}  ({int_rev*0.1:.1f}%)")
        print(f"      P03.11 external fwd lim = {ext_fwd:5d}  ({ext_fwd*0.1:.1f}%)")
        print(f"      P03.12 external rev lim = {ext_rev:5d}  ({ext_rev*0.1:.1f}%)")
        print(f"      P21.00 servo status     = {status}  (1=ready, 2=running)")

        # Decide which register to write based on source
        if source == 0:
            target_addr = P03_10
            target_name = "P03.10 (internal reverse)"
            original_value = int_rev
        else:
            target_addr = P03_12
            target_name = "P03.12 (external reverse)"
            original_value = ext_rev
        print(f"\n      => active reverse-limit register: {target_name} = {original_value}")

        # ---------- Baseline torque under 5 kg pull ----------
        print("\n[A] baseline torque capture")
        print("      Set HMI to 5 kg and pull steadily on the cable.")
        input("      Press Enter when you're holding a steady pull... ")
        print("      sampling P21.04 for 5 s @ 10 Hz...")
        mean_pct, min_pct, max_pct, n = sample_torque(client, 5.0, 10)
        print(f"      |torque| = mean {mean_pct:.1f}%  min {min_pct:.1f}%  max {max_pct:.1f}%  (n={n})")

        if mean_pct < 3.0:
            print("\n      mean torque < 3% — pull was too light or torque didn't register.")
            print("      Aborting (need a clear baseline above the proposed cap).")
            return 2

        # ---------- Phase B: propose cap and write ----------
        cap_pct = round(mean_pct / 2.0, 1)            # half the mean
        cap_pct = max(cap_pct, 3.0)                    # floor 3% so it's not noise
        cap_raw = int(round(cap_pct / 0.1))            # raw count
        if cap_raw >= original_value:
            print(f"\n      proposed cap {cap_pct}% (raw {cap_raw}) is not below current limit "
                  f"{original_value*0.1:.1f}% (raw {original_value}). Aborting — would not "
                  f"actually clamp anything.")
            return 3

        print("\n[B] proposed write")
        print(f"      target   : {target_name}  (addr 0x{target_addr:04X})")
        print(f"      original : {original_value} ({original_value*0.1:.1f}%)")
        print(f"      new      : {cap_raw} ({cap_pct:.1f}%)  — half of measured {mean_pct:.1f}%")
        print(f"      restore  : write {original_value} back at end")
        if not confirm("      proceed with cap write?"):
            print("      aborted by user.")
            return 0

        print(f"\n      writing {cap_raw} to {target_name}...")
        resp = write_u16(client, target_addr, cap_raw)
        print(f"      response: {describe(resp)}")
        if resp.isError():
            print("\n      WRITE REJECTED — see error code above. Nothing to restore.")
            return 4
        write_done = True
        time.sleep(0.05)
        readback = read_u16(client, target_addr)
        print(f"      readback: {readback}  ({'OK' if readback == cap_raw else 'MISMATCH'})")
        if readback != cap_raw:
            print("      readback does not match write — drive may be silently rejecting.")

        # ---------- Phase B verify clamp ----------
        print("\n[B] clamp verification")
        print("      Pull steadily on the cable at 5 kg HMI again.")
        input("      Press Enter when you're holding a steady pull... ")
        print("      sampling P21.04 for 5 s @ 10 Hz...")
        mean2, min2, max2, n2 = sample_torque(client, 5.0, 10)
        print(f"      |torque| = mean {mean2:.1f}%  min {min2:.1f}%  max {max2:.1f}%  (n={n2})")
        print(f"      cap was   {cap_pct:.1f}%   (limit register set to {cap_raw})")
        margin = 0.5  # %
        if max2 <= cap_pct + margin:
            print(f"      RESULT: torque clamped at or below cap (max {max2:.1f}% <= {cap_pct:.1f}% + {margin}%)")
            print(f"              cap test PASSED — Modbus limit-modulator works.")
        elif mean2 < mean_pct * 0.8:
            print(f"      RESULT: mean dropped from {mean_pct:.1f}% to {mean2:.1f}% — partial clamp,")
            print(f"              but max {max2:.1f}% exceeded cap {cap_pct:.1f}%. Inspect.")
        else:
            print(f"      RESULT: torque NOT clamped — drive ignored the limit write.")
            print(f"              (mean {mean_pct:.1f}% -> {mean2:.1f}%, max {max2:.1f}% vs cap {cap_pct:.1f}%)")

        # ---------- Phase C: restore ----------
        print("\n[C] restore")
        if not confirm(f"      restore {target_name} to {original_value}?"):
            print("      restore SKIPPED by user — limit is still at cap value.")
            print(f"      MANUAL RESTORE: write {original_value} to register 0x{target_addr:04X}")
            return 5
        # Restore happens in finally — fall through.
        return 0

    finally:
        # Always attempt restore if we wrote
        if write_done and target_addr is not None and original_value is not None:
            try:
                print(f"\n      restoring {target_name} = {original_value}...")
                resp = write_u16(client, target_addr, original_value)
                print(f"      response: {describe(resp)}")
                time.sleep(0.05)
                final = read_u16(client, target_addr)
                if final == original_value:
                    print(f"      restored OK ({final})")
                else:
                    print(f"      [!] readback {final} != original {original_value}")
                    print(f"      [!] MANUAL RESTORE NEEDED — write {original_value} to 0x{target_addr:04X}")
            except Exception as e:
                print(f"\n      [!] restore raised: {e}")
                print(f"      [!] MANUAL RESTORE NEEDED — write {original_value} to 0x{target_addr:04X}")
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
