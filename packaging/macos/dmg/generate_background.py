"""Regenerate the SpaceRouter DMG background.

Produces a 600x400 @1x PNG + a 1200x800 @2x PNG, then combine them into a
HiDPI TIFF with `tiffutil -cathidpicheck`. The TIFF is what create-dmg uses
via --background in .github/workflows/build.yml.

Layout:
  - Default macOS Finder light gray (#f0f0f0).
  - Font Awesome `chevron-right-solid` centered at (300, 185), dimmed to #84868c
    so it doesn't compete with the app/Applications icons.
  - The chevron SVG is rasterized ahead of time via `qlmanage -t` into a PNG.

Usage:
  # Step 1: rasterize chevron-right-solid.svg to a PNG at any high resolution
  qlmanage -t -s 1200 -o /tmp/out packaging/macos/dmg/chevron-right-solid.svg
  # Step 2: regenerate both PNGs
  python3 packaging/macos/dmg/generate_background.py \
      packaging/macos/dmg/ /tmp/out/chevron-right-solid.svg.png
  # Step 3: combine into HiDPI TIFF
  tiffutil -cathidpicheck packaging/macos/dmg/background.png \
      packaging/macos/dmg/background@2x.png \
      -out packaging/macos/dmg/background.tiff
"""
from PIL import Image, ImageOps
import os
import sys


def draw_background(img):
    w, h = img.size
    img.paste((240, 240, 240, 255), (0, 0, w, h))


def load_chevron(src_path, tint=(130, 132, 140)):
    raw = Image.open(src_path).convert("RGB")
    alpha = ImageOps.invert(raw.convert("L"))
    out = Image.new("RGBA", raw.size, tint + (0,))
    out.putalpha(alpha)
    return out


def compose_chevron(img, scale, chevron_src):
    chev = load_chevron(chevron_src)
    target_h_layout = 44
    target_h = round(target_h_layout * scale)
    target_w = round(target_h * (320 / 512))
    chev = chev.resize((target_w, target_h), Image.LANCZOS)
    cx = round(300 * scale)
    cy = round(185 * scale)
    img.alpha_composite(chev, (cx - target_w // 2, cy - target_h // 2))


def make(width, height, path, chevron_src):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_background(img)
    compose_chevron(img, width / 600.0, chevron_src)
    img.save(path)
    print(f"wrote {path}")


if __name__ == "__main__":
    out_dir = sys.argv[1]
    chevron_src = sys.argv[2]
    make(600, 400, os.path.join(out_dir, "background.png"), chevron_src)
    make(1200, 800, os.path.join(out_dir, "background@2x.png"), chevron_src)
