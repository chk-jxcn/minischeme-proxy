"""HTTP side of the font server (a service of its own, beside the app proxy).

Public, no password (boards and the WebREPL fetch glyphs without one):
    GET /font/glyphs?face=simsun&size=16&cps=4e2d,1f600   glyph reply (glyphs.py)
    GET /font/faces                                         the enabled fonts (JSON)
    GET /font/preview?face=simsun&size=16&text=한국어       a PNG of the text
    GET /health
Administration (the page at /, a token from POST /admin/login):
    POST /admin/login            {"password": "..."} -> {"token": "..."}
    GET  /admin/fonts            every font, uploaded and (on a test PC) installed
    POST /admin/upload?name=F    the font file as the body
    POST /admin/order            {"order": [ids]}: the enabled fonts, in order
    POST /admin/remove?id=ID     an uploaded font
    GET  /admin/stats
"""

import hmac
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .admin_page import ADMIN_HTML
from .glyphs import GlyphError, GlyphService, parse_cps, parse_size

TOKEN_TTL = 12 * 3600
MAX_JSON = 64 * 1024


class FontServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, library, admin_password, login_delay=0, public=False):
        super().__init__(addr, FontHandler)
        self.library = library
        self.glyphs = GlyphService(library)
        self.admin_password = admin_password
        self.login_delay = login_delay
        self.public = public
        self.tokens = {}
        self.tokens_lock = threading.Lock()
        self.started = time.time()

    def new_token(self):
        token = secrets.token_urlsafe(24)
        with self.tokens_lock:
            now = time.time()
            self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
            self.tokens[token] = now + TOKEN_TTL
        return token

    def token_ok(self, token):
        with self.tokens_lock:
            exp = self.tokens.get(token or "")
            return bool(exp and exp > time.time())


class FontHandler(BaseHTTPRequestHandler):
    server_version = "MiniSchemeFontServer/1"

    def log_message(self, fmt, *args):   # quiet: one line per glyph request is noise
        pass

    # ---- replies ----------------------------------------------------------
    def _send(self, status, body, ctype="application/json; charset=utf-8", cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")   # the WebREPL page fetches directly
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, msg):
        self._send(status, {"success": False, "error": msg})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self, limit):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > limit:
            return None
        return self.rfile.read(n) if n else b""

    def _admin(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not self.server.token_ok(token):
            self._error(401, "log in first")
            return False
        return True

    # ---- routes -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        q = lambda k, d="": qs.get(k, [d])[0]
        lib = self.server.library
        try:
            if url.path == "/font/glyphs":
                body = self.server.glyphs.reply(q("face"), parse_size(q("size")), parse_cps(q("cps")))
                return self._send(200, body, "application/octet-stream", cache="public, max-age=3600")
            if url.path == "/font/faces":
                fonts = [f for f in lib.list() if f["enabled"] or f["source"] == "upload"]
                return self._send(200, {"success": True, "fonts": fonts})
            if url.path == "/font/preview":
                text = q("text", "中文 한국어 日本語 😀")
                png = self.server.glyphs.preview(q("face"), parse_size(q("size")), text)
                return self._send(200, png, "image/png")
            if url.path == "/health":
                return self._send(200, {"ok": True, "service": "font", "fonts": len(lib.order),
                                        "uptime": int(time.time() - self.server.started)})
            if url.path in ("/", "/admin"):
                return self._send(200, ADMIN_HTML, "text/html; charset=utf-8")
            if url.path == "/admin/fonts":
                if self._admin():
                    self._send(200, {"success": True, "fonts": lib.list(), "order": lib.order})
                return None
            if url.path == "/admin/stats":
                if self._admin():
                    self._send(200, {"success": True, **self.server.glyphs.stats,
                                     "cached": len(self.server.glyphs.cache)})
                return None
            return self._error(404, "not found")
        except GlyphError as e:
            return self._error(400, str(e))

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        lib = self.server.library
        if url.path == "/admin/login":
            body = self._body(MAX_JSON)
            try:
                pw = str(json.loads(body or b"{}").get("password", ""))
            except ValueError:
                pw = ""
            if self.server.admin_password and hmac.compare_digest(pw.encode(), self.server.admin_password.encode()):
                return self._send(200, {"success": True, "token": self.server.new_token()})
            time.sleep(self.server.login_delay)
            return self._error(403, "wrong password")
        if not self._admin():
            return None
        try:
            if url.path == "/admin/upload":
                from .library import MAX_UPLOAD
                data = self._body(MAX_UPLOAD)
                if data is None:
                    return self._error(413, "too large")
                return self._send(200, {"success": True, "font": lib.upload(qs.get("name", [""])[0], data)})
            if url.path == "/admin/order":
                body = self._body(MAX_JSON)
                order = json.loads(body or b"{}").get("order", [])
                if not isinstance(order, list):
                    return self._error(400, "order: a list of font ids")
                return self._send(200, {"success": True, "order": lib.set_order([str(i) for i in order])})
            if url.path == "/admin/remove":
                lib.remove(qs.get("id", [""])[0])
                return self._send(200, {"success": True})
        except (ValueError, OSError) as e:
            return self._error(400, str(e))
        return self._error(404, "not found")
