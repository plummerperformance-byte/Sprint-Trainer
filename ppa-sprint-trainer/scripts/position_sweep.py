"""Position sweep: pull cable to a known HMI-displayed distance, script
reads drive encoder counts. Pairs metres → counts to derive counts/metre.

Read-only. One Modbus read per data point.

Usage:
  Set HMI to 0 m (cable fully retracted) as the baseline first.
  Then for each prompt, pull cable out to a known metre value (HMI display),
  type the metres, press Enter. Type 'done' to finish.
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pymodbus.client import ModbusSerialClient

PORT = "COM6"
DEV = 1
P21_07 = (21 << 8) | 7   # low word of cumulative position (32-bit signed)


def s32(lo, hi):
    v = (hi << 16) | lo
    return v - (1 << 32) if v >= (1 << 31) else v


def read_pos(c):
    r = c.read_holding_registers(address=P21_07, count=2, device_id=DEV)
    if r.isError():
        return None, f"read error: {r}"
    pos = s32(r.registers[0], r.registers[1])
    return pos, "OK"


def main():
    print("=" * 64)
    print(" Position sweep — cable metres (HMI) vs drive encoder counts")
    print("=" * 64)
    print()
    print("CN3 plugged in. Cable fully RETRACTED (HMI showing 0 m or close).")
    input("Press Enter when ready for baseline read... ")

    c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {PORT}")
        return 1

    samples = []  # list of (metres, counts_absolute, counts_relative_to_zero)

    try:
        zero, msg = read_pos(c)
        if zero is None:
            print(f"baseline read failed: {msg}")
            return 2
        print(f"baseline counts at 0 m  = {zero}")
        print()
        samples.append((0.0, zero, 0))

        while True:
            ans = input("Pull cable to a known distance, type the HMI metres "
                        "(or 'done'): ").strip().lower()
            if ans in ("done", "d", "q", "quit", "exit", ""):
                break
            try:
                m = float(ans)
            except ValueError:
                print(f"   couldn't parse '{ans}' as a number, try again")
                continue
            pos, msg = read_pos(c)
            if pos is None:
                print(f"   read failed: {msg}")
                continue
            rel = pos - zero
            samples.append((m, pos, rel))
            cpm = (rel / m) if m else None
            cpm_str = f"{cpm:.1f}" if cpm is not None else "n/a"
            print(f"   {m} m -> counts {pos}  (rel {rel:+d})   counts/m so far: {cpm_str}")
            print()

        # Summary + fit
        print()
        print("=" * 64)
        print(" Summary")
        print("=" * 64)
        print(f"{'metres':>8} | {'counts abs':>12} | {'counts rel':>12} | {'cnt/m':>10}")
        print("-" * 56)
        for m, abs_c, rel_c in samples:
            cpm = (rel_c / m) if m else None
            cpm_s = f"{cpm:.1f}" if cpm is not None else "-"
            print(f"{m:>8.2f} | {abs_c:>12d} | {rel_c:>+12d} | {cpm_s:>10}")

        good = [(m, rc) for m, _, rc in samples if m]
        if len(good) >= 2:
            n = len(good)
            sx = sum(m for m, _ in good)
            sy = sum(c for _, c in good)
            sxy = sum(m * c for m, c in good)
            sxx = sum(m * m for m, _ in good)
            denom = n * sxx - sx * sx
            if denom:
                slope = (n * sxy - sx * sy) / denom
                intercept = (sy - slope * sx) / n
                print()
                print(f"Linear fit (using {n} points, force-zero baseline):")
                print(f"   counts = {slope:.2f} * metres + {intercept:.2f}")
                print(f"   counts_per_metre = {abs(slope):.2f}")
                print(f"   metres_per_count = {1/abs(slope):.6e}")
                # Sanity: typical 17-bit encoder = 131072 counts/rev.
                # If counts_per_metre is in the thousands, the drum
                # circumference can be derived: c = 131072 / counts_per_metre.
                cpr = 131072
                circ = cpr / abs(slope)
                print(f"   implied drum circumference (if 17-bit encoder, 131072 cpr) ≈ {circ*1000:.1f} mm")
    finally:
        c.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
