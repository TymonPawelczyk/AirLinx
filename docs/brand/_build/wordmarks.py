"""Wordmarks: shaped with HarfBuzz (font kerning), outlined to paths, one custom detail per direction.

Fonts (both SIL Open Font License 1.1, installed on Zorin OS 17):
  Inter 3.x (Rasmus Andersson)          — A, B
  JetBrains Mono 2.x (JetBrains)        — C
"""
import math
from geom import *  # noqa
from marks import _chevron, SQ3

SIZE = 1000  # wordmarks are built at 1000 units per em, baseline y=0


def _terminal_span(glyph_path, y0, y1, xmin, xmax):
    b = inter(glyph_path, box(xmin, y0, xmax, y1)).bounds
    return b[0], b[2]


# ------------------------------------------------------------------ A: Inter SemiBold, woven x
def wordmark_A(gap=34):
    F = Font(FONT_DIR_INTER + "Inter-SemiBold.ttf")

    def woven_x(x, base, size, adv):
        g = F.glyph("x", x, base, size)
        bx0, by0, bx1, by1 = g.bounds
        mid = (bx0 + bx1) / 2
        tl = _terminal_span(g, by0, by0 + 4, bx0 - 1, mid)
        tr = _terminal_span(g, by0, by0 + 4, mid, bx1 + 1)
        bl = _terminal_span(g, by1 - 4, by1, bx0 - 1, mid)
        br = _terminal_span(g, by1 - 4, by1, mid, bx1 + 1)
        back = poly([(tl[0], by0), (tl[1], by0), (br[1], by1), (br[0], by1)])   # "\\" over
        fwd = poly([(bl[0], by1), (bl[1], by1), (tr[1], by0), (tr[0], by0)])    # "/" under
        slope = ((br[0] + br[1]) - (tl[0] + tl[1])) / 2 / (by1 - by0)
        hg = gap * math.sqrt(1 + slope * slope)          # horizontal width of a perpendicular gap
        e = 200
        cut = poly([(tl[0] - hg - slope * e, by0 - e), (tl[1] + hg - slope * e, by0 - e),
                    (br[1] + hg + slope * e, by1 + e), (br[0] - hg + slope * e, by1 + e)])
        return union(back, diff(fwd, cut))

    text = "AirLinx"
    glyphs, end = F.shape(text, SIZE, tracking=-0.012, replace={6: woven_x})
    return {"word": union(*glyphs.values())}, F.capheight / F.upem * SIZE, F


# ------------------------------------------------------------------ B: Inter ExtraBold lowercase, ear tittles
def wordmark_B(dots="screen"):
    F = Font(FONT_DIR_INTER + "Inter-ExtraBold.ttf")
    xh = F.xheight / F.upem * SIZE

    def ear_i(lean):
        def draw(x, base, size, adv):
            stem = F.glyph("dotlessi", x, base, size)
            sx0, _, sx1, _ = stem.bounds
            w = sx1 - sx0
            top = base - xh - 0.075 * size        # gap above the stem
            if dots == "screen":
                sw, sh = w * 1.42, w * 0.86
                cx = (sx0 + sx1) / 2
                return union(stem, rrect(cx - sw / 2, top - sh, sw, sh, sh * 0.32))
            h = 0.19 * size
            ear = rounded(poly([(sx0 - 0.035 * size, top), (sx0 + w / 2 + lean, top - h), (sx1 + 0.035 * size, top)]), 8)
            return union(stem, ear)
        return draw

    text = "airlinx"
    glyphs, end = F.shape(text, SIZE, tracking=-0.018, replace={1: ear_i(-44), 4: ear_i(44)})
    return {"word": union(*glyphs.values())}, xh, F


# ------------------------------------------------------------------ C: JetBrains Mono ExtraBold, chevron x
def wordmark_C(diamond_dots=True, chevron_x=False):
    F = Font(FONT_DIR_JB + "JetBrainsMono-ExtraBold.ttf")
    xh = F.xheight / F.upem * SIZE

    def chev_x(x, base, size, adv):
        ref = F.glyph("x", x, base, size)
        bx0, by0, bx1, by1 = ref.bounds
        h = by1 - by0
        a = h / 2
        w = 0.165 * size                    # matches the ExtraBold stem colour
        tipgap = 0.07 * size
        cxm = (bx0 + bx1) / 2
        vx = cxm - tipgap / 2 - w
        left = _chevron(0, a, w, 1)
        lx0, _, lx1, _ = left.bounds
        # left edge where the font's x starts; _chevron is centred on y=128
        left = xform(left, 1, 0, 0, 1, bx0 - lx0, (by0 + by1) / 2 - 128)
        tipx = bx0 + (lx1 - lx0)
        right = mirror_x(left, tipx + tipgap / 2)
        return union(left, right)

    def dia_i(x, base, size, adv):
        stem = F.glyph("dotlessi", x, base, size)
        full = F.glyph("i", x, base, size)
        dot = diff(full, box(-1e5, base - xh - 2, 1e5, 1e5))
        dx0, dy0, dx1, dy1 = dot.bounds
        cx, cy = (dx0 + dx1) / 2, (dy0 + dy1) / 2
        r = (dy1 - dy0) * 0.62
        return union(stem, poly([(cx - r, cy), (cx, cy - r), (cx + r, cy), (cx, cy + r)]))

    rep = {6: chev_x} if chevron_x else {}
    if diamond_dots:
        rep.update({1: dia_i, 4: dia_i})
    text = "airlinx"
    glyphs, end = F.shape(text, SIZE, tracking=-0.04, replace=rep)
    return {"word": union(*glyphs.values())}, xh, F
