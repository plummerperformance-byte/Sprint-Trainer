"""Static torque sweep: at each HMI weight setting, take a single one-shot
read of the drive's commanded torque P21.04. Pairs HMI kg with drive torque%
to derive the calibration ratio.

Read-only. No writes. No polling. One Modbus read per kg setting.

Procedure: user sets HMI to a target weight, presses Enter, script reads
P21.04 once and prints the pair. Repeats for the requested set of weights.
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
P21_04 = (21 << 8) | 4
P21_00 = (21 << 8) | 0


def s16(v):
    return v - 65536 if v >= 32768 else v


def read_torque(c, samples=3, gap=0.15):
    """Read torque a few times to filter any single-read jitter."""
    vals = []
    for _ in range(samples):
        r = c.read_holding_registers(address=P21_04, count=1, device_id=DEV)
        if r.isError():
            return None, f"read error: {r}"
        vals.append(s16(r.registers[0]) * 0.1)
        time.sleep(gap)
    return sum(vals) / len(vals), f"min {min(vals):.1f}% max {max(vals):.1f}% (n={len(vals)})"


def read_status(c):
    r = c.read_holding_registers(address=P21_00, count=1, device_id=DEV)
    if r.isError():
        return None
    return r.registers[0]


def main():
    print("=" * 60)
    print(" Static torque sweep — HMI kg vs drive P21.04 (% commanded)")
    print("=" * 60)
    print()
    print("CN3 should be plugged in for this. After each prompt, set the HMI")
    print("weight to the requested value, wait ~2s for the setpoint to settle,")
    print("then press Enter. Cable must be slack (no pull) so we capture the")
    print("idle setpoint, not a pull-modulated value.")
    print()

    c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {PORT}")
        return 1

    weights = [2, 5, 10, 15]
    results = []

    try:
        status = read_status(c)
        print(f"servo status = {status}  (1=ready, 2=running). Need 2 for valid setpoints.")
        print()

        for kg in weights:
            input(f"Set HMI to {kg} kg, cable slack, then press Enter... ")
            mean_pct, stats = read_torque(c)
            if mean_pct is None:
                print(f"   {kg} kg : FAILED — {stats}")
                results.append((kg, None))
            else:
                ratio = abs(mean_pct) / kg if kg else None
                print(f"   {kg} kg -> {mean_pct:+.2f} %    ({stats})    ratio = {ratio:.3f} %/kg")
                results.append((kg, mean_pct))
            print()

        print("=" * 60)
        print(" Summary")
        print("=" * 60)
        print(f"{'kg':>4} | {'torque %':>10} | {'%/kg':>8}")
        print("-" * 32)
        for kg, t in results:
            if t is None:
                print(f"{kg:>4} | {'FAILED':>10} | {'-':>8}")
            else:
                print(f"{kg:>4} | {t:>+10.2f} | {abs(t)/kg:>8.3f}")
        # Linear fit summary
        good = [(kg, t) for kg, t in results if t is not None]
        if len(good) >= 2:
            n = len(good)
            sx = sum(kg for kg, _ in good)
            sy = sum(t for _, t in good)
            sxy = sum(kg * t for kg, t in good)
            sxx = sum(kg * kg for kg, _ in good)
            denom = n * sxx - sx * sx
            if denom:
                slope = (n * sxy - sx * sy) / denom
                intercept = (sy - slope * sx) / n
                print()
                print(f"Linear fit (using {n} points):")
                print(f"   torque% = {slope:.3f} * kg + {intercept:.3f}")
                print(f"   |slope| = {abs(slope):.3f} %/kg")
    finally:
        c.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
