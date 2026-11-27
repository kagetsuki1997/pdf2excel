# /// script
# requires-python = ">=3.9"
# dependencies = ["pdfplumber", "xlsxwriter", "pymupdf", "fonttools"]
# ///
"""PDF tables -> Excel, keeping subscripts/superscripts as Excel rich text and merged cells merged.
Optionally also exports figures (diagrams, charts) as pictures, one sheet per page, plus, next to
the Excel file, a folder named {pdf filename}_svg with a clean SVG and an _outlined.svg
(text as outlines) per figure.

usage: uv run pdf_tables_to_excel.py in.pdf out.xlsx [first] [last] [--figures [PAGES]]
  no pages      -> all pages
  first         -> first page to the end
  first last    -> that range (1-based, inclusive)
  none last     -> page 1 to last
  --figures           also export figures found on the pages in the range
  --figures=5,8,31    export figures only from these pages (ranges like 10-12 work too)

The heavy libraries (pdfplumber, pymupdf, ...) are imported only when a conversion runs, so
importing this module (and starting the GUI) is instant.
"""
import argparse
import io
import math
import os
import re
import sys

import watermark
from symbolmap import fix_char          # Symbol-font private-use characters -> real symbols (<, <=, >=, ...)
SIZE_DROP = 0.9     # a char is "script" if its size < 90% of the largest size on its line
SHIFT = 0.15        # pt a smaller char's baseline must be raised/lowered to count as super/subscript
WORD_GAP = 0.15     # gap (x font size) between two letters that means a word break (letters inside a
                    # word sit within ~0.03 x size of each other, word gaps are ~0.25 x size or more)
LINE_SHIFT = 0.7    # baselines further apart than ~0.6 x font size are different lines
MIN_CURVE = 20      # pt: curves smaller than this are never table borders
MIN_FILL = 0.6      # a table must have text in at least this share of its cells (else: diagram boxes)
MAX_COL = 100      # widest Excel column (characters); wide enough that a PDF line is not re-wrapped


def parse_pages(text):
    """'5, 8, 10-12' -> sorted list of 1-based page numbers (raises ValueError on junk)."""
    pages = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        pages.update(range(min(lo, hi), max(lo, hi) + 1))
    return sorted(p for p in pages if p >= 1)


def count_pages(pdf):
    import pymupdf
    with pymupdf.open(pdf) as d:
        return len(d)


def _frame(c):
    """(along, across): the char's origin measured along its writing direction and across it."""
    a, b, e, f = c["matrix"][0], c["matrix"][1], c["matrix"][4], c["matrix"][5]
    if c["upright"] and abs(b) < 1e-6 and a > 0:       # normal text: use the glyph's centre, so a
        return (c["x0"] + c["x1"]) / 2, f              # space that overlaps its neighbour stays in order
    n = math.hypot(a, b) or 1.0
    a, b = a / n, b / n
    return e * a + f * b, f * a - e * b


def split_lines(chars):
    """Order a cell's characters as a reader would see them: lines top to bottom, characters
    left to right. The order in which a PDF writes its characters is not trusted (some generators
    write lines bottom-up or words out of order), so this works from positions only.

    Lines are found by clustering baselines; the gap needed to start a new line is larger than any
    subscript/superscript offset but smaller than a line pitch. Rotated text is handled in its own
    frame (a vertical label reads along its own direction)."""
    groups = {}
    for c in chars:
        angle = round(math.degrees(math.atan2(c["matrix"][1], c["matrix"][0])) / 5) * 5
        groups.setdefault(angle, []).append(c)
    lines = []
    for angle in sorted(groups, key=lambda x: (x != 0, x)):         # upright text first
        cs = groups[angle]
        letters = sorted(c["size"] for c in cs if c["text"].strip()) or [c["size"] for c in cs]
        gap = max(2.0, LINE_SHIFT * letters[len(letters) // 2] * 0.85)
        pos = sorted(((*_frame(c)[::-1], c) for c in cs), key=lambda t: -t[0])    # across, descending
        cluster, last = [], None
        for across, along, c in pos:
            if last is not None and last - across > gap:
                lines.append([x[1] for x in sorted(cluster, key=lambda t: t[0])])
                cluster = []
            cluster.append((along, c))
            last = across
        if cluster:
            lines.append([x[1] for x in sorted(cluster, key=lambda t: t[0])])
    return lines


def _is_watermark(c):
    """Large faded or diagonal text (a watermark behind the table) is not table content."""
    return watermark.is_watermark_text(c["size"], watermark.lightness(c.get("non_stroking_color")),
                                       c["matrix"][0], c["matrix"][1])


def cell_chars(page, bbox):
    """The cell's characters: those whose centre lies inside it. (page.crop would also take the
    edge of a character in the next cell that pokes slightly over the border.)"""
    x0, top, x1, bottom = bbox
    return [c for c in page.chars
            if x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom
            and c["text"] != "\n" and not _is_watermark(c)]


def cell_runs(page, bbox):
    """Return [(text, kind)] runs; kind is '', 'sub' or 'sup'. Every line of the cell in the PDF
    stays its own line ('\\n' between them), so the Excel cell reads like the PDF."""
    lines = [ln for ln in split_lines(cell_chars(page, bbox)) if any(c["text"].strip() for c in ln)]
    runs = []
    for i, line in enumerate(lines):
        letters = [c for c in line if c["text"].strip()]
        if i:
            runs.append(("\n", ""))
        big = max(c["size"] for c in letters)
        ref_base = next(c["matrix"][5] for c in letters if c["size"] >= big - 0.05)
        prev = None
        for c in line:
            # PDFs without real space characters separate words only by a gap: restore the space
            if (prev is not None and c["text"].strip() and prev["text"].strip() and c["upright"] and prev["upright"]
                    and c["x0"] - prev["x1"] > WORD_GAP * max(c["size"], prev["size"])):
                runs.append((" ", ""))
            prev = c if c["text"].strip() else None
            kind = ""
            if c["text"].strip() and c["size"] < big * SIZE_DROP:
                # PDF y grows upward. Smaller AND raised = superscript, smaller AND lowered = subscript;
                # smaller on the same baseline is small caps / small print: leave it normal.
                shift = c["matrix"][5] - ref_base
                kind = "sup" if shift > SHIFT else "sub" if shift < -SHIFT else ""
            runs.append((fix_char(c["text"]), kind))
    merged = []
    for t, k in runs:
        if merged and merged[-1][1] == k:
            merged[-1] = (merged[-1][0] + t, k)
        else:
            merged.append((t, k))
    return merged


def clean_runs(runs):
    """Collapse repeated spaces, drop spaces next to a line break and at the ends; keep '\\n'."""
    runs = [[re.sub(r"[ \t]+", " ", t), k] for t, k in runs if t]
    for i in range(len(runs)):
        runs[i][0] = re.sub(r" ?\n ?", "\n", runs[i][0])
        if i and runs[i - 1][0].endswith((" ", "\n")):
            runs[i][0] = runs[i][0].lstrip(" ")
        if i + 1 < len(runs) and runs[i + 1][0].startswith("\n"):
            runs[i][0] = runs[i][0].rstrip(" ")
    runs = [r for r in runs if r[0]]
    if runs:
        runs[0][0] = runs[0][0].lstrip()
        runs[-1][0] = runs[-1][0].rstrip()
    return [r for r in runs if r[0]]


def cell_text(runs):
    return "".join(t for t, _ in clean_runs(runs))


def write_cell(ws, wb, row, col, runs, fmt):
    runs = clean_runs(runs)
    if not runs:
        return
    if all(k == "" for _, k in runs):
        ws.write_string(row, col, "".join(t for t, _ in runs), fmt)
        return
    parts = []
    for t, k in runs:
        if k == "":
            parts.append(t)
        else:
            parts.extend([wb.add_format({"font_script": 2 if k == "sub" else 1}), t])
    if len(parts) == 2:   # one single formatted run: rich string needs 2+ fragments
        parts = ["", *parts]
    ws.write_rich_string(row, col, *parts, fmt)


def _boundaries(values, tol=1.5):
    """Sorted coordinates with values closer than tol merged into one boundary."""
    out = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(g) / len(g) for g in out]


def _index(v, bounds):
    return min(range(len(bounds)), key=lambda i: abs(bounds[i] - v))


def _grid_line(obj):
    """False for drawings that cannot be table borders: curves with slanted segments or smaller
    than MIN_CURVE (logos, icons, glyph outlines). pdfplumber snaps nearby vertical edges of the
    whole page together, so such shapes could drag a real column border sideways and lose cells."""
    if obj.get("object_type") != "curve":
        return True
    if max(obj["width"], obj["height"]) < MIN_CURVE:
        return False
    pts = obj.get("pts") or []
    return all(abs(a[0] - b[0]) < 0.5 or abs(a[1] - b[1]) < 0.5 for a, b in zip(pts, pts[1:]))


def find_tables(page):
    return page.filter(_grid_line).find_tables()


def is_table(grid, table):
    """A real table has text in most of its cells. Boxes in a circuit or block diagram (a labelled
    box, a dashed frame, a package outline) are found as 'tables' too, but are mostly empty."""
    filled = sum(1 for cells in grid for runs in cells if cell_text(runs))
    return filled >= 2 and filled >= MIN_FILL * len(table.cells)


def table_grid(page, table):
    """Return (grid, merges). grid[r][c] = runs of the cell starting there ([] elsewhere);
    merges = [(r0, c0, r1, c1)] for cells that span several rows/columns (merged cells)."""
    cells = list(table.cells)
    xs = _boundaries([c[0] for c in cells] + [c[2] for c in cells])
    ys = _boundaries([c[1] for c in cells] + [c[3] for c in cells])
    grid = [[[] for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
    merges = []
    for cb in cells:
        c0, c1 = _index(cb[0], xs), _index(cb[2], xs) - 1
        r0, r1 = _index(cb[1], ys), _index(cb[3], ys) - 1
        if c1 < c0 or r1 < r0:
            continue
        grid[r0][c0] = cell_runs(page, cb)
        if c1 > c0 or r1 > r0:
            merges.append((r0, c0, r1, c1))
    return grid, merges


def save_svg(page, rect, path, pad=4):
    """Write one figure as SVG (text converted to outlines so symbols and fonts stay exact)."""
    import pdf_figures
    original = page.cropbox
    try:
        page.set_cropbox(pdf_figures._inflate(rect, pad) & page.rect)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page.get_svg_image(text_as_path=True))
    finally:
        page.set_cropbox(original)


def write_figures(wb, pdf, pages, manual, svg_dir, dpi=200):
    """One sheet per page that has figures. Also writes, per figure, a clean SVG and an
    outlined SVG into svg_dir. Returns {page: number_of_figures}."""
    import pymupdf
    import pdf_figures
    import pdf_vector
    found = {}
    bold = wb.add_format({"bold": True})
    with pymupdf.open(pdf) as doc:
        for n in pages:
            page = doc[n - 1]
            rects = pdf_figures.detect_figures(page)
            if not rects and manual:           # user named this page: export its whole content area
                H = page.rect.height
                rects = [pymupdf.Rect(page.rect.x0, H * pdf_figures.HEADER, page.rect.x1,
                                      H * (1 - pdf_figures.FOOTER))]
            if not rects:
                continue
            ws = wb.add_worksheet(f"Fig p{n}")
            row = 0
            for k, r in enumerate(rects, 1):
                clean = pdf_vector.extract_figure(page, r)
                os.makedirs(svg_dir, exist_ok=True)
                if clean["watermark"]:
                    # the page picture would show the watermark: draw the figure from its clean data
                    png, w_pt, h_pt = pdf_vector.render_png(clean, dpi), clean["width"], clean["height"]
                else:
                    png, w_pt, h_pt = pdf_figures.render_png(page, r, dpi)
                    save_svg(page, r, os.path.join(svg_dir, f"p{n}_fig{k}_outlined.svg"))
                with open(os.path.join(svg_dir, f"p{n}_fig{k}.svg"), "w", encoding="utf-8") as fh:
                    fh.write(pdf_vector.to_svg(clean))
                ws.write_string(row, 0, f"Page {n} - figure {k}", bold)
                # the PNG carries its dpi, so Excel shows it at natural size
                ws.insert_image(row + 1, 0, f"p{n}_fig{k}.png",
                                {"image_data": io.BytesIO(png), "object_position": 1})
                row += int(h_pt * 96 / 72 / 20) + 4      # default row height is 20 px
            found[n] = len(rects)
    return found


def main(pdf, out, first=None, last=None, figures=None):
    """figures: None = no figures, "auto" = detect on the page range, or a list of pages."""
    import pdfplumber
    import xlsxwriter
    wb = xlsxwriter.Workbook(out)
    ws = wb.add_worksheet("All")
    cell = wb.add_format({"text_wrap": True, "valign": "top"})
    bold = wb.add_format({"bold": True})
    row, width = 0, {}
    spans = []          # (row, first_col, last_col, text_length) of merged cells, for row heights
    with pdfplumber.open(pdf) as p:
        total = len(p.pages)
        first = max(1, first or 1)
        last = min(total, last or total)
        if first > last:
            sys.exit(f"Invalid page range {first}-{last} (PDF has {total} pages)")
        for n in range(first, last + 1):
            page = p.pages[n - 1]
            ti = 0
            for t in find_tables(page):
                grid, merges = table_grid(page, t)
                if not is_table(grid, t):
                    continue
                ti += 1
                ws.write_string(row, 0, f"Page {n} - table {ti}", bold)
                row += 1
                for r0, c0, r1, c1 in merges:       # merge first, then write the top-left cell
                    try:
                        ws.merge_range(row + r0, c0, row + r1, c1, "", cell)
                    except Exception:               # overlapping/degenerate span in an odd PDF: keep unmerged
                        pass
                for ri, cells_in_row in enumerate(grid):
                    for ci, runs in enumerate(cells_in_row):
                        write_cell(ws, wb, row + ri, ci, runs, cell)
                for ri, cells_in_row in enumerate(grid):
                    for ci, runs in enumerate(cells_in_row):
                        lines = [len(x) for x in cell_text(runs).split("\n")]
                        span = next((m for m in merges if (m[0], m[1]) == (ri, ci)), None)
                        if span is None:
                            width[ci] = max(width.get(ci, 0), max(lines))
                        elif span[2] == span[0] and max(lines):
                            spans.append((row + ri, span[1], span[3], lines))
                row += len(grid) + 1
    for ci, w in width.items():
        ws.set_column(ci, ci, min(MAX_COL, w + 2))
    for r, c0, c1, lines in spans:        # Excel does not auto-fit merged cells: size their rows
        chars = sum(min(MAX_COL, width.get(c, 8) + 2) for c in range(c0, c1 + 1))
        ws.set_row(r, 15 * sum(max(1, math.ceil(n * 1.1 / max(chars, 1))) for n in lines))
    found = {}
    svg_dir = None
    if figures:
        manual = figures != "auto"
        pages = [n for n in figures if 1 <= n <= total] if manual else range(first, last + 1)
        stem = os.path.splitext(os.path.basename(pdf))[0]
        svg_dir = os.path.join(os.path.dirname(os.path.abspath(out)), f"{stem}_svg")
        found = write_figures(wb, pdf, pages, manual, svg_dir)
    wb.close()
    print(row, "table rows")
    if figures:
        print("figures:", ", ".join(f"p{n} x{k}" for n, k in found.items()) or "none found")
        if found:
            print("SVG files:", svg_dir)
    return found


def _page(v):
    return None if v.lower() in ("none", "-", "") else int(v)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PDF tables (and optionally figures) to Excel")
    ap.add_argument("pdf")
    ap.add_argument("out")
    ap.add_argument("first", nargs="?", type=_page, default=None)
    ap.add_argument("last", nargs="?", type=_page, default=None)
    ap.add_argument("--figures", nargs="?", const="auto", metavar="PAGES",
                    help="export figures; PAGES like 5,8,31 or 10-12 (default: detect automatically)")
    a = ap.parse_args()
    figs = a.figures
    if figs and figs != "auto":
        try:
            figs = parse_pages(figs)
        except ValueError:
            sys.exit("--figures: use page numbers like 5,8,31 or 10-12")
    main(a.pdf, a.out, a.first, a.last, figs)
