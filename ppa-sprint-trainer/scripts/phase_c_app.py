"""phase_c_app — Phase C runner. App drives the rig directly via Modbus.

Architecture:
- FastAPI on 127.0.0.1:8767 (different port from phase_b_app on 8766)
- Background heartbeat thread holds the S-ON via 0x3607 every 1.5s
- State machine: IDLE → ARMED → IDLE
- HTML UI: arm/disarm buttons, kg slider, live drive telemetry

Safety:
- Setpoint hard-clamped to ±10 kg (≈ ±56% torque). Phase C.5 confirmed 5%
  doesn't even produce motion against static friction; 56% gives meaningful
  load while staying well under the 300% factory limit.
- During arming, speed limits forced to 200 RPM (~1.15 m/s) before any S-ON.
  Originals saved and restored on disarm.
- Disarm sequence: zero P03.25 → stop heartbeat → wait for watchdog →
  restore mode 7 → restore P09.05/P04.01/P03.27/P03.28.
- Lifespan/shutdown handler runs disarm on SIGTERM. If the process crashes,
  the drive's 5s watchdog is the backstop.
- Operates only with CN3 plugged in. CN3↔HMI exclusion still applies — do
  not run this with the HMI in active session.

Run: python phase_c_app.py
URL: http://127.0.0.1:8767/
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from pymodbus.client import ModbusSerialClient

# ---- Drive ----
PORT = "COM6"
DEV = 1
P00_01 = (0 << 8) | 1
P03_25 = (3 << 8) | 25
P03_27 = (3 << 8) | 27
P03_28 = (3 << 8) | 28
P04_01 = (4 << 8) | 1
P09_05 = (9 << 8) | 5
P09_11 = (9 << 8) | 11
P21_00 = (21 << 8) | 0
P21_01 = (21 << 8) | 1
P21_04 = (21 << 8) | 4
P21_41 = (21 << 8) | 41
SON_REG = 0x3607
SON_CMD = 0x02

PCT_PER_KG = 5.64
HEARTBEAT_INTERVAL = 1.5
SPEED_LIMIT_ARMED = 200          # RPM cap during armed operation
KG_LIMIT = 10.0                  # hard cap on UI-set setpoint
PCT_LIMIT = KG_LIMIT * PCT_PER_KG  # ≈ 56.4%
WATCHDOG_GRACE = 7.0


def s16(v): return v - 65536 if v >= 32768 else v
def to_u16(v): return v if v >= 0 else v + 65536


class State(str, Enum):
    IDLE = "idle"
    ARMING = "arming"
    ARMED = "armed"
    DISARMING = "disarming"
    ERROR = "error"


class Drive:
    """Owns COM6 + Modbus client + heartbeat thread + state machine."""
    def __init__(self):
        self.client: Optional[ModbusSerialClient] = None
        self.lock = threading.Lock()
        self.state = State.IDLE
        self.error: Optional[str] = None
        self.originals: dict = {}
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.heartbeat_writes = 0
        self.heartbeat_errs = 0
        self.last_setpoint_pct = 0.0

    def connect(self):
        if self.client:
            return
        c = ModbusSerialClient(port=PORT, baudrate=9600, parity="N", stopbits=1,
                               bytesize=8, timeout=0.8)
        if not c.connect():
            raise RuntimeError(f"could not open {PORT} (CN3 plugged in?)")
        self.client = c

    def disconnect(self):
        if self.client:
            self.client.close()
            self.client = None

    # -- low-level (must be called under self.lock) --
    def _read1(self, addr):
        r = self.client.read_holding_registers(address=addr, count=1, device_id=DEV)
        return None if r.isError() else r.registers[0]

    def _write1(self, addr, value):
        if value < 0:
            value = to_u16(value)
        r = self.client.write_register(address=addr, value=value, device_id=DEV)
        return not r.isError()

    # -- public reads --
    def telemetry(self) -> dict:
        with self.lock:
            try:
                mode = self._read1(P00_01)
                status = self._read1(P21_00)
                rpm_raw = self._read1(P21_01)
                t_raw = self._read1(P21_04)
                fault = self._read1(P21_41)
            except Exception as e:
                return {"error": repr(e)}
        return {
            "mode": mode,
            "status": status,
            "rpm": s16(rpm_raw) if rpm_raw is not None else None,
            "torque_pct": (s16(t_raw) * 0.1) if t_raw is not None else None,
            "fault": fault,
        }

    # -- heartbeat --
    def _heartbeat_run(self):
        while not self.heartbeat_stop.is_set():
            with self.lock:
                try:
                    r = self.client.write_register(
                        address=SON_REG, value=SON_CMD, device_id=DEV)
                    if r.isError():
                        self.heartbeat_errs += 1
                    else:
                        self.heartbeat_writes += 1
                except Exception:
                    self.heartbeat_errs += 1
            self.heartbeat_stop.wait(HEARTBEAT_INTERVAL)

    def _heartbeat_start(self):
        self.heartbeat_stop.clear()
        self.heartbeat_writes = 0
        self.heartbeat_errs = 0
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_run, daemon=True)
        self.heartbeat_thread.start()

    def _heartbeat_stop_wait(self):
        self.heartbeat_stop.set()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=HEARTBEAT_INTERVAL + 1)
            self.heartbeat_thread = None

    # -- state transitions --
    def arm(self):
        if self.state != State.IDLE:
            raise RuntimeError(f"cannot arm from state {self.state}")
        self.state = State.ARMING
        try:
            with self.lock:
                # Snapshot originals
                self.originals = {
                    "p04_01": self._read1(P04_01),
                    "p09_05": self._read1(P09_05),
                    "p00_01": self._read1(P00_01),
                    "p03_25": self._read1(P03_25),
                    "p03_27": self._read1(P03_27),
                    "p03_28": self._read1(P03_28),
                }
                if self.originals["p00_01"] != 7:
                    raise RuntimeError(f"drive not in EtherCAT mode (P00.01={self.originals['p00_01']})")
                if self.originals["p03_25"] != 0:
                    raise RuntimeError(f"P03.25 not zero ({s16(self.originals['p03_25'])})")
                fault = self._read1(P21_41)
                if fault:
                    raise RuntimeError(f"drive fault {fault} before arm")
                # Speed limits low
                self._write1(P03_27, SPEED_LIMIT_ARMED)
                self._write1(P03_28, SPEED_LIMIT_ARMED)
                # Comm-DI prep
                self._write1(P04_01, 0)
                self._write1(P09_05, 0x02)
                # Mode 2
                self._write1(P00_01, 2)
            time.sleep(0.2)
            # Start heartbeat → motor enables at zero torque
            self._heartbeat_start()
            # Wait up to 2s for status=2
            t0 = time.time()
            while time.time() - t0 < 2.0:
                with self.lock:
                    s = self._read1(P21_00)
                if s == 2:
                    self.state = State.ARMED
                    self.last_setpoint_pct = 0.0
                    return
                time.sleep(0.1)
            raise RuntimeError("motor failed to enable within 2s")
        except Exception as e:
            self.error = repr(e)
            self.state = State.ERROR
            self._safe_disarm()
            raise

    def set_kg(self, kg: float):
        if self.state != State.ARMED:
            raise RuntimeError(f"cannot set kg in state {self.state}")
        if abs(kg) > KG_LIMIT:
            raise ValueError(f"|kg| > {KG_LIMIT} hard cap")
        # Sign convention: positive kg = retract direction (negative torque)
        pct = -abs(kg) * PCT_PER_KG  # always retract (neg torque)
        # Optional: if kg negative, command positive torque (extend). For safety
        # in this v1 we only allow retract direction.
        if kg < 0:
            raise ValueError("only positive kg (retract) supported in v1")
        raw = int(round(pct * 10))  # 0.1%/count, signed
        with self.lock:
            ok = self._write1(P03_25, raw)
            if not ok:
                raise RuntimeError("setpoint write failed")
        self.last_setpoint_pct = pct
        return {"kg": kg, "torque_pct": round(pct, 2), "p03_25_raw": raw}

    def disarm(self):
        if self.state == State.IDLE:
            return
        self.state = State.DISARMING
        try:
            self._safe_disarm()
            self.state = State.IDLE
            self.last_setpoint_pct = 0.0
            self.error = None
        except Exception as e:
            self.error = repr(e)
            self.state = State.ERROR

    def _safe_disarm(self):
        """Best-effort restore. Always tries every step even if one fails."""
        # 1. Zero setpoint
        try:
            with self.lock:
                self._write1(P03_25, 0)
        except Exception:
            pass
        # 2. Stop heartbeat & wait for watchdog
        self._heartbeat_stop_wait()
        time.sleep(WATCHDOG_GRACE)
        # 3. Restore mode 7 first (defangs comm-DI control before unsetting bits)
        try:
            with self.lock:
                if "p00_01" in self.originals:
                    self._write1(P00_01, self.originals["p00_01"])
        except Exception:
            pass
        # 4. Restore comm-DI bits
        try:
            with self.lock:
                if "p09_05" in self.originals:
                    self._write1(P09_05, self.originals["p09_05"])
                if "p04_01" in self.originals:
                    self._write1(P04_01, self.originals["p04_01"])
        except Exception:
            pass
        # 5. Restore speed limits
        try:
            with self.lock:
                if "p03_27" in self.originals:
                    self._write1(P03_27, self.originals["p03_27"])
                if "p03_28" in self.originals:
                    self._write1(P03_28, self.originals["p03_28"])
        except Exception:
            pass


drive = Drive()


@asynccontextmanager
async def lifespan(app: FastAPI):
    drive.connect()
    print(f"Phase C app: connected to {PORT}, state={drive.state}")
    try:
        yield
    finally:
        print("Phase C app: shutdown — running disarm")
        try:
            drive.disarm()
        except Exception as e:
            print(f"  disarm during shutdown: {e}")
        drive.disconnect()


app = FastAPI(lifespan=lifespan)


@app.get("/api/state")
async def api_state():
    tel = drive.telemetry()
    return JSONResponse({
        "state": drive.state.value,
        "error": drive.error,
        "setpoint_pct": round(drive.last_setpoint_pct, 2),
        "setpoint_kg": round(abs(drive.last_setpoint_pct) / PCT_PER_KG, 2),
        "telemetry": tel,
        "heartbeat_writes": drive.heartbeat_writes,
        "heartbeat_errs": drive.heartbeat_errs,
        "limits": {"kg_max": KG_LIMIT, "pct_max": round(PCT_LIMIT, 1),
                   "speed_limit_rpm_armed": SPEED_LIMIT_ARMED},
    })


@app.post("/api/arm")
async def api_arm():
    try:
        await asyncio.to_thread(drive.arm)
        return {"state": drive.state.value}
    except Exception as e:
        raise HTTPException(500, f"arm failed: {e}")


@app.post("/api/disarm")
async def api_disarm():
    try:
        await asyncio.to_thread(drive.disarm)
        return {"state": drive.state.value}
    except Exception as e:
        raise HTTPException(500, f"disarm error: {e}")


@app.post("/api/setkg")
async def api_setkg(kg: float):
    try:
        return await asyncio.to_thread(drive.set_kg, kg)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"setkg failed: {e}")


INDEX_HTML = """<!doctype html>
<html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>1080 PPA · Phase C</title>
<style>
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff;
          --warn:#d29922; --bad:#f85149; --good:#3fb950;
          --card:#161b22; --border:#30363d; }
  *{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
  body{margin:0;background:var(--bg);color:var(--fg);padding:14px;font-size:16px}
  h1{margin:0 0 12px 0;font-size:20px}
  .card{background:var(--card);border:1px solid var(--border);
        border-radius:12px;padding:14px;margin-bottom:14px}
  .meta{color:var(--muted);font-size:12px}
  .err{color:var(--bad)}
  .ok{color:var(--good)}
  .warn{color:var(--warn)}
  button{appearance:none;width:100%;padding:14px;font-size:17px;
         background:var(--accent);color:#0d1117;border:0;border-radius:8px;
         font-weight:600;margin-top:6px}
  button.disarm{background:var(--bad);color:#fff}
  button:disabled{opacity:0.4}
  input[type=range]{width:100%;margin:8px 0}
  .big{font-size:32px;font-weight:700}
  .row{display:flex;gap:12px;align-items:baseline}
  .tag{display:inline-block;padding:2px 8px;border-radius:6px;
       background:#21262d;color:var(--muted);font-size:12px;margin-right:4px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td{padding:4px 0;color:var(--muted)} td:last-child{text-align:right;color:var(--fg)}
</style>
</head><body>

<h1>1080 PPA — Phase C (direct drive control)</h1>

<div class="card">
  <div class="meta" id="state-line">connecting…</div>
  <div class="big" id="state-big">–</div>
  <div class="meta" id="error-line"></div>
</div>

<div class="card">
  <div class="row">
    <div class="big" id="kg-display">0.0 kg</div>
    <span class="tag" id="pct-display">0.0%</span>
  </div>
  <input type="range" id="kg-slider" min="0" max="10" step="0.5" value="0" disabled>
  <div class="meta" id="slider-info">arm to enable slider · max 10 kg (≈56% torque)</div>
</div>

<div class="card">
  <button id="arm-btn">ARM (motor enable, zero torque)</button>
  <button id="disarm-btn" class="disarm">DISARM (motor off, restore EtherCAT)</button>
</div>

<div class="card">
  <div class="meta" style="margin-bottom:8px">live telemetry</div>
  <table id="tel-table"></table>
</div>

<script>
async function getState() {
  const r = await fetch('/api/state'); return r.json();
}
async function postJSON(url) {
  const r = await fetch(url, {method:'POST'});
  return [r.status, await r.json().catch(()=>({}))];
}
function fmt(v, suffix='') {
  if (v === null || v === undefined) return '–';
  return (typeof v === 'number' ? v.toFixed(2) : v) + suffix;
}

const slider = document.getElementById('kg-slider');
const kgDisplay = document.getElementById('kg-display');
const pctDisplay = document.getElementById('pct-display');
const stateLine = document.getElementById('state-line');
const stateBig = document.getElementById('state-big');
const errorLine = document.getElementById('error-line');
const sliderInfo = document.getElementById('slider-info');
const armBtn = document.getElementById('arm-btn');
const disarmBtn = document.getElementById('disarm-btn');
const telTable = document.getElementById('tel-table');

let lastSentKg = 0;
let pendingKg = null;

slider.addEventListener('input', () => {
  const kg = parseFloat(slider.value);
  kgDisplay.textContent = kg.toFixed(1) + ' kg';
  pctDisplay.textContent = (kg * 5.64).toFixed(1) + '%';
  pendingKg = kg;
});

async function flushSetpoint() {
  if (pendingKg === null || pendingKg === lastSentKg) return;
  const kg = pendingKg;
  pendingKg = null;
  try {
    const r = await fetch('/api/setkg?kg=' + kg, {method:'POST'});
    if (r.ok) lastSentKg = kg;
  } catch(e) {}
}
setInterval(flushSetpoint, 250);

armBtn.onclick = async () => {
  if (!confirm('Arm? Motor will be enabled at zero torque. Cable detached?')) return;
  armBtn.disabled = true;
  const [s, j] = await postJSON('/api/arm');
  armBtn.disabled = false;
  if (s !== 200) alert('arm failed: ' + JSON.stringify(j));
};

disarmBtn.onclick = async () => {
  disarmBtn.disabled = true;
  const [s, j] = await postJSON('/api/disarm');
  disarmBtn.disabled = false;
};

async function refresh() {
  let j;
  try { j = await getState(); }
  catch(e) { stateLine.innerHTML = '<span class="err">poll error</span>'; return; }
  stateLine.textContent = 'state · last update ' + new Date().toLocaleTimeString();
  const stateLabels = {
    idle: '<span class="muted">IDLE</span>',
    arming: '<span class="warn">ARMING…</span>',
    armed: '<span class="ok">ARMED · motor enabled</span>',
    disarming: '<span class="warn">DISARMING…</span>',
    error: '<span class="err">ERROR</span>',
  };
  stateBig.innerHTML = stateLabels[j.state] || j.state;
  errorLine.textContent = j.error ? ('error: ' + j.error) : '';

  const armed = j.state === 'armed';
  slider.disabled = !armed;
  if (!armed) {
    slider.value = 0;
    kgDisplay.textContent = '0.0 kg';
    pctDisplay.textContent = '0.0%';
    lastSentKg = 0;
    pendingKg = null;
    sliderInfo.textContent = armed ? '' : 'arm to enable slider · max 10 kg (≈56% torque)';
  } else {
    sliderInfo.textContent = 'commanding ' + j.setpoint_kg + ' kg (' + j.setpoint_pct + '%)';
  }

  const t = j.telemetry || {};
  const rows = [
    ['mode', t.mode + (t.mode === 7 ? ' (EtherCAT)' : t.mode === 2 ? ' (torque)' : '')],
    ['status', t.status + (t.status === 2 ? ' (running)' : t.status === 1 ? ' (ready)' : '')],
    ['rpm', t.rpm],
    ['torque %', fmt(t.torque_pct, '%')],
    ['fault', t.fault],
    ['heartbeat writes', j.heartbeat_writes + ' · errs ' + j.heartbeat_errs],
    ['kg cap', j.limits.kg_max],
  ];
  telTable.innerHTML = rows.map(([k,v]) =>
    '<tr><td>'+k+'</td><td>'+(v===null||v===undefined?'–':v)+'</td></tr>'
  ).join('');
}
refresh(); setInterval(refresh, 1000);
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    print("Phase C app starting on http://127.0.0.1:8767/")
    print("Open on phone via http://<laptop-LAN-IP>:8767/")
    print("CN3 must be plugged in. HMI session must NOT be active.")
    uvicorn.run(app, host="0.0.0.0", port=8767, log_level="info")
