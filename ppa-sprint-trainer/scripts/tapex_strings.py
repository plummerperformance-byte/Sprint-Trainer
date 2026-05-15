"""Extract ASCII strings from T-APEX .dex files and grep for interesting patterns.
This is an `strings` clone that focuses on what we want from a competitor APK
binary: BLE UUIDs, command parameters, modes, credentials.
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEX_DIR = Path("C:/Users/trigo/tapex_unpack")
MIN_LEN = 5  # min ASCII run

# Patterns we want to spot
UUID_RE = re.compile(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
URL_RE = re.compile(rb"https?://[^\s\"']{3,200}")
KEYWORDS = [
    rb"vchange", rb"rchange", rb"recspeed", rb"training_distance",
    rb"resistance", rb"assistance", rb"overspeed", rb"isotonic",
    rb"calibration", rb"7777", rb"RE07", rb"OTA", rb"firmware",
    rb"BluetoothGatt", rb"writeCharacteristic", rb"setCharacteristicNotification",
    rb"normal", rb"NORMAL", rb"ISOTONIC", rb"OVERSPEED", rb"CALIBRATION",
    rb"Vchange", rb"Rchange",
]


def ascii_strings(buf, min_len=MIN_LEN):
    """Yield ASCII strings of length >= min_len from a byte buffer."""
    return re.findall(rb"[\x20-\x7e]{%d,}" % min_len, buf)


def main():
    all_uuids = set()
    all_urls = set()
    found_keywords = {kw.decode(errors="replace"): set() for kw in KEYWORDS}
    interesting_strings = set()

    for dex in sorted(DEX_DIR.glob("classes*.dex")):
        print(f"--- {dex.name} ({dex.stat().st_size} bytes) ---")
        buf = dex.read_bytes()
        for u in UUID_RE.findall(buf):
            all_uuids.add(u.decode(errors="replace"))
        for u in URL_RE.findall(buf):
            all_urls.add(u.decode(errors="replace"))
        for kw in KEYWORDS:
            if kw in buf:
                # extract ascii context around occurrences
                for m in re.finditer(re.escape(kw), buf):
                    start = max(0, m.start() - 40)
                    end = min(len(buf), m.end() + 40)
                    context = buf[start:end]
                    # strip non-printable
                    context = re.sub(rb"[^\x20-\x7e]+", b" ", context).strip()
                    found_keywords[kw.decode()].add(context.decode(errors="replace")[:120])

        # collect "interesting" strings (mid-length, mostly text)
        for s in ascii_strings(buf, min_len=8):
            txt = s.decode(errors="replace")
            # filter to ones with at least a few alphanumeric chars and lowercase
            if any(c.islower() for c in txt) and any(c.isupper() or c.isdigit() for c in txt):
                if 8 <= len(txt) <= 80:
                    interesting_strings.add(txt)

    print()
    print("=" * 60)
    print(f"BLE / UUID candidates ({len(all_uuids)} unique):")
    print("=" * 60)
    for u in sorted(all_uuids)[:30]:
        print(f"  {u}")

    print()
    print("=" * 60)
    print(f"URLs ({len(all_urls)} unique):")
    print("=" * 60)
    for u in sorted(all_urls)[:30]:
        print(f"  {u}")

    print()
    print("=" * 60)
    print("Keyword hits:")
    print("=" * 60)
    for kw, contexts in found_keywords.items():
        if contexts:
            print(f"\n[{kw}] ({len(contexts)} contexts)")
            for c in list(contexts)[:5]:
                print(f"  ... {c} ...")


if __name__ == "__main__":
    main()
