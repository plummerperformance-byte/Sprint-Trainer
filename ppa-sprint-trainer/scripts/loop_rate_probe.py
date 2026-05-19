"""loop_rate_probe.py — measure the real Modbus loop-rate ceiling on CN4/RS485.

READ-ONLY. No writes, no arming. This is the Phase 0 diagnostic from
docs/flywheel-loop-rate-plan.md — run it before any loop-rate work.

It answers:
  1. What baud is the drive actually at?  (sweep — Land Fitness may have changed
     it; the sweep is also the recovery tool if a P09.01 change ever goes wrong.)
  2. Per-transaction time: single-register read vs 9-register block read.
  3. Is the FTDI latency timer biting?  (~16 ms/txn = default; ~2-5 ms = fixed.)
  4. Estimated achievable control-loop rate at the current baud.

Usage:
  python loop_rate_probe.py                 # autodetect FTDI port, sweep baud
  python loop_rate_probe.py --port COM7     # force a port
  python loop_rate_probe.py --baud 9600     # skip the sweep, use this baud
  python loop_rate_probe.py --samples 300   # transactions per timing run
"""
import argparse
import statistics
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pymodbus.client import ModbusSerialClient
from serial.tools import list_ports

DEV = 1                       # Modbus slave id
P21_00 = (21 << 8) | 0        # 0x1500 — block-read start (status..position)
P21_01 = (21 << 8) | 1        # 0x1501 — motor speed, always-live probe register
SWEEP_BAUDS = [9600, 19200, 38400, 57600, 115200, 230400]


def find_ftdi_port():
    """Mirror ppa_service._find_drive_port — FTDI USB-RS485 adapter, VID 0x0403."""
    ftdi = [p for p in list_ports.comports() if p.vid == 0x0403]
    if not ftdi:
        return None
    return ftdi[0].device


def try_baud(port, baud):
    """Open at `baud` and attempt one read of P21.01. Returns True if the drive
    answered with a valid response."""
    c = ModbusSerialClient(port=port, baudrate=baud, parity="N", stopbits=1,
                           bytesize=8, timeout=0.5)
    if not c.connect():
        return False
    try:
        r = c.read_holding_registers(address=P21_01, count=1, device_id=DEV)
        return not r.isError()
    except Exception:
        return False
    finally:
        c.close()


def time_reads(client, address, count, samples):
    """Time `samples` reads of `count` registers at `address`. Returns the list
    of per-transaction milliseconds (first read discarded as warm-up)."""
    times = []
    for i in range(samples + 1):
        t0 = time.perf_counter()
        r = client.read_holding_registers(address=address, count=count,
                                          device_id=DEV)
        dt = (time.perf_counter() - t0) * 1000.0
        if r.isError():
            raise IOError(f"read error at 0x{address:04X}: {r}")
        if i > 0:                       # discard warm-up
            times.append(dt)
    return times


def stats(label, times):
    times = sorted(times)
    n = len(times)
    p95 = times[min(n - 1, int(n * 0.95))]
    print(f"  {label:24s} mean {statistics.mean(times):6.1f} ms   "
          f"median {statistics.median(times):6.1f} ms   p95 {p95:6.1f} ms")
    return statistics.median(times)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default=None, help="serial port (default: autodetect FTDI)")
    ap.add_argument("--baud", type=int, default=None, help="fixed baud — skip the sweep")
    ap.add_argument("--samples", type=int, default=200, help="transactions per timing run")
    args = ap.parse_args()

    port = args.port or find_ftdi_port()
    if port is None:
        print("FAIL: no FTDI USB-RS485 adapter found and no --port given.")
        print("      Plug the SH-U14 into CN4 (pins 3/4/5 = A/B/SG) with the drive powered on.")
        raise SystemExit(2)
    print(f"port: {port}")

    # ---- 1. find the drive's baud ---------------------------------------
    if args.baud:
        baud = args.baud
        print(f"baud: {baud} (forced, sweep skipped)")
        if not try_baud(port, baud):
            print(f"FAIL: no valid response at {baud}. Drop --baud to sweep.")
            raise SystemExit(3)
    else:
        print("baud sweep (read P21.01 at each rate)...")
        baud = None
        for b in SWEEP_BAUDS:
            ok = try_baud(port, b)
            print(f"  {b:>7} : {'RESPONDS' if ok else '--'}")
            if ok and baud is None:
                baud = b
        if baud is None:
            print("FAIL: drive did not respond at any swept baud.")
            print("      Check: drive powered, wiring A/B not swapped, 220 ohm "
                  "termination, slave id = 1.")
            raise SystemExit(3)
        print(f"-> drive is at {baud} baud")

    # ---- 2. time the transactions ---------------------------------------
    c = ModbusSerialClient(port=port, baudrate=baud, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {port} at {baud}")
        raise SystemExit(2)
    try:
        print(f"\ntiming {args.samples} transactions at {baud} baud:")
        single = stats("single register read", time_reads(c, P21_01, 1, args.samples))
        block = stats("9-register block read", time_reads(c, P21_00, 9, args.samples))
    finally:
        c.close()

    # ---- 3. diagnose & estimate -----------------------------------------
    print("\ndiagnosis:")
    if single > 12:
        print(f"  FTDI latency timer looks like the DEFAULT 16 ms "
              f"(single read {single:.1f} ms).")
        print("  FIX: Device Manager -> the COM port -> Port Settings -> Advanced")
        print("       -> Latency Timer -> 1 ms. Re-run this probe afterwards.")
    elif single > 7:
        print(f"  FTDI latency timer may not be at 1 ms (single read {single:.1f} ms).")
        print("  Check Device Manager -> COM port -> Advanced -> Latency Timer.")
    else:
        print(f"  FTDI latency looks fine (single read {single:.1f} ms).")

    # A control cycle = 1 block read + 1 write. A single-register write is the
    # same transaction shape as a single-register read, so use `single` as the
    # write-cost proxy. This is an estimate, not a measurement (no writes here).
    cycle_ms = block + single
    hz = 1000.0 / cycle_ms if cycle_ms > 0 else 0.0
    print(f"\nestimated control-loop ceiling at {baud} baud:")
    print(f"  cycle ~= block read ({block:.1f}) + write ({single:.1f}) "
          f"= {cycle_ms:.1f} ms  ->  ~{hz:.0f} Hz")
    if hz < 90:
        print(f"  Below the 100 Hz target. Levers, in order: fix FTDI latency, "
              f"batch the loop to 1 read + 1 write, then bump baud (P09.01).")
        print(f"  See docs/flywheel-loop-rate-plan.md.")
    else:
        print("  At or above the 100 Hz target.")


if __name__ == "__main__":
    main()
