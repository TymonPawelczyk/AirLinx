"""Geometry helpers for the AirLinx brand build (skia-pathops booleans, fontTools outlines)."""
import math
import pathops
from fontTools.svgLib.path import parse_path
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import DecomposingRecordingPen
import uharfbuzz as hb

OP = pathops.PathOp


def P(d):
    p = pathops.Path()
    parse_path(d, p.getPen())
    return p


def clone(p):
    q = pathops.Path()
    p.draw(q.getPen())
    return q


def rrect(x, y, w, h, r=0):
    r = max(0, min(r, w / 2, h / 2))
    if r == 0:
        return P(f"M{x} {y}H{x+w}V{y+h}H{x}Z")
    return P(
        f"M{x+r} {y}H{x+w-r}A{r} {r} 0 0 1 {x+w} {y+r}V{y+h-r}A{r} {r} 0 0 1 {x+w-r} {y+h}"
        f"H{x+r}A{r} {r} 0 0 1 {x} {y+h-r}V{y+r}A{r} {r} 0 0 1 {x+r} {y}Z")


def box(x0, y0, x1, y1, r=0):
    return rrect(x0, y0, x1 - x0, y1 - y0, r)


def circle(cx, cy, r):
    return P(f"M{cx-r} {cy}A{r} {r} 0 1 0 {cx+r} {cy}A{r} {r} 0 1 0 {cx-r} {cy}Z")


def poly(pts):
    return P("M" + "L".join(f"{x} {y}" for x, y in pts) + "Z")


def empty():
    return pathops.Path()


def union(*ps):
    ps = [p for p in ps if p is not None]
    out = clone(ps[0])
    for p in ps[1:]:
        out = pathops.op(out, p, OP.UNION)
    return out


def diff(a, *bs):
    for b in bs:
        a = pathops.op(a, b, OP.DIFFERENCE)
    return a


def inter(a, b):
    return pathops.op(a, b, OP.INTERSECTION)


_CAPS = {"round": pathops.LineCap.ROUND_CAP, "butt": pathops.LineCap.BUTT_CAP,
         "square": pathops.LineCap.SQUARE_CAP}
_JOINS = {"round": pathops.LineJoin.ROUND_JOIN, "miter": pathops.LineJoin.MITER_JOIN,
          "bevel": pathops.LineJoin.BEVEL_JOIN}


def stroke(src, w, cap="round", join="round", miter=4):
    p = P(src) if isinstance(src, str) else clone(src)
    p.stroke(w, _CAPS[cap], _JOINS[join], miter)
    p.convertConicsToQuads()
    return pathops.simplify(p)


def dilate(p, k):
    return union(p, stroke(p, 2 * k))


def erode(p, k):
    return diff(p, stroke(p, 2 * k))


def rounded(p, k):
    """Round the convex corners of a shape by radius k without changing its size."""
    return dilate(erode(p, k), k)


def ring(x0, y0, x1, y1, t, r):
    return diff(box(x0, y0, x1, y1, r), box(x0 + t, y0 + t, x1 - t, y1 - t, max(r - t, 0)))


def xform(p, a=1, b=0, c=0, d=1, e=0, f=0):
    q = pathops.Path()
    p.draw(TransformPen(q.getPen(), (a, b, c, d, e, f)))
    return q


def mirror_x(p, axis=128):
    return xform(p, -1, 0, 0, 1, 2 * axis, 0)


def bounds(parts):
    bs = [p.bounds for p in parts if p is not None and list(p.segments)]
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def fit(parts: dict, x0, y0, x1, y1, dy=0.0):
    """Uniformly scale+centre a dict of paths into the box; dy is an optical nudge in box units."""
    bx0, by0, bx1, by1 = bounds(parts.values())
    s = min((x1 - x0) / (bx1 - bx0), (y1 - y0) / (by1 - by0))
    ox = (x0 + x1) / 2 - s * (bx0 + bx1) / 2
    oy = (y0 + y1) / 2 - s * (by0 + by1) / 2 + dy
    return {k: xform(v, s, 0, 0, s, ox, oy) for k, v in parts.items()}, s


def place(parts: dict, s, ox, oy):
    return {k: xform(v, s, 0, 0, s, ox, oy) for k, v in parts.items()}


def fmt(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def to_d(p):
    pen = SVGPathPen(None, ntos=fmt)
    p.draw(pen)
    return pen.getCommands()


def svg_doc(w, h, layers, title, extra_defs="", bg=None):
    """layers: list of (path, fill) or raw strings."""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {fmt(w)} {fmt(h)}" width="{fmt(w)}" height="{fmt(h)}">',
           f"<title>{title}</title>"]
    if extra_defs:
        out.append(f"<defs>{extra_defs}</defs>")
    if bg:
        out.append(f'<rect width="{fmt(w)}" height="{fmt(h)}" fill="{bg}"/>')
    for layer in layers:
        if isinstance(layer, str):
            out.append(layer)
        else:
            p, fill = layer
            out.append(f'<path fill="{fill}" d="{to_d(p)}"/>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- type
FONT_DIR_INTER = "/usr/share/fonts/truetype/inter-zorin-os/"
FONT_DIR_JB = "/usr/share/fonts/truetype/jetbrains-mono-zorin-os/"


class Font:
    def __init__(self, path, features=None):
        self.path = path
        self.tt = TTFont(path)
        self.gs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.upem = self.tt["head"].unitsPerEm
        blob = hb.Blob.from_file_path(path)
        self.hbfont = hb.Font(hb.Face(blob))
        self.features = features or {}
        os2 = self.tt["OS/2"]
        self.xheight = getattr(os2, "sxHeight", 0) or 0
        self.capheight = getattr(os2, "sCapHeight", 0) or 0

    def glyph(self, name, x, baseline, size):
        s = size / self.upem
        rec = DecomposingRecordingPen(self.gs)
        self.gs[name].draw(rec)
        p = pathops.Path()
        rec.replay(TransformPen(p.getPen(), (s, 0, 0, -s, x, baseline)))
        return pathops.simplify(p)

    def shape(self, text, size, tracking=0.0, kern_adjust=None, x=0.0, baseline=0.0, replace=None):
        """Return (dict index->path, advance end x). tracking in em; kern_adjust {(i): em} extra after glyph i.
        replace: {index: callable(x, baseline, size, advance) -> path} to swap a glyph for a custom drawing."""
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        feats = {"kern": True, "liga": False, "calt": False}
        feats.update(self.features)
        hb.shape(self.hbfont, buf, feats)
        s = size / self.upem
        glyphs = {}
        cx = x
        for i, (info, pos) in enumerate(zip(buf.glyph_infos, buf.glyph_positions)):
            name = self.order[info.codepoint]
            adv = pos.x_advance * s
            if replace and i in replace:
                glyphs[i] = replace[i](cx, baseline, size, adv)
            else:
                glyphs[i] = self.glyph(name, cx + pos.x_offset * s, baseline - pos.y_offset * s, size)
            cx += adv + tracking * size
            if kern_adjust and i in kern_adjust:
                cx += kern_adjust[i] * size
        return glyphs, cx - tracking * size

    def advance(self, ch, size):
        name = self.tt.getBestCmap()[ord(ch)]
        return self.tt["hmtx"][name][0] * size / self.upem


# ---------------------------------------------------------------- colour
def _lin(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcol):
    h = hexcol.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)
