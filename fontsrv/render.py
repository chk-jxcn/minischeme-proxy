"""Glyph rendering for the font server.

The same algorithm as minischeme-webrepl/tools/make_font.py, which made the
board's font ROM: a glyph fills a cell `height` rows tall, the face's ink
centred in the cell by one vertical offset per face and size (auto_offset),
bitmaps one bit per pixel, MSB = leftmost pixel. Keep the two in step, or
downloaded glyphs sit higher or lower than the ROM's.
"""

import functools

from PIL import Image, ImageDraw, ImageFont

MAX_WIDTH_CELLS = 2    # a glyph is at most 2 cells wide (emoji in proportional faces)


@functools.lru_cache(maxsize=64)
def pil_font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def auto_offset(font, height, chars=()):
    """Vertical offset that puts the glyphs' ink in the cell: the ink of all
    of chars, centred, when it fits; else that of 98 % of them, the tops kept
    first (make_font.auto_offset)."""
    tops, bottoms = [], []
    for ch in chars:
        bb = font.getbbox(ch)
        if bb and bb[3] > bb[1]:
            tops.append(bb[1])
            bottoms.append(bb[3])
    if not tops:
        ascent, descent = font.getmetrics()
        return (height - (ascent + descent)) // 2
    tops.sort()
    bottoms.sort()
    top, bottom = tops[0], bottoms[-1]
    if bottom - top > height:
        k = len(tops) // 100
        top, bottom = tops[k], bottoms[-1 - k]
    if bottom - top > height:
        return -top
    return -top + (height - (bottom - top)) // 2


def _pack_rows(img, width, height):
    """Rows of a mode "1" image, one bit per pixel, MSB first."""
    bpr = (width + 7) // 8
    px = img.load()
    rows = bytearray(bpr * height)
    for y in range(height):
        for x in range(width):
            if px[x, y]:
                rows[y * bpr + x // 8] |= 0x80 >> (x % 8)
    return bpr, bytes(rows)


def render_glyph(font, ch, height, y_offset):
    """(advance, bytes per row, rows) of one character in the face's own
    spacing (make_font spacing 'font'), at most MAX_WIDTH_CELLS cells wide."""
    max_w = height * MAX_WIDTH_CELLS
    bbox = font.getbbox(ch)
    own = max(1, int(font.getlength(ch) + 0.5))
    advance = min(own, max_w)
    if not bbox or bbox[2] <= bbox[0]:
        bpr = (advance + 7) // 8
        return advance, bpr, bytes(bpr * height)
    width = min(max(advance, bbox[2]), max_w)
    img = Image.new("1", ((width + 7) // 8 * 8, height), 0)
    ImageDraw.Draw(img).text((0, y_offset), ch, font=font, fill=1)
    bpr, rows = _pack_rows(img, (width + 7) // 8 * 8, height)
    return advance, bpr, rows


RENDER_VERSION = 2     # part of the glyphs' identity: boards drop glyphs of an older rendering


def render_fitted(path, height, ch, y_offset):
    """render_glyph with the face's offset, unless the glyph's ink would not
    fit the cell: one taller than the cell (an emoji 19 rows tall at 16 px)
    is drawn at the largest size that fits and centred; one that fits but
    would be cut at the top or bottom is moved in. Glyphs inside the cell
    keep the face's offset (and so match the font ROM's)."""
    font = pil_font(path, height)
    bb = font.getbbox(ch)
    if bb and bb[3] > bb[1]:
        top, bottom = bb[1] + y_offset, bb[3] + y_offset
        if bottom - top > height:
            size = height
            while size > 6:
                size -= 1
                smaller = pil_font(path, size)
                sb = smaller.getbbox(ch)
                if sb and sb[3] - sb[1] <= height:
                    break
            font, bb = smaller, sb
            y_offset = -bb[1] + (height - (bb[3] - bb[1])) // 2
        elif bottom > height:
            y_offset -= bottom - height
        elif top < 0:
            y_offset -= top
    return render_glyph(font, ch, height, y_offset)


def preview_png(glyphs, height, scale=3, fg=(255, 255, 255), bg=(16, 24, 32)):
    """A PNG of rendered glyphs [(advance, bpr, rows)] side by side, for the
    admin page."""
    import io
    total = sum(max(1, g[0]) for g in glyphs) or 1
    img = Image.new("RGB", (total * scale, height * scale), bg)
    px = img.load()
    x0 = 0
    for advance, bpr, rows in glyphs:
        for y in range(height):
            for x in range(bpr * 8):
                if rows and rows[y * bpr + x // 8] & (0x80 >> (x % 8)):
                    for dy in range(scale):
                        for dx in range(scale):
                            xx, yy = (x0 + x) * scale + dx, y * scale + dy
                            if xx < img.width:
                                px[xx, yy] = fg
        x0 += max(1, advance)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
