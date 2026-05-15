"""Probe /admin/features/webview — can we reach it with the API bearer token,
or is it a session-cookie form-auth route? Returns whatever we find.
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://192.168.88.222"
USERNAME = "admin"
PASSWORD = "Plummer-1994"
TARGET = "/admin/features/webview"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def show(label, status, body, headers=None):
    print(f"--- {label} ---")
    print(f"status: {status}")
    if headers:
        for k, v in headers:
            if k.lower() in {"content-type", "location", "set-cookie", "www-authenticate"}:
                print(f"  {k}: {v}")
    snippet = body[:1200] if body else "(empty)"
    print(snippet)
    print()


def get_unauthed(path):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), list(r.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), list(e.headers.items())


def login_for_token():
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
        return json.loads(r.read())


def get_with_bearer(path, token):
    req = urllib.request.Request(
        BASE + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/html,application/xhtml+xml,application/json,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), list(r.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), list(e.headers.items())


def session_login_get(path):
    """Try a cookie-based session: POST /admin/login form, then GET path."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=CTX),
    )
    body = urllib.parse.urlencode({
        "username": USERNAME,
        "password": PASSWORD,
    }).encode()
    login_req = urllib.request.Request(
        f"{BASE}/admin/login",
        data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,*/*",
        },
    )
    try:
        with opener.open(login_req, timeout=10) as r:
            login_status = r.status
            login_body = r.read().decode("utf-8", errors="replace")[:300]
    except urllib.error.HTTPError as e:
        login_status = e.code
        login_body = e.read().decode("utf-8", errors="replace")[:300]

    print(f"[session login POST /admin/login] status={login_status}")
    print(f"   cookies after login: {[c.name for c in cj]}")
    print(f"   body snippet: {login_body!r}")
    print()

    target_req = urllib.request.Request(
        BASE + path,
        headers={"Accept": "text/html,*/*"},
    )
    try:
        with opener.open(target_req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), list(r.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), list(e.headers.items())


def main():
    print(f"Probing {BASE}{TARGET}\n")

    # 1. unauthed
    st, body, hdrs = get_unauthed(TARGET)
    show("1. GET unauthed", st, body, hdrs)

    # 2. with API bearer token
    print("[acquiring API bearer token...]")
    try:
        token_resp = login_for_token()
        token = token_resp.get("access_token", "")
        print(f"   token len={len(token)}")
        print(f"   token_resp keys: {list(token_resp.keys())}")
    except Exception as e:
        print(f"   token fetch failed: {e!r}")
        token = None
    print()

    if token:
        st, body, hdrs = get_with_bearer(TARGET, token)
        show("2. GET with Bearer token", st, body, hdrs)

    # 3. session form login
    st, body, hdrs = session_login_get(TARGET)
    show("3. GET after form login (cookie)", st, body, hdrs)


if __name__ == "__main__":
    main()
