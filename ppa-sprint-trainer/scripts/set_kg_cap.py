"""set_kg_cap — single-shot tool to write a kg-equivalent torque ceiling on
the drive. Phase B (limit-modulator path). Athlete uses HMI as normal;
drive clamps motor torque to the cap.

Usage:
    python set_kg_cap.py             -> shows current state, no write
    python set_kg_cap.py 30          -> proposes capping at 30 kg, asks YES
    python set_kg_cap.py --restore   -> restores P03.10 to factory 3000

Safety:
- Only writes P03.10 (internal reverse limit). The cap test 2026-05-10 proved
  this register gates real motor torque. Forward limit P03.09 is left at its
  current value (not individually proven; deferred per CLAUDE.md).
- Refuses cap above 300% (300/5.64 ≈ 53 kg).
- Reads back after every write and reports mismatch.
- Calibration: torque% = kg × 5.64 (locked-in 4-point fit 2026-05-10).
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pymodbus.client import ModbusSerialClient

PORT = "COM6"
DEV = 1

P03_08 = (3 << 8) | 8    # limit source 0=internal 1=external
P03_09 = (3 << 8) | 9    # internal forward
P03_10 = (3 << 8) | 10   # internal reverse  <-- the proven cap register
P03_11 = (3 << 8) | 11   # external forward
P03_12 = (3 << 8) | 12   # external reverse

KG_PER_PCT = 1 / 5.64    # ~0.1773 kg per torque-percent
PCT_PER_KG = 5.64
FACTORY_CAP_PCT = 300.0
FACTORY_CAP_RAW = 3000   # 0.1%/count units


def s16(v): return v - 65536 if v >= 32768 else v


def read_state(c):
    r = c.read_holding_registers(address=P03_08, count=5, device_id=DEV)
    if r.isError():
        raise IOError(f"read P03.08–12 failed: {r}")
    src, fwd_int, rev_int, fwd_ext, rev_ext = r.registers
    return {
        "source": src,
        "internal_forward_pct": fwd_int * 0.1,
        "internal_reverse_pct": rev_int * 0.1,
        "external_forward_pct": fwd_ext * 0.1,
        "external_reverse_pct": rev_ext * 0.1,
        "_rev_int_raw": rev_int,
    }


def show(state):
    src = state["source"]
    src_name = "external" if src else "internal"
    print(f"  P03.08 limit source     = {src} ({src_name})")
    print(f"  P03.09 internal fwd     = {state['internal_forward_pct']:.1f}%")
    print(f"  P03.10 internal rev     = {state['internal_reverse_pct']:.1f}%   (cap target)")
    print(f"  P03.11 external fwd     = {state['external_forward_pct']:.1f}%")
    print(f"  P03.12 external rev     = {state['external_reverse_pct']:.1f}%")
    if state["internal_reverse_pct"] < FACTORY_CAP_PCT - 0.05:
        kg_eq = state["internal_reverse_pct"] / PCT_PER_KG
        print(f"  current cap is below factory ceiling: equivalent to ~{kg_eq:.1f} kg")
    else:
        print(f"  current cap is at or near factory ceiling (no athletic-range clamping)")


def confirm(prompt, auto_yes=False):
    if auto_yes:
        print(f"{prompt} (--yes flag set, auto-confirmed)")
        return True
    return input(f"{prompt} (type YES to proceed): ").strip() == "YES"


def write_p03_10(c, raw):
    r = c.write_register(address=P03_10, value=raw, device_id=DEV)
    if r.isError():
        raise IOError(f"write P03.10 failed: {r}")
    rb = c.read_holding_registers(address=P03_10, count=1, device_id=DEV)
    if rb.isError():
        raise IOError(f"readback P03.10 failed: {rb}")
    return rb.registers[0]


def main():
    args = sys.argv[1:]
    restore = "--restore" in args
    auto_yes = "--yes" in args
    target_kg = None
    for a in args:
        if a in ("--restore", "-r", "--yes"):
            continue
        try:
            target_kg = float(a)
        except ValueError:
            print(f"unknown arg: {a}")
            return 2

    print("=" * 60)
    print(" set_kg_cap — drive torque ceiling (Phase B limit-modulator)")
    print("=" * 60)
    print()

    c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {PORT}. Is CN3 mini-USB plugged in?")
        return 1

    try:
        print("[reading current state...]")
        state = read_state(c)
        show(state)
        print()

        if restore:
            current = state["_rev_int_raw"]
            if current == FACTORY_CAP_RAW:
                print(f"P03.10 already at factory {FACTORY_CAP_RAW}. Nothing to do.")
                return 0
            print(f"Restore: write P03.10 from {current} ({current*0.1:.1f}%) "
                  f"to {FACTORY_CAP_RAW} ({FACTORY_CAP_PCT}%).")
            if not confirm("proceed with restore?", auto_yes=auto_yes):
                print("aborted by user.")
                return 0
            new = write_p03_10(c, FACTORY_CAP_RAW)
            print(f"  readback: {new}  ({'OK' if new == FACTORY_CAP_RAW else 'MISMATCH'})")
            return 0

        if target_kg is None:
            print("No target kg given. Pass a number to set a cap, "
                  "or --restore to revert to factory.")
            return 0

        # Validate
        if target_kg <= 0:
            print(f"target {target_kg} kg invalid (must be > 0)")
            return 2
        target_pct = target_kg * PCT_PER_KG
        if target_pct > FACTORY_CAP_PCT:
            print(f"target {target_kg} kg → {target_pct:.1f}% which exceeds "
                  f"factory cap {FACTORY_CAP_PCT}%. Refusing.")
            print(f"max allowable kg = {FACTORY_CAP_PCT/PCT_PER_KG:.1f} kg")
            return 2
        target_raw = int(round(target_pct * 10))  # 0.1%/count

        print(f"Proposed write:")
        print(f"  target kg     = {target_kg} kg")
        print(f"  -> torque%    = {target_pct:.2f}%   (= kg × 5.64)")
        print(f"  -> P03.10 raw = {target_raw}")
        print(f"  current P03.10 raw = {state['_rev_int_raw']}")
        print()
        print("Reminder: this caps reverse torque only (cable retract direction).")
        print("Forward direction (P03.09) untouched.")
        print()

        if not confirm("proceed with cap write?", auto_yes=auto_yes):
            print("aborted by user.")
            return 0

        new = write_p03_10(c, target_raw)
        print(f"  readback: {new}  "
              f"({'OK' if new == target_raw else 'MISMATCH — re-check!'})")
        if new == target_raw:
            print()
            print(f"Cap is set. Athlete cable load will clamp at ~{target_kg} kg.")
            print("You can unplug CN3 now and start the HMI session as normal.")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
