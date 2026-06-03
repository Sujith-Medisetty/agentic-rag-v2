#!/usr/bin/env python3
"""
Generate PWA PNG icons from web/public/icon.svg.

Run ONCE after the first install (and again whenever icon.svg changes):

    pip install pillow cairosvg
    python scripts/generate-pwa-icons.py

Outputs to web/public/icons/:
    icon-192.png             — Android home screen (legacy)
    icon-512.png             — Android home screen (modern)
    icon-maskable-512.png    — Android adaptive icon (full bleed, safe zone 80%)
    apple-touch-icon.png     — iOS home screen (180×180)

The SVG icon already works in modern browsers' PWA manifests, but iOS
specifically wants apple-touch-icon as PNG and old Android phones prefer
PNG too — so this script bakes the PNG variants once.

Why two deps:
  Pillow alone can't rasterise SVG — it understands raster formats only.
  cairosvg converts SVG → PNG bytes using libcairo; Pillow then handles the
  maskable-icon padding (the 'safe zone' Android needs).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "web" / "public" / "icon.svg"
OUT_DIR = ROOT / "web" / "public" / "icons"

SIZES = [
    ("icon-192.png", 192, False),
    ("icon-512.png", 512, False),
    # Maskable icon: keep the glyph inside the central 80% so Android's
    # adaptive shape never clips it. We render the SVG at 80% size and pad
    # with the manifest background_color (#0b0d10).
    ("icon-maskable-512.png", 512, True),
    # iOS apple-touch-icon — Apple recommends 180×180 for current devices.
    ("apple-touch-icon.png", 180, False),
]
BACKGROUND_RGBA = (11, 13, 16, 255)   # #0b0d10


def main() -> int:
    try:
        import cairosvg
        from PIL import Image
    except ImportError as e:
        print(
            f"error: missing Python dependency ({e.name}). "
            "Install with:  pip install pillow cairosvg",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        # cairosvg imports cleanly but libcairo (the native C library) is
        # missing. Common on a fresh macOS — cairosvg is just a wrapper.
        print(
            "error: cairosvg loaded but the native libcairo library is missing.\n"
            "\n"
            "  macOS:   brew install cairo\n"
            "  Ubuntu:  sudo apt-get install libcairo2\n"
            "  Windows: pip install cairocffi   (bundles a Windows build)\n"
            "\n"
            f"  Original error: {e}",
            file=sys.stderr,
        )
        return 1

    if not SVG_PATH.is_file():
        print(f"error: source icon missing: {SVG_PATH}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svg_bytes = SVG_PATH.read_bytes()

    for name, size, maskable in SIZES:
        out_path = OUT_DIR / name
        if maskable:
            inner = int(size * 0.8)
            png_bytes = cairosvg.svg2png(
                bytestring=svg_bytes,
                output_width=inner, output_height=inner,
            )
            inner_img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            canvas = Image.new("RGBA", (size, size), BACKGROUND_RGBA)
            offset = ((size - inner) // 2, (size - inner) // 2)
            canvas.paste(inner_img, offset, inner_img)
            canvas.save(out_path, "PNG", optimize=True)
        else:
            png_bytes = cairosvg.svg2png(
                bytestring=svg_bytes,
                output_width=size, output_height=size,
            )
            Image.open(io.BytesIO(png_bytes)).convert("RGBA").save(
                out_path, "PNG", optimize=True,
            )
        print(f"  wrote {out_path.relative_to(ROOT)} ({size}×{size})")

    # favicon.ico — multi-size ICO so the browser tab uses a crisp variant
    # at every zoom level. Built from the 192 and 512 PNGs.
    try:
        from PIL import Image
        favicon = OUT_DIR.parent / "favicon.ico"
        img_192 = Image.open(OUT_DIR / "icon-192.png")
        img_192.save(favicon, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        print(f"  wrote {favicon.relative_to(ROOT)}")
    except Exception as e:
        print(f"  warn: favicon.ico generation skipped ({e})")

    print()
    print("done. Commit the icons to git so they ship with the next build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
