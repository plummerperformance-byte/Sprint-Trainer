"""Probe /app/dashboard — different namespace from /admin. Try unauthed and bearer-token."""
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

BASES = ["http://192.168.88.222", "https://192.168.88.222"]
USERNAME = "admin"
PASSWORD = "Plummer-1994"
TARGET = "/app/dashboard"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def get(base, path, headers=None):
    h = {"Accept": "text/html,application/json,*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(base + path, headers=h)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), list(r.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), list(e.headers.items())
    except Exception as e:
        return None, repr(e), []


def login(base):
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "username": USERNAME,
        "password": PASSWORD,
    }).encode()
    req = urllib.request.Request(
        f"{base}/api/v1/auth/token/admin",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
            return json.loads(r.read())["access_token"]
    except Exception as e:
        return None


def show(label, status, body, headers):
    print(f"--- {label} ---")
    print(f"status: {status}")
    if headers:
        for k, v in headers:
            if k.lower() in {"content-type", "location", "set-cookie", "www-authenticate"}:
                print(f"  {k}: {v}")
    snippet = body[:1500] if body else "(empty)"
    print(snippet)
    print()


for base in BASES:
    print(f"\n========== {base} ==========")
    st, body, hdrs = get(base, TARGET)
    show(f"GET {TARGET} unauthed", st, body, hdrs)

    token = login(base)
    if token:
        print(f"[got bearer token len={len(token)}]")
        st, body, hdrs = get(base, TARGET, {"Authorization": f"Bearer {token}"})
        show(f"GET {TARGET} with Bearer", st, body, hdrs)
    else:
        print("[bearer login failed at this base]")
