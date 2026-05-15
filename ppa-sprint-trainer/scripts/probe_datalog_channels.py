"""Probe HMI datalog channels 0..5 to find where rep data lives."""
import json
import ssl
import sys
import urllib.parse
import urllib.request
import urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://192.168.88.222"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def login():
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "username": "admin",
        "password": "Plummer-1994",
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/api/v1/auth/token/admin",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def probe(token, ch):
    q = urllib.parse.urlencode({"start": 0, "end": 9999999999, "order": ""})
    req = urllib.request.Request(
        f"{BASE}/api/v1/data/datalog/{ch}/data/totalcount?{q}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=8) as r:
            j = json.loads(r.read())
        cols = [c.get("comment", "?") for c in j.get("columns", [])]
        total = j.get("total", "?")
        sample = j.get("data", [])[:1]
        return f"OK total={total} cols={cols}", sample
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read()[:200]!r}", []
    except Exception as e:
        return f"ERR {e!r}", []


token = login()
print("Token OK\n")
for ch in range(6):
    msg, sample = probe(token, ch)
    print(f"channel {ch}: {msg}")
    for s in sample:
        print(f"   sample row: rid={s.get('rid')} ts={s.get('ts')} ds={s.get('ds')}")
    print()
