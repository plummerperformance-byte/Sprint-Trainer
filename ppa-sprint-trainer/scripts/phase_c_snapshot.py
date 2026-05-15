"""phase_c_snapshot — read-only drive state snapshot covering all the
registers that Phase C will eventually touch. Confirms current values BEFORE
any plan to write is finalised.

NO WRITES. Pure observation.

Reads:
  P00.01 control mode (current = 7 = CANOpen/EtherCAT expected)
  P03.08–P03.12 torque limit block (already known good)
  P04.01 DI1 function assignment (S-ON/Modbus DI gating)
  P04.22 Torque instruction source (mode 2 only)
  P04.25 Torque instruction digital setting value (mode 2 setpoint)
  P04.26 Speed limit source in torque control
  P04.27 Internal positive speed limit
  P04.28 Internal negative speed limit
  P04.29 Hard limit torque limit
  P09.05 Communication DI enable bits (S-ON via Modbus prep)
  P09.11 Communication DI timeout (the WATCHDOG)
  P21.00 servo status, P21.04 torque %, P21.41 fault, etc
  0x3607 (would be the S-ON command register — read first)
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pymodbus.client import ModbusSerialClient

PORT = "COM6"
DEV = 1


def s16(v):
    return v - 65536 if v >= 32768 else v


def addr(group, idx):
    return (group << 8) | idx


def read_one(c, label, address, signed=False):
    r = c.read_holding_registers(address=address, count=1, device_id=DEV)
    if r.isError():
        return f"{label:48s} = ERROR: {r}"
    v = r.registers[0]
    if signed:
        v = s16(v)
    return f"{label:48s} = {v}  (0x{r.registers[0]:04X})"


def main():
    print("=" * 74)
    print(" Phase C state snapshot — READ ONLY, no writes")
    print("=" * 74)
    print()
    c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                           bytesize=8, timeout=0.8)
    if not c.connect():
        print(f"FAIL: cannot open {PORT}")
        return 1

    try:
        print("[mode + status]")
        print(read_one(c, "P00.01 control mode (7=EtherCAT, 1=Spd, 2=Trq)", addr(0, 1)))
        print(read_one(c, "P21.00 servo status (1=ready, 2=running)", addr(21, 0)))
        print(read_one(c, "P21.04 commanded torque (0.1%/count, signed)", addr(21, 4), signed=True))
        print(read_one(c, "P21.41 fault code (0=no fault)", addr(21, 41)))
        print()

        print("[torque-mode setpoint registers (P03 group, CORRECTED)]")
        print(read_one(c, "P04.01 DI1 terminal function selection (0=free)", addr(4, 1)))
        print(read_one(c, "P03.22 Torque instruction source (0=digital, 3=comm)", addr(3, 22)))
        print(read_one(c, "P03.25 Torque instruction digital setting (signed 0.1%)", addr(3, 25), signed=True))
        print(read_one(c, "P03.26 Speed limit source (0=internal P03.27/28, 1=SPL)", addr(3, 26)))
        print(read_one(c, "P03.27 Internal positive speed limit (RPM)", addr(3, 27)))
        print(read_one(c, "P03.28 Internal negative speed limit (RPM)", addr(3, 28)))
        print(read_one(c, "P03.29 Hard limit torque limit", addr(3, 29)))
        print()

        print("[torque limits (P03 group, already known)]")
        print(read_one(c, "P03.08 limit source (0=int, 1=ext)", addr(3, 8)))
        print(read_one(c, "P03.09 internal forward limit (0.1%)", addr(3, 9)))
        print(read_one(c, "P03.10 internal reverse limit (0.1%)", addr(3, 10)))
        print(read_one(c, "P03.11 external forward limit", addr(3, 11)))
        print(read_one(c, "P03.12 external reverse limit", addr(3, 12)))
        print()

        print("[Modbus comm-DI controls (P09 group) — Phase C uses these]")
        print(read_one(c, "P09.04 communication response delay", addr(9, 4)))
        print(read_one(c, "P09.05 communication-DI enable bits (set 0x02 to enable S-ON via comm)", addr(9, 5)))
        print(read_one(c, "P09.11 communication-DI WATCHDOG timeout (s)", addr(9, 11)))
        print()

        print("[S-ON command register (Modbus path)]")
        print(read_one(c, "0x3607 S-ON via Modbus (we'd write 0x02 to enable)", 0x3607))
        print()

        print("[bus + position]")
        print(read_one(c, "P21.06 DC bus voltage (0.1V)", addr(21, 6)))
        regs = c.read_holding_registers(address=addr(21, 7), count=2, device_id=DEV)
        if not regs.isError():
            lo, hi = regs.registers
            pos = (hi << 16) | lo
            if pos >= (1 << 31):
                pos -= 1 << 32
            print(f"{'P21.07/8 cumulative position (signed 32-bit)':48s} = {pos}")
        print()

        print("Snapshot complete. No writes performed.")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
