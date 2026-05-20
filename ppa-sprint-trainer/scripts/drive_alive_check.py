"""One-shot read to check the SV-X3E is still responding on Modbus.
Read-only. No writes. Reports status, fault code, RPM, torque, position."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from pymodbus.client import ModbusSerialClient

PORT = "COM6"
DEV = 1


def s16(v): return v - 65536 if v >= 32768 else v
def s32(lo, hi):
    v = (hi << 16) | lo
    return v - (1 << 32) if v >= (1 << 31) else v


c = ModbusSerialClient(port=PORT, baudrate=115200, parity="N", stopbits=1, bytesize=8, timeout=0.8)
if not c.connect():
    print(f"FAIL: cannot open {PORT}")
    raise SystemExit(2)

try:
    # P21.00..P21.08 = status, RPM, x, x, torque, x, busV, posLo, posHi
    r = c.read_holding_registers(address=(21<<8)|0, count=9, device_id=DEV)
    if r.isError():
        print(f"DRIVE NOT RESPONDING TO MODBUS: {r}")
        raise SystemExit(3)
    regs = r.registers
    status = regs[0]
    rpm = s16(regs[1])
    torque_pct = s16(regs[4]) * 0.1
    bus_v = regs[6] * 0.1
    pos = s32(regs[7], regs[8])
    # also P21.41 fault code
    f = c.read_holding_registers(address=(21<<8)|41, count=1, device_id=DEV)
    fault = f.registers[0] if not f.isError() else "?"

    print(f"DRIVE IS ALIVE on Modbus")
    print(f"  P21.00 status     = {status}  (1=ready, 2=running)")
    print(f"  P21.01 RPM        = {rpm}")
    print(f"  P21.04 torque %   = {torque_pct:+.1f}")
    print(f"  P21.06 bus V      = {bus_v:.1f}")
    print(f"  P21.07/8 position = {pos}")
    print(f"  P21.41 fault code = {fault}  (0 = no fault)")
finally:
    c.close()
