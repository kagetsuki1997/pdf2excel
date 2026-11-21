# /// script
# requires-python = ">=3.9"
# dependencies = ["pdfplumber", "xlsxwriter", "pymupdf", "fonttools"]
# ///
"""Simple window for pdf_tables_to_excel.  Run: uv run pdf2excel_gui.py"""
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pdf_figures
import pdf_tables_to_excel as core


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=14)
        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        self.pdf = tk.StringVar()
        self.out = tk.StringVar()
        self.first = tk.StringVar()
        self.last = tk.StringVar()
        self.do_figs = tk.BooleanVar(value=True)
        self.fig_pages = tk.StringVar()
        self.status = tk.StringVar(value="Choose a PDF to start.")

        ttk.Label(self, text="PDF file").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.pdf, width=55).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(self, text="Browse…", command=self.pick_pdf).grid(row=0, column=2)

        ttk.Label(self, text="Save Excel as").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.out).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(self, text="Browse…", command=self.pick_out).grid(row=1, column=2)

        pages = ttk.Frame(self)
        pages.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 4))
        ttk.Label(pages, text="Pages  from").pack(side="left")
        ttk.Entry(pages, textvariable=self.first, width=6).pack(side="left", padx=6)
        ttk.Label(pages, text="to").pack(side="left")
        ttk.Entry(pages, textvariable=self.last, width=6).pack(side="left", padx=6)
        ttk.Label(pages, text="(leave blank = first / last page)", foreground="gray").pack(side="left", padx=6)

        figs = ttk.Frame(self)
        figs.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 4))
        ttk.Checkbutton(figs, text="Also export figures (one sheet per page)",
                        variable=self.do_figs, command=self.toggle_figs).pack(side="left")
        ttk.Label(figs, text="   Figure pages").pack(side="left")
        self.fig_entry = ttk.Entry(figs, textvariable=self.fig_pages, width=18)
        self.fig_entry.pack(side="left", padx=6)
        ttk.Label(figs, text="e.g. 5, 8, 31-33 (blank = auto-detect)", foreground="gray").pack(side="left")

        self.btn = ttk.Button(self, text="Convert", command=self.run)
        self.btn.grid(row=4, column=0, columnspan=3, pady=(12, 6))
        self.bar = ttk.Progressbar(self, mode="indeterminate")
        self.bar.grid(row=5, column=0, columnspan=3, sticky="ew")
        ttk.Label(self, textvariable=self.status, wraplength=520).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def toggle_figs(self):
        self.fig_entry.state(["!disabled"] if self.do_figs.get() else ["disabled"])

    def pick_pdf(self):
        f = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
        if f:
            self.pdf.set(f)
            if not self.out.get():
                self.out.set(os.path.splitext(f)[0] + ".xlsx")

    def pick_out(self):
        f = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if f:
            self.out.set(f)

    def run(self):
        pdf, out = self.pdf.get().strip(), self.out.get().strip()
        if not os.path.isfile(pdf):
            return messagebox.showerror("PDF", "Choose an existing PDF file.")
        if not out:
            return messagebox.showerror("Output", "Choose where to save the Excel file.")
        try:
            first = int(self.first.get()) if self.first.get().strip() else None
            last = int(self.last.get()) if self.last.get().strip() else None
        except ValueError:
            return messagebox.showerror("Pages", "Page numbers must be whole numbers (or blank).")
        figures = None
        if self.do_figs.get():
            figures = "auto"
            if self.fig_pages.get().strip():
                try:
                    with core.pymupdf.open(pdf) as d:
                        figures = pdf_figures.parse_pages(self.fig_pages.get(), len(d))
                except ValueError:
                    return messagebox.showerror("Figure pages", "Use page numbers like 5, 8, 31-33.")
                if not figures:
                    return messagebox.showerror("Figure pages", "None of those pages exist in this PDF.")
        self.btn.state(["disabled"])
        self.bar.start(12)
        self.status.set("Converting… (large PDFs can take a while)")
        threading.Thread(target=self.work, args=(pdf, out, first, last, figures), daemon=True).start()

    def work(self, pdf, out, first, last, figures):
        try:
            found = core.main(pdf, out, first, last, figures)
            msg, ok = f"Done: {out}", True
            if figures:
                msg += "\nFigures: " + (", ".join(f"p{n} x{k}" for n, k in found.items()) or "none found")
                if found:
                    stem = os.path.splitext(os.path.basename(pdf))[0]
                    folder = os.path.dirname(os.path.abspath(out))
                    msg += f"\nSVG files: {os.path.join(folder, stem + '_svg')}\nVisio files: {os.path.join(folder, stem + '_figures_outlined.vsdx')} (and _figures.vsdx)"
        except SystemExit as e:           # e.g. invalid page range
            msg, ok = str(e.code), False
        except Exception as e:            # noqa: BLE001 - show any failure to the user
            msg, ok = f"Failed: {e}", False
        self.after(0, self.finish, msg, ok)

    def finish(self, msg, ok):
        self.bar.stop()
        self.btn.state(["!disabled"])
        self.status.set(msg)
        if not ok:
            messagebox.showerror("Error", msg)


if __name__ == "__main__":
    root = tk.Tk()
    root.title("PDF tables → Excel")
    App(root)
    root.mainloop()
