"""Force-write P00.01 = 7 via a fresh Modbus client. Recovery for when
ppa_service's automated cleanup didn't take. Reports the actual response
codes so we can diagnose silent failures.
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
DEV = 1
P00_01 = (0 << 8) | 1
P21_00 = (21 << 8) | 0


def describe(resp):
    if isinstance(resp, ExceptionResponse):
        return f"ExceptionResponse code={getattr(resp,'exception_code',None)} | {resp}"
    if hasattr(resp, "isError") and resp.isError():
        return f"isError code={getattr(resp,'exception_code',None)} | {resp}"
    return f"OK | {resp}"


print("Note: this needs ppa_service.py to NOT be running (COM6 single owner).")
print("If service is running, kill it first.")
c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                       bytesize=8, timeout=1.0)
if not c.connect():
    print(f"FAIL: cannot open {PORT}")
    raise SystemExit(2)

try:
    r = c.read_holding_registers(address=P00_01, count=1, device_id=DEV)
    print(f"P00.01 before = {r.registers[0] if not r.isError() else describe(r)}")
    s = c.read_holding_registers(address=P21_00, count=1, device_id=DEV)
    print(f"P21.00        = {s.registers[0] if not s.isError() else describe(s)}")

    print("writing P00.01 = 7 (FC06)...")
    w = c.write_register(address=P00_01, value=7, device_id=DEV)
    print(f"  response: {describe(w)}")

    time.sleep(0.2)
    r = c.read_holding_registers(address=P00_01, count=1, device_id=DEV)
    print(f"P00.01 after  = {r.registers[0] if not r.isError() else describe(r)}")
finally:
    c.close()
