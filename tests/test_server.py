import json
import os
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import pytest

from proxy.server import ProxyServer
from proxy.crypto import (
    derive_hardware_hash_key,
    create_challenge_response,
    derive_request_keys,
    aes_gcm_encrypt,
    aes_gcm_decrypt,
    HAS_CRYPTOGRAPHY
)


@pytest.fixture(scope="module")
def proxy_server():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    master_key_1 = b"master_level_1_test_key_32bytes"
    admin_pwd = "test_admin_password_123"
    server = ProxyServer(("127.0.0.1", 18087), master_key_1, admin_pwd, db_path=db_path)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)

    yield "http://127.0.0.1:18087", admin_pwd, db_path

    server.shutdown()
    server.server_close()
    time.sleep(0.1)
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_public_endpoints(proxy_server):
    base_url, _, _ = proxy_server
    req = urllib.request.urlopen(f"{base_url}/app/ping")
    data = json.loads(req.read().decode())
    assert data["items"][0]["t"] == "ok"


def test_device_registration_and_recovery(proxy_server):
    base_url, _, _ = proxy_server
    mac = "A1B2C3D4E5F6"
    hw_pwd = "device_test_password"
    hw_hash = derive_hardware_hash_key(hw_pwd, mac)

    # 1. Step 2: Register device
    reg_body = json.dumps({
        "mac": mac,
        "hardware_hash_key": hw_hash.hex()
    }).encode()
    req = urllib.request.Request(f"{base_url}/channel/register", data=reg_body, headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req).read().decode())
    assert resp["success"] is True
    assert "master_key_2" in resp
    m2 = bytes.fromhex(resp["master_key_2"])
    assert len(m2) == 32

    # 2. Step 4: Factory Reset Recovery
    ch_req_body = json.dumps({"mac": mac}).encode()
    ch_req = urllib.request.Request(f"{base_url}/channel/recover/challenge", data=ch_req_body, headers={"Content-Type": "application/json"})
    ch_resp = json.loads(urllib.request.urlopen(ch_req).read().decode())
    assert ch_resp["success"] is True
    challenge = bytes.fromhex(ch_resp["challenge"])

    # Respond with HMAC
    sol = create_challenge_response(hw_hash, challenge)
    verify_body = json.dumps({"mac": mac, "response": sol.hex()}).encode()
    v_req = urllib.request.Request(f"{base_url}/channel/recover/verify", data=verify_body, headers={"Content-Type": "application/json"})
    v_resp = json.loads(urllib.request.urlopen(v_req).read().decode())
    assert v_resp["success"] is True
    assert v_resp["master_key_2"] == m2.hex()


def test_admin_portal_flow(proxy_server):
    base_url, admin_pwd, _ = proxy_server

    # Admin Login
    login_body = json.dumps({"password": admin_pwd}).encode()
    req = urllib.request.Request(f"{base_url}/api/admin/login", data=login_body, headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req).read().decode())
    assert resp["success"] is True
    token = resp["token"]

    # Preset User Data
    preset_body = json.dumps({
        "token": token,
        "mac": "A1B2C3D4E5F6",
        "app_id": "weather",
        "key": "preset_city",
        "value": "Guangzhou"
    }).encode()
    p_req = urllib.request.Request(f"{base_url}/api/admin/preset", data=preset_body, headers={"Content-Type": "application/json"})
    p_resp = json.loads(urllib.request.urlopen(p_req).read().decode())
    assert p_resp["success"] is True

    # List devices
    d_req = urllib.request.urlopen(f"{base_url}/api/admin/devices?token={token}")
    d_resp = json.loads(d_req.read().decode())
    assert d_resp["success"] is True
    assert any(d["mac"] == "A1B2C3D4E5F6" for d in d_resp["devices"])


def test_unified_auth_flow(proxy_server):
    """Verifies minimal round-trip unified authentication (/channel/auth)."""
    base_url, _, _ = proxy_server
    mac = "B2C3D4E5F6A1"
    hw_pwd = "device_test_pwd_2"
    hw_hash = derive_hardware_hash_key(hw_pwd, mac)

    # 1. 1-RTT Registration
    reg_body = json.dumps({"mac": mac, "hardware_hash_key": hw_hash.hex()}).encode()
    req = urllib.request.Request(f"{base_url}/channel/auth", data=reg_body, headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req).read().decode())
    assert resp["success"] is True
    assert resp["status"] == "registered"
    assert "master_key_2" in resp
    assert "time" in resp
    m2 = resp["master_key_2"]

    # 2. Challenge requested on reconnect/recovery
    req2 = urllib.request.Request(f"{base_url}/channel/auth", data=reg_body, headers={"Content-Type": "application/json"})
    resp2 = json.loads(urllib.request.urlopen(req2).read().decode())
    assert resp2["success"] is True
    assert resp2["status"] == "challenge_required"
    assert "challenge" in resp2
    challenge = bytes.fromhex(resp2["challenge"])

    # 3. 1-Reply Recovery verification
    sol = create_challenge_response(hw_hash, challenge)
    verify_body = json.dumps({"mac": mac, "response": sol.hex()}).encode()
    req3 = urllib.request.Request(f"{base_url}/channel/auth", data=verify_body, headers={"Content-Type": "application/json"})
    resp3 = json.loads(urllib.request.urlopen(req3).read().decode())
    assert resp3["success"] is True
    assert resp3["status"] == "recovered"
    assert resp3["master_key_2"] == m2


def test_per_app_endpoints(proxy_server):
    """Verifies individual app endpoints and multi-page aggregation."""
    base_url, _, _ = proxy_server

    # 1. Weather: serves all 5 pages
    w_req = urllib.request.urlopen(f"{base_url}/app/weather?loc=Beijing")
    w_data = json.loads(w_req.read().decode())
    types = {it["t"] for it in w_data["items"]}
    assert "loc" in types and "now" in types and "day" in types and "hour" in types and "idx" in types

    # 2. News: cat=all returns all categories in one query
    n_req = urllib.request.urlopen(f"{base_url}/app/news?cat=all&n=5")
    n_data = json.loads(n_req.read().decode())
    cats = {it.get("c") for it in n_data["items"] if "c" in it}
    assert "realtime" in cats and "livelihood" in cats and "finance" in cats

    # 3. Stocks: view=all returns quotes, trends, and klines together
    s_req = urllib.request.urlopen(f"{base_url}/app/stocks?view=all")
    s_data = json.loads(s_req.read().decode())
    s_types = {it["t"] for it in s_data["items"]}
    assert "quote" in s_types and "trend" in s_types and "kline" in s_types

    # 4. Crypto: view=all
    c_req = urllib.request.urlopen(f"{base_url}/app/crypto?view=all")
    c_data = json.loads(c_req.read().decode())
    assert any(it["t"] == "crypto" or it["t"] == "quote" for it in c_data["items"])

    # 5. Futures: view=all
    f_req = urllib.request.urlopen(f"{base_url}/app/futures?view=all")
    f_data = json.loads(f_req.read().decode())
    assert any(it["t"] == "quote" for it in f_data["items"])

