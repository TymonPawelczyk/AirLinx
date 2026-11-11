#!/usr/bin/env python3
"""Render the 9-slice bitmaps behind the TV drop shadow (ui/assets/shade/tv-{dark,light}.png).

The UI used `box-shadow: 0 40px 90px -30px rgba(0,0,0,.9)` on the TV frame. WebKitGTK repaints such a blurred
shadow in full whenever anything under it changes, which cost ~100 ms per frame on a 4K window, so
ui/app.css draws the same shadow with `border-image` from these pre-blurred bitmaps instead.

Parameters must match tokens.css (--sh-*): blur radius B -> sigma = B / 2, extent E = 3 * sigma, slice = 2 * E.
Usage: python scripts/gen_tv_shadow.py   (needs Pillow)
"""
import pathlib

from PIL import Image, ImageDraw, ImageFilter

OUT = pathlib.Path(__file__).resolve().parent.parent / "ui" / "assets" / "shade"
THEMES = {"dark": dict(blur=90, alpha=.90), "light": dict(blur=60, alpha=.45)}
MID = 8


def render(blur: int, alpha: float) -> Image.Image:
    sigma = blur / 2
    e = round(3 * sigma)
    size = 4 * e + MID
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rectangle([e, e, size - e - 1, size - e - 1], fill=255)   # the shadow rectangle
    mask = mask.filter(ImageFilter.GaussianBlur(sigma)).point(lambda v: round(v * alpha))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.putalpha(mask)
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, p in THEMES.items():
        img = render(**p)
        img.save(OUT / f"tv-{name}.png", optimize=True)
        print(name, img.size, "slice", 2 * round(3 * p["blur"] / 2))
