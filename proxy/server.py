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
    derive_master_key_2,
    derive_request_keys,
    verify_challenge_response,
    aes_gcm_encrypt,
    aes_gcm_decrypt,
    HAS_CRYPTOGRAPHY
)
from .database import Database
from .endpoints import get_weather, get_stocks, get_crypto, get_news, get_futures
from .user_portal import USER_HTML, generate_user_nonce, verify_user_login, check_user_session
from .admin_portal import ADMIN_HTML, create_admin_session, verify_admin_session

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
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-MiniScheme-Auth")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

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

        # Plain app endpoints (dedicated per-app interfaces)
        if path == "/app/weather":
            loc = qs.get("loc", ["auto"])[0]
            key = qs.get("key", [""])[0]
            host = qs.get("host", [""])[0]
            return self._send_json(get_weather(loc, key, host))
        if path == "/app/stocks":
            view = qs.get("view", ["all"])[0]
            codes = qs.get("codes", ["sh600519,sz300750,sz000858"])[0]
            index = qs.get("index", ["sh000001"])[0]
            return self._send_json(get_stocks(view, codes, index))
        if path == "/app/crypto":
            view = qs.get("view", ["all"])[0]
            symbols = qs.get("symbols", ["BTC,ETH,SOL"])[0]
            return self._send_json(get_crypto(view, symbols))
        if path == "/app/futures":
            view = qs.get("view", ["all"])[0]
            codes = qs.get("codes", ["RB0,AU0,CU0"])[0]
            return self._send_json(get_futures(view, codes))
        if path == "/app/news":
            cat = qs.get("cat", ["all"])[0]
            n = int(qs.get("n", ["8"])[0])
            return self._send_json(get_news(cat, n))

        if path == "/app/ping":
            return self._send_json({"items": [{"t": "ok", "time": int(time.time())}]})

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""

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
                    client_ip = self.client_address[0]
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

        # Admin Portal Login
        if path == "/api/admin/login":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                pwd = payload["password"]
            except Exception:
                return self._send_json({"success": False, "error": "bad request"}, 400)

            if pwd == self.server_ctx.admin_password:
                token = create_admin_session()
                return self._send_json({"success": True, "token": token})
            return self._send_json({"success": False, "error": "password incorrect"}, 403)

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
            plaintext = aes_gcm_decrypt(k_req, nonce, ciphertext_and_tag, aad)
        except Exception:
            return self._send_json({"error": "authentication tag failed"}, 403)

        # Plaintext format: u32 timestamp | u8 method_len | method | u16 url_len | url | body
        ts = struct.unpack(">I", plaintext[:4])[0]
        now = int(time.time())
        if abs(now - ts) > CHANNEL_SKEW_SECONDS:
            return self._send_json({"error": f"timestamp skewed by {ts - now}s"}, 403)

        m_len = plaintext[4]
        method = plaintext[5:5 + m_len].decode("ascii", "replace")
        u_len = struct.unpack(">H", plaintext[5 + m_len:7 + m_len])[0]
        url = plaintext[7 + m_len:7 + m_len + u_len].decode("utf-8", "replace")
        body = plaintext[7 + m_len + u_len:]

        # Step 5: Route request to separate app endpoints
        resp_status = 200
        parsed_url = urllib.parse.urlparse(url)
        path = parsed_url.path
        qs = urllib.parse.parse_qs(parsed_url.query)

        if path == "/app/weather":
            loc = qs.get("loc", ["auto"])[0]
            key = qs.get("key", [""])[0]
            host = qs.get("host", [""])[0]
            resp_body = json.dumps(get_weather(loc, key, host)).encode("utf-8")
        elif path == "/app/stocks":
            view = qs.get("view", ["all"])[0]
            codes = qs.get("codes", ["sh600519,sz300750,sz000858"])[0]
            index = qs.get("index", ["sh000001"])[0]
            resp_body = json.dumps(get_stocks(view, codes, index)).encode("utf-8")
        elif path == "/app/crypto":
            view = qs.get("view", ["all"])[0]
            symbols = qs.get("symbols", ["BTC,ETH,SOL"])[0]
            resp_body = json.dumps(get_crypto(view, symbols)).encode("utf-8")
        elif path == "/app/futures":
            view = qs.get("view", ["all"])[0]
            codes = qs.get("codes", ["RB0,AU0,CU0"])[0]
            resp_body = json.dumps(get_futures(view, codes)).encode("utf-8")
        elif path == "/app/news":
            cat = qs.get("cat", ["all"])[0]
            n = int(qs.get("n", ["8"])[0])
            resp_body = json.dumps(get_news(cat, n)).encode("utf-8")
        elif path == "/app/ping":
            resp_body = json.dumps({"items": [{"t": "ok", "time": now}]}).encode("utf-8")
        else:
            resp_body = json.dumps({"items": [{"t": "ok"}]}).encode("utf-8")

        # Encrypt response
        resp_nonce = secrets.token_bytes(16)
        resp_header = struct.pack(">HI", resp_status, len(resp_body))
        sealed_resp = aes_gcm_encrypt(k_resp, resp_nonce, resp_header + resp_body, resp_nonce)

        out = resp_nonce + sealed_resp
        client_ip = self.client_address[0]
        self.server_ctx.db.update_device_seen(mac, client_ip)
        self.server_ctx.db.log_access(mac, client_ip, method, url, resp_status, len(raw))

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ProxyServer(http.server.ThreadingHTTPServer):
    def __init__(self, server_address, master_key_1: bytes, admin_password: str, db_path: str = "minischeme_proxy.db"):
        super().__init__(server_address, ProxyHandler)
        self.master_key_1 = master_key_1
        self.admin_password = admin_password
        self.db = Database(db_path)
        self.recovery_challenges: dict = {}
