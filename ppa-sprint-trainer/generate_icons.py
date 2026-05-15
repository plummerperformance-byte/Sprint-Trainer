"""Generate placeholder PWA icons (192 and 512 px) using PPA palette.
Run once: `python generate_icons.py`. Replace the PNGs with real artwork later.
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUT = Path(__file__).parent / "static"
BG = (10, 10, 12, 255)
ACCENT = (212, 130, 58, 255)


def make(size: int, path: Path) -> None:
    img = Image.new("RGBA", (size, size), BG)
    d = ImageDraw.Draw(img)
    # Outer accent ring (maskable-safe: stays inside 80% safe zone)
    pad = int(size * 0.10)
    inset = int(size * 0.18)
    d.rounded_rectangle([pad, pad, size - pad, size - pad],
                        radius=int(size * 0.18), outline=ACCENT, width=max(2, size // 32))
    # Big "P"
    try:
        font = ImageFont.truetype("arial.ttf", int(size * 0.55))
    except Exception:
        font = ImageFont.load_default()
    text = "P"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - int(size * 0.02)),
           text, fill=ACCENT, font=font)
    img.save(path, "PNG")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    make(192, OUT / "icon-192.png")
    make(512, OUT / "icon-512.png")
