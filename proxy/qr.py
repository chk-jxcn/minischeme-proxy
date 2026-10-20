"""A small QR code encoder (ISO/IEC 18004, byte mode, versions 1-10) on the
standard library, after Project Nayuki's QR Code generator (MIT): the
proxy draws login QR codes for the board (gui-bitmap rows) without Pillow.

qr_matrix(text, ecl='M', mask=None) -> rows of 0/1 (no quiet zone).
"""

from typing import List, Optional

# per error correction level: codewords of ECC per block, and blocks (index: version)
_ECC_PER_BLOCK = {
    'L': (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18),
    'M': (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26),
    'Q': (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24),
    'H': (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28),
}
_BLOCKS = {
    'L': (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4),
    'M': (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5),
    'Q': (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8),
    'H': (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8),
}
_FORMAT_BITS = {'L': 1, 'M': 0, 'Q': 3, 'H': 2}
MAX_VERSION = 10


def _raw_data_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int, ecl: str) -> int:
    return _raw_data_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _BLOCKS[ecl][ver]


def _rs_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> List[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_multiply(root, 0x02)
    return result


def _rs_remainder(data: List[int], divisor: List[int]) -> List[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _rs_multiply(coef, factor)
    return result


def _codewords(data: bytes, ver: int, ecl: str) -> List[int]:
    """The data codewords (byte mode, terminated and padded) with their ECC, interleaved."""
    bits = []

    def put(value, n):
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(data), 8 if ver <= 9 else 16)
    for b in data:
        put(b, 8)
    capacity = _data_codewords(ver, ecl) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    numblocks, ecclen = _BLOCKS[ecl][ver], _ECC_PER_BLOCK[ecl][ver]
    raw = _raw_data_modules(ver) // 8
    numshort = numblocks - raw % numblocks
    shortlen = raw // numblocks
    divisor = _rs_divisor(ecclen)
    blocks, k = [], 0
    for i in range(numblocks):
        dat = words[k:k + shortlen - ecclen + (0 if i < numshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < numshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= numshort:
                out.append(blk[i])
    return out


class _Grid:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.m = [[0] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x, y, dark):
        self.m[y][x] = 1 if dark else 0
        self.fn[y][x] = True

    def alignment_positions(self):
        if self.ver == 1:
            return []
        numalign = self.ver // 7 + 2
        step = (self.ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
        return list(reversed([self.size - 7 - i * step for i in range(numalign - 1)] + [6]))

    def function_patterns(self):
        n = self.size
        for i in range(n):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self.alignment_positions()
        last = len(pos) - 1
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits('M', 0)          # reserved; drawn again for the chosen mask
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1
                a, b = n - 11 + i % 3, i // 3
                self.set_fn(a, b, bit)
                self.set_fn(b, a, bit)

    def format_bits(self, ecl, mask):
        data = _FORMAT_BITS[ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1   # noqa: E731
        n = self.size
        for i in range(0, 6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(0, 8):
            self.set_fn(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, bit(i))
        self.set_fn(8, n - 8, 1)          # the dark module

    def codewords(self, words):
        n, i, total = self.size, 0, len(words) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (n - 1 - vert) if upward else vert
                    if not self.fn[y][x] and i < total:
                        self.m[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        f = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] ^= 1

    def penalty(self):
        """N1 (runs), N2 (2x2 blocks) and N4 (balance): enough to pick a readable mask."""
        n, m, score = self.size, self.m, 0
        for line in list(m) + [list(col) for col in zip(*m)]:
            run, prev = 0, None
            for v in line + [None]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, v
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, m))
        score += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return score


def qr_matrix(text, ecl: str = 'M', mask: Optional[int] = None, min_version: int = 1) -> List[List[int]]:
    """The modules of text's QR code (rows of 0/1, no quiet zone): the
    smallest version from min_version (up to 10) that holds it at level ecl;
    mask None picks the one with the lowest penalty."""
    data = text.encode('utf-8') if isinstance(text, str) else bytes(text)
    ver = next((v for v in range(max(1, min_version), MAX_VERSION + 1)
                if (4 + (8 if v <= 9 else 16) + 8 * len(data) + 7) // 8 <= _data_codewords(v, ecl)), None)
    if ver is None:
        raise ValueError('QR: too much data for version 10')
    words = _codewords(data, ver, ecl)
    best = None
    for k in ([mask] if mask is not None else range(8)):
        g = _Grid(ver)
        g.function_patterns()
        g.codewords(words)
        g.apply_mask(k)
        g.format_bits(ecl, k)
        p = g.penalty() if mask is None else 0
        if best is None or p < best[0]:
            best = (p, g)
    return best[1].m


def qr_hex(bits) -> str:
    """A row of modules as hex digits, 4 modules each, the first module the high bit."""
    bits = list(bits) + [0] * (-len(bits) % 4)
    return ''.join('0123456789abcdef'[bits[i] * 8 + bits[i + 1] * 4 + bits[i + 2] * 2 + bits[i + 3]]
                   for i in range(0, len(bits), 4))
