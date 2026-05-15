"""Calibrate drive readings against the HMI's published per-rep stats.

The HMI at 192.168.88.222 exposes a datalog where each row is a completed rep
with calibrated columns: pos (m), avgSpd (m/s), load (kg). We read raw Modbus
state from the drive on COM6 in parallel, accumulate peaks/deltas per rep, and
print HMI vs drive side-by-side when a new rep row appears. Do reps at varied
HMI weight settings; each tuple becomes a calibration data point.

Read-only: no Modbus writes. HMI access is GET-only.
"""
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

from pymodbus.client import ModbusSerialClient

# ---- HMI ----
BASE = "https://192.168.88.222"
USERNAME = "admin"
PASSWORD = "Plummer-1994"
CHANNEL = 0
HMI_POLL_SEC = 2.0

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ---- Drive ----
PORT = "COM6"
DEVICE_ID = 1
DRIVE_HZ = 5.0

P21_00 = (21 << 8) | 0
P21_01 = (21 << 8) | 1
P21_04 = (21 << 8) | 4
P21_07 = (21 << 8) | 7  # low word, P21_08 high word


def s16(v): return v - 65536 if v >= 32768 else v
def s32(lo, hi):
    v = (hi << 16) | lo
    return v - (1 << 32) if v >= (1 << 31) else v


def hmi_login():
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


def hmi_fetch_today(token):
    """Return (columns, {rid: row}) for full datalog channel."""
    start, end = 0, 9999999999
    q = urllib.parse.urlencode({"start": start, "end": end, "order": ""})

    def _req(path):
        return urllib.request.Request(
            BASE + path, headers={"Authorization": f"Bearer {token}"})

    with urllib.request.urlopen(
        _req(f"/api/v1/data/datalog/{CHANNEL}/data/totalcount?{q}"),
        context=SSL_CTX, timeout=10,
    ) as r:
        initial = json.loads(r.read())

    columns = [c["comment"] for c in initial["columns"]]
    total = initial["total"]
    rows = {row["rid"]: row for row in initial.get("data", [])}

    # paginate if needed
    page = 1
    while len(rows) < total:
        q2 = urllib.parse.urlencode({
            "totalcount": total, "start": start, "end": end,
            "index": page, "order": "",
        })
        with urllib.request.urlopen(
            _req(f"/api/v1/data/datalog/{CHANNEL}/data?{q2}"),
            context=SSL_CTX, timeout=10,
        ) as r:
            extra = json.loads(r.read())
        before = len(rows)
        for row in extra.get("data", []):
            rows[row["rid"]] = row
        if len(rows) == before:
            break
        page += 1

    return columns, rows


class RepAccumulator:
    """Accumulate drive readings between HMI rep boundaries."""
    def __init__(self):
        self.reset()

    def reset(self, position_counts=None):
        self.start_pos = position_counts
        self.last_pos = position_counts
        self.peak_torque_pct = 0.0
        self.peak_rpm = 0
        self.n = 0
        self.t_start = time.monotonic()

    def update(self, position_counts, rpm, torque_pct):
        if self.start_pos is None:
            self.start_pos = position_counts
        self.last_pos = position_counts
        if abs(torque_pct) > abs(self.peak_torque_pct):
            self.peak_torque_pct = torque_pct
        if abs(rpm) > abs(self.peak_rpm):
            self.peak_rpm = rpm
        self.n += 1

    def snapshot(self):
        delta = (self.last_pos - self.start_pos) if self.start_pos is not None else 0
        return {
            "n_samples": self.n,
            "elapsed_s": round(time.monotonic() - self.t_start, 2),
            "peak_torque_pct": round(self.peak_torque_pct, 1),
            "peak_rpm": self.peak_rpm,
            "pos_delta_counts": delta,
        }


def main():
    print("=" * 72)
    print(" Calibrate drive against HMI per-rep datalog")
    print("=" * 72)

    print(f"[HMI] logging in to {BASE} as {USERNAME}...")
    try:
        token = hmi_login()
    except Exception as e:
        print(f"      HMI login FAILED: {e!r}")
        print(f"      Check network reachability to {BASE} (browser the URL).")
        return 1
    print(f"      token acquired (len={len(token)})")

    print(f"[HMI] baseline fetch of channel {CHANNEL}...")
    columns, rows = hmi_fetch_today(token)
    print(f"      columns: {columns}")
    print(f"      baseline rows: {len(rows)}  (these are pre-existing reps; ignored)")
    seen_rids = set(rows.keys())

    print(f"[DRV] opening Modbus on {PORT}...")
    client = ModbusSerialClient(port=PORT, baudrate=9600, parity="N",
                                stopbits=1, bytesize=8, timeout=0.5)
    if not client.connect():
        print(f"      could not open {PORT}. Is ppa_service holding it?")
        return 2
    print(f"      OK")

    acc = RepAccumulator()
    last_hmi_poll = 0.0
    drive_period = 1.0 / DRIVE_HZ
    next_drive_read = time.monotonic()
    rep_count = 0

    print()
    print("Now: do a rep at a known HMI weight setting. The script will print a")
    print("comparison line when the HMI logs the rep. Vary the kg setting between")
    print("reps. Ctrl+C to stop.")
    print()

    try:
        while True:
            now = time.monotonic()

            # ---- Drive read ----
            if now >= next_drive_read:
                try:
                    r = client.read_holding_registers(
                        address=P21_00, count=9, device_id=DEVICE_ID)
                    if r.isError():
                        print(f"      drive read error: {r}", file=sys.stderr)
                    else:
                        regs = r.registers  # P21.00..P21.08
                        status = regs[0]
                        rpm = s16(regs[1])
                        torque_pct = s16(regs[4]) * 0.1
                        pos = s32(regs[7], regs[8])
                        acc.update(pos, rpm, torque_pct)
                        # Live one-line status (overwrite-style)
                        sys.stdout.write(
                            f"\r[live] status={status} rpm={rpm:+5d} "
                            f"torque={torque_pct:+6.1f}%  "
                            f"pos={pos:+12d}  "
                            f"rep_peak_t={acc.peak_torque_pct:+6.1f}%  "
                            f"rep_n={acc.n:4d}        ")
                        sys.stdout.flush()
                except Exception as e:
                    print(f"\n      drive read raised: {e!r}", file=sys.stderr)
                next_drive_read = now + drive_period

            # ---- HMI poll ----
            if now - last_hmi_poll >= HMI_POLL_SEC:
                last_hmi_poll = now
                try:
                    columns, rows = hmi_fetch_today(token)
                except urllib.error.HTTPError as e:
                    if e.code == 401:
                        print("\n      HMI token expired, re-auth...", file=sys.stderr)
                        token = hmi_login()
                        continue
                    print(f"\n      HMI HTTP error: {e}", file=sys.stderr)
                    continue
                except Exception as e:
                    print(f"\n      HMI fetch raised: {e!r}", file=sys.stderr)
                    continue

                new_rids = sorted(set(rows.keys()) - seen_rids)
                for rid in new_rids:
                    row = rows[rid]
                    ds = row["ds"]

                    def col(name):
                        return ds[columns.index(name)] if name in columns else None

                    drive_snap = acc.snapshot()
                    rep_count += 1
                    print()  # break the live line
                    print("-" * 72)
                    print(f" REP #{rep_count}  rid={rid}  ts={row['ts']}")
                    print(f"   HMI:    pos={col('pos')}  avgSpd={col('avgSpd')}  "
                          f"load={col('load')}  time={col('time')}  Dur={col('Dur')}")
                    print(f"   DRIVE:  peak_torque={drive_snap['peak_torque_pct']}%  "
                          f"peak_rpm={drive_snap['peak_rpm']}  "
                          f"pos_delta={drive_snap['pos_delta_counts']}  "
                          f"elapsed={drive_snap['elapsed_s']}s  n={drive_snap['n_samples']}")
                    # Derived ratios (only meaningful if non-zero)
                    try:
                        kg_per_pct = (
                            float(col("load")) / abs(drive_snap["peak_torque_pct"])
                            if drive_snap["peak_torque_pct"] else None)
                    except (TypeError, ValueError):
                        kg_per_pct = None
                    try:
                        counts_per_m = (
                            abs(drive_snap["pos_delta_counts"]) / float(col("pos"))
                            if col("pos") not in (None, 0, "0") else None)
                    except (TypeError, ValueError, ZeroDivisionError):
                        counts_per_m = None
                    if kg_per_pct is not None:
                        print(f"   RATIO:  kg per peak-torque-%: {kg_per_pct:.3f}  "
                              f"(=> {1/kg_per_pct:.3f} %/kg)")
                    if counts_per_m is not None:
                        print(f"   RATIO:  counts per metre:     {counts_per_m:.1f}")
                    print("-" * 72)
                    seen_rids.add(rid)
                    # Reset for next rep — start at current position
                    acc.reset(position_counts=acc.last_pos)

            time.sleep(0.02)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
