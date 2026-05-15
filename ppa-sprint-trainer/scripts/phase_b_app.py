"""phase_b_app — mobile-friendly Phase B web UI.

What it does:
- Polls the HMI datalog every 2s (background task), caches latest reps in memory
- Serves a phone-friendly HTML page showing rep history + a cap-setter form
- POST /api/cap writes P03.10 via Modbus (requires CN3 mini-USB plugged in;
  fails gracefully if not)
- POST /api/restore reverts P03.10 to factory 3000
- GET /api/reps returns cached reps as JSON

Architectural rules (see CLAUDE.md):
- HMI is master. Modbus is for cap writes only, on demand.
- CN3 plugged in destabilises HMI, so live data MUST come from the HMI REST
  API, not Modbus polling. Modbus is opened only at the moment of a cap write.

Run:  python phase_b_app.py
URL:  http://127.0.0.1:8766/  (open on phone via http://<laptop-LAN-IP>:8766/)
"""
from __future__ import annotations

import asyncio
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# Optional pymodbus import — only needed if a cap write is requested.
# We tolerate import failure so the read-only path still works on machines
# without pymodbus installed.
try:
    from pymodbus.client import ModbusSerialClient
    HAVE_MODBUS = True
except ImportError:
    HAVE_MODBUS = False

# ---- HMI ----
BASE = "https://192.168.88.222"
USERNAME = "admin"
PASSWORD = "Plummer-1994"
CHANNEL = 0
HMI_POLL_SEC = 2.0
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ---- Drive (Phase B cap writes only) ----
COM_PORT = "COM6"
DEV = 1
P03_10 = (3 << 8) | 10
P03_REGS = (3 << 8) | 8  # block of 5: P03.08..P03.12
PCT_PER_KG = 5.64
FACTORY_CAP_RAW = 3000

# ---- shared cache ----
class Cache:
    def __init__(self):
        self.token: Optional[str] = None
        self.last_fetch_ts: float = 0
        self.last_fetch_iso: str = ""
        self.error: Optional[str] = None
        self.columns: list[str] = []
        self.reps: list[dict] = []   # most-recent-first

cache = Cache()


def hmi_login() -> str:
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "username": USERNAME,
        "password": PASSWORD,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/api/v1/auth/token/admin",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def hmi_fetch_reps(token: str) -> tuple[list[str], list[dict]]:
    start, end = 0, 9999999999
    q = urllib.parse.urlencode({"start": start, "end": end, "order": ""})

    def _req(path):
        return urllib.request.Request(
            BASE + path, headers={"Authorization": f"Bearer {token}"})

    with urllib.request.urlopen(
        _req(f"/api/v1/data/datalog/{CHANNEL}/data/totalcount?{q}"),
        context=SSL_CTX, timeout=8,
    ) as r:
        initial = json.loads(r.read())

    columns = [c["comment"] for c in initial["columns"]]
    total = initial["total"]
    rows = {row["rid"]: row for row in initial.get("data", [])}

    page = 1
    while len(rows) < total and page < 20:  # safety cap on pagination
        q2 = urllib.parse.urlencode({
            "totalcount": total, "start": start, "end": end,
            "index": page, "order": "",
        })
        with urllib.request.urlopen(
            _req(f"/api/v1/data/datalog/{CHANNEL}/data?{q2}"),
            context=SSL_CTX, timeout=8,
        ) as r:
            extra = json.loads(r.read())
        before = len(rows)
        for row in extra.get("data", []):
            rows[row["rid"]] = row
        if len(rows) == before:
            break
        page += 1

    sorted_rows = sorted(rows.values(), key=lambda r: r["rid"], reverse=True)
    return columns, sorted_rows


async def poll_loop():
    """Background task: refresh datalog every HMI_POLL_SEC seconds."""
    while True:
        try:
            if cache.token is None:
                cache.token = await asyncio.to_thread(hmi_login)
            cols, rows = await asyncio.to_thread(hmi_fetch_reps, cache.token)
            cache.columns = cols
            cache.reps = rows[:25]  # keep last 25
            cache.last_fetch_ts = time.time()
            cache.last_fetch_iso = time.strftime("%H:%M:%S")
            cache.error = None
        except urllib.error.HTTPError as e:
            if e.code == 401:
                cache.token = None  # re-auth next loop
                cache.error = "HMI 401, re-auth on next poll"
            else:
                cache.error = f"HMI HTTP {e.code}"
        except Exception as e:
            cache.error = f"{type(e).__name__}: {e}"
        await asyncio.sleep(HMI_POLL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)


# ---- API ----

@app.get("/api/reps")
async def api_reps():
    return JSONResponse({
        "fetched_at": cache.last_fetch_iso,
        "error": cache.error,
        "columns": cache.columns,
        "reps": [
            {
                "rid": r["rid"],
                "ts": r["ts"],
                **{cache.columns[i]: r["ds"][i] for i in range(len(cache.columns))},
            }
            for r in cache.reps
        ],
    })


def _modbus_open():
    if not HAVE_MODBUS:
        raise HTTPException(500, "pymodbus not installed on this host")
    c = ModbusSerialClient(
        port=COM_PORT, baudrate=9600, parity="N", stopbits=1, bytesize=8,
        timeout=0.8,
    )
    if not c.connect():
        raise HTTPException(503,
            f"could not open {COM_PORT} — is the CN3 mini-USB plugged in?")
    return c


@app.post("/api/cap")
async def api_cap(kg: float):
    if kg <= 0:
        raise HTTPException(400, f"target {kg} must be > 0")
    pct = kg * PCT_PER_KG
    if pct > 300.0:
        raise HTTPException(400,
            f"{kg} kg → {pct:.1f}% exceeds 300% factory cap "
            f"(max {300/PCT_PER_KG:.1f} kg)")
    raw = int(round(pct * 10))
    c = _modbus_open()
    try:
        # snapshot before
        before_resp = c.read_holding_registers(address=P03_10, count=1, device_id=DEV)
        if before_resp.isError():
            raise HTTPException(502, f"P03.10 read failed: {before_resp}")
        before = before_resp.registers[0]
        # write
        w = c.write_register(address=P03_10, value=raw, device_id=DEV)
        if w.isError():
            raise HTTPException(502, f"P03.10 write failed: {w}")
        # readback
        after_resp = c.read_holding_registers(address=P03_10, count=1, device_id=DEV)
        if after_resp.isError():
            raise HTTPException(502, f"P03.10 readback failed: {after_resp}")
        after = after_resp.registers[0]
    finally:
        c.close()
    return {
        "kg": kg, "torque_pct": round(pct, 2),
        "p03_10_before": before, "p03_10_after": after,
        "match": after == raw,
    }


@app.post("/api/restore")
async def api_restore():
    c = _modbus_open()
    try:
        before_resp = c.read_holding_registers(address=P03_10, count=1, device_id=DEV)
        before = before_resp.registers[0] if not before_resp.isError() else None
        w = c.write_register(address=P03_10, value=FACTORY_CAP_RAW, device_id=DEV)
        if w.isError():
            raise HTTPException(502, f"restore write failed: {w}")
        after_resp = c.read_holding_registers(address=P03_10, count=1, device_id=DEV)
        after = after_resp.registers[0] if not after_resp.isError() else None
    finally:
        c.close()
    return {"p03_10_before": before, "p03_10_after": after}


# ---- mobile UI ----

INDEX_HTML = """<!doctype html>
<html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>1080 PPA · Phase B</title>
<style>
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff;
          --warn:#d29922; --bad:#f85149; --good:#3fb950;
          --card:#161b22; --border:#30363d; }
  *{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
  body{margin:0;background:var(--bg);color:var(--fg);padding:14px;font-size:16px}
  h1{margin:0 0 12px 0;font-size:20px;font-weight:600}
  .card{background:var(--card);border:1px solid var(--border);
        border-radius:12px;padding:14px;margin-bottom:14px}
  .meta{color:var(--muted);font-size:12px}
  .err{color:var(--bad)}
  .ok{color:var(--good)}
  label{display:block;margin:8px 0 4px;color:var(--muted);font-size:13px}
  input[type=number]{width:100%;padding:12px;font-size:18px;
        background:#0d1117;color:var(--fg);border:1px solid var(--border);
        border-radius:8px}
  button{appearance:none;width:100%;padding:14px;font-size:17px;
         background:var(--accent);color:#0d1117;border:0;border-radius:8px;
         font-weight:600;margin-top:6px}
  button.secondary{background:transparent;color:var(--accent);
         border:1px solid var(--accent)}
  button.danger{background:var(--bad);color:#fff}
  button:disabled{opacity:0.5}
  .row{display:flex;gap:8px;align-items:baseline}
  .big{font-size:30px;font-weight:700}
  .tag{display:inline-block;padding:2px 8px;border-radius:6px;
       background:#21262d;color:var(--muted);font-size:12px;margin-right:4px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:6px 4px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:500}
  pre{margin:0;font-size:11px;color:var(--muted);overflow-x:auto}
</style>
</head><body>
<h1>1080 PPA — Phase B</h1>

<div class="card" id="status-card">
  <div class="meta" id="status-meta">connecting…</div>
  <div id="latest-rep" style="margin-top:8px"></div>
</div>

<div class="card">
  <label>Set kg cap (CN3 must be plugged in)</label>
  <div class="row">
    <input type="number" id="kg" min="1" max="53" step="0.5" value="20">
    <span class="tag">×5.64 = % torque</span>
  </div>
  <button id="set-cap">Set cap</button>
  <button class="secondary" id="restore">Restore to factory (300%)</button>
  <div id="cap-result" class="meta" style="margin-top:10px"></div>
</div>

<div class="card">
  <div class="meta" style="margin-bottom:6px">Recent reps (newest first)</div>
  <table id="reps-table"><thead><tr id="reps-head"></tr></thead>
    <tbody id="reps-body"></tbody></table>
</div>

<script>
const fmt = (v) => {
  if (typeof v === 'number') return v.toFixed(3);
  if (typeof v === 'string' && /^-?\\d+\\.\\d+$/.test(v))
    return parseFloat(v).toFixed(3);
  return v;
};
async function refresh() {
  try {
    const r = await fetch('/api/reps');
    const j = await r.json();
    const meta = document.getElementById('status-meta');
    if (j.error) {
      meta.innerHTML = `<span class="err">${j.error}</span> · last fetch ${j.fetched_at || 'never'}`;
    } else {
      meta.innerHTML = `<span class="ok">live</span> · last fetch ${j.fetched_at} · ${j.reps.length} reps cached`;
    }
    const head = document.getElementById('reps-head');
    const body = document.getElementById('reps-body');
    const cols = j.columns || [];
    const showCols = ['id','pos','time','Dur','avgSpd','load'];
    const useCols = showCols.filter(c => cols.includes(c));
    head.innerHTML = ['ts', ...useCols].map(c => `<th>${c}</th>`).join('');
    body.innerHTML = (j.reps || []).map(rep => {
      const cells = ['<td>'+rep.ts+'</td>'];
      for (const c of useCols) cells.push('<td>'+fmt(rep[c])+'</td>');
      return '<tr>'+cells.join('')+'</tr>';
    }).join('');
    const latest = (j.reps || [])[0];
    const lat = document.getElementById('latest-rep');
    if (latest) {
      const load = latest.load || '0';
      const spd = latest.avgSpd || '0';
      lat.innerHTML =
        `<div class="big">${fmt(load)} kg · ${fmt(spd)} m/s</div>` +
        `<div class="meta">latest rep at ${latest.ts}</div>`;
    } else {
      lat.innerHTML = '<div class="meta">no reps yet</div>';
    }
  } catch (e) {
    document.getElementById('status-meta').innerHTML =
      '<span class="err">poll error: '+e+'</span>';
  }
}
document.getElementById('set-cap').onclick = async () => {
  const kg = document.getElementById('kg').value;
  if (!kg) return;
  if (!confirm(`Set drive torque cap at ${kg} kg? CN3 must be plugged in.`))
    return;
  document.getElementById('cap-result').innerHTML = 'writing…';
  const r = await fetch('/api/cap?kg=' + encodeURIComponent(kg), {method:'POST'});
  const j = await r.json().catch(() => ({}));
  const s = r.status;
  document.getElementById('cap-result').innerHTML = (s === 200)
    ? `<span class="ok">cap set: P03.10 ${j.p03_10_before} → ${j.p03_10_after} (${j.torque_pct}%)</span>`
    : `<span class="err">${s}: ${j.detail || JSON.stringify(j)}</span>`;
};
document.getElementById('restore').onclick = async () => {
  if (!confirm('Restore P03.10 to factory 3000 (300%)?'))
    return;
  document.getElementById('cap-result').innerHTML = 'restoring…';
  const r = await fetch('/api/restore', {method:'POST'});
  const j = await r.json().catch(()=>({}));
  document.getElementById('cap-result').innerHTML = (r.status === 200)
    ? `<span class="ok">restored: P03.10 ${j.p03_10_before} → ${j.p03_10_after}</span>`
    : `<span class="err">${r.status}: ${j.detail || ''}</span>`;
};
refresh(); setInterval(refresh, 2000);
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    print("Phase B service starting on http://127.0.0.1:8766/")
    print("Open on phone via http://<laptop-LAN-IP>:8766/")
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")
