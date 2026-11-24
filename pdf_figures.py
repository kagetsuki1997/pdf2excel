"""Find figures (diagrams, charts, pictures) on PDF pages and render them to PNG.

A figure is a cluster of vector drawing items (lines, curves, boxes) or a raster
image that is not part of a table and not in the page header/footer. Short text
labels around the drawing and a "Figure ..." caption below it are included.
"""
import pymupdf

HEADER = 0.10        # top 10% of the page is header
FOOTER = 0.11        # bottom 11% of the page is footer
GAP = 12             # pt: drawing items closer than this belong to the same figure
MIN_ITEMS = 6        # a figure has at least this many drawing items
MIN_W, MIN_H = 50, 60
LABEL_MARGIN = 22    # pt around the drawing in which text labels are pulled in
CAPTION_REACH = 50   # pt below the drawing in which a "Figure ..." caption is looked for
MAX_LABEL_W = 0.6    # text lines wider than this fraction of the page are body text
MAX_LABEL_SIZE = 12  # text with a bigger font is a heading, not a label
MAX_LABEL_CHARS = 32 # longer text lines near a drawing are body text, not labels


def _inflate(r, d):
    return pymupdf.Rect(r.x0 - d, r.y0 - d, r.x1 + d, r.y1 + d)


def _clusters(rects, organic, gap):
    """Group rectangles that touch when inflated by gap; returns [(bbox, count, n_organic)]."""
    order = sorted(range(len(rects)), key=lambda i: rects[i].x0)
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    infl = [_inflate(r, gap / 2) for r in rects]
    for a, i in enumerate(order):
        for j in order[a + 1:]:
            if infl[j].x0 > infl[i].x1:
                break
            if infl[i].intersects(infl[j]):
                parent[find(j)] = find(i)
    groups = {}
    for i in range(len(rects)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for g in groups.values():
        box = pymupdf.Rect(rects[g[0]])
        for i in g[1:]:
            box |= rects[i]
        out.append((box, len(g), sum(organic[i] for i in g)))
    return out


def _is_organic(drawing):
    """True if the drawing has a curve or a diagonal line (tables and grids never do)."""
    for it in drawing["items"]:
        if it[0] == "c":
            return True
        if it[0] == "l" and abs(it[1].x - it[2].x) > 1 and abs(it[1].y - it[2].y) > 1:
            return True
    return False


def detect_figures(page):
    """Return a list of pymupdf.Rect, one per figure found on the page (top to bottom)."""
    W, H = page.rect.width, page.rect.height
    top, bottom = H * HEADER, H * (1 - FOOTER)
    try:
        tables = [_inflate(pymupdf.Rect(t.bbox), 2) for t in page.find_tables().tables]
    except Exception:
        tables = []

    def usable(r):
        return not (r.y1 < top or r.y0 > bottom) and not any(r.intersects(t) for t in tables)

    items, organic = [], []
    for d in page.get_drawings():
        r = _inflate(pymupdf.Rect(d["rect"]), 0.5)
        if usable(r):
            items.append(r)
            organic.append(_is_organic(d))
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if r.width >= 30 and r.height >= 30 and usable(r):
            items.extend([r] * MIN_ITEMS)         # one picture counts as a figure on its own
            organic.extend([True] * MIN_ITEMS)

    # a cluster made only of horizontal/vertical lines and boxes is a table/grid, not a figure
    boxes = [b for b, n, o in _clusters(items, organic, GAP)
             if n >= MIN_ITEMS and o > 0 and b.width >= MIN_W and b.height >= MIN_H]
    if not boxes:
        return []

    lines = []   # text lines outside header/footer/tables: (bbox, text, max font size)
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            r = pymupdf.Rect(ln["bbox"])
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            size = max((s["size"] for s in ln["spans"]), default=0)
            if txt and usable(r):
                lines.append((r, txt, size))

    figs = []
    for box in boxes:
        fig = pymupdf.Rect(box)
        zone = _inflate(box, LABEL_MARGIN)
        for r, txt, size in lines:
            inside = box.contains(r)
            label = (r.intersects(zone) and r.width < MAX_LABEL_W * W and size < MAX_LABEL_SIZE
                     and len(txt) <= MAX_LABEL_CHARS)
            caption = (txt.lower().startswith(("figure", "fig.")) and box.y1 <= r.y0 <= box.y1 + CAPTION_REACH
                       and r.x0 < box.x1 and r.x1 > box.x0)
            if inside or label or caption:
                fig |= r
        figs.append(fig)

    merged = []   # merge figures that ended up overlapping
    for f in sorted(figs, key=lambda r: (r.y0, r.x0)):
        for m in merged:
            if f.intersects(m):
                m |= f
                break
        else:
            merged.append(pymupdf.Rect(f))
    return sorted(merged, key=lambda r: (r.y0, r.x0))


def render_png(page, rect, dpi=200, pad=4):
    """Render a region of the page; returns (png_bytes, width_pt, height_pt)."""
    clip = _inflate(rect, pad) & page.rect
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix.tobytes("png"), clip.width, clip.height
