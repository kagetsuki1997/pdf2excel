"""Recognise watermark text/shapes so they can be ignored.

A watermark is large (a headline-sized or bigger) and either drawn at a diagonal angle or in a
very light colour, usually behind the real content ("CONFIDENTIAL", "DRAFT", a company name...).
Ordinary text is small; large light text on a dark header would be white, so the size limit
and the angle test keep real content safe.
"""
import math

MIN_SIZE = 24          # pt: smaller text is never treated as watermark
LIGHT = 0.6            # lightness (0 black .. 1 white) at or above which large text counts as faded
DIAGONAL_TOL = 5       # degrees away from a multiple of 90 that still counts as straight
BIG_SHAPE = 0.15       # a faded shape covering this fraction of the page is a watermark/background
BIG_IMAGE = 0.5        # a picture covering this fraction of the page is a watermark/background


def lightness(color):
    """0 (black) .. 1 (white) for a pdfplumber colour: None, gray, RGB or CMYK tuple/number."""
    if color is None:
        return 0.0
    if isinstance(color, (int, float)):
        return float(color)
    try:
        v = [float(x) for x in color]
    except (TypeError, ValueError):
        return 0.0
    if len(v) == 1:
        return v[0]
    if len(v) == 3:
        return 0.299 * v[0] + 0.587 * v[1] + 0.114 * v[2]
    if len(v) == 4:
        c, m, y, k = v
        return 0.299 * (1 - c) * (1 - k) + 0.587 * (1 - m) * (1 - k) + 0.114 * (1 - y) * (1 - k)
    return 0.0


def int_lightness(rgb):
    """Lightness of a 0xRRGGBB integer colour (pymupdf spans)."""
    return lightness(((rgb >> 16 & 255) / 255, (rgb >> 8 & 255) / 255, (rgb & 255) / 255))


def is_diagonal(dx, dy):
    """True if the text direction (dx, dy) is not close to horizontal/vertical."""
    angle = math.degrees(math.atan2(dy, dx)) % 90
    return min(angle, 90 - angle) > DIAGONAL_TOL


def is_watermark_text(size, light, dx, dy):
    return size >= MIN_SIZE and (is_diagonal(dx, dy) or light >= LIGHT)


def is_faded_fill(fill_rgb, stroke, area, page_area):
    """A big light filled shape without an outline (page-sized grey logo / background)."""
    return (fill_rgb is not None and stroke is None and area > BIG_SHAPE * page_area
            and lightness(fill_rgb) >= 0.75)
