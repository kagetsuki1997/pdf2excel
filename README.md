# pdf2excel

Extract tables from a PDF page range into one Excel sheet. Subscripts and
superscripts (V_IN, T_A, θ_JA ...) are kept as real Excel formatting.

Built and tested on the Richtek RT6338 datasheet (pages 12-15). Works on
text-based PDFs only (no OCR for scanned pages).

## Setup

Install [uv](https://docs.astral.sh/uv/). That's all: `uv run` reads the
dependencies (pdfplumber, xlsxwriter) from the header of the script and installs
them automatically into a cached environment. Works on Windows, Mac and Linux.

## Usage

    uv run pdf_tables_to_excel.py INPUT.pdf OUTPUT.xlsx [FIRST] [LAST]

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

Without uv: `pip install pdfplumber xlsxwriter`, then `python pdf_tables_to_excel.py ...`.

## Notes

- A script character = smaller than the largest text on its line. Raised above
  the baseline = superscript, otherwise subscript. Tune `SIZE_DROP` / `RAISE`
  at the top of the script if a PDF uses different sizes.
- Superscripts are implemented but not yet tested on a real PDF.
- Blank-header columns are merged into the column on their left
  (fixes spanning headers such as "Test Conditions").
- Symbol-font characters are mapped to θ Ψ ° µ Ω ω. Add more to `M` if a PDF
  shows odd characters.
- Rows like "RT6338C/CH" with an empty Parameter cell belong to the row above.
- Formatting shows in Excel; some viewers (Preview, Numbers) may not show it.

## Windows exe (no Python or uv needed)

`.github/workflows/release.yml` builds two exe files with PyInstaller on GitHub:
`pdf2excel-gui.exe` (window) and `pdf2excel.exe` (command line, same arguments
as the script). Push a version tag to build them and attach them to a release:

    git tag v1.0.0
    git push origin v1.0.0

Running the workflow manually (Actions tab, "Run workflow") builds the exe files
and keeps them as a downloadable artifact without making a release.
