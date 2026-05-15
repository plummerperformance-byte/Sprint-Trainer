"""PPA Sprint Trainer — desktop launcher.

Runs ppa_service in a background thread, opens the coach view in a native
window via pywebview, and shows a system-tray icon for quick actions.

Usage:  python ppa_app.py    (or pythonw ppa_app.py for no console)
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path

import pystray
import uvicorn
import webview
from PIL import Image, ImageDraw

from ppa_service import make_app

PORT = 8765
ICON_PATH = Path(__file__).with_name("ppa_icon.png")

log = logging.getLogger("ppa_app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def load_icon() -> Image.Image:
    if ICON_PATH.exists():
        try:
            return Image.open(ICON_PATH)
        except Exception as e:
            log.warning("couldn't open %s: %s — using placeholder", ICON_PATH, e)
    # Placeholder: orange "P" on dark square. Replace with real .ico/.png later.
    img = Image.new("RGBA", (64, 64), (10, 10, 12, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([4, 4, 60, 60], outline=(212, 130, 58, 255), width=2)
    d.text((19, 14), "P", fill=(212, 130, 58, 255))
    return img


def run_service() -> None:
    """Run uvicorn in this thread; blocks forever."""
    # make_app() defaults to port="auto" — ppa_service auto-detects the FTDI adapter.
    uvicorn.run(make_app(), host="0.0.0.0", port=PORT, log_level="warning")


def wait_for_service(timeout_s: float = 8.0) -> bool:
    """Poll localhost until the service answers or timeout."""
    import urllib.request
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/info", timeout=0.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def main() -> None:
    # Service runs on a daemon thread — exits when main thread exits.
    threading.Thread(target=run_service, daemon=True).start()

    if not wait_for_service():
        log.error("service didn't come up on 127.0.0.1:%d — opening window anyway", PORT)

    icon_image = load_icon()

    coach_url = f"http://127.0.0.1:{PORT}/coach"
    athlete_url = f"http://{get_lan_ip()}:{PORT}/athlete"

    # ---- Tray menu actions ----
    def on_open_coach(icon, item):
        try:
            wins = list(webview.windows)
            if wins:
                wins[0].show()
                if hasattr(wins[0], "restore"):
                    wins[0].restore()
        except Exception as e:
            log.warning("open coach failed: %s", e)

    def on_show_phone_url(icon, item):
        # Pop a small window with the phone URL + QR code
        body = (
            f"<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
            f"background:#0a0a0c;color:#f0f0f3;padding:24px;text-align:center\">"
            f"<div style=\"font-size:12px;color:#8a8a96;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:8px\">Athlete view</div>"
            f"<div style=\"font-size:18px;font-weight:600;margin-bottom:14px\">"
            f"<a href=\"{athlete_url}\" style=\"color:#d4823a;text-decoration:none\">{athlete_url}</a></div>"
            f"<div style=\"font-size:12px;color:#8a8a96\">Open this URL on the athlete's phone (same Wi-Fi).</div>"
            f"</div>"
        )
        webview.create_window("Athlete URL", html=body, width=480, height=200, resizable=False)

    def on_quit(icon, item):
        try:
            for w in list(webview.windows):
                w.destroy()
        except Exception:
            pass
        icon.stop()
        # Daemon thread carrying the FastAPI service dies with the process.
        sys.exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open coach view", on_open_coach, default=True),
        pystray.MenuItem("Show athlete (phone) URL", on_show_phone_url),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    tray = pystray.Icon("PPA Sprint Trainer", icon_image, "PPA Sprint Trainer", menu)
    threading.Thread(target=tray.run, daemon=True).start()

    # Main thread blocks here on the GUI loop.
    webview.create_window(
        "PPA Sprint Trainer",
        coach_url,
        width=1200, height=800, resizable=True,
        background_color="#0a0a0c",
    )
    try:
        webview.start()
    finally:
        # Window closed — disarm Phase C while the daemon service thread is
        # still alive. Otherwise the process exits without restoring the drive
        # and it is left stranded in mode 2.
        try:
            import urllib.request
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{PORT}/api/c/disarm", method="POST"),
                timeout=12,
            ).read()
            log.info("disarmed Phase C on exit")
        except Exception as e:
            log.warning("disarm-on-exit failed: %s", e)
        try:
            tray.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
