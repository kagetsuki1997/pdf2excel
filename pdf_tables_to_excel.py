# /// script
# requires-python = ">=3.9"
# dependencies = ["pdfplumber", "xlsxwriter", "pymupdf", "fonttools"]
# ///
"""PDF tables -> Excel, keeping subscripts/superscripts as Excel rich text.
Optionally also exports figures (diagrams, charts) as pictures, one sheet per page,
plus, next to the Excel file, one clean SVG per figure in a folder named {pdf filename}_svg
(and an _outlined.svg with text as outlines) and one {pdf filename}_figures.vsdx Visio file
with every figure on its own page.

usage: uv run pdf_tables_to_excel.py in.pdf out.xlsx [first] [last] [--figures [PAGES]]
  no pages      -> all pages
  first         -> first page to the end
  first last    -> that range (1-based, inclusive)
  none last     -> page 1 to last
  --figures           also export figures found on the pages in the range
  --figures=5,8,31    export figures only from these pages (ranges like 10-12 work too)
"""
import argparse
import io
import os
import re
import sys
import pdfplumber
import pymupdf
import xlsxwriter

import pdf_figures
import pdf_vector

M = {"": "θ", "": "Ψ", "": "°", "": "µ", "": "Ω", "": "ω"}
SIZE_DROP = 0.9     # a char is "script" if its size < 90% of the largest size on its line
RAISE = 0.15        # pt above the line's baseline that counts as superscript


def cell_runs(page, bbox):
    """Return [(text, kind)] runs; kind is '', 'sub' or 'sup'."""
    chars = [c for c in page.crop(bbox).chars if c["text"] != "\n"]
    lines = []                      # chars arrive in reading order; split where text drops to a new line
    for c in chars:
        if lines and c["top"] < max(x["bottom"] for x in lines[-1]) - 0.3 * c["size"]:
            lines[-1].append(c)
        else:
            lines.append([c])
    runs = []
    for line in lines:
        big = max(c["size"] for c in line)
        ref_base = next(c["matrix"][5] for c in line if c["size"] >= big - 0.05)
        if runs:
            runs.append((" ", ""))
        for c in line:
            kind = ""
            if c["size"] < big * SIZE_DROP and c["text"].strip():
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


def join_runs(parts):
    res = []
    for p in parts:
        if p:
            if res:
                res.append((" ", ""))
            res.extend(p)
    return res


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


def table_rows(page, table):
    rows = [[cell_runs(page, c) if c else [] for c in row.cells] for row in table.rows]
    # columns with a blank header are the right-hand part of a spanning header: fold them left
    header = ["".join(t for t, _ in c).strip() for c in rows[0]]
    groups = []
    for i, h in enumerate(header):
        if h or not groups:
            groups.append([i])
        else:
            groups[-1].append(i)
    return [[join_runs([r[i] for i in g]) for g in groups] for r in rows]


def save_svg(page, rect, path, pad=4):
    """Write one figure as SVG (text converted to outlines so symbols and fonts stay exact)."""
    original = page.cropbox
    try:
        page.set_cropbox(pdf_figures._inflate(rect, pad) & page.rect)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page.get_svg_image(text_as_path=True))
    finally:
        page.set_cropbox(original)


def write_figures(wb, pdf, pages, manual, svg_dir, vsdx_path, dpi=200):
    """One sheet per page that has figures. Also writes, per figure, a clean SVG and an
    outlined SVG into svg_dir, and all figures into one .vsdx (one Visio page per figure).
    Returns {page: number_of_figures}."""
    found = {}
    visio = pdf_vector.VsdxWriter()                    # real, editable Visio text
    visio_outlined = pdf_vector.VsdxWriter()           # text drawn as outline shapes (works in any viewer)
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
                visio.add_figure(f"p{n} fig{k}", clean)
                visio_outlined.add_figure(f"p{n} fig{k}", pdf_vector.extract_figure(page, r, text_outlines=True))
                ws.write_string(row, 0, f"Page {n} - figure {k}", bold)
                # the PNG carries its dpi, so Excel shows it at natural size
                ws.insert_image(row + 1, 0, f"p{n}_fig{k}.png",
                                {"image_data": io.BytesIO(png), "object_position": 1})
                row += int(h_pt * 96 / 72 / 20) + 4      # default row height is 20 px
            found[n] = len(rects)
    if found:
        visio.save(vsdx_path)
        visio_outlined.save(vsdx_path.replace(".vsdx", "_outlined.vsdx"))
    return found


def main(pdf, out, first=None, last=None, figures=None):
    """figures: None = no figures, "auto" = detect on the page range, or a list of pages."""
    wb = xlsxwriter.Workbook(out)
    ws = wb.add_worksheet("All")
    cell = wb.add_format({"text_wrap": True, "valign": "top"})
    bold = wb.add_format({"bold": True})
    row, width = 0, {}
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
                for r in table_rows(page, t):
                    for ci, runs in enumerate(r):
                        write_cell(ws, wb, row, ci, runs, cell)
                        w = len("".join(x for x, _ in runs))
                        width[ci] = max(width.get(ci, 0), w)
                    row += 1
                row += 1
    for ci, w in width.items():
        ws.set_column(ci, ci, min(45, w + 2))
    found = {}
    if figures:
        manual = figures != "auto"
        pages = [n for n in figures if 1 <= n <= total] if manual else range(first, last + 1)
        stem = os.path.splitext(os.path.basename(pdf))[0]
        svg_dir = os.path.join(os.path.dirname(os.path.abspath(out)), f"{stem}_svg")
        vsdx_path = os.path.join(os.path.dirname(os.path.abspath(out)), f"{stem}_figures.vsdx")
        found = write_figures(wb, pdf, pages, manual, svg_dir, vsdx_path)
    wb.close()
    print(row, "table rows")
    if figures:
        print("figures:", ", ".join(f"p{n} x{k}" for n, k in found.items()) or "none found")
        if found:
            print("SVG files:", svg_dir)
            print("Visio files:", vsdx_path, "and", vsdx_path.replace(".vsdx", "_outlined.vsdx"))
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
        with pymupdf.open(a.pdf) as d:
            try:
                figs = pdf_figures.parse_pages(figs, len(d))
            except ValueError:
                sys.exit("--figures: use page numbers like 5,8,31 or 10-12")
    main(a.pdf, a.out, a.first, a.last, figs)
