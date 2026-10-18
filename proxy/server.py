"""HTTP Server implementing the AES-GCM Encrypted Channel, Device Registration,
Batch Query, User Portal, and Admin Portal.
"""

import http.server
import json
import os
import secrets
import struct
import time
import urllib.parse
from typing import Optional, Tuple

from .crypto import (
    derive_hardware_hash_key,
    derive_master_key_2,
    derive_request_keys,
    verify_challenge_response,
    aes_gcm_encrypt,
    aes_gcm_decrypt,
    pack_channel_response,
    HAS_CRYPTOGRAPHY
)
from .database import Database
from .endpoints import get_weather, get_stocks, get_crypto, get_news, get_futures, handle_app_query
from .user_portal import (
    USER_HTML,
    generate_user_nonce,
    verify_user_login,
    verify_simulator_login,
    check_user_session,
    remove_user_session,
)
from .admin_portal import (
    ADMIN_HTML,
    create_admin_session,
    verify_admin_session,
    remove_admin_session,
)

CHANNEL_SKEW_SECONDS = 300  # ±5 minutes anti-replay window


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Concise logging
        pass

    @property
    def server_ctx(self):
        return self.server

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(body)

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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-MiniScheme-Auth, Authorization, X-Requested-With, Range, Cache-Control, Pragma")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Type, Content-Range, X-MiniScheme-Auth")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # Health & Server Discovery API
        if path == "/health":
            return self._send_json({
                "status": "ok",
                "protocol_mode": getattr(self.server_ctx, "protocol_mode", "both"),
                "time": int(time.time()),
            })

        if path == "/api/server_info":
            mode = getattr(self.server_ctx, "protocol_mode", "both")
            port = getattr(self.server_ctx, "server_port", 8087)
            return self._send_json({
                "success": True,
                "proxy_server": True,
                "port": port,
                "protocol_mode": mode,
                "components": ["proxy", "converter", "portal"],
                "time": int(time.time()),
            })

        # Web Portals
        if path == "/user" or path == "/user/":
            return self._send_html(USER_HTML)
        if path == "/admin" or path == "/admin/":
            return self._send_html(ADMIN_HTML)

        # User API
        if path == "/api/user/nonce":
            mac = qs.get("mac", [""])[0]
            if not mac:
                return self._send_json({"success": False, "error": "missing mac"}, 400)
            nonce = generate_user_nonce(mac)
            return self._send_json({"success": True, "nonce": nonce})

        if path == "/api/user/data":
            mac = qs.get("mac", [""])[0]
            token = qs.get("token", [""])[0]
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            items = self.server_ctx.db.get_app_data(mac, masked=True)
            return self._send_json({"success": True, "items": items})

        if path == "/api/user/logs":
            mac = qs.get("mac", [""])[0]
            token = qs.get("token", [""])[0]
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            items = self.server_ctx.db.get_device_logs(mac)
            return self._send_json({"success": True, "items": items})

        # Admin API
        if path == "/api/admin/status":
            mode = getattr(self.server_ctx, "protocol_mode", "both")
            pwd = getattr(self.server_ctx, "admin_password", "admin")
            auth_req = (mode == "encrypted" or pwd != "admin")
            resp = {"success": True, "auth_required": auth_req, "protocol_mode": mode}
            if not auth_req:
                resp["token"] = create_admin_session()
            return self._send_json(resp)

        if path == "/api/admin/devices":
            token = qs.get("token", [""])[0]
            if not verify_admin_session(token):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            devs = self.server_ctx.db.admin_list_devices()
            return self._send_json({"success": True, "devices": devs})

        if path == "/api/admin/device/data":
            token = qs.get("token", [""])[0]
            mac = qs.get("mac", [""])[0]
            if not verify_admin_session(token):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            items = self.server_ctx.db.get_app_data(mac, masked=False)  # Admin gets plaintext
            return self._send_json({"success": True, "items": items})

        # Plain app & market endpoints (/app/*, /market/*, /fetch)
        if path.startswith("/app/") or path.startswith("/market/") or path == "/fetch":
            if getattr(self.server_ctx, "protocol_mode", "both") == "encrypted":
                return self._send_json({
                    "success": False,
                    "error": "Plaintext protocol is disabled on this server. Encrypted channel protocol (MSC1) is required."
                }, 403)
            status, ctype, body = self._dispatch_proxy_url(self.path)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            self.wfile.write(body)
            return

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

    def _dispatch_proxy_url(self, path_with_query: str, client_ip: str = "") -> Tuple[int, str, bytes]:
        if not client_ip:
            client_ip = self._get_client_ip()
        parsed = urllib.parse.urlparse(path_with_query)
        path = parsed.path
        if path.startswith("/market/"):
            rel = path[len("/market/"):].lstrip("/")
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for cand_dir in (os.path.join(base_dir, "apps"), os.path.join(base_dir, "dist")):
                cand = os.path.join(cand_dir, rel)
                if os.path.isfile(cand):
                    ctype = "application/json" if rel.endswith(".json") else "application/octet-stream"
                    with open(cand, "rb") as f:
                        return 200, ctype, f.read()
            return 404, "application/json", b'{"error":"market file not found"}'
        if path == "/fetch":
            qs = urllib.parse.parse_qs(parsed.query)
            target_url = (qs.get("url") or [""])[0]
            if not target_url.startswith(("http://", "https://")):
                return 400, "text/plain", b"missing or invalid url"
            p_target = urllib.parse.urlparse(target_url)
            if p_target.path.startswith("/app/") or p_target.path == "/health":
                sub_url = p_target.path + ("?" + p_target.query if p_target.query else "")
                return handle_app_query(sub_url, client_ip=client_ip)
            try:
                import urllib.request as _ureq
                hdrs = {"User-Agent": "MiniScheme-Proxy/1.0"}
                if client_ip:
                    hdrs["X-Forwarded-For"] = client_ip
                req = _ureq.Request(target_url, headers=hdrs)
                with _ureq.urlopen(req, timeout=5) as resp:
                    body = resp.read(512 * 1024)
                    ctype = resp.headers.get("Content-Type", "application/json")
                    return 200, ctype, body
            except Exception as e:
                return 502, "text/plain", f"fetch error: {e}".encode("utf-8")
        return handle_app_query(path_with_query, client_ip=client_ip)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""

        if path.startswith("/channel") and getattr(self.server_ctx, "protocol_mode", "both") == "unencrypted":
            return self._send_json({
                "success": False,
                "error": "Encrypted channel protocol is disabled on this server. Plaintext protocol is required."
            }, 403)

        # 1. Step 2: Device First Registration
        if path == "/channel/register":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                mac = payload["mac"].replace(":", "").replace("-", "").upper()
                hw_hash_hex = payload["hardware_hash_key"]
                hw_hash_key = bytes.fromhex(hw_hash_hex)
            except Exception as e:
                return self._send_json({"success": False, "error": f"invalid payload: {e}"}, 400)

            # Derive master_key_2
            m2 = derive_master_key_2(self.server_ctx.master_key_1, hw_hash_key, mac)
            client_ip = self.client_address[0]
            self.server_ctx.db.register_device(mac, hw_hash_key, m2, client_ip)
            self.server_ctx.db.log_access(mac, client_ip, "POST", "/channel/register", 200, len(raw_body))

            return self._send_json({
                "success": True,
                "master_key_2": m2.hex(),
                "time": int(time.time())
            })

        # 2. Step 4: Factory Reset Recovery
        if path == "/channel/recover/challenge":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                mac = payload["mac"].replace(":", "").replace("-", "").upper()
            except Exception as e:
                return self._send_json({"success": False, "error": f"invalid payload: {e}"}, 400)

            dev = self.server_ctx.db.get_device(mac)
            if not dev:
                return self._send_json({"success": False, "error": "unknown device"}, 404)

            ch_nonce = secrets.token_bytes(32)
            self.server_ctx.recovery_challenges[mac] = (ch_nonce, time.time() + 60)
            return self._send_json({"success": True, "challenge": ch_nonce.hex()})

        if path == "/channel/recover/verify":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                mac = payload["mac"].replace(":", "").replace("-", "").upper()
                resp_hex = payload["response"]
                response = bytes.fromhex(resp_hex)
            except Exception as e:
                return self._send_json({"success": False, "error": f"invalid payload: {e}"}, 400)

            if mac not in self.server_ctx.recovery_challenges:
                return self._send_json({"success": False, "error": "challenge expired or not requested"}, 400)
            ch_nonce, exp = self.server_ctx.recovery_challenges.pop(mac)
            if exp < time.time():
                return self._send_json({"success": False, "error": "challenge expired"}, 400)

            dev = self.server_ctx.db.get_device(mac)
            if not dev or not verify_challenge_response(dev["hardware_hash_key"], ch_nonce, response):
                client_ip = self.client_address[0]
                self.server_ctx.db.log_access(mac, client_ip, "POST", "/channel/recover", 403, len(raw_body))
                return self._send_json({"success": False, "error": "verification failed"}, 403)

            # Re-issue master_key_2
            m2 = dev["master_key_2"]
            client_ip = self.client_address[0]
            self.server_ctx.db.update_device_seen(mac, client_ip)
            self.server_ctx.db.log_access(mac, client_ip, "POST", "/channel/recover", 200, len(raw_body))

            return self._send_json({
                "success": True,
                "master_key_2": m2.hex(),
                "time": int(time.time())
            })

        # Unified Minimal-RTT Authentication (/channel/auth)
        if path == "/channel/auth":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                mac = payload["mac"].replace(":", "").replace("-", "").upper()
            except Exception as e:
                return self._send_json({"success": False, "error": f"invalid payload: {e}"}, 400)

            # A. Challenge-response verification (factory reset recovery)
            if "response" in payload:
                resp_hex = payload["response"]
                response = bytes.fromhex(resp_hex)
                if mac not in self.server_ctx.recovery_challenges:
                    return self._send_json({"success": False, "error": "challenge expired or not requested"}, 400)
                ch_nonce, exp = self.server_ctx.recovery_challenges.pop(mac)
                if exp < time.time():
                    return self._send_json({"success": False, "error": "challenge expired"}, 400)

                dev = self.server_ctx.db.get_device(mac)
                if not dev or not verify_challenge_response(dev["hardware_hash_key"], ch_nonce, response):
                    client_ip = self.client_address[0]
                    self.server_ctx.db.log_access(mac, client_ip, "POST", "/channel/auth", 403, len(raw_body))
                    return self._send_json({"success": False, "error": "verification failed"}, 403)

                m2 = dev["master_key_2"]
                client_ip = self.client_address[0]
                self.server_ctx.db.update_device_seen(mac, client_ip)
                self.server_ctx.db.log_access(mac, client_ip, "POST", "/channel/auth", 200, len(raw_body))
                return self._send_json({
                    "success": True,
                    "status": "recovered",
                    "master_key_2": m2.hex(),
                    "time": int(time.time())
                })

            # B. Initial Registration / Connection check
            if "hardware_hash_key" in payload:
                hw_hash_hex = payload["hardware_hash_key"]
                hw_hash_key = bytes.fromhex(hw_hash_hex)
                dev = self.server_ctx.db.get_device(mac)
                if dev:
                    # Device already has data on server: trigger challenge-response (Step 4)
                    ch_nonce = secrets.token_bytes(32)
                    self.server_ctx.recovery_challenges[mac] = (ch_nonce, time.time() + 60)
                    return self._send_json({
                        "success": True,
                        "status": "challenge_required",
                        "challenge": ch_nonce.hex(),
                        "time": int(time.time())
                    })
                else:
                    # Brand new device: derive master_key_2 and register (Step 2 - 1 RTT)
                    m2 = derive_master_key_2(self.server_ctx.master_key_1, hw_hash_key, mac)
                    client_ip = self._get_client_ip()
                    self.server_ctx.db.register_device(mac, hw_hash_key, m2, client_ip)
                    self.server_ctx.db.log_access(mac, client_ip, "POST", "/channel/auth", 200, len(raw_body))
                    return self._send_json({
                        "success": True,
                        "status": "registered",
                        "master_key_2": m2.hex(),
                        "time": int(time.time())
                    })

            # C. Request challenge explicitly
            dev = self.server_ctx.db.get_device(mac)
            if not dev:
                return self._send_json({"success": False, "error": "unknown device"}, 404)
            ch_nonce = secrets.token_bytes(32)
            self.server_ctx.recovery_challenges[mac] = (ch_nonce, time.time() + 60)
            return self._send_json({
                "success": True,
                "status": "challenge",
                "challenge": ch_nonce.hex(),
                "time": int(time.time())
            })

        # 3. Step 3 & 5: Encrypted Channel (Binary MSC1)
        if path == "/channel":
            return self._handle_encrypted_channel(raw_body)

        # User Portal Login
        if path == "/api/user/login":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                mac = payload["mac"]
                client_hash = payload["hash"]
                nonce = payload["nonce"]
            except Exception:
                return self._send_json({"success": False, "error": "bad request"}, 400)

            ok, res = verify_user_login(mac, client_hash, nonce, self.server_ctx.db)
            if ok:
                return self._send_json({"success": True, "token": res})
            return self._send_json({"success": False, "error": res}, 403)

        if path == "/api/user/sim_register":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                uuid = payload.get("uuid", "")
                if not uuid:
                    return self._send_json({"success": False, "error": "missing uuid"}, 400)
                client_ip = self._get_client_ip()
                self.server_ctx.db.register_simulator(uuid, client_ip)
                return self._send_json({"success": True})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 400)

        if path == "/api/user/sim_login":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                uuid = payload.get("uuid", "")
                if not uuid:
                    return self._send_json({"success": False, "error": "missing uuid"}, 400)
                client_ip = self._get_client_ip()
                ok, res = verify_simulator_login(uuid, self.server_ctx.db, client_ip)
                if ok:
                    return self._send_json({"success": True, "token": res, "mac": uuid.upper()})
                return self._send_json({"success": False, "error": res}, 400)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 400)

        if path == "/api/user/data/clear":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                payload = {}
            mac = payload.get("mac") or qs.get("mac", [""])[0]
            token = payload.get("token") or qs.get("token", [""])[0]
            app_id = payload.get("app_id") or qs.get("app_id", [""])[0]
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            if not app_id or app_id == "all":
                self.server_ctx.db.clear_all_app_data(mac)
            else:
                self.server_ctx.db.clear_app_data(mac, app_id)
            return self._send_json({"success": True})

        if path == "/api/user/account/delete":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                payload = {}
            mac = payload.get("mac") or qs.get("mac", [""])[0]
            token = payload.get("token") or qs.get("token", [""])[0]
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            self.server_ctx.db.delete_device_account(mac)
            remove_user_session(token)
            return self._send_json({"success": True})

        if path == "/api/user/logout":
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                remove_user_session(payload.get("token", ""))
            except Exception:
                pass
            return self._send_json({"success": True})

        # Admin Portal Login
        if path == "/api/admin/login":
            delay = getattr(self.server_ctx, "admin_login_delay", 0)
            if delay > 0:
                time.sleep(delay)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                pwd = payload["password"]
            except Exception:
                return self._send_json({"success": False, "error": "bad request"}, 400)

            if pwd == self.server_ctx.admin_password:
                token = create_admin_session()
                return self._send_json({"success": True, "token": token})
            return self._send_json({"success": False, "error": "password incorrect"}, 403)

        if path == "/api/admin/logout":
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                remove_admin_session(payload.get("token", ""))
            except Exception:
                pass
            return self._send_json({"success": True})

        # Admin Preset User Data
        if path == "/api/admin/preset":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                token = payload.get("token", "")
                if not verify_admin_session(token):
                    return self._send_json({"success": False, "error": "unauthorized"}, 401)
                mac = payload["mac"]
                app_id = payload["app_id"]
                key = payload["key"]
                val = payload["value"]
                self.server_ctx.db.set_app_data(mac, app_id, key, val)
                return self._send_json({"success": True})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 400)

        if path == "/api/admin/device/clear":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                token = payload.get("token", "")
                if not verify_admin_session(token):
                    return self._send_json({"success": False, "error": "unauthorized"}, 401)
                mac = payload["mac"]
                self.server_ctx.db.clear_all_app_data(mac)
                return self._send_json({"success": True})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 400)

        if path == "/api/admin/device/delete":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                token = payload.get("token", "")
                if not verify_admin_session(token):
                    return self._send_json({"success": False, "error": "unauthorized"}, 401)
                mac = payload["mac"]
                self.server_ctx.db.delete_device_account(mac)
                return self._send_json({"success": True})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 400)

        self._send_json({"error": "not found"}, 404)

    def _handle_encrypted_channel(self, raw: bytes):
        if len(raw) < 5 + 16 + 4 + 16 or raw[:4] != b"MSC1":
            return self._send_json({"error": "malformed channel frame"}, 400)

        id_len = raw[4]
        dev_id_bytes = raw[5:5 + id_len]
        mac = dev_id_bytes.decode("utf-8", "replace").upper().replace(":", "").replace("-", "")
        nonce = raw[5 + id_len:5 + id_len + 16]
        sz_bytes = raw[5 + id_len + 16:5 + id_len + 20]
        size = struct.unpack(">I", sz_bytes)[0]
        ciphertext_and_tag = raw[5 + id_len + 20:5 + id_len + 20 + size + 16]

        dev = self.server_ctx.db.get_device(mac)
        if not dev:
            return self._send_json({"error": "unknown device"}, 403)

        m2 = dev["master_key_2"]
        k_req, k_resp = derive_request_keys(m2, nonce)

        # Decrypt
        aad = raw[:5 + id_len + 20]
        try:
            plaintext = aes_gcm_decrypt(k_req, bytes(12), ciphertext_and_tag, aad)
        except Exception:
            try:
                plaintext = aes_gcm_decrypt(k_req, nonce, ciphertext_and_tag, aad)
            except Exception:
                return self._send_json({"error": "authentication tag failed"}, 403)

        # Plaintext format: u32 timestamp | u8 method_len | method | u16 url_len | url | body
        ts = struct.unpack(">I", plaintext[:4])[0]
        now = int(time.time())
        if ts != 0 and abs(now - ts) > CHANNEL_SKEW_SECONDS:
            return self._send_json({"error": f"timestamp skewed by {ts - now}s"}, 403)

        m_len = plaintext[4]
        method = plaintext[5:5 + m_len].decode("ascii", "replace")
        u_len = struct.unpack(">H", plaintext[5 + m_len:7 + m_len])[0]
        url = plaintext[7 + m_len:7 + m_len + u_len].decode("utf-8", "replace")
        body = plaintext[7 + m_len + u_len:]

        # Step 5: Route request to unified app & market dispatcher
        resp_status, _, resp_body = self._dispatch_proxy_url(url)

        # Encrypt response using MSC1 chunked records
        out = pack_channel_response(k_resp, resp_status, resp_body)
        client_ip = self._get_client_ip()
        self.server_ctx.db.update_device_seen(mac, client_ip)
        self.server_ctx.db.log_access(mac, client_ip, method, url, resp_status, len(raw))

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ProxyServer(http.server.ThreadingHTTPServer):
    def __init__(self, server_address, master_key_1: bytes, admin_password: str, db_path: str = "minischeme_proxy.db", protocol_mode: str = "both", admin_login_delay: int = 0):
        super().__init__(server_address, ProxyHandler)
        self.master_key_1 = master_key_1
        self.admin_password = admin_password
        self.db = Database(db_path)
        self.recovery_challenges: dict = {}
        self.protocol_mode = (protocol_mode or "both").lower().strip()
        self.admin_login_delay = max(0, admin_login_delay or 0)
