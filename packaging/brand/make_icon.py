"""Regenerate the app icon from the DME Innovation wordmark.

Writes ``packaging/icon.png`` (512 px) and ``packaging/icon.ico`` (16..256 px):
the large **DME** row of the wordmark in white, centred on a rounded papaya
square.

Only the DME row is used — the "INNOVATION" line is far too fine to survive a
16 px taskbar tile. Each ICO size is rendered separately with size-appropriate
padding (small tiles get less) so no stroke ever falls below one pixel; small
tiles are supersampled 8x for clean edges.

    pip install cairosvg pillow
    python packaging/brand/make_icon.py
"""

from __future__ import annotations

import io
import os
import re

import cairosvg
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE_SVG = os.path.join(ROOT, "webui", "public", "assets", "dme-logo.svg")
OUT_PNG = os.path.join(ROOT, "packaging", "icon.png")
OUT_ICO = os.path.join(ROOT, "packaging", "icon.ico")

BRAND = (255, 122, 0, 255)          # papaya #FF7A00, the UI accent
CORNER = 0.22                       # rounded-square radius, fraction of the tile
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# Bounding box of the DME row inside the wordmark's coordinate system.
DME_BOX = (54.55, 24.0, 913.37, 308.49)


def _dme_mark() -> str:
    """The DME row alone, as a white-filled standalone SVG."""

    svg = open(SOURCE_SVG, encoding="utf-8").read()
    paths = re.findall(r'<path[^>]*d="([^"]+)"[^>]*>', svg)

    def top(d: str) -> float:
        ys = [float(v) for v in re.findall(r"-?\d+\.?\d*", d)][1::2]
        return min(ys) if ys else 1e9

    # The wordmark is two rows; everything above y=330 is the big DME letters.
    dme = [d for d in paths if top(d) < 330]
    if not dme:
        raise SystemExit(f"no DME paths found in {SOURCE_SVG}")
    x0, y0, x1, y1 = DME_BOX
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%f %f %f %f">'
        % (x0, y0, x1 - x0, y1 - y0)
        + "".join('<path fill="#FFFFFF" d="%s"/>' % d for d in dme)
        + "</svg>"
    )


def render(size: int, mark: str) -> Image.Image:
    x0, y0, x1, y1 = DME_BOX
    w, h = x1 - x0, y1 - y0
    pad = 0.07 if size <= 32 else (0.11 if size <= 64 else 0.16)
    ss = 8 if size <= 64 else 2      # supersample small tiles
    tw = max(8, int(size * (1 - 2 * pad)))
    th = max(3, int(round(tw * h / w)))
    png = cairosvg.svg2png(bytestring=mark.encode(),
                           output_width=tw * ss, output_height=th * ss)
    logo = Image.open(io.BytesIO(png)).convert("RGBA")

    tile = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, size * ss - 1, size * ss - 1],
        radius=int(size * ss * CORNER), fill=BRAND)
    tile.alpha_composite(logo, ((size * ss - logo.width) // 2,
                                (size * ss - logo.height) // 2))
    return tile.resize((size, size), Image.LANCZOS) if ss > 1 else tile


def main() -> int:
    mark = _dme_mark()
    render(512, mark).save(OUT_PNG)
    tiles = {s: render(s, mark) for s in ICO_SIZES}
    tiles[256].save(OUT_ICO, sizes=[(s, s) for s in ICO_SIZES],
                    append_images=[tiles[s] for s in ICO_SIZES if s != 256])
    print(f"wrote {OUT_PNG} and {OUT_ICO} ({', '.join(map(str, ICO_SIZES))} px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
