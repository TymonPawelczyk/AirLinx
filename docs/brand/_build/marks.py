"""Symbol construction for the AirLinx mark (the final kit uses the chevron helpers).

Every builder returns a dict of named parts (pathops.Path) in a free coordinate space;
callers fit() them into a canvas. Parts are named so colour versions can paint them separately.
"""
import math
from geom import *  # noqa

SQ3 = math.sqrt(3)


# ======================================================================== A — Ogniwo
def symbol_A(t=16, rR=22, rL=20, g=7, Rw=176, Rh=99, Lw=84, Lh=76):
    """Two rounded frames interlocked like chain links: a small window (L) centred exactly on the
    bottom-left corner of a 16:9 screen (R). Both crossings land mid-bar, symmetric about the diagonal.

    Crossing 1: R's left bar passes OVER L's top bar  (gap cut in L).
    Crossing 2: L's right bar passes OVER R's bottom bar (gap cut in R).
    """
    Rx0, Ry0 = 0, 0
    Rx1, Ry1 = Rw, Rh
    cx, cy = Rx0 + t / 2, Ry1 - t / 2            # centre L on the centreline of R's corner
    Lx0, Lx1 = cx - Lw / 2, cx + Lw / 2
    Ly0, Ly1 = cy - Lh / 2, cy + Lh / 2
    L = ring(Lx0, Ly0, Lx1, Ly1, t, rL)
    R = ring(Rx0, Ry0, Rx1, Ry1, t, rR)
    L = diff(L, box(Rx0 - g, Ly0 - 1, Rx0 + t + g, Ly0 + t + 1))
    R = diff(R, box(Lx1 - t - g, Ry1 - t - 1, Lx1 + g, Ry1 + 1))
    return {"window": L, "screen": R}


def symbol_A_small(t=26, rR=22, rL=18, g=12, Rw=170, Rh=104, Lw=92, Lh=84):
    """Small-size cut (<= 48 px): the weave is dropped; the window becomes a solid tile laid over
    the screen's corner with a clearance gap, which survives at 16 px where two thin rings blur."""
    R = ring(0, 0, Rw, Rh, t, rR)
    cx, cy = t / 2, Rh - t / 2
    L = box(cx - Lw / 2, cy - Lh / 2, cx + Lw / 2, cy + Lh / 2, rL)
    R = diff(R, dilate(L, g))
    return {"window": L, "screen": R}


# ======================================================================== B — Ryś
def symbol_B(variant="screen", small=False):
    """A TV head with lynx ears and tufts.

    variant "screen": bezel with the screen cut out, two eyes shown *on* the screen.
    variant "solid":  solid head, eyes as negative space.
    """
    def ear_pts(x0, y0, apex_y, base_y):
        """Outer edge exactly 75 deg, inner edge exactly 45 deg (clean angles)."""
        ax = x0 + (y0 - apex_y) / math.tan(math.radians(75))
        return [(x0, y0), (ax, apex_y), (ax + (base_y - apex_y), base_y)], ax

    if small:
        body = box(24, 92, 232, 228, 46)
        pts, ax = ear_pts(34, 132, 24, 104)
        ear = rounded(poly(pts), 10)
    else:
        body = box(28, 96, 228, 220, 42)
        pts, ax = ear_pts(36, 128, 34, 100)
        ear = rounded(poly(pts), 7)
    parts = [body, ear, mirror_x(ear)]
    if not small:
        tip, base = 18, 60                       # tuft: a narrow spike, edges at 75/105 deg
        hw = (base - tip) / math.tan(math.radians(75))
        tuft = rounded(poly([(ax - hw, base), (ax, tip), (ax + hw, base)]), 2.5)
        parts += [tuft, mirror_x(tuft)]
    head = union(*parts)
    out = {}
    if variant == "screen":
        bez = 22 if not small else 30
        scr = box(28 + bez, 96 + bez, 228 - bez, 220 - bez, 42 - bez + 4) if not small else \
            box(24 + bez, 92 + bez, 232 - bez, 228 - bez, 20)
        head = diff(head, scr)
        if small:
            eye = circle(98, 160, 15)
        else:
            eye = circle(98, 158, 12)
        out["eyes"] = union(eye, mirror_x(eye))
    else:
        eye = circle(96, 160, 14) if not small else circle(94, 164, 19)
        head = diff(head, eye, mirror_x(eye))
    out["head"] = head
    return out


# ======================================================================== C — Styk
def _chevron(vx, a, w, direction=1):
    """Chevron with 60-degree arms, vertex at (vx,128). direction=1 points right (>)."""
    e = w  # overshoot, clipped away for horizontal terminals
    dx = (a + e) / SQ3
    d = f"M{vx - direction*dx} {128 - a - e}L{vx} 128L{vx - direction*dx} {128 + a + e}"
    s = stroke(d, w, cap="butt", join="miter", miter=4)
    return inter(s, box(-1000, 128 - a, 1000, 128 + a))


def symbol_C(w=36, a=72, hx=17, clear=12, diamond=True, tip_gap=None):
    """Two opposed chevrons form an x; a rhombus (the frame being passed) sits in the gap."""
    hy = hx * SQ3
    if diamond:
        s = clear / math.sin(math.radians(60))       # horizontal clearance for a perpendicular `clear`
        tip = 128 - hx - s
    else:
        tip = 128 - (tip_gap if tip_gap is not None else w * 0.6) / 2
    vx = tip - w                                     # 60-degree miter tip sits w past the vertex
    left = _chevron(vx, a, w, 1)
    right = mirror_x(left)
    parts = {"left": left, "right": right}
    if diamond:
        parts["frame"] = poly([(128 - hx, 128), (128, 128 - hy), (128 + hx, 128), (128, 128 + hy)])
    return parts


def symbol_C_small():
    return symbol_C(w=44, a=68, hx=20, clear=15)
