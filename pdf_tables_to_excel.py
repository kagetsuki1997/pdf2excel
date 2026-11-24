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

M = {"": "θ", "": "Ψ", "": "°", "": "µ", "": "Ω", "": "ω"}
SIZE_DROP = 0.9     # a char is "script" if its size < 90% of the largest size on its line
RAISE = 0.15        # pt above the line's baseline that counts as superscript
LINE_SHIFT = 0.7    # a char whose baseline moves more than 0.7 x font size starts a new line


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


def split_lines(chars):
    """Group a cell's characters into text lines by baseline.

    Spaces never decide anything (some PDFs size them differently from the letters). A line is
    broken only when a letter's baseline moves by more than LINE_SHIFT x font size, which is far
    more than any subscript/superscript offset but less than any line pitch."""
    lines = []
    for c in chars:
        if not c["text"].strip():
            if lines:
                lines[-1].append(c)
            else:
                lines.append([c])
            continue
        if lines:
            letters = [x for x in lines[-1] if x["text"].strip()]
            if letters:
                big = max(x["size"] for x in letters)
                ref = next(x["matrix"][5] for x in letters if x["size"] >= big - 0.05)
                if abs(c["matrix"][5] - ref) > LINE_SHIFT * max(big, c["size"]):
                    lines.append([c])
                    continue
            lines[-1].append(c)
        else:
            lines.append([c])
    return lines


def cell_runs(page, bbox):
    """Return [(text, kind)] runs; kind is '', 'sub' or 'sup'."""
    chars = [c for c in page.crop(bbox).chars if c["text"] != "\n"]
    runs = []
    for line in split_lines(chars):
        letters = [c for c in line if c["text"].strip()]
        if not letters:
            continue
        if runs:
            runs.append((" ", ""))
        big = max(c["size"] for c in letters)
        ref_base = next(c["matrix"][5] for c in letters if c["size"] >= big - 0.05)
        for c in line:
            kind = ""
            if c["text"].strip() and c["size"] < big * SIZE_DROP:
                # PDF y grows upward: raised baseline = superscript, otherwise subscript
                kind = "sup" if c["matrix"][5] - ref_base > RAISE else "sub"
            runs.append((M.get(c["text"], c["text"]), kind))
    merged = []
    for t, k in runs:
        if merged and merged[-1][1] == k:
            merged[-1] = (merged[-1][0] + t, k)
        else:
            merged.append((t, k))
    return merged


def clean_runs(runs):
    runs = [[t, k] for t, k in runs if t]
    for i in range(len(runs)):
        runs[i][0] = re.sub(r" {2,}", " ", runs[i][0])
        if i and runs[i - 1][0].endswith(" "):
            runs[i][0] = runs[i][0].lstrip(" ")
    runs = [r for r in runs if r[0]]
    if runs:
        runs[0][0] = runs[0][0].lstrip()
        runs[-1][0] = runs[-1][0].rstrip()
    return [r for r in runs if r[0]]


def write_cell(ws, wb, row, col, runs, fmt):
    runs = clean_runs(runs)
    if not runs:
        return
    if all(k == "" for _, k in runs):
        ws.write_string(row, col, " ".join("".join(t for t, _ in runs).split()), fmt)
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
                png, w_pt, h_pt = pdf_figures.render_png(page, r, dpi)
                os.makedirs(svg_dir, exist_ok=True)
                save_svg(page, r, os.path.join(svg_dir, f"p{n}_fig{k}_outlined.svg"))
                clean = pdf_vector.extract_figure(page, r)
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
            for ti, t in enumerate(page.find_tables(), 1):
                ws.write_string(row, 0, f"Page {n} - table {ti}", bold)
                row += 1
                grid, merges = table_grid(page, t)
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
                        length = len("".join(x for x, _ in clean_runs(runs)))
                        span = next((m for m in merges if (m[0], m[1]) == (ri, ci)), None)
                        if span is None:
                            width[ci] = max(width.get(ci, 0), length)
                        elif span[2] == span[0] and length:
                            spans.append((row + ri, span[1], span[3], length))
                row += len(grid) + 1
    for ci, w in width.items():
        ws.set_column(ci, ci, min(45, w + 2))
    for r, c0, c1, length in spans:       # Excel does not auto-fit merged cells: size their rows
        chars = sum(min(45, width.get(c, 8) + 2) for c in range(c0, c1 + 1))
        ws.set_row(r, 15 * max(1, math.ceil(length * 1.1 / max(chars, 1))))
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
