"""HTTP Server implementing the AES-GCM Encrypted Channel, Device Registration,
Batch Query, User Portal, and Admin Portal.
"""

import http.server
import json
import os
import secrets
import time
import urllib.parse
from typing import Tuple

from .admin_password import can_change, check_new_password, read_admin_password, write_admin_password
from .channel import ChannelError, answer, open_request, register_device, seal
from .crypto import derive_master_key_2, verify_challenge_response
from .database import Database
from .endpoints import handle_app_query
from . import wxpusher
from .security import (
    FetchRefused,
    client_ip as trusted_client_ip,
    cors_origin,
    guarded_fetch,
    is_private_host,
    relay_fetch,
    safe_join,
)
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

    def _send_raw(self, reply: Tuple[int, str, bytes]):
        status, ctype, body = reply
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(body)

    def _wx_request(self, body: bytes = b"") -> Tuple[int, str, bytes]:
        """/notify, /wxpusher/send and /wxpusher/logout: on a public server with
        the admin's portal session or the device's own."""
        token = self._session_token(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
        public = getattr(self.server_ctx, "public", False)
        return wxpusher.handle_request(self.path, body, public,
                                       lambda mac: verify_admin_session(token) or (bool(mac) and check_user_session(token, mac)),
                                       self.server_ctx.db, self.client_address[0])

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
        # CORS goes only to allowed origins (cors_origin): the value a handler
        # asks for is replaced by the request's own origin, or dropped
        k_lower = keyword.lower()
        if k_lower in ("access-control-allow-origin", "access-control-allow-private-network"):
            allowed = cors_origin(self.headers.get("Origin", ""), self.headers.get("Host", ""),
                                  urllib.parse.urlparse(self.path).path)
            if allowed is None:
                return
            if k_lower == "access-control-allow-origin":
                value = allowed
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-MiniScheme-Auth, X-MiniScheme-Device, Authorization, X-Requested-With, Range, Cache-Control, Pragma")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Type, Content-Range, X-MiniScheme-Auth")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/notify":   # GET works too (phones, curl, a browser)
            return self._send_raw(self._wx_request())

        if path == "/device/check":   # the device page's test button: registers nothing
            from .device_auth import check
            status, detail = check(self.server_ctx.db, self.headers, self._get_client_ip())
            return self._send_json({"success": True, "status": status, "detail": detail})

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
            token = self._session_token(qs)
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            items = self.server_ctx.db.get_app_data(mac, masked=True)
            return self._send_json({"success": True, "items": items})

        if path == "/api/user/logs":
            mac = qs.get("mac", [""])[0]
            token = self._session_token(qs)
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            items = self.server_ctx.db.get_device_logs(mac)
            return self._send_json({"success": True, "items": items})

        if path == "/api/user/notes":   # the notifier's messages, newest first
            mac = qs.get("mac", [""])[0]
            token = self._session_token(qs)
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            return self._send_json(wxpusher.notes_page(self.server_ctx.db, mac, qs))

        if path == "/api/user/med_records":   # the medicine app's records, newest first
            mac = qs.get("mac", [""])[0]
            token = self._session_token(qs)
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            from .medicine import portal_page
            return self._send_json(portal_page(self.server_ctx.db, mac, qs))

        # Admin API
        if path == "/api/admin/status":
            # app_proxy.py always has an admin password: the portal asks for it
            # and says when a local server still uses the default "admin"
            mode = getattr(self.server_ctx, "protocol_mode", "both")
            _, source = self.server_ctx.current_admin_password()
            return self._send_json({"success": True, "auth_required": True, "protocol_mode": mode,
                                    "default_password": source == "default",
                                    "can_change_password": self.server_ctx.can_change_admin_password()})

        if path == "/api/admin/devices":
            token = self._session_token(qs)
            if not verify_admin_session(token):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            devs = self.server_ctx.db.admin_list_devices()
            return self._send_json({"success": True, "devices": devs})

        if path == "/api/admin/device/data":
            token = self._session_token(qs)
            mac = qs.get("mac", [""])[0]
            if not verify_admin_session(token):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            items = self.server_ctx.db.get_app_data(mac, masked=False)  # Admin gets plaintext
            return self._send_json({"success": True, "items": items})

        # Plain app & market endpoints (/app/*, /market/*, /fetch)
        if path.startswith("/app/") or path.startswith("/market/") or path in ("/fetch", "/relay"):
            if getattr(self.server_ctx, "protocol_mode", "both") == "encrypted":
                return self._send_json({
                    "success": False,
                    "error": "Plaintext protocol is disabled on this server. Encrypted channel protocol (MSC1) is required."
                }, 403)
            from .device_auth import authenticate
            device, plain_device, problem = authenticate(self.server_ctx.db, self.server_ctx.master_key_1,
                                                         self.headers, self._get_client_ip())
            if problem:
                return self._send_json({"success": False, "error": problem}, 403)
            status, ctype, body = self._dispatch_proxy_url(self.path, device=device, forward_device=plain_device)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            self.wfile.write(body)
            return

    def _get_client_ip(self) -> str:
        peer = str(self.client_address[0]) if getattr(self, "client_address", None) else ""
        return trusted_client_ip(peer, self.headers)

    def _session_token(self, qs=None, payload=None) -> str:
        """The portal session token: X-Session-Token header (or the older
        token= query / JSON field)."""
        tok = self.headers.get("X-Session-Token", "")
        if not tok and payload:
            tok = payload.get("token", "") or ""
        if not tok and qs:
            tok = (qs.get("token") or [""])[0]
        return tok

    def _allow_private(self, client_ip: str) -> bool:
        """A local proxy fetches from this network for clients on it; a public one never."""
        return not getattr(self.server_ctx, "public", False) and is_private_host(client_ip or "")

    def _dispatch_proxy_url(self, path_with_query: str, client_ip: str = "", device: str = "",
                            forward_device: str = "") -> Tuple[int, str, bytes]:
        if not client_ip:
            client_ip = self._get_client_ip()
        parsed = urllib.parse.urlparse(path_with_query)
        path = parsed.path
        if path == "/relay":   # an app server through this converter, with the device id
            target = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            return relay_fetch(target, device or forward_device, self._allow_private(client_ip))
        if path.startswith("/market/"):
            rel = urllib.parse.unquote(path[len("/market/"):])
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for cand_dir in (os.path.join(base_dir, "apps"), os.path.join(base_dir, "dist")):
                cand = safe_join(cand_dir, rel)
                if cand and os.path.isfile(cand):
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
                return handle_app_query(sub_url, client_ip=client_ip, device=device, public=self._public(),
                                        plain_device=forward_device, db=self.server_ctx.db)
            try:
                ctype, body = guarded_fetch(target_url, allow_private=self._allow_private(client_ip))
                return 200, ctype, body
            except FetchRefused as e:
                return 403, "text/plain", f"fetch refused: {e}".encode("utf-8")
            except Exception as e:
                return 502, "text/plain", f"fetch error: {type(e).__name__}".encode("utf-8")
        return handle_app_query(path_with_query, client_ip=client_ip, device=device, public=self._public(),
                                plain_device=forward_device, db=self.server_ctx.db)

    def _public(self) -> bool:
        return bool(getattr(self.server_ctx, "public", False))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""

        if path in wxpusher.HTTP_PATHS:
            limit = wxpusher.IMAGE_BODY_MAX if path == "/notify/image" else 65536
            return self._send_raw(self._wx_request(raw_body[:limit]))

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

            # A new device is registered; a registered one gets its own key
            # back only with the same hash key (its keys are never replaced)
            client_ip = self._get_client_ip()
            got = register_device(self.server_ctx.db, self.server_ctx.master_key_1, mac, hw_hash_key, client_ip)
            self.server_ctx.db.log_access(mac, client_ip, "POST", "/channel/register", 200 if got else 403, len(raw_body))
            if not got:
                return self._send_json({"success": False, "error": "verification failed"}, 403)
            return self._send_json({
                "success": True,
                "status": got[0],
                "master_key_2": got[1].hex(),
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
                if not dev and payload.get("check"):
                    # the device's settings page tests a password: nothing is registered
                    return self._send_json({"success": True, "status": "new", "time": int(time.time())})
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
                    if not self.server_ctx.db.register_device(mac, hw_hash_key, m2, client_ip):
                        return self._send_json({"success": False, "error": "verification failed"}, 403)
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
            token = self._session_token(qs, payload)
            app_id = payload.get("app_id") or qs.get("app_id", [""])[0]
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            if not app_id or app_id == "all":
                self.server_ctx.db.clear_all_app_data(mac)
            else:   # id: one record (a notifier message)
                self.server_ctx.db.clear_app_data(mac, app_id, payload.get("id") or qs.get("id", [None])[0])
            return self._send_json({"success": True})

        if path == "/api/user/account/delete":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                payload = {}
            mac = payload.get("mac") or qs.get("mac", [""])[0]
            token = self._session_token(qs, payload)
            if not check_user_session(token, mac):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            self.server_ctx.db.delete_device_account(mac)
            remove_user_session(token)
            return self._send_json({"success": True})

        if path == "/api/user/logout":
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                remove_user_session(self._session_token(None, payload))
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

            current, source = self.server_ctx.current_admin_password()
            if current and secrets.compare_digest(str(pwd), current):
                token = create_admin_session()
                return self._send_json({"success": True, "token": token})
            error = "password incorrect"
            if not self.server_ctx.public and self.server_ctx.config_path and source in ("config", "default"):
                # a local server says which file the password comes from
                error = (f"密码不正确。本地服务使用的密码来自：{self.server_ctx.config_path} 的 [security] admin_password"
                         if source == "config" else
                         f"密码不正确。本地服务使用默认密码 admin（{self.server_ctx.config_path} 里没有设置 admin_password）")
            return self._send_json({"success": False, "error": error}, 403)

        if path == "/api/admin/password":
            # a local server's password, written into its config.ini [security]
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                return self._send_json({"success": False, "error": "bad request"}, 400)
            if not verify_admin_session(self._session_token(None, payload)):
                return self._send_json({"success": False, "error": "unauthorized"}, 401)
            if not self.server_ctx.can_change_admin_password():
                return self._send_json({"success": False, "error": "密码由环境变量或公网服务器的配置决定，请在那里修改"}, 403)
            current = self.server_ctx.current_admin_password()[0]
            if not secrets.compare_digest(str(payload.get("old", "")), current):
                return self._send_json({"success": False, "error": "原密码不正确"}, 403)
            new = str(payload.get("new", ""))
            why = check_new_password(new)
            if why:
                return self._send_json({"success": False, "error": why}, 400)
            try:
                write_admin_password(self.server_ctx.config_path, new)
            except OSError as e:
                return self._send_json({"success": False, "error": f"cannot write config.ini: {e}"}, 500)
            self.server_ctx.admin_password = new
            return self._send_json({"success": True})

        if path == "/api/admin/logout":
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                remove_admin_session(self._session_token(None, payload))
            except Exception:
                pass
            return self._send_json({"success": True})

        # Admin Preset User Data
        if path == "/api/admin/preset":
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                token = self._session_token(None, payload)
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
                token = self._session_token(None, payload)
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
                token = self._session_token(None, payload)
                if not verify_admin_session(token):
                    return self._send_json({"success": False, "error": "unauthorized"}, 401)
                mac = payload["mac"]
                self.server_ctx.db.delete_device_account(mac)
                return self._send_json({"success": True})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 400)

        self._send_json({"error": "not found"}, 404)

    def _handle_encrypted_channel(self, raw: bytes):
        """MSC1 (channel.py): each request once, the proxy's own paths answered
        here, any other URL fetched from the internet (or this network, locally)."""
        def master_key_2_of(device):
            dev = self.server_ctx.db.get_device(device)
            return bytes(dev["master_key_2"]) if dev and dev.get("master_key_2") else None

        try:
            device, k_resp, method, url, body, clock = open_request(raw, master_key_2_of)
        except ChannelError as e:
            return self._send_json({"error": e.message}, e.status)
        client_ip = self._get_client_ip()
        resp_status, resp_body = answer(method, url, body,
                                        lambda p: self._dispatch_proxy_url(p, client_ip=client_ip, device=device),
                                        allow_private=self._allow_private(client_ip))
        out = seal(k_resp, resp_status, resp_body)
        self.server_ctx.db.update_device_seen(device, client_ip)
        self.server_ctx.db.log_access(device, client_ip, method, url, resp_status, len(raw))

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ProxyServer(http.server.ThreadingHTTPServer):
    def __init__(self, server_address, master_key_1: bytes, admin_password: str, db_path: str = "minischeme_proxy.db", protocol_mode: str = "both", admin_login_delay: int = 0, public: bool = False,
                 config_path: str = ""):
        super().__init__(server_address, ProxyHandler)
        self.public = public   # on the internet: never fetches from private networks
        self.master_key_1 = master_key_1
        self.admin_password = admin_password   # without config_path: fixed
        self.config_path = config_path         # with it: read again at each login
        self.db = Database(db_path)
        wxpusher.set_state_dir(os.path.join(os.path.dirname(os.path.abspath(db_path)), "wxpusher"))
        if config_path and os.path.exists(config_path):
            import configparser
            cfg = configparser.ConfigParser()
            try:
                cfg.read(config_path, encoding="utf-8")
                wxpusher.set_sender(cfg.get("wxpusher", "app_token", fallback=""), cfg.get("wxpusher", "uids", fallback=""),
                                    cfg.get("wxpusher", "topic_ids", fallback=""))
            except configparser.Error:
                pass
        self.recovery_challenges: dict = {}
        self.protocol_mode = (protocol_mode or "both").lower().strip()
        self.admin_login_delay = max(0, admin_login_delay or 0)

    def current_admin_password(self) -> Tuple[str, str]:
        """(password, source): from config.ini when the server has one (an edit
        applies at the next login), else the password it was started with."""
        if self.config_path:
            return read_admin_password(self.config_path, self.public)
        return self.admin_password, "fixed"

    def can_change_admin_password(self) -> bool:
        return can_change(self.config_path, self.public)
