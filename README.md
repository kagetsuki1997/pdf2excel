# pdf2excel

Extract tables from a PDF page range into one Excel sheet. Subscripts and
superscripts (V_IN, T_A, θ_JA ...) are kept as real Excel formatting.

Built and tested on the Richtek RT6338 datasheet (pages 12-15). Works on
text-based PDFs only (no OCR for scanned pages).

## Setup

Install [uv](https://docs.astral.sh/uv/). That's all: `uv run` reads the
dependencies (pdfplumber, xlsxwriter, pymupdf, fonttools) from the header of the script and installs
them automatically into a cached environment. Works on Windows, Mac and Linux.

## Usage

    uv run pdf_tables_to_excel.py INPUT.pdf OUTPUT.xlsx [FIRST] [LAST] [--figures[=PAGES]]

Pages are 1-based and inclusive.

| Command | Pages extracted |
|---|---|
| `uv run pdf_tables_to_excel.py in.pdf out.xlsx` | all pages |
| `uv run pdf_tables_to_excel.py in.pdf out.xlsx 12` | 12 to the end |
| `uv run pdf_tables_to_excel.py in.pdf out.xlsx 12 15` | 12 to 15 |
| `uv run pdf_tables_to_excel.py in.pdf out.xlsx none 15` | 1 to 15 |

`none` (or `-`) means "no limit" for that side. A last page past the end of
the PDF is clamped to the last page.

Output: one sheet "All"; each table is preceded by a bold "Page N - table M" label.

Without uv: `pip install pdfplumber xlsxwriter pymupdf fonttools`, then `python pdf_tables_to_excel.py ...`.

## Figures

Add `--figures` to also export diagrams and charts as pictures. Each page that
has figures gets its own sheet ("Fig p8", ...); several figures on one page are
stacked down the sheet, each with a "Page N - figure K" label.

| Command | Figures exported from |
|---|---|
| `... in.pdf out.xlsx --figures` | pages found automatically in the FIRST-LAST range |
| `... in.pdf out.xlsx --figures=5,8,31` | only these pages (ranges like `10-12` work) |

Put `--figures` after the page numbers, or write it as `--figures=5,8,31`.
If you name a page and no figure is detected there, the whole content area of
the page is exported.

Auto-detect treats a cluster of drawing items (lines, curves, boxes) or a picture
as a figure, outside the page header/footer. Short text labels around it and a
"Figure ..." caption below it are included. Clusters made only of horizontal and
vertical lines (tables, grids) and anything under 60 pt tall (equations) are
skipped. It is a heuristic: check the result, and use the page list for pages it
gets wrong. Figures are rendered at 200 dpi PNG (Excel cannot embed SVG), so they
stay sharp but are not editable.

Every exported figure is also saved as SVG, in a folder named `{pdf filename}_svg` next to
the Excel file:

- `p8_fig1.svg` - **clean SVG**: plain paths with absolute coordinates, real `<text>` (font, size,
  subscripts kept), no transforms or clipping. Symbols the PDF stores as hidden glyphs (like >= and
  ->) are redrawn from the embedded font as shapes.
- `p8_fig1_outlined.svg` - text converted to outlines: looks exact, text not editable.

To use a figure in Visio, insert the SVG (Insert, Pictures), or open it in draw.io and export
`.vsdx` for editable shapes. (A direct `.vsdx` export was tried and removed: it did not display
reliably in viewers.) Raster images inside a figure are included in the clean SVG.

The window has an "Also export figures" checkbox and a "Figure pages" box for the
same thing (blank = auto-detect).

## Notes

- A script character = smaller than the largest text on its line. Raised above
  the baseline = superscript, otherwise subscript. Tune `SIZE_DROP` / `RAISE`
  at the top of the script if a PDF uses different sizes.
- Superscripts are implemented but not yet tested on a real PDF.
- Merged cells in the PDF table are merged in Excel too (spanning headers, vertically merged cells).
- A line inside a cell is a new line only when its baseline moves by more than 0.7 x font size,
  so wrapped text works and spaces sized differently from the letters cannot turn a cell into subscripts.
- Symbol-font characters are mapped to θ Ψ ° µ Ω ω. Add more to `M` if a PDF
  shows odd characters.
- Formatting shows in Excel; some viewers (Preview, Numbers) may not show it.

## Windows build (no Python or uv needed)

`.github/workflows/release.yml` builds with PyInstaller on GitHub:
`pdf2excel-gui-windows.zip` (the window: unzip the folder and run `pdf2excel-gui.exe` inside it)
and `pdf2excel.exe` (command line, same arguments as the script). The window is a folder, not a
single exe, on purpose: a one-file exe unpacks about 100 MB on every start, which is slow
(more so with antivirus). Push a version tag to build them and attach them to a release:

    git tag v1.0.0
    git push origin v1.0.0

Running the workflow manually (Actions tab, "Run workflow") builds them and keeps them as a
downloadable artifact without making a release.

