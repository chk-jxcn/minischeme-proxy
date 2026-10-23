"""The font server (font_server.py, fontsrv/): glyph replies, uploads, order,
administration, without network or system fonts (a test font is built)."""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fontsrv.glyphs import GlyphError, GlyphService, decode_reply, parse_cps  # noqa: E402
from fontsrv.library import Library  # noqa: E402
from fontsrv.server import FontServer  # noqa: E402


def make_font(family, chars, tall=()):
    """A TTF with a filled square for each of chars (tall: two cells high)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    names = [".notdef"] + [f"u{ord(c):04X}" for c in chars]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({ord(c): f"u{ord(c):04X}" for c in chars})
    glyphs = {}
    for name in names:
        pen = TTGlyphPen(None)
        if name != ".notdef":
            top = 1700 if chr(int(name[1:], 16)) in tall else 800
            pen.moveTo((100, -100))
            pen.lineTo((100, top))
            pen.lineTo((900, top))
            pen.lineTo((900, -100))
            pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (1000, 100) for n in names})
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({"familyName": family, "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=880, sTypoDescender=-120, usWinAscent=880, usWinDescent=120)
    fb.setupPost()
    out = io.BytesIO()
    fb.save(out)
    return out.getvalue()


class FontServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fontsrv-test-")
        cls.cjk = make_font("TestHan", "中文A")
        cls.extra = make_font("TestExtra", "한😀中", tall="😀")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.data = tempfile.mkdtemp(dir=self.tmp)
        self.lib = Library(self.data, system_fonts=False)

    # ---- library and replies ----------------------------------------------
    def test_upload_order_and_reply(self):
        self.lib.upload("testhan.ttf", self.cjk)
        self.lib.upload("testextra.ttf", self.extra)
        self.assertEqual(self.lib.order, ["testhan", "testextra"])
        gs = GlyphService(self.lib)
        recs, (height, hsh) = decode_reply(gs.reply("simsun", 16, [0x4E2D, 0xD55C, 0x1F600, 0x10FFFD]))
        self.assertEqual(height, 16)
        self.assertNotEqual(hsh, 0)
        by_cp = {r[0]: r for r in recs}
        self.assertEqual([r[0] for r in recs], [0x4E2D, 0xD55C, 0x1F600, 0x10FFFD])   # the request's order
        self.assertTrue(by_cp[0x4E2D][2] > 0 and by_cp[0xD55C][2] > 0 and by_cp[0x1F600][2] > 0)
        self.assertEqual(by_cp[0x10FFFD][1:3], (0, 0))      # no font has it
        self.assertEqual(len(by_cp[0x4E2D][3]), by_cp[0x4E2D][2] * 16)

    def test_requested_face_first(self):
        self.lib.upload("testhan.ttf", self.cjk)
        self.lib.upload("testextra.ttf", self.extra)
        gs = GlyphService(self.lib)
        # 中 is in both: the face the board names comes first, else the order
        self.assertEqual(gs.glyph(self.lib.chain("testextra"), 0x4E2D, 16)[3], "testextra")
        self.assertEqual(gs.glyph(self.lib.chain("simsun"), 0x4E2D, 16)[3], "testhan")
        self.lib.set_order(["testextra", "testhan"])
        self.assertEqual(gs.glyph(self.lib.chain("simsun"), 0x4E2D, 16)[3], "testextra")

    def test_tall_glyph_fits_the_cell(self):
        self.lib.upload("testextra.ttf", self.extra)
        gs = GlyphService(self.lib)
        advance, bpr, rows, _ = gs.glyph(self.lib.chain(""), 0x1F600, 16)
        self.assertGreater(bpr, 0)
        # the square, 29 rows tall at 16 px, is drawn smaller and centred: most of the
        # cell inked, nothing cut at the top or bottom
        inked = [y for y in range(16) if any(rows[y * bpr:(y + 1) * bpr])]
        self.assertGreaterEqual(len(inked), 12)
        self.assertEqual(inked, list(range(inked[0], inked[-1] + 1)))
        self.assertTrue(inked[0] >= 0 and inked[-1] <= 15)

    def test_hash_changes_with_fonts(self):
        self.lib.upload("testhan.ttf", self.cjk)
        h1 = self.lib.chain_hash("simsun", 16)
        self.lib.upload("testextra.ttf", self.extra)
        self.assertNotEqual(h1, self.lib.chain_hash("simsun", 16))
        self.assertNotEqual(self.lib.chain_hash("simsun", 16), self.lib.chain_hash("simsun", 24))

    def test_remove_and_bad_uploads(self):
        self.lib.upload("testhan.ttf", self.cjk)
        self.lib.remove("testhan")
        self.assertEqual(self.lib.order, [])
        with self.assertRaises(ValueError):
            self.lib.upload("x.txt", b"hello")
        with self.assertRaises(ValueError):
            self.lib.upload("x.ttf", b"not a font")
        with self.assertRaises(ValueError):
            self.lib.set_order(["nosuch"])

    def test_parse_cps(self):
        self.assertEqual(parse_cps("4e2d, 1F600,4e2d"), [0x4E2D, 0x1F600])
        for bad in ("", "zz", "d800", "110000", ",".join("4e%02x" % i for i in range(65))):
            with self.assertRaises(GlyphError):
                parse_cps(bad)

    # ---- HTTP ---------------------------------------------------------------
    def test_http(self):
        self.lib.upload("testhan.ttf", self.cjk)
        srv = FontServer(("127.0.0.1", 0), self.lib, "secret-pw")
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/font/glyphs?face=simsun&size=16&cps=4e2d,41") as r:
                self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
                recs, _ = decode_reply(r.read())
                self.assertEqual([x[0] for x in recs], [0x4E2D, 0x41])
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/font/glyphs?cps=zz")
            self.assertEqual(e.exception.code, 400)
            with urllib.request.urlopen(base + "/font/preview?text=%E4%B8%AD") as r:
                self.assertEqual(r.read()[:4], b"\x89PNG")
            # administration needs a token
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/admin/fonts")
            self.assertEqual(e.exception.code, 401)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + "/admin/login", data=b'{"password":"no"}'))
            self.assertEqual(e.exception.code, 403)
            with urllib.request.urlopen(urllib.request.Request(base + "/admin/login", data=b'{"password":"secret-pw"}')) as r:
                token = json.loads(r.read())["token"]
            auth = {"Authorization": "Bearer " + token}
            req = urllib.request.Request(base + "/admin/upload?name=testextra.ttf", data=self.extra, headers=auth)
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.loads(r.read())["font"]["id"], "testextra")
            with urllib.request.urlopen(urllib.request.Request(base + "/admin/fonts", headers=auth)) as r:
                self.assertEqual(json.loads(r.read())["order"], ["testhan", "testextra"])
            req = urllib.request.Request(base + "/admin/order", data=b'{"order":["testextra"]}', headers=auth)
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.loads(r.read())["order"], ["testextra"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
