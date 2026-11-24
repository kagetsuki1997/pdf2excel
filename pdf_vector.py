"""Turn a figure on a PDF page into a clean vector SVG.

The figure is read once (extract_figure) as simple shapes + text, then written by to_svg():
a flat SVG with paths in absolute coordinates, real <text> with sub/superscripts, no transforms
or clipping. Symbols the PDF stores as hidden glyphs (like >= and ->) are redrawn as shapes.

Everything is in PDF points with the origin at the top-left of the figure.
"""
import base64
import io
import math
import re
from xml.sax.saxutils import escape, quoteattr

import pymupdf
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

SIZE_DROP = 0.9    # a span is subscript/superscript if smaller than 90% of the biggest span in its line
RAISE = 0.15       # pt a span's baseline must be above the line's to count as superscript
PAGE_FRACTION = 0.9
PUA = {"": "θ", "": "Ψ", "": "°", "": "µ", "": "Ω", "": "ω"}


# ----------------------------------------------------------------------------- extraction
def _hex(rgb):
    return "#%02x%02x%02x" % tuple(round(max(0.0, min(1.0, c)) * 255) for c in rgb)


def _family(font):
    name = re.sub(r"^[A-Z]{6}\+", "", font or "")
    name = re.split(r"[-,]", name)[0]
    name = re.sub(r"(MT|PS)$", "", name)
    low = name.lower()
    if low.startswith(("arial", "helvetica")) or not name:
        return "Arial"
    if low.startswith("times"):
        return "Times New Roman"
    if low.startswith("courier"):
        return "Courier New"
    return name


def _same(a, b):
    return abs(a.x - b.x) < 0.01 and abs(a.y - b.y) < 0.01


def _subpaths(items, close):
    """pymupdf drawing items -> list of subpaths; each is [('M',x,y), ('L',x,y), ('C',x1,y1,x2,y2,x,y)...]."""
    subs, cur, end = [], None, None
    for it in items:
        kind = it[0]
        if kind in ("l", "c"):
            p1 = it[1]
            if cur is None or not _same(end, p1):
                cur = [("M", p1.x, p1.y)]
                subs.append(cur)
            if kind == "l":
                cur.append(("L", it[2].x, it[2].y))
                end = it[2]
            else:
                c1, c2, p2 = it[2], it[3], it[4]
                cur.append(("C", c1.x, c1.y, c2.x, c2.y, p2.x, p2.y))
                end = p2
        elif kind in ("re", "qu"):
            if kind == "re":
                r = it[1]
                pts = [r.tl, r.tr, r.br, r.bl]
            else:
                q = it[1]
                pts = [q.ul, q.ur, q.lr, q.ll]
            cur = [("M", pts[0].x, pts[0].y)] + [("L", p.x, p.y) for p in pts[1:]] + [("L", pts[0].x, pts[0].y)]
            subs.append(cur)
            end = pts[0]
    if close and cur and cur[-1][0] != "Z" and len(cur) > 1:
        first = cur[0]
        cur.append(("L", first[1], first[2]))
    return subs


def _shift(sub, dx, dy):
    out = []
    for seg in sub:
        out.append((seg[0], *[v + (dx if i % 2 == 0 else dy) for i, v in enumerate(seg[1:])]))
    return out


def _dash(d):
    """'[3 2] 0' -> [3.0, 2.0] (empty list = solid)."""
    m = re.match(r"\s*\[(.*?)\]", d or "")
    return [float(v) for v in m.group(1).split()] if m and m.group(1).strip() else []


class _OutlinePen(BasePen):
    """Collect a TrueType glyph outline as subpaths in page coordinates."""

    def __init__(self, glyphset, scale, ox, oy, direction=(1.0, 0.0)):
        super().__init__(glyphset)
        self.s, self.ox, self.oy = scale, ox, oy
        self.dx, self.dy = direction
        self.subs, self.cur, self.start = [], None, None

    def _pt(self, p):
        # glyph x runs along the text direction, glyph y (up) is (dy, -dx) in top-left coordinates
        x, y = p
        return (self.ox + self.s * (x * self.dx + y * self.dy),
                self.oy + self.s * (x * self.dy - y * self.dx))

    def _moveTo(self, p):
        x, y = self._pt(p)
        self.cur = [("M", x, y)]
        self.start = (x, y)
        self.subs.append(self.cur)

    def _lineTo(self, p):
        x, y = self._pt(p)
        self.cur.append(("L", x, y))

    def _curveToOne(self, p1, p2, p3):
        (a, b), (c, d), (e, f) = self._pt(p1), self._pt(p2), self._pt(p3)
        self.cur.append(("C", a, b, c, d, e, f))

    def _qCurveToOne(self, p1, p2):       # quadratic -> cubic
        p0 = self._getCurrentPoint()
        q0, q1, q2 = self._pt(p0), self._pt(p1), self._pt(p2)
        c1 = (q0[0] + 2 / 3 * (q1[0] - q0[0]), q0[1] + 2 / 3 * (q1[1] - q0[1]))
        c2 = (q2[0] + 2 / 3 * (q1[0] - q2[0]), q2[1] + 2 / 3 * (q1[1] - q2[1]))
        self.cur.append(("C", c1[0], c1[1], c2[0], c2[1], q2[0], q2[1]))

    def _closePath(self):
        if self.cur and self.start:
            self.cur.append(("L", *self.start))


def _unmapped_glyphs(page, clip):
    """Symbols (>=, ->, ...) that the PDF stores as a blank char, as filled outline shapes.
    A real space has an empty outline; contours behind a ' ' mean a hidden symbol."""
    shapes, marks, fonts = [], [], {}
    doc, pno = page.parent, page.number
    try:
        trace = page.get_texttrace()
    except Exception:
        return shapes, marks
    page_fonts = None
    for sp in trace:
        for code, gid, origin, *_ in sp["chars"]:
            if code != 32 or not clip.contains(pymupdf.Point(*origin)):
                continue
            if page_fonts is None:
                page_fonts = [f for f in doc.get_page_fonts(pno) if f[1] in ("ttf", "otf")]
            for xref, _ext, _t, basefont, *_ in page_fonts:
                if re.sub(r"^[A-Z]{6}\+", "", basefont) != sp["font"]:
                    continue
                if xref not in fonts:
                    try:
                        fonts[xref] = TTFont(io.BytesIO(doc.extract_font(xref)[3]), lazy=True)
                    except Exception:
                        fonts[xref] = None
                tt = fonts[xref]
                if tt is None:
                    continue
                order = tt.getGlyphOrder()
                if gid >= len(order) or "glyf" not in tt:
                    continue
                name = order[gid]
                if tt["glyf"][name].numberOfContours == 0:
                    continue                      # empty outline = a genuine space
                pen = _OutlinePen(tt.getGlyphSet(), sp["size"] / tt["head"].unitsPerEm,
                                  origin[0] - clip.x0, origin[1] - clip.y0, sp.get("dir") or (1.0, 0.0))
                tt.getGlyphSet()[name].draw(pen)
                adv = tt["hmtx"][name][0] / tt["head"].unitsPerEm * sp["size"]
                marks.append((origin[0], origin[1], max(1, round(adv / (0.278 * sp["size"])))))
                col = sp.get("color") or (0, 0, 0)
                shapes.append({"subpaths": [s for s in pen.subs if len(s) > 1], "fill": _hex(col),
                               "fill_opacity": 1.0, "stroke": None, "stroke_opacity": 1.0, "width": 0.5,
                               "dash": [], "even_odd": False})
                break
    return shapes, marks


def extract_figure(page, rect, pad=4):
    """Read the vector content of `rect` (plus pad) on a pymupdf page."""
    clip = (pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad) & page.rect)
    ox, oy = clip.x0, clip.y0
    page_area = page.rect.width * page.rect.height

    shapes = []
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"])
        probe = pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5)
        if not probe.intersects(clip) or r.width * r.height > PAGE_FRACTION * page_area:
            continue
        fill = d.get("fill") if d["type"] in ("f", "fs") else None
        stroke = d.get("color") if d["type"] in ("s", "fs") else None
        if fill is None and stroke is None:
            continue
        subs = [_shift(s, -ox, -oy) for s in _subpaths(d["items"], d.get("closePath"))]
        subs = [s for s in subs if len(s) > 1]
        if not subs:
            continue
        shapes.append({
            "subpaths": subs,
            "fill": _hex(fill) if fill is not None else None,
            "fill_opacity": d.get("fill_opacity") if d.get("fill_opacity") is not None else 1.0,
            "stroke": _hex(stroke) if stroke is not None else None,
            "stroke_opacity": d.get("stroke_opacity") if d.get("stroke_opacity") is not None else 1.0,
            "width": d.get("width") or 0.5,
            "dash": _dash(d.get("dashes")),
            "even_odd": bool(d.get("even_odd")),
        })

    glyph_shapes, marks = _unmapped_glyphs(page, clip)
    shapes.extend(glyph_shapes)

    texts = []
    def span_text(sp):
        out = []
        for c in sp["chars"]:
            ch = c["c"]
            if ch == " ":                       # a hidden symbol keeps its width as extra spaces
                for mx, my, n in marks:
                    if abs(mx - c["origin"][0]) < 0.6 and abs(my - c["origin"][1]) < 0.6:
                        ch = " " * n
                        break
            out.append(ch)
        return "".join(out)

    for blk in page.get_text("rawdict", clip=clip)["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                sp["text"] = span_text(sp)
            spans = [s for s in ln["spans"] if s["text"] != ""]
            if not spans or not "".join(s["text"] for s in spans).strip():
                continue
            dx, dy = ln["dir"]
            n = math.hypot(dx, dy) or 1.0
            dx, dy = dx / n, dy / n
            big = max(s["size"] for s in spans)
            first = next(s for s in spans if s["size"] >= big - 0.05)
            nx, ny = -dy, dx                      # unit normal, "downwards" in the text's own frame
            p0 = first["origin"]
            runs = []
            for s in spans:
                v = (s["origin"][0] - p0[0]) * nx + (s["origin"][1] - p0[1]) * ny   # baseline offset
                kind = ""
                if s["size"] < big * SIZE_DROP and s["text"].strip():
                    kind = "sup" if v < -RAISE else "sub"
                runs.append({
                    "text": "".join(PUA.get(c, c) for c in s["text"]),
                    "size": s["size"], "color": _hex(((s["color"] >> 16 & 255) / 255,
                                                      (s["color"] >> 8 & 255) / 255, (s["color"] & 255) / 255)),
                    "family": _family(s["font"]), "bold": bool(s["flags"] & 16), "italic": bool(s["flags"] & 2),
                    "kind": kind, "v": v,
                    "origin": (s["origin"][0] - ox, s["origin"][1] - oy),
                })
            b = pymupdf.Rect(ln["bbox"])
            texts.append({"runs": runs, "dir": (dx, dy), "size": big,
                          "bbox": (b.x0 - ox, b.y0 - oy, b.x1 - ox, b.y1 - oy)})

    images = []
    for info in page.get_image_info():
        b = pymupdf.Rect(info["bbox"])
        if b.width >= 20 and b.height >= 20 and b.intersects(clip) and b.width * b.height < PAGE_FRACTION * page_area:
            png = page.get_pixmap(clip=b & clip, dpi=150).tobytes("png")
            c = b & clip
            images.append({"bbox": (c.x0 - ox, c.y0 - oy, c.x1 - ox, c.y1 - oy), "png": png})

    return {"width": clip.width, "height": clip.height, "shapes": shapes, "texts": texts, "images": images}


# ----------------------------------------------------------------------------- SVG
def _n(v):
    s = ("%.3f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _path_d(subpaths):
    parts = []
    for sub in subpaths:
        for seg in sub:
            parts.append(seg[0] + " ".join(_n(v) for v in seg[1:]))
    return " ".join(parts)


def to_svg(fig):
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" '
           f'width="{_n(fig["width"])}" height="{_n(fig["height"])}" viewBox="0 0 {_n(fig["width"])} {_n(fig["height"])}">']
    for im in fig["images"]:
        x0, y0, x1, y1 = im["bbox"]
        data = base64.b64encode(im["png"]).decode()
        out.append(f'<image x="{_n(x0)}" y="{_n(y0)}" width="{_n(x1 - x0)}" height="{_n(y1 - y0)}" '
                   f'xlink:href="data:image/png;base64,{data}"/>')
    for sh in fig["shapes"]:
        a = [f'd="{_path_d(sh["subpaths"])}"']
        a.append(f'fill="{sh["fill"]}"' if sh["fill"] else 'fill="none"')
        if sh["fill"]:
            if sh["fill_opacity"] < 1:
                a.append(f'fill-opacity="{_n(sh["fill_opacity"])}"')
            if sh["even_odd"]:
                a.append('fill-rule="evenodd"')
        if sh["stroke"]:
            a.append(f'stroke="{sh["stroke"]}" stroke-width="{_n(sh["width"])}" stroke-linecap="round" stroke-linejoin="round"')
            if sh["stroke_opacity"] < 1:
                a.append(f'stroke-opacity="{_n(sh["stroke_opacity"])}"')
            if sh["dash"]:
                a.append(f'stroke-dasharray="{" ".join(_n(v) for v in sh["dash"])}"')
        out.append(f'<path {" ".join(a)}/>')
    for t in fig["texts"]:
        dx, dy = t["dir"]
        runs = t["runs"]
        x0, y0 = runs[0]["origin"]
        angle = math.degrees(math.atan2(dy, dx))
        tr = f' transform="rotate({_n(angle)} {_n(x0)} {_n(y0)})"' if abs(angle) > 0.01 else ""
        spans, prev_v = [], runs[0]["v"]
        for r in runs:
            at = f' dy="{_n(r["v"] - prev_v)}"' if abs(r["v"] - prev_v) > 0.01 else ""
            prev_v = r["v"]
            w = ' font-weight="bold"' if r["bold"] else ""
            i = ' font-style="italic"' if r["italic"] else ""
            spans.append(f'<tspan{at} font-size="{_n(r["size"])}" fill="{r["color"]}"{w}{i}>{escape(r["text"])}</tspan>')
        out.append(f'<text x="{_n(x0)}" y="{_n(y0)}" font-family={quoteattr(runs[0]["family"] + ", sans-serif")}'
                   f' xml:space="preserve"{tr}>{"".join(spans)}</text>')
    out.append("</svg>")
    return "\n".join(out)
