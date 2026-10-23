"""Glyph replies: the bitmaps of the code points a board lacks.

GET /font/glyphs?face=simsun&size=16&cps=4e2d,1f600  (hex code points)

Reply (application/octet-stream), little-endian, "MGLY" version 1:
    0  "MGLY", u8 version 1, u8 height, u8 bits per pixel (1), u8 flags (0)
    8  u32 hash (what the glyphs are made from: Library.chain_hash), u16 count,
       u16 reserved
   16  count records: u32 code point, u8 advance, u8 bytes per row, then
       bytes per row x height bytes of rows (MSB = leftmost pixel).
       Advance and bytes per row 0: no font of the server has it.
The records follow the request's order; every requested code point has one.
"""

import collections
import struct
import threading

from . import render

MAX_CPS = 64
MIN_SIZE, MAX_SIZE = 8, 48
MAX_CP = 0x10FFFF


class GlyphError(ValueError):
    pass


def parse_cps(text):
    out = []
    for part in (text or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        try:
            cp = int(part, 16)
        except ValueError:
            raise GlyphError(f"bad code point {part!r} (hex, comma separated)")
        if cp < 0x20 or cp > MAX_CP or 0xD800 <= cp <= 0xDFFF:
            raise GlyphError(f"code point out of range: {part}")
        if cp not in out:
            out.append(cp)
    if not out:
        raise GlyphError("no code points (cps=4e2d,1f600)")
    if len(out) > MAX_CPS:
        raise GlyphError(f"at most {MAX_CPS} code points per request")
    return out


def parse_size(text):
    try:
        size = int(text or "16")
    except ValueError:
        raise GlyphError("size: a number of pixels")
    if not MIN_SIZE <= size <= MAX_SIZE:
        raise GlyphError(f"size: {MIN_SIZE}..{MAX_SIZE}")
    return size


class GlyphService:
    """Renders and caches glyphs (keyed by the library's generation, so a
    change of fonts drops the cache)."""

    def __init__(self, library, cache_entries=50000):
        self.library = library
        self.cache = collections.OrderedDict()
        self.cache_entries = cache_entries
        self.lock = threading.Lock()
        self.stats = {"requests": 0, "glyphs": 0, "missing": 0, "rendered": 0}

    def glyph(self, chain, cp, size):
        """(advance, bpr, rows, font id) of cp from the first font of chain
        that maps it, else (0, 0, b"", None)."""
        for font in chain:
            if cp in font.cmap:
                key = (self.library.generation, font.id, size, cp)
                with self.lock:
                    hit = self.cache.get(key)
                    if hit:
                        self.cache.move_to_end(key)
                        return hit
                advance, bpr, rows = render.render_fitted(font.path, size, chr(cp), font.offset(size))
                value = (advance, bpr, rows, font.id)
                with self.lock:
                    self.stats["rendered"] += 1
                    self.cache[key] = value
                    while len(self.cache) > self.cache_entries:
                        self.cache.popitem(last=False)
                return value
        return 0, 0, b"", None

    def reply(self, face, size, cps):
        chain = self.library.chain(face)
        out = [b"MGLY", struct.pack("<BBBBIHH", 1, size, 1, 0, self.library.chain_hash(face, size), len(cps), 0)]
        missing = 0
        for cp in cps:
            advance, bpr, rows, _ = self.glyph(chain, cp, size)
            if not bpr:
                missing += 1
                advance, rows = 0, b""
            out.append(struct.pack("<IBB", cp, advance, bpr))
            out.append(rows)
        with self.lock:
            self.stats["requests"] += 1
            self.stats["glyphs"] += len(cps)
            self.stats["missing"] += missing
        return b"".join(out)

    def preview(self, face, size, text):
        chain = self.library.chain(face)
        glyphs = []
        for ch in text[:40]:
            advance, bpr, rows, _ = self.glyph(chain, ord(ch), size)
            glyphs.append((advance or size, bpr, rows))
        return render.preview_png(glyphs, size)


def decode_reply(data):
    """[(cp, advance, bpr, rows)] and (height, hash) of a reply (tests)."""
    if data[:4] != b"MGLY":
        raise GlyphError("not an MGLY reply")
    version, height, bpp, _flags, hsh, count, _ = struct.unpack_from("<BBBBIHH", data, 4)
    pos, out = 16, []
    for _ in range(count):
        cp, advance, bpr = struct.unpack_from("<IBB", data, pos)
        pos += 6
        rows = data[pos:pos + bpr * height]
        pos += bpr * height
        out.append((cp, advance, bpr, rows))
    if pos != len(data):
        raise GlyphError("trailing bytes")
    return out, (height, hsh)
