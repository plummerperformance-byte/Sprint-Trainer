"""One-shot fetch of HMI datalog channel 0 (per-rep records). Pure HTTPS, no Modbus."""
import json
import ssl
import sys
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://192.168.88.222"
USERNAME = "admin"
PASSWORD = "Plummer-1994"
CHANNEL = 0

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def login():
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
    with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def fetch_all(token):
    start, end = 0, 9999999999
    q = urllib.parse.urlencode({"start": start, "end": end, "order": ""})

    def _req(path):
        return urllib.request.Request(
            BASE + path, headers={"Authorization": f"Bearer {token}"})

    with urllib.request.urlopen(
        _req(f"/api/v1/data/datalog/{CHANNEL}/data/totalcount?{q}"),
        context=CTX, timeout=10,
    ) as r:
        initial = json.loads(r.read())

    columns = [c["comment"] for c in initial["columns"]]
    total = initial["total"]
    rows = {row["rid"]: row for row in initial.get("data", [])}

    page = 1
    while len(rows) < total:
        q2 = urllib.parse.urlencode({
            "totalcount": total, "start": start, "end": end,
            "index": page, "order": "",
        })
        with urllib.request.urlopen(
            _req(f"/api/v1/data/datalog/{CHANNEL}/data?{q2}"),
            context=CTX, timeout=10,
        ) as r:
            extra = json.loads(r.read())
        before = len(rows)
        for row in extra.get("data", []):
            rows[row["rid"]] = row
        if len(rows) == before:
            break
        page += 1

    return columns, sorted(rows.values(), key=lambda r: r["rid"])


def main():
    print("Logging in to HMI...")
    token = login()
    print(f"Fetching channel {CHANNEL} datalog...\n")
    columns, rows = fetch_all(token)
    print(f"Columns: {columns}")
    print(f"Total rows in datalog: {len(rows)}\n")
    if not rows:
        print("(no rows)")
        return
    # Pretty table
    headers = ["rid", "ts"] + columns
    table = []
    for r in rows:
        ds = r["ds"]
        row = [str(r["rid"]), r["ts"]] + [str(v) for v in ds]
        table.append(row)
    widths = [max(len(headers[i]), max(len(r[i]) for r in table)) for i in range(len(headers))]
    print(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("-+-".join("-" * w for w in widths))
    for row in table:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


if __name__ == "__main__":
    main()
