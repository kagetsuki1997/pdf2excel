# /// script
# requires-python = ">=3.9"
# dependencies = ["pdfplumber", "xlsxwriter"]
# ///
"""PDF tables -> Excel, keeping subscripts/superscripts as Excel rich text.

usage: uv run pdf_tables_to_excel.py in.pdf out.xlsx [first] [last]
  no pages      -> all pages
  first         -> first page to the end
  first last    -> that range (1-based, inclusive)
  none last     -> page 1 to last
"""
import re
import sys
import pdfplumber
import xlsxwriter

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


def main(pdf, out, first=None, last=None):
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
    wb.close()
    print(row, "rows")


if __name__ == "__main__":
    def page(v):
        return None if v.lower() in ("none", "-", "") else int(v)

    if len(sys.argv) < 3 or len(sys.argv) > 5:
        sys.exit(__doc__)
    pages = [page(v) for v in sys.argv[3:]]
    main(sys.argv[1], sys.argv[2], *pages)
