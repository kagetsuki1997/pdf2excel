"""Turn a figure on a PDF page into clean vector output: a plain SVG and a Visio .vsdx.

The figure is read once (extract_figure) as simple shapes + text, then written as
  * to_svg(fig)        - flat SVG: paths with absolute coordinates, real <text>, no transforms/clipping
  * VsdxWriter         - one .vsdx file, one Visio page per figure, every line/curve a Visio shape,
                         every text line a Visio text shape (subscripts/superscripts kept)

Everything is in PDF points with the origin at the top-left of the figure.
"""
import base64
import io
import math
import re
import zipfile
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


def _unmapped_glyphs(page, clip, all_text=False):
    """Outlines of glyphs as filled shapes. By default only symbols (>=, ->, ...) that the PDF stores
    as a blank char (a real space has an empty outline; contours behind a ' ' mean a hidden symbol).
    With all_text=True every character becomes an outline (text that needs no font at all)."""
    shapes, marks, fonts = [], [], {}
    doc, pno = page.parent, page.number
    try:
        trace = page.get_texttrace()
    except Exception:
        return shapes, marks
    page_fonts = None
    for sp in trace:
        for code, gid, origin, *_ in sp["chars"]:
            if (code != 32 and not all_text) or not clip.contains(pymupdf.Point(*origin)):
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
                if code == 32:
                    adv = tt["hmtx"][name][0] / tt["head"].unitsPerEm * sp["size"]
                    marks.append((origin[0], origin[1], max(1, round(adv / (0.278 * sp["size"])))))
                col = sp.get("color") or (0, 0, 0)
                shapes.append({"subpaths": [s for s in pen.subs if len(s) > 1], "fill": _hex(col),
                               "fill_opacity": 1.0, "stroke": None, "stroke_opacity": 1.0, "width": 0.5,
                               "dash": [], "even_odd": False})
                break
    return shapes, marks


def extract_figure(page, rect, pad=4, text_outlines=False):
    """Read the vector content of `rect` (plus pad) on a pymupdf page.
    text_outlines=True turns all text into outline shapes (no text objects)."""
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

    glyph_shapes, marks = _unmapped_glyphs(page, clip, all_text=text_outlines)
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

    for blk in ([] if text_outlines else page.get_text("rawdict", clip=clip)["blocks"]):
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


# ----------------------------------------------------------------------------- Visio .vsdx
NS = "http://schemas.microsoft.com/office/visio/2012/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MARGIN = 0.3   # inches around the figure on its Visio page


def _v(x):
    return "%.6f" % x


def _cell(name, value, unit=None):
    u = f' U="{unit}"' if unit else ""
    return f'<Cell N="{name}" V="{value}"{u}/>'


def _flatten(sub):
    """Replace cubic Beziers by short straight segments: ['M',x,y] / ['L',x,y] only."""
    out, cur = [], None
    for seg in sub:
        if seg[0] == "C" and cur is not None:
            x0, y0 = cur
            x1, y1, x2, y2, x3, y3 = seg[1:]
            length = (math.hypot(x1 - x0, y1 - y0) + math.hypot(x2 - x1, y2 - y1) + math.hypot(x3 - x2, y3 - y2))
            n = max(6, min(30, int(length / 2.5) + 4))
            for i in range(1, n + 1):
                t = i / n
                u = 1 - t
                out.append(("L", u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                            u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
            cur = (x3, y3)
        else:
            out.append(seg)
            cur = (seg[1], seg[2]) if seg[0] in ("M", "L") else (seg[5], seg[6])
    return out


def _dash_pieces(points, pattern):
    """Split a polyline [(x, y)...] into the 'on' pieces of a dash pattern [on, off, on, off...]."""
    pat = [max(p, 0.05) for p in pattern]
    if len(pat) % 2:
        pat = pat * 2
    pieces, cur, idx, left = [], [points[0]], 0, pat[0]
    for (xa, ya), (xb, yb) in zip(points, points[1:]):
        seg = math.hypot(xb - xa, yb - ya)
        pos = 0.0
        while seg - pos > 1e-9:
            step = min(left, seg - pos)
            pos += step
            left -= step
            px, py = xa + (xb - xa) * pos / seg, ya + (yb - ya) * pos / seg
            if idx % 2 == 0:
                cur.append((px, py))
            if left <= 1e-9:
                if idx % 2 == 0:
                    pieces.append(cur)
                idx += 1
                left = pat[idx % len(pat)]
                cur = [(px, py)]
    if idx % 2 == 0 and len(cur) > 1:
        pieces.append(cur)
    return [p for p in pieces if len(p) > 1]


class VsdxWriter:
    """Collects figures (as pages) and writes a single .vsdx file.

    flatten=True     curves become straight segments (viewers that ignore Bezier rows still draw them)
    native_dash=False dashed lines become explicit short solid segments (instead of a Visio line pattern)
    """

    def __init__(self, flatten=True, native_dash=False):
        self.pages = []          # (name, page_xml, width_in, height_in)
        self.fonts = ["Arial"]
        self.flatten, self.native_dash = flatten, native_dash

    def _prepare(self, shapes):
        out = []
        for sh in shapes:
            subs = [_flatten(sub) for sub in sh["subpaths"]] if self.flatten else sh["subpaths"]
            sh = dict(sh, subpaths=subs)
            if sh["dash"] and sh["stroke"] and not self.native_dash:
                if sh["fill"]:
                    out.append(dict(sh, stroke=None, dash=[]))
                pieces = []
                for sub in subs:
                    pts = [(seg[1], seg[2]) for seg in sub if seg[0] in ("M", "L")]
                    if len(pts) > 1 and len(pts) == len(sub):
                        for piece in _dash_pieces(pts, sh["dash"]):
                            pieces.append([("M", *piece[0])] + [("L", *p) for p in piece[1:]])
                    else:
                        pieces.append(sub)           # contains curves we could not flatten: keep solid
                out.append(dict(sh, fill=None, dash=[], subpaths=pieces))
            else:
                out.append(sh)
        return out

    def _font_id(self, family):
        if family not in self.fonts:
            self.fonts.append(family)
        return self.fonts.index(family)

    def add_figure(self, name, fig):
        H = fig["height"]
        W_in = fig["width"] / 72 + 2 * MARGIN
        H_in = H / 72 + 2 * MARGIN

        def px(x):
            return MARGIN + x / 72

        def py(y):
            return MARGIN + (H - y) / 72

        shapes, sid = [], 0
        for sh in self._prepare(fig["shapes"]):
            rows, pts = [], []
            ix = 0
            for sub in sh["subpaths"]:
                for seg in sub:
                    c = [(px(seg[i]), py(seg[i + 1])) for i in range(1, len(seg), 2)]
                    pts.extend(c)
            if not pts:
                continue
            minx, miny = min(p[0] for p in pts), min(p[1] for p in pts)
            w, h = max(max(p[0] for p in pts) - minx, 1e-4), max(max(p[1] for p in pts) - miny, 1e-4)
            for sub in sh["subpaths"]:
                for seg in sub:
                    c = [(px(seg[i]) - minx, py(seg[i + 1]) - miny) for i in range(1, len(seg), 2)]
                    ix += 1
                    if seg[0] == "M":
                        rows.append(f'<Row T="MoveTo" IX="{ix}">{_cell("X", _v(c[0][0]))}{_cell("Y", _v(c[0][1]))}</Row>')
                    elif seg[0] == "L":
                        rows.append(f'<Row T="LineTo" IX="{ix}">{_cell("X", _v(c[0][0]))}{_cell("Y", _v(c[0][1]))}</Row>')
                    else:   # cubic Bezier: end point, then the two control points
                        rows.append(f'<Row T="CubicBezTo" IX="{ix}">{_cell("X", _v(c[2][0]))}{_cell("Y", _v(c[2][1]))}'
                                    f'{_cell("A", _v(c[0][0]))}{_cell("B", _v(c[0][1]))}'
                                    f'{_cell("C", _v(c[1][0]))}{_cell("D", _v(c[1][1]))}</Row>')
            sid += 1
            dash = sh["dash"]
            pattern = 1 if not dash else (3 if dash[0] < 1.0 else 2)
            cells = [_cell("PinX", _v(minx)), _cell("PinY", _v(miny)), _cell("Width", _v(w)), _cell("Height", _v(h)),
                     _cell("LocPinX", "0"), _cell("LocPinY", "0"), _cell("Angle", "0")]
            if sh["stroke"]:
                cells += [_cell("LineWeight", _v(sh["width"] / 72)), _cell("LineColor", sh["stroke"]),
                          _cell("LinePattern", pattern)]
            else:
                cells += [_cell("LinePattern", "0")]
            if sh["fill"]:
                cells += [_cell("FillForegnd", sh["fill"]), _cell("FillPattern", "1")]
            else:
                cells += [_cell("FillPattern", "0")]
            geom = (f'<Section N="Geometry" IX="0">{_cell("NoFill", 0 if sh["fill"] else 1)}'
                    f'{_cell("NoLine", 0 if sh["stroke"] else 1)}{_cell("NoShow", 0)}{_cell("NoSnap", 0)}'
                    f'{_cell("NoQuickDrag", 0)}{"".join(rows)}</Section>')
            shapes.append(f'<Shape ID="{sid}" Type="Shape" LineStyle="0" FillStyle="0" TextStyle="0">'
                          f'{"".join(cells)}{geom}</Shape>')

        for t in fig["texts"]:
            sid += 1
            dx, dy = t["dir"]
            x0, y0, x1, y1 = t["bbox"]
            bw, bh = x1 - x0, y1 - y0
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            length = abs(bw * dx) + abs(bh * dy)
            thick = abs(bw * dy) + abs(bh * dx)
            sx, sy = cx - dx * length / 2, cy - dy * length / 2        # where the text starts
            width_in = length / 72 * 1.15 + 0.04                       # a little slack so Visio never wraps
            height_in = max(thick / 72, 0.05)
            angle = math.atan2(-dy, dx)                                # Visio: counter-clockwise, y up
            text, chars = [], []
            for i, r in enumerate(t["runs"]):
                text.append(f'<cp IX="{i}"/>{escape(r["text"])}')
                style = (1 if r["bold"] else 0) | (2 if r["italic"] else 0)
                size = t["size"] if r["kind"] else r["size"]
                pos = {"": 0, "sup": 1, "sub": 2}[r["kind"]]
                chars.append(f'<Row IX="{i}">{_cell("Font", self._font_id(r["family"]))}{_cell("Color", r["color"])}'
                             f'{_cell("Style", style)}{_cell("Pos", pos)}{_cell("Size", _v(size / 72))}</Row>')
            cells = [_cell("PinX", _v(px(sx))), _cell("PinY", _v(py(sy))), _cell("Width", _v(width_in)),
                     _cell("Height", _v(height_in)), _cell("LocPinX", "0"), _cell("LocPinY", _v(height_in / 2)),
                     _cell("Angle", _v(angle)), _cell("LeftMargin", "0"), _cell("RightMargin", "0"),
                     _cell("TopMargin", "0"), _cell("BottomMargin", "0"), _cell("VerticalAlign", "1"),
                     _cell("FillPattern", "0"), _cell("LinePattern", "0")]
            geom = (f'<Section N="Geometry" IX="0">{_cell("NoFill", 1)}{_cell("NoLine", 1)}{_cell("NoShow", 0)}'
                    f'{_cell("NoSnap", 0)}{_cell("NoQuickDrag", 0)}'
                    f'<Row T="MoveTo" IX="1">{_cell("X", "0")}{_cell("Y", "0")}</Row>'
                    f'<Row T="LineTo" IX="2">{_cell("X", _v(width_in))}{_cell("Y", "0")}</Row>'
                    f'<Row T="LineTo" IX="3">{_cell("X", _v(width_in))}{_cell("Y", _v(height_in))}</Row>'
                    f'<Row T="LineTo" IX="4">{_cell("X", "0")}{_cell("Y", _v(height_in))}</Row>'
                    f'<Row T="LineTo" IX="5">{_cell("X", "0")}{_cell("Y", "0")}</Row></Section>')
            para = f'<Section N="Paragraph"><Row IX="0">{_cell("HorzAlign", "0")}</Row></Section>'
            char = f'<Section N="Character">{"".join(chars)}</Section>'
            shapes.append(f'<Shape ID="{sid}" Type="Shape" LineStyle="0" FillStyle="0" TextStyle="0">'
                          f'{"".join(cells)}{para}{char}{geom}<Text><pp IX="0"/>{"".join(text)}</Text></Shape>')

        xml = (f'<?xml version="1.0" encoding="utf-8"?>\n<PageContents xmlns="{NS}" xmlns:r="{NS_R}" '
               f'xml:space="preserve"><Shapes>{"".join(shapes)}</Shapes></PageContents>')
        self.pages.append((name, xml, W_in, H_in, len(fig["images"])))

    def save(self, path):
        n = len(self.pages)
        ct = ['<?xml version="1.0" encoding="utf-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>'
              '<Override PartName="/visio/pages/pages.xml" ContentType="application/vnd.ms-visio.pages+xml"/>']
        ct += [f'<Override PartName="/visio/pages/page{i}.xml" ContentType="application/vnd.ms-visio.page+xml"/>'
               for i in range(1, n + 1)]
        ct.append("</Types>")

        rels = ('<?xml version="1.0" encoding="utf-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/document" Target="visio/document.xml"/>'
                '</Relationships>')
        doc_rels = ('<?xml version="1.0" encoding="utf-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/pages" Target="pages/pages.xml"/>'
                    '</Relationships>')
        faces = "".join(f'<FaceName ID="{i}" NameU={quoteattr(f)} UnicodeRanges="-536870145 1073786111 1 0" '
                        f'CharSets="1073742335 -65536" Panose="2 11 6 4 2 2 2 2 2 4" Flags="325"/>'
                        for i, f in enumerate(self.fonts))
        style = ('<StyleSheets><StyleSheet ID="0" NameU="No Style" IsCustomName="1" Name="No Style">'
                 + _cell("LineWeight", "0.01041666666666667") + _cell("LineColor", "#000000") + _cell("LinePattern", "1")
                 + _cell("FillForegnd", "#ffffff") + _cell("FillBkgnd", "#000000") + _cell("FillPattern", "1")
                 + _cell("LeftMargin", "0.1") + _cell("RightMargin", "0.1") + _cell("TopMargin", "0.05")
                 + _cell("BottomMargin", "0.05") + _cell("VerticalAlign", "1")
                 + '<Section N="Character"><Row IX="0">' + _cell("Font", "0") + _cell("Color", "#000000")
                 + _cell("Size", "0.1111111111111111") + '</Row></Section>'
                 + '<Section N="Paragraph"><Row IX="0">' + _cell("HorzAlign", "1") + '</Row></Section>'
                 + '</StyleSheet></StyleSheets>')
        document = (f'<?xml version="1.0" encoding="utf-8"?>\n<VisioDocument xmlns="{NS}" xmlns:r="{NS_R}" xml:space="preserve">'
                    '<DocumentSettings TopPage="0" DefaultTextStyle="0" DefaultLineStyle="0" DefaultFillStyle="0"/>'
                    f'<FaceNames>{faces}</FaceNames>{style}</VisioDocument>')
        page_entries, page_rels = [], []
        for i, (name, _xml, w, h, _imgs) in enumerate(self.pages, 1):
            sheet = ("<PageSheet LineStyle=\"0\" FillStyle=\"0\" TextStyle=\"0\">"
                     + _cell("PageWidth", _v(w)) + _cell("PageHeight", _v(h)) + _cell("PageScale", "1", "IN")
                     + _cell("DrawingScale", "1", "IN") + _cell("DrawingSizeType", "3") + _cell("DrawingScaleType", "0")
                     + _cell("PageLockReplace", "0") + "</PageSheet>")
            page_entries.append(f'<Page ID="{i - 1}" NameU={quoteattr(name)} Name={quoteattr(name)}>{sheet}'
                                f'<Rel r:id="rId{i}"/></Page>')
            page_rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.microsoft.com/visio/2010/relationships/page" '
                             f'Target="page{i}.xml"/>')
        pages = (f'<?xml version="1.0" encoding="utf-8"?>\n<Pages xmlns="{NS}" xmlns:r="{NS_R}" xml:space="preserve">'
                 f'{"".join(page_entries)}</Pages>')
        pages_rels = ('<?xml version="1.0" encoding="utf-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      f'{"".join(page_rels)}</Relationships>')

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "".join(ct))
            z.writestr("_rels/.rels", rels)
            z.writestr("visio/document.xml", document)
            z.writestr("visio/_rels/document.xml.rels", doc_rels)
            z.writestr("visio/pages/pages.xml", pages)
            z.writestr("visio/pages/_rels/pages.xml.rels", pages_rels)
            for i, (_n_, xml, *_rest) in enumerate(self.pages, 1):
                z.writestr(f"visio/pages/page{i}.xml", xml)
