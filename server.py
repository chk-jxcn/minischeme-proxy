#!/usr/bin/env python3
"""
MiniScheme WASM WebREPL Local Server, App Proxy & Windows System Tray Application.

Features:
- Default port 8087 with automatic fallback to next available port if 8087 is busy.
- Unified HTTP server serving WebREPL static assets, WASM compiler/simulator,
  font generator, and App Proxy / Protocol Converter (/app/*, /api/fetch, /channel/*).
- Native Windows System Tray icon (via Win32 ctypes) displaying LAN IP and listening
  port, with one-click browser launch and right-click context menu.
- Compatible with PyInstaller single-file executable (--onefile --noconsole).
"""

import argparse
import base64
import configparser
import http.server
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

DEFAULT_PORT = int(os.environ.get("WEBREPL_PORT", "8087"))

# PyInstaller bundle directory vs writable runtime directory
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BASE_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR

TOOLS_DIR = os.path.join(BASE_DIR, "tools")
if not os.path.exists(TOOLS_DIR):
    TOOLS_DIR = os.path.join(os.path.dirname(BASE_DIR), "tools")

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if os.path.exists(TOOLS_DIR) and TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

_font_lock = threading.Lock()
ACTIVE_PORT = DEFAULT_PORT


# ---------------------------------------------------------------------------
# 1. Network IP Discovery & Port Binding with Automatic Fallback
# ---------------------------------------------------------------------------

def get_local_ips():
    """Return a list of non-loopback IPv4 addresses for this machine."""
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            if primary and not primary.startswith("127."):
                ips.append(primary)
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    if not ips:
        ips.append("127.0.0.1")
    return ips


def get_primary_ip():
    """Return the primary LAN IPv4 address of the host."""
    return get_local_ips()[0]


class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    # Strict check on Windows so busy ports trigger fallback; allow reuse on Linux/Unix for service restarts
    allow_reuse_address = (os.name != "nt")


def create_server_with_fallback(host, preferred_port, handler_cls, max_tries=50):
    """Try binding to preferred_port (default 8087); if busy, try subsequent ports."""
    for offset in range(max_tries):
        port = preferred_port + offset
        try:
            httpd = ReusableThreadingHTTPServer((host, port), handler_cls)
            return httpd, port
        except OSError:
            continue
    # Final fallback: let OS pick any available ephemeral port
    httpd = ReusableThreadingHTTPServer((host, 0), handler_cls)
    return httpd, httpd.server_address[1]


# ---------------------------------------------------------------------------
# 2. Font Generator & Persistent Native VM Simulator
# ---------------------------------------------------------------------------

def font_engine():
    """Load tools/make_font.py dynamically."""
    import make_font
    return make_font


def font_faces():
    with _font_lock:
        faces = font_engine().list_faces()
    return {
        "success": True,
        "faces": [
            {k: f[k] for k in ("id", "family", "family_zh", "file", "cjk", "glyphs")}
            for f in faces
        ],
    }


def font_make(req):
    """{face, size, presets, chars, name, y_offset, spacing, bpp} -> MFNT (base64) + stats."""
    mf = font_engine()
    with _font_lock:
        faces = {f["file"].lower(): f["path"] for f in mf.list_faces()}
    face = str(req.get("face", ""))
    face = faces.get(face.lower(), face)
    if os.path.isabs(face) and face not in faces.values():
        raise mf.FontError("unknown font file")
    chars = mf.collect_chars(req.get("presets") or [], str(req.get("chars") or ""))
    y = req.get("y_offset")
    data, info = mf.build_font(
        face,
        int(req.get("size", 16)),
        chars,
        (req.get("name") or None),
        None if y in (None, "") else int(y),
        str(req.get("spacing") or "font"),
        int(req.get("bpp") or 1),
    )
    info["face"] = os.path.basename(info["face"])
    return {"success": True, **info, "mfnt": base64.b64encode(data).decode("ascii")}


SIM_EXE = (
    os.path.join(BASE_DIR, "sim_esp32_v2.exe")
    if os.path.exists(os.path.join(BASE_DIR, "sim_esp32_v2.exe"))
    else os.path.join(BASE_DIR, "sim_esp32.exe")
)


class PersistentVM:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        if os.path.exists(SIM_EXE):
            self._start()

    def _start(self):
        if not os.path.exists(SIM_EXE):
            self.proc = None
            return
        try:
            self.proc = subprocess.Popen(
                [SIM_EXE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=BASE_DIR,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self.proc = None

    def execute_hex(self, hex_str):
        if not hex_str or not hex_str.strip():
            return {"success": False, "error": "empty bytecode"}
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                self._start()
            if not self.proc:
                return {"success": False, "error": "VM process unavailable"}
            try:
                self.proc.stdin.write(hex_str.strip() + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if not line:
                    return {"success": False, "error": "Device simulator exited unexpectedly"}
                return json.loads(line)
            except Exception as e:
                return {"success": False, "error": str(e)}


vm_node = PersistentVM()


# ---------------------------------------------------------------------------
# 3. Integrated App Proxy & Protocol Converter Context & Server Configuration
# ---------------------------------------------------------------------------

ALL_COMPONENTS = {"webrepl", "proxy", "channel", "portal", "font", "vm"}


class ServerConfig:
    def __init__(self):
        self.protocol_mode = "unencrypted"
        self.components = set(ALL_COMPONENTS)
        self.admin_login_delay = 0
        self.config_file = os.path.join(DATA_DIR, "config.ini")
        self.init_defaults()

    def init_defaults(self):
        cfg = configparser.ConfigParser()
        if os.path.exists(self.config_file):
            try:
                cfg.read(self.config_file, encoding="utf-8")
            except Exception:
                pass
        self.protocol_mode = (
            os.getenv("MINISCHEME_PROTOCOL_MODE")
            or cfg.get("security", "protocol_mode", fallback="")
            or cfg.get("server", "protocol_mode", fallback="")
            or "unencrypted"
        ).lower().strip()
        if self.protocol_mode not in ("unencrypted", "encrypted", "both"):
            self.protocol_mode = "unencrypted"

        comp_str = (
            os.getenv("MINISCHEME_COMPONENTS")
            or cfg.get("server", "components", fallback="")
            or "all"
        )
        self.set_components_from_str(comp_str)

        try:
            self.admin_login_delay = int(
                os.getenv("MINISCHEME_ADMIN_LOGIN_DELAY")
                or cfg.get("security", "admin_login_delay", fallback="")
                or 0
            )
        except Exception:
            self.admin_login_delay = 0

    def set_components_from_str(self, comp_str):
        if not comp_str or comp_str.lower().strip() in ("all", "*"):
            self.components = set(ALL_COMPONENTS)
        else:
            comps = {c.strip().lower() for c in comp_str.split(",") if c.strip()}
            self.components = comps.intersection(ALL_COMPONENTS)


server_config = ServerConfig()


class IntegratedProxyContext:
    def __init__(self):
        self.db = None
        self.master_key_1 = None
        self.admin_password = "admin"
        self.admin_login_delay = 0
        self.recovery_challenges = {}
        self._lock = threading.Lock()

    def ensure_initialized(self):
        if self.db is not None:
            return
        with self._lock:
            if self.db is not None:
                return
            try:
                from proxy.database import Database
                cfg_path = server_config.config_file
                cfg = configparser.ConfigParser()
                if os.path.exists(cfg_path):
                    try:
                        cfg.read(cfg_path, encoding="utf-8")
                    except Exception:
                        pass
                mk1_hex = os.getenv("MINISCHEME_MASTER_KEY_1") or cfg.get("security", "master_key_1", fallback="")
                if mk1_hex and len(mk1_hex) == 64:
                    self.master_key_1 = bytes.fromhex(mk1_hex)
                else:
                    self.master_key_1 = secrets.token_bytes(32)
                self.admin_password = (
                    os.getenv("MINISCHEME_ADMIN_PASSWORD")
                    or cfg.get("security", "admin_password", fallback="")
                    or "admin"
                )
                self.admin_login_delay = server_config.admin_login_delay
                db_path = os.path.join(DATA_DIR, "minischeme_proxy.db")
                self.db = Database(db_path)
            except Exception as e:
                print(f"[Warn] Proxy DB init fallback: {e}")


proxy_ctx = IntegratedProxyContext()
_release_cache = {}
_release_lock = threading.Lock()


def get_latest_github_release(repo="minischeme-webrepl", ttl=120):
    """Dynamically resolve the latest release metadata and asset URLs for a chk-jxcn repository."""
    now = time.time()
    with _release_lock:
        cached = _release_cache.get(repo)
        if cached and cached[1] > now:
            return cached[0]

    api_url = f"https://api.github.com/repos/chk-jxcn/{repo}/releases/latest"
    fallback_base = f"https://github.com/chk-jxcn/{repo}/releases/latest/download/"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "MiniScheme-WebREPL/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assets = {}
        for a in data.get("assets") or []:
            if a.get("name") and a.get("browser_download_url"):
                assets[a["name"]] = {
                    "url": a["browser_download_url"],
                    "size": a.get("size", 0),
                }
        info = {
            "success": True,
            "repo": repo,
            "tag_name": data.get("tag_name") or "latest",
            "name": data.get("name") or data.get("tag_name") or "latest",
            "published_at": data.get("published_at") or "",
            "html_url": data.get("html_url") or f"https://github.com/chk-jxcn/{repo}/releases/latest",
            "download_base": fallback_base,
            "assets": assets,
        }
        with _release_lock:
            _release_cache[repo] = (info, now + ttl)
        return info
    except Exception as e:
        return {
            "success": True,
            "repo": repo,
            "tag_name": "latest",
            "name": "latest",
            "html_url": f"https://github.com/chk-jxcn/{repo}/releases/latest",
            "download_base": fallback_base,
            "assets": {},
            "fallback": True,
            "note": str(e),
        }


def fetch_latest_release_asset(repo, asset_name):
    """Fetch an asset from local bundle if available, otherwise from latest GitHub release."""
    ctype = "application/json" if asset_name.endswith(".json") else "application/octet-stream"

    # Local bundle first (fastest, guaranteed match with current build)
    sub = "firmware" if repo == "minischeme-esp32" else "apps"
    local_candidate = os.path.join(BASE_DIR, sub, asset_name)
    if os.path.isfile(local_candidate):
        with open(local_candidate, "rb") as f:
            return 200, ctype, f.read()

    rel = get_latest_github_release(repo)
    asset_info = (rel.get("assets") or {}).get(asset_name)
    urls = []
    if asset_info and asset_info.get("url"):
        urls.append(asset_info["url"])
    urls.append(f"https://github.com/chk-jxcn/{repo}/releases/latest/download/{asset_name}")

    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MiniScheme-WebREPL/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                body = resp.read(32 * 1024 * 1024)
                if len(body) > 0:
                    return 200, ctype, body
        except Exception:
            continue

    return 404, "application/json", b'{"error":"release asset not found"}'


def handle_proxy_endpoint(path_with_query, client_ip=""):
    """Dispatch /app/*, /health, and /market/* requests in-process."""
    parsed = urllib.parse.urlparse(path_with_query)
    path = parsed.path

    if path.startswith("/market/"):
        rel = path[len("/market/"):].lstrip("/")
        local_candidate = os.path.join(BASE_DIR, "apps", rel)
        if os.path.isfile(local_candidate):
            ctype = "application/json" if rel.endswith(".json") else "application/octet-stream"
            with open(local_candidate, "rb") as f:
                return 200, ctype, f.read()
        if "/" not in rel:
            status, ctype, body = fetch_latest_release_asset("minischeme-apps", rel)
            if status == 200:
                return status, ctype, body
        return 404, "application/json", b'{"error":"market file not found"}'

    if path == "/fetch":
        qs = urllib.parse.parse_qs(parsed.query)
        target_url = (qs.get("url") or [""])[0]
        if not target_url.startswith(("http://", "https://")):
            return 400, "text/plain", b"missing or invalid url"
        p_target = urllib.parse.urlparse(target_url)
        if p_target.path.startswith("/app/") or p_target.path == "/health":
            sub_url = p_target.path + ("?" + p_target.query if p_target.query else "")
            from proxy.endpoints import handle_app_query
            return handle_app_query(sub_url, client_ip=client_ip)
        try:
            req = urllib.request.Request(target_url, headers={"User-Agent": "MiniScheme-Proxy/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read(512 * 1024)
                ctype = resp.headers.get("Content-Type", "application/json")
                return 200, ctype, body
        except Exception as e:
            return 502, "text/plain", f"fetch error: {e}".encode("utf-8")

    from proxy.endpoints import handle_app_query
    return handle_app_query(path_with_query, client_ip=client_ip)


# ---------------------------------------------------------------------------
# 4. Unified WebREPL + App Proxy HTTP Request Handler
# ---------------------------------------------------------------------------

class WebReplHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def translate_path(self, path):
        clean_path = super().translate_path(path)
        if os.path.exists(clean_path):
            return clean_path

        # Fallback to categorized subdirectories if requested without folder prefix
        rel = os.path.relpath(clean_path, BASE_DIR).replace("\\", "/")
        if rel in ("app.msbc", "apps/app.msbc"):
            for cand_cal in (
                os.path.join(BASE_DIR, "apps", "calendar.msbc"),
                os.path.join(BASE_DIR, "apps", "app.msbc"),
                os.path.join(BASE_DIR, "app.msbc"),
            ):
                if os.path.exists(cand_cal):
                    return cand_cal

        for sub in ("js", "css", "wasm", "data", "tests", "apps", "firmware", "fonts"):
            candidate = os.path.join(BASE_DIR, sub, rel)
            if os.path.exists(candidate):
                return candidate
        return clean_path

    def log_message(self, format, *args):
        # Avoid crashing when running without a console window (--noconsole)
        if sys.stderr is not None:
            try:
                super().log_message(format, *args)
            except Exception:
                pass

    def send_header(self, keyword, value):
        # Prevent duplicate CORS headers that violate browser CORS policies (e.g. multiple "*, *")
        k_lower = keyword.lower()
        if k_lower in ("access-control-allow-origin", "access-control-allow-private-network"):
            prefix = f"{k_lower}:".encode("latin-1")
            if hasattr(self, "_headers_buffer") and any(
                h.lower().startswith(prefix) for h in self._headers_buffer
            ):
                return
        super().send_header(keyword, value)

    def _get_client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            ip = xff.split(",")[0].strip()
            if ":" in ip and not ip.startswith("[") and ip.count(":") == 1:
                ip = ip.split(":")[0].strip()
            if ip:
                return ip
        x_real = self.headers.get("X-Real-IP")
        if x_real:
            ip = x_real.strip()
            if ":" in ip and not ip.startswith("[") and ip.count(":") == 1:
                ip = ip.split(":")[0].strip()
            if ip:
                return ip
        if hasattr(self, "client_address") and self.client_address:
            return str(self.client_address[0]).strip()
        return ""

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed_self = urllib.parse.urlparse(self.path)
        req_path = parsed_self.path

        # 1. Server Discovery & Status API (/api/server_info)
        if req_path == "/api/server_info":
            ips = get_local_ips()
            primary_ip = ips[0] if ips else "127.0.0.1"

            # Check if accessed via public IP or hostname in Host header
            raw_host = (self.headers.get("Host") or "").split(":")[0].strip()
            from proxy.endpoints import is_private_ip
            is_public_host = False
            effective_ip = primary_ip

            if raw_host and raw_host not in ("localhost", "0.0.0.0", "127.0.0.1"):
                if not is_private_ip(raw_host):
                    effective_ip = raw_host
                    is_public_host = True
                    # Put public host at top and suppress internal cloud VPC IPs (e.g. 10.0.0.4)
                    filtered_ips = [raw_host] + [ip for ip in ips if not ip.startswith("10.")]
                    ips = filtered_ips
                elif is_private_ip(primary_ip) and primary_ip.startswith("10.") and not raw_host.startswith("10."):
                    effective_ip = raw_host

            if is_public_host or not is_private_ip(effective_ip):
                lan_target = f"http://{effective_ip}:{ACTIVE_PORT}"
            else:
                lan_target = f"http://{primary_ip}:{ACTIVE_PORT}"

            self._json_reply({
                "success": True,
                "ip": effective_ip,
                "ips": ips,
                "port": ACTIVE_PORT,
                "is_public": is_public_host,
                "local_url": f"http://127.0.0.1:{ACTIVE_PORT}",
                "lan_url": lan_target,
                "proxy_url": lan_target,
                "converter_url": lan_target,
                "protocol_mode": server_config.protocol_mode,
                "components": sorted(list(server_config.components)),
                "admin_login_delay": server_config.admin_login_delay,
                "frozen": bool(getattr(sys, "frozen", False)),
            })
            return

        if req_path == "/health":
            self._json_reply({
                "status": "ok",
                "protocol_mode": server_config.protocol_mode,
                "components": sorted(list(server_config.components)),
                "time": int(time.time()),
            })
            return

        if req_path == "/api/preset_fonts":
            if "font" not in server_config.components:
                self.send_error(404, "Font component is disabled on this server")
                return
            idx_path = os.path.join(BASE_DIR, "fonts", "index.json")
            if os.path.exists(idx_path):
                try:
                    with open(idx_path, "r", encoding="utf-8") as f:
                        return self._json_reply(json.load(f))
                except Exception:
                    pass
            return self._json_reply([])

        # 1b. Dynamic GitHub Latest Release Discovery & Asset Proxy
        if req_path == "/api/latest_release":
            qs = urllib.parse.parse_qs(parsed_self.query)
            repo = (qs.get("repo") or ["minischeme-webrepl"])[0]
            if repo not in ("minischeme-webrepl", "minischeme-esp32", "minischeme-apps", "minischeme"):
                return self._json_reply({"success": False, "error": "unsupported repository"}, 400)
            info = get_latest_github_release(repo)
            return self._json_reply(info, 200 if info.get("success") else 502)

        if req_path == "/api/release_asset":
            qs = urllib.parse.parse_qs(parsed_self.query)
            repo = (qs.get("repo") or ["minischeme-esp32"])[0]
            asset = (qs.get("asset") or [""])[0]
            if repo not in ("minischeme-webrepl", "minischeme-esp32", "minischeme-apps") or not asset or "/" in asset:
                self.send_error(400, "invalid repo or asset")
                return
            status, ctype, body = fetch_latest_release_asset(repo, asset)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        # 2. Direct App Proxy & Market endpoints (/app/*, /market/*, /fetch)
        if (
            req_path.startswith("/app/")
            or req_path.startswith("/market/")
            or req_path == "/fetch"
        ):
            if "proxy" not in server_config.components:
                self.send_error(404, "Proxy component is disabled on this server")
                return
            if server_config.protocol_mode == "encrypted":
                self._json_reply({
                    "success": False,
                    "error": "Plaintext protocol is disabled on this server. Encrypted channel protocol (MSC1) is required."
                }, status=403)
                return
            status, ctype, body = handle_proxy_endpoint(self.path, client_ip=self._get_client_ip())
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            self.wfile.write(body)
            return

        # 3. Simulator CORS proxy (/api/fetch?url=... or /api/lhm?url=...)
        if self.path.startswith("/api/fetch?") or self.path.startswith("/api/lhm?"):
            if "proxy" not in server_config.components:
                self.send_error(404, "Proxy component is disabled on this server")
                return
            if server_config.protocol_mode == "encrypted":
                self._json_reply({
                    "success": False,
                    "error": "Plaintext simulator proxy is disabled on this server. Encrypted channel protocol is required."
                }, status=403)
                return
            query = urllib.parse.parse_qs(parsed_self.query)
            url = (query.get("url") or [""])[0]
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme in ("http", "https") and (
                parsed.path.startswith("/app/")
                or parsed.path == "/fetch"
                or parsed.path.startswith("/market/")
                or parsed.path == "/health"
            ):
                p_query = parsed.path + ("?" + parsed.query if parsed.query else "")
                status, ctype, body = handle_proxy_endpoint(p_query, client_ip=self._get_client_ip())
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.end_headers()
                self.wfile.write(body)
                return

            image = parsed.path.endswith((".msbc", ".fasl"))
            if parsed.scheme not in ("http", "https") or not (parsed.path.endswith(".json") or image):
                self.send_error(400, "url must be http(s)://<host>/<path>.json, an app image or an app proxy URL (/app/...)")
                return
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MiniScheme-WebREPL/1.0"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    body = resp.read(512 * 1024)
                status = 200
            except Exception as e:
                body = f"{url} unreachable: {e}".encode("utf-8")
                status = 502
            self.send_response(status)
            self.send_header(
                "Content-Type",
                ("application/octet-stream" if image else "application/json") if status == 200 else "text/plain",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        # 4. Font Generator API
        if req_path == "/api/font/faces":
            if "font" not in server_config.components:
                self.send_error(404, "Font component is disabled on this server")
                return
            self._json_reply(self._font_call(font_faces))
            return

        # 5. User & Admin Portals from Proxy
        if req_path in ("/user", "/user/", "/admin", "/admin/") or req_path.startswith(("/api/user/", "/api/admin/")):
            if "portal" not in server_config.components:
                self.send_error(404, "Portal component is disabled on this server")
                return
            proxy_ctx.ensure_initialized()
            from proxy.user_portal import USER_HTML, generate_user_nonce, check_user_session
            from proxy.admin_portal import ADMIN_HTML, create_admin_session, verify_admin_session
            qs = urllib.parse.parse_qs(parsed_self.query)
            if req_path in ("/user", "/user/"):
                body = USER_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if req_path in ("/admin", "/admin/"):
                body = ADMIN_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if req_path == "/api/user/nonce":
                mac = qs.get("mac", [""])[0]
                if not mac:
                    return self._json_reply({"success": False, "error": "missing mac"}, 400)
                return self._json_reply({"success": True, "nonce": generate_user_nonce(mac)})
            if req_path == "/api/user/data":
                mac = qs.get("mac", [""])[0]
                token = qs.get("token", [""])[0]
                if not check_user_session(token, mac):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                return self._json_reply({"success": True, "items": proxy_ctx.db.get_app_data(mac, masked=True)})
            if req_path == "/api/admin/status":
                mode = server_config.protocol_mode
                pwd = proxy_ctx.admin_password
                auth_req = (mode == "encrypted" or pwd != "admin")
                resp = {"success": True, "auth_required": auth_req, "protocol_mode": mode}
                if not auth_req:
                    resp["token"] = create_admin_session()
                return self._json_reply(resp)
            if req_path == "/api/admin/devices":
                token = qs.get("token", [""])[0]
                if not verify_admin_session(token):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                return self._json_reply({"success": True, "devices": proxy_ctx.db.admin_list_devices()})
            if req_path == "/api/admin/device/data":
                token = qs.get("token", [""])[0]
                mac = qs.get("mac", [""])[0]
                if not verify_admin_session(token):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                return self._json_reply({"success": True, "items": proxy_ctx.db.get_app_data(mac, masked=False)})
            if req_path == "/api/user/logs":
                mac = qs.get("mac", [""])[0]
                token = qs.get("token", [""])[0]
                if not check_user_session(token, mac):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                return self._json_reply({"success": True, "items": proxy_ctx.db.get_device_logs(mac)})

        if "webrepl" not in server_config.components:
            body = (
                b"<!DOCTYPE html><html><head><meta charset='utf-8'><title>MiniScheme Proxy</title></head>"
                b"<body style='font-family:-apple-system,sans-serif;padding:40px;line-height:1.6;color:#1e293b;background:#f8fafc;'>"
                b"<h2>MiniScheme App Proxy &amp; Protocol Converter</h2>"
                b"<p>WebREPL static web interface is disabled on this server (Proxy-only mode).</p>"
                b"<p>WebREPL is officially hosted on GitHub Pages: "
                b"<a href='https://chk-jxcn.github.io/minischeme-webrepl/'>https://chk-jxcn.github.io/minischeme-webrepl/</a></p>"
                b"<p><a href='/admin'>Admin Portal</a> &middot; <a href='/user'>User Portal</a></p>"
                b"</body></html>"
            )
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        super().do_GET()

    def _font_call(self, fn, *args):
        try:
            return fn(*args)
        except ImportError as e:
            return {"success": False, "error": f"font generator needs Pillow and fontTools: {e}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _json_reply(self, res, status=200):
        body = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed_self = urllib.parse.urlparse(self.path)
        req_path = parsed_self.path

        if req_path == "/api/font/make":
            if "font" not in server_config.components:
                self.send_error(404, "Font component is disabled on this server")
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                req = json.loads(body.decode("utf-8"))
            except ValueError:
                req = None
            self._json_reply(
                self._font_call(font_make, req)
                if isinstance(req, dict)
                else {"success": False, "error": "expected a JSON object"}
            )
            return

        if req_path == "/api/execute_bytecode":
            if "vm" not in server_config.components:
                self.send_error(404, "VM component is disabled on this server")
                return
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                req_data = json.loads(body)
                hex_str = req_data.get("bytecode_hex", "")
                res = vm_node.execute_hex(hex_str)
            except Exception as e:
                res = {"success": False, "error": f"Invalid request: {e}"}
            self._json_reply(res)
            return

        if req_path.startswith(("/api/user/", "/api/admin/")):
            if "portal" not in server_config.components:
                self.send_error(404, "Portal component is disabled on this server")
                return
            proxy_ctx.ensure_initialized()
            from proxy.user_portal import (
                verify_user_login,
                verify_simulator_login,
                check_user_session,
                remove_user_session,
            )
            from proxy.admin_portal import (
                create_admin_session,
                verify_admin_session,
                remove_admin_session,
            )
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception as e:
                return self._json_reply({"success": False, "error": f"invalid json: {e}"}, 400)

            if req_path == "/api/admin/login":
                if server_config.admin_login_delay > 0:
                    time.sleep(server_config.admin_login_delay)
                pwd = payload.get("password", "")
                if pwd == proxy_ctx.admin_password:
                    token = create_admin_session()
                    return self._json_reply({"success": True, "token": token})
                return self._json_reply({"success": False, "error": "password incorrect"}, 403)

            if req_path == "/api/admin/logout":
                token = payload.get("token", "")
                remove_admin_session(token)
                return self._json_reply({"success": True})

            if req_path == "/api/admin/device/clear":
                token = payload.get("token", "")
                if not verify_admin_session(token):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                mac = payload.get("mac", "")
                proxy_ctx.db.clear_all_app_data(mac)
                return self._json_reply({"success": True})

            if req_path == "/api/admin/device/delete":
                token = payload.get("token", "")
                if not verify_admin_session(token):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                mac = payload.get("mac", "")
                proxy_ctx.db.delete_device_account(mac)
                return self._json_reply({"success": True})

            if req_path == "/api/admin/preset":
                token = payload.get("token", "")
                if not verify_admin_session(token):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                mac = payload.get("mac", "")
                app_id = payload.get("app_id", "")
                key = payload.get("key", "")
                val = payload.get("value", "")
                proxy_ctx.db.set_app_data(mac, app_id, key, val)
                return self._json_reply({"success": True})

            if req_path == "/api/user/sim_register":
                uuid = payload.get("uuid", "")
                if not uuid:
                    return self._json_reply({"success": False, "error": "missing uuid"}, 400)
                client_ip = self._get_client_ip()
                proxy_ctx.db.register_simulator(uuid, client_ip)
                return self._json_reply({"success": True})

            if req_path == "/api/user/sim_login":
                uuid = payload.get("uuid", "")
                if not uuid:
                    return self._json_reply({"success": False, "error": "missing uuid"}, 400)
                client_ip = self._get_client_ip()
                ok, res = verify_simulator_login(uuid, proxy_ctx.db, client_ip)
                if ok:
                    return self._json_reply({"success": True, "token": res, "mac": uuid.upper()})
                return self._json_reply({"success": False, "error": res}, 400)

            if req_path == "/api/user/login":
                mac = payload.get("mac", "")
                client_hash = payload.get("hash", "")
                nonce = payload.get("nonce", "")
                ok, res = verify_user_login(mac, client_hash, nonce, proxy_ctx.db)
                if ok:
                    return self._json_reply({"success": True, "token": res})
                return self._json_reply({"success": False, "error": res}, 403)

            if req_path == "/api/user/data/clear":
                qs = urllib.parse.parse_qs(parsed_self.query)
                mac = payload.get("mac") or qs.get("mac", [""])[0]
                token = payload.get("token") or qs.get("token", [""])[0]
                app_id = payload.get("app_id") or qs.get("app_id", [""])[0]
                if not check_user_session(token, mac):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                if not app_id or app_id == "all":
                    proxy_ctx.db.clear_all_app_data(mac)
                else:
                    proxy_ctx.db.clear_app_data(mac, app_id)
                return self._json_reply({"success": True})

            if req_path == "/api/user/account/delete":
                qs = urllib.parse.parse_qs(parsed_self.query)
                mac = payload.get("mac") or qs.get("mac", [""])[0]
                token = payload.get("token") or qs.get("token", [""])[0]
                if not check_user_session(token, mac):
                    return self._json_reply({"success": False, "error": "unauthorized"}, 401)
                proxy_ctx.db.delete_device_account(mac)
                remove_user_session(token)
                return self._json_reply({"success": True})

            if req_path == "/api/user/logout":
                token = payload.get("token", "")
                remove_user_session(token)
                return self._json_reply({"success": True})

        if req_path.startswith("/channel"):
            if "channel" not in server_config.components:
                self.send_error(404, "Channel component is disabled on this server")
                return
            if server_config.protocol_mode == "unencrypted":
                return self._json_reply({
                    "success": False,
                    "error": "Encrypted channel protocol is disabled on this server. Plaintext protocol is required."
                }, status=403)
            proxy_ctx.ensure_initialized()
            from proxy.crypto import (
                derive_hardware_hash_key,
                derive_master_key_2,
                derive_request_keys,
                verify_challenge_response,
                aes_gcm_encrypt,
                aes_gcm_decrypt,
                pack_channel_response,
            )
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""

            if req_path == "/channel/auth":
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                    mac = payload["mac"].replace(":", "").replace("-", "").upper()
                except Exception as e:
                    return self._json_reply({"success": False, "error": f"invalid payload: {e}"}, 400)

                if "hardware_password" in payload:
                    hw_pass = payload["hardware_password"]
                    hw_hash_key = derive_hardware_hash_key(hw_pass, mac)
                    m2 = derive_master_key_2(proxy_ctx.master_key_1, hw_hash_key, mac)
                    client_ip = self._get_client_ip()
                    proxy_ctx.db.register_device(mac, hw_hash_key, m2, client_ip)
                    return self._json_reply({"success": True, "status": "registered", "master_key_2": m2.hex(), "time": int(time.time())})

                if "response" in payload:
                    response = bytes.fromhex(payload["response"])
                    if mac not in proxy_ctx.recovery_challenges:
                        return self._json_reply({"success": False, "error": "challenge expired"}, 400)
                    ch_nonce, exp = proxy_ctx.recovery_challenges.pop(mac)
                    dev = proxy_ctx.db.get_device(mac)
                    if exp < time.time() or not dev or not verify_challenge_response(dev["hardware_hash_key"], ch_nonce, response):
                        return self._json_reply({"success": False, "error": "verification failed"}, 403)
                    return self._json_reply({"success": True, "status": "recovered", "master_key_2": dev["master_key_2"].hex(), "time": int(time.time())})

                if "hardware_hash_key" in payload:
                    hw_hash_key = bytes.fromhex(payload["hardware_hash_key"])
                    dev = proxy_ctx.db.get_device(mac)
                    if dev:
                        ch_nonce = secrets.token_bytes(32)
                        proxy_ctx.recovery_challenges[mac] = (ch_nonce, time.time() + 60)
                        return self._json_reply({"success": True, "status": "challenge_required", "challenge": ch_nonce.hex(), "time": int(time.time())})
                    m2 = derive_master_key_2(proxy_ctx.master_key_1, hw_hash_key, mac)
                    client_ip = self._get_client_ip()
                    proxy_ctx.db.register_device(mac, hw_hash_key, m2, client_ip)
                    return self._json_reply({"success": True, "status": "registered", "master_key_2": m2.hex(), "time": int(time.time())})

            if req_path == "/channel":
                if len(raw_body) < 5 + 16 + 4 + 16 or raw_body[:4] != b"MSC1":
                    return self._json_reply({"error": "malformed channel frame"}, 400)
                id_len = raw_body[4]
                mac = raw_body[5:5 + id_len].decode("utf-8", "replace").upper().replace(":", "").replace("-", "")
                nonce = raw_body[5 + id_len:5 + id_len + 16]
                size = struct.unpack(">I", raw_body[5 + id_len + 16:5 + id_len + 20])[0]
                ct_tag = raw_body[5 + id_len + 20:5 + id_len + 20 + size + 16]

                dev = proxy_ctx.db.get_device(mac)
                if not dev:
                    return self._json_reply({"error": "unknown device"}, 403)

                m2 = dev["master_key_2"]
                k_req, k_resp = derive_request_keys(m2, nonce)

                aad = raw_body[:5 + id_len + 20]
                try:
                    pt = aes_gcm_decrypt(k_req, bytes(12), ct_tag, aad)
                except Exception:
                    try:
                        pt = aes_gcm_decrypt(k_req, nonce, ct_tag, aad)
                    except Exception:
                        return self._json_reply({"error": "authentication tag failed"}, 403)

                ts = struct.unpack(">I", pt[:4])[0]
                now = int(time.time())
                # Relaxed timestamp validation (allow clock drift up to 7 days)
                if ts != 0 and abs(now - ts) > 604800:
                    return self._json_reply({"error": f"timestamp skewed by {ts - now}s"}, 403)

                m_len = pt[4]
                method = pt[5:5 + m_len].decode("ascii", "replace")
                u_len = struct.unpack(">H", pt[5 + m_len:7 + m_len])[0]
                url = pt[7 + m_len:7 + m_len + u_len].decode("utf-8", "replace")
                body = pt[7 + m_len + u_len:]

                client_ip = self._get_client_ip()
                status, _, resp_body = handle_proxy_endpoint(url, client_ip=client_ip)
                out = pack_channel_response(k_resp, status, resp_body)
                proxy_ctx.db.update_device_seen(mac, client_ip)
                proxy_ctx.db.log_access(mac, client_ip, method, url, status, len(raw_body))

                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.end_headers()
                self.wfile.write(out)
                return

        self.send_error(404, "Endpoint not found")

    def end_headers(self):
        clean_url = self.path.split("?")[0]
        if clean_url.endswith(".wasm"):
            self.send_header("Content-Type", "application/wasm")
        elif clean_url.endswith((".fasl", ".msbc")):
            self.send_header("Content-Type", "application/octet-stream")
        elif clean_url.endswith(".js"):
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
        elif clean_url.endswith(".css"):
            self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-MiniScheme-Auth, Authorization, X-Requested-With, Range, Cache-Control, Pragma")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Type, Content-Range, X-MiniScheme-Auth")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        super().end_headers()


# ---------------------------------------------------------------------------
# 5. Native Windows System Tray Icon (Pure Win32 ctypes - Zero External Deps)
# ---------------------------------------------------------------------------

class Win32SystemTray:
    """Native Windows System Tray icon showing local IP & port, with click-to-open."""

    def __init__(self, ip, port, on_exit):
        self.ip = ip
        self.port = port
        self.on_exit = on_exit
        self.hwnd = None
        self.hicon = None
        self._wndproc_ref = None

    def _copy_to_clipboard(self, text):
        try:
            subprocess.run(
                ["clip"],
                input=text.encode("utf-8"),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    def _create_custom_icon(self):
        """Create a crisp 32x32 ARGB MiniScheme icon via Win32 CreateIconIndirect."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        w, h = 32, 32
        # Build BGRA pixel buffer: dark blue-slate rounded square with cyan inner frame & dot
        pixels = bytearray(w * h * 4)
        for y in range(h):
            for x in range(w):
                idx = (y * w + x) * 4
                # Rounded corners
                dx = min(x, w - 1 - x)
                dy = min(y, h - 1 - y)
                if dx + dy < 3:
                    continue
                # Cyan border
                if x in (2, 3, w - 3, w - 4) or y in (2, 3, h - 3, h - 4):
                    b, g, r, a = 240, 208, 98, 255  # #62d0f0
                # Inner terminal prompt '>' symbol
                elif (8 <= x <= 14 and abs((y - 16) - (x - 11)) <= 1) or (8 <= x <= 14 and abs((y - 16) + (x - 11)) <= 1):
                    b, g, r, a = 150, 214, 114, 255  # #72d696
                elif 16 <= x <= 23 and 20 <= y <= 22:
                    b, g, r, a = 240, 208, 98, 255  # #62d0f0 underscore
                else:
                    b, g, r, a = 32, 21, 13, 255  # #0d1520 dark background
                pixels[idx:idx + 4] = bytes((b, g, r, a))

        class ICONINFO(ctypes.Structure):
            _fields_ = [
                ("fIcon", wintypes.BOOL),
                ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP),
            ]

        buf = (ctypes.c_char * len(pixels)).from_buffer(pixels)
        hbm_color = gdi32.CreateBitmap(w, h, 1, 32, ctypes.byref(buf))
        mask_bytes = (ctypes.c_char * (w * h // 8))()
        hbm_mask = gdi32.CreateBitmap(w, h, 1, 1, ctypes.byref(mask_bytes))

        ii = ICONINFO(True, 0, 0, hbm_mask, hbm_color)
        hicon = user32.CreateIconIndirect(ctypes.byref(ii))
        gdi32.DeleteObject(hbm_color)
        gdi32.DeleteObject(hbm_mask)
        if hicon:
            return hicon
        return user32.LoadIconW(None, 32512)  # IDI_APPLICATION fallback

    def run(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        WM_DESTROY = 0x0002
        WM_COMMAND = 0x0111
        WM_USER = 0x0400
        WM_TRAY = WM_USER + 20
        WM_LBUTTONUP = 0x0202
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205

        NIM_ADD = 0x00000000
        NIM_MODIFY = 0x00000001
        NIM_DELETE = 0x00000002
        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004
        NIF_INFO = 0x00000010

        TPM_RIGHTBUTTON = 0x0002
        TPM_RETURNCMD = 0x0100
        MF_STRING = 0x0000
        MF_SEPARATOR = 0x0800
        MF_GRAYED = 0x0001

        ID_OPEN_LOCAL = 1001
        ID_OPEN_LAN = 1002
        ID_COPY_PROXY = 1003
        ID_EXIT = 1004
        ID_CHECK_RELEASE = 1005
        ID_OPEN_ADMIN = 1006
        ID_OPEN_USER = 1007

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
            ]

        local_url = f"http://127.0.0.1:{self.port}"
        lan_url = f"http://{self.ip}:{self.port}"
        admin_url = f"{local_url}/admin"
        user_url = f"{local_url}/user"

        def show_context_menu(hwnd):
            hmenu = user32.CreatePopupMenu()
            user32.AppendMenuW(hmenu, MF_STRING, ID_OPEN_LOCAL, f"🌐 打开 WebREPL 页面 ({local_url})")
            user32.AppendMenuW(hmenu, MF_STRING, ID_OPEN_ADMIN, f"🛡️ 打开代理服务管理后台 ({admin_url})")
            user32.AppendMenuW(hmenu, MF_STRING, ID_OPEN_USER, f"👤 打开设备用户门户 ({user_url})")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, ID_OPEN_LAN, f"📡 本机 IP: {self.ip}  |  监听端口: {self.port}")
            user32.AppendMenuW(hmenu, MF_STRING, ID_COPY_PROXY, f"📋 复制局域网代理地址 ({lan_url})")
            user32.AppendMenuW(hmenu, MF_STRING, ID_CHECK_RELEASE, "🔄 检查并下载最新 Release 版本")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, ID_EXIT, "❌ 退出 MiniScheme 本地服务")
            user32.SetMenuDefaultItem(hmenu, ID_OPEN_LOCAL, False)

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
            user32.DestroyMenu(hmenu)

            if cmd == ID_OPEN_LOCAL:
                webbrowser.open(local_url)
            elif cmd == ID_OPEN_ADMIN:
                webbrowser.open(admin_url)
            elif cmd == ID_OPEN_USER:
                webbrowser.open(user_url)
            elif cmd == ID_OPEN_LAN:
                webbrowser.open(lan_url)
            elif cmd == ID_COPY_PROXY:
                self._copy_to_clipboard(lan_url)
            elif cmd == ID_CHECK_RELEASE:
                rel = get_latest_github_release("minischeme-webrepl")
                exe_asset = (rel.get("assets") or {}).get("MiniScheme-WebREPL.exe")
                target = (exe_asset and exe_asset.get("url")) or rel.get("html_url") or "https://github.com/chk-jxcn/minischeme-webrepl/releases/latest"
                webbrowser.open(target)
            elif cmd == ID_EXIT:
                user32.DestroyWindow(hwnd)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    webbrowser.open(local_url)
                    return 0
                if lparam == WM_RBUTTONUP:
                    show_context_menu(hwnd)
                    return 0
            elif msg == WM_DESTROY:
                nid_del = NOTIFYICONDATAW()
                nid_del.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid_del.hWnd = hwnd
                nid_del.uID = 1
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid_del))
                user32.PostQuitMessage(0)
                if self.on_exit:
                    threading.Thread(target=self.on_exit, daemon=True).start()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"MiniSchemeTrayWnd_{os.getpid()}"

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))

        self.hwnd = user32.CreateWindowExW(
            0, class_name, "MiniScheme WebREPL Tray", 0,
            0, 0, 0, 0, None, None, hinst, None
        )
        self.hicon = self._create_custom_icon()

        tip = f"MiniScheme WebREPL 本地服务\n本机 IP: {self.ip}\n监听端口: {self.port}\n点击打开页面: {local_url}"
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = tip[:127]
        nid.szInfoTitle = "MiniScheme 本地服务已启动"
        nid.szInfo = f"本机 IP: {self.ip}  |  端口: {self.port}\n左键点击托盘图标即可打开 WebREPL 页面"
        nid.dwInfoFlags = 1  # NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------------------
# 6. Main Entrypoint
# ---------------------------------------------------------------------------

def main():
    global ACTIVE_PORT

    parser = argparse.ArgumentParser(description="MiniScheme WASM WebREPL & Local Proxy Server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Preferred port to listen on (default: 8087, auto-fallbacks if busy)")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--with-proxy", action="store_true", help="Also start standalone App Proxy on --proxy-port if different from --port")
    parser.add_argument("--proxy-port", type=int, default=8087, help="Standalone App Proxy port")
    parser.add_argument("--no-tray", action="store_true", help="Run without system tray icon (headless/CLI mode)")
    parser.add_argument("--open-browser", action="store_true", help="Automatically open WebREPL in default browser on startup")
    parser.add_argument("--protocol", "--protocol-mode", "-P", dest="protocol_mode", choices=["unencrypted", "encrypted", "both"], default=None, help="Protocol encryption mode: unencrypted (default locally), encrypted (public server), or both")
    parser.add_argument("--components", default=None, help="Comma-separated components to enable (choices: all, webrepl, proxy, channel, portal, font, vm)")
    parser.add_argument("--proxy-only", action="store_true", help="Shorthand for --components proxy,channel,portal (disables static WebREPL files, fonts, and VM)")
    parser.add_argument("--admin-login-delay", type=int, default=None, help="Delay in seconds before returning admin login responses (anti-bruteforce, default: 0)")
    parser.add_argument("--config", default=None, help="Path to config.ini file")
    args = parser.parse_args()

    if args.config:
        server_config.config_file = os.path.abspath(args.config)
        server_config.init_defaults()

    if args.proxy_only:
        server_config.components = {"proxy", "channel", "portal"}
    elif args.components is not None:
        server_config.set_components_from_str(args.components)

    if args.protocol_mode is not None:
        server_config.protocol_mode = args.protocol_mode.lower().strip()

    if args.admin_login_delay is not None:
        server_config.admin_login_delay = max(0, args.admin_login_delay)

    os.chdir(BASE_DIR)
    httpd, bound_port = create_server_with_fallback(args.host, args.port, WebReplHandler)
    ACTIVE_PORT = bound_port
    primary_ip = get_primary_ip()

    # Optional secondary proxy listener if user explicitly asked for --with-proxy on a different port
    proxy_httpd = None
    if args.with_proxy and args.proxy_port != bound_port:
        try:
            proxy_httpd, _ = create_server_with_fallback(args.host, args.proxy_port, WebReplHandler, max_tries=1)
            threading.Thread(target=proxy_httpd.serve_forever, daemon=True).start()
        except Exception:
            pass

    if sys.stdout is not None:
        try:
            print("=" * 64)
            print("  MiniScheme Server Started!")
            if bound_port != args.port:
                print(f"  [Note] Port {args.port} was busy -> automatically switched to {bound_port}")
            print(f"  Protocol Mode:       {server_config.protocol_mode.upper()}")
            print(f"  Enabled Components:  {', '.join(sorted(server_config.components))}")
            if server_config.admin_login_delay > 0:
                print(f"  Admin Login Delay:   {server_config.admin_login_delay}s")
            if "webrepl" in server_config.components:
                print(f"  Local WebREPL URL:   http://127.0.0.1:{bound_port}")
            if "proxy" in server_config.components or "channel" in server_config.components:
                print(f"  LAN / Proxy URL:     http://{primary_ip}:{bound_port}")
            if "portal" in server_config.components:
                print(f"  Admin Portal:        http://{primary_ip}:{bound_port}/admin")
            print(f"  Listening Port:      {bound_port}")
            print("=" * 64, flush=True)
        except Exception:
            pass

    is_frozen = bool(getattr(sys, "frozen", False))
    if args.open_browser or is_frozen:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{bound_port}")).start()

    use_tray = (
        os.name == "nt"
        and not args.no_tray
        and os.environ.get("WEBREPL_NO_TRAY", "") != "1"
        and (is_frozen or sys.stdout is None or sys.stdin.isatty())
    )

    if use_tray:
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        def shutdown_server():
            try:
                httpd.shutdown()
                if proxy_httpd:
                    proxy_httpd.shutdown()
            except Exception:
                pass
            os._exit(0)

        tray = Win32SystemTray(primary_ip, bound_port, on_exit=shutdown_server)
        try:
            tray.run()
        except KeyboardInterrupt:
            shutdown_server()
    else:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            if proxy_httpd:
                proxy_httpd.server_close()


if __name__ == "__main__":
    main()
