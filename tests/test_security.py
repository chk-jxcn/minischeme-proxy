"""Regression tests for the 2026-10-19 review (S1 path traversal, S2 device
re-keying, S3 SSRF, CORS allow-list, XFF trust, static file exposure)."""
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from proxy.crypto import derive_hardware_hash_key
from proxy.security import (
    client_ip,
    cors_origin,
    guarded_fetch,
    FetchRefused,
    redact_url,
    safe_join,
    static_path_blocked,
)
from proxy.server import ProxyServer


@pytest.fixture(scope="module")
def public_server():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    server = ProxyServer(("127.0.0.1", 18097), b"k" * 32, "pw_for_tests", db_path=db_path, public=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield "http://127.0.0.1:18097"
    server.shutdown()
    server.server_close()
    try:
        os.remove(db_path)
    except OSError:
        pass


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_safe_join_refuses_escapes(tmp_path):
    assert safe_join(str(tmp_path), "index.json") == os.path.realpath(str(tmp_path / "index.json"))
    for bad in ("../config.ini", "a/../../x", "/etc/passwd", "..\\x", "C:/x", "", "a//b", "./a"):
        assert safe_join(str(tmp_path), bad) is None, bad


def test_market_path_traversal(public_server):
    for path in ("/market/../config.ini", "/market/%2e%2e/config.ini", "/market/..%2fminischeme_proxy.db"):
        status, _, _ = _get(public_server + path)
        assert status == 404, path


def test_registered_device_is_never_rekeyed(public_server):
    mac = "C0FFEE000001"
    ok_key = derive_hardware_hash_key("right-password", mac)
    status, first = _post(public_server + "/channel/register", {"mac": mac, "hardware_hash_key": ok_key.hex()})
    assert status == 200 and first["status"] == "registered"
    # someone who knows only the MAC cannot take the device over
    other = derive_hardware_hash_key("attacker", mac)
    status, res = _post(public_server + "/channel/register", {"mac": mac, "hardware_hash_key": other.hex()})
    assert status == 403 and "master_key_2" not in res
    # the device itself (same password, e.g. after a factory reset) gets its key back
    status, again = _post(public_server + "/channel/register", {"mac": mac, "hardware_hash_key": ok_key.hex()})
    assert status == 200 and again["status"] == "recovered" and again["master_key_2"] == first["master_key_2"]


def test_fetch_refuses_private_hosts(public_server):
    for target in ("http://127.0.0.1:18097/other.json", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/"):
        status, _, body = _get(public_server + "/fetch?url=" + urllib.request.quote(target, safe=""))
        assert status == 403, (target, body)
    with pytest.raises(FetchRefused):
        guarded_fetch("http://localhost:1/")
    with pytest.raises(FetchRefused):
        guarded_fetch("file:///etc/passwd")


def test_admin_status_never_hands_out_tokens(public_server):
    status, _, body = _get(public_server + "/api/admin/status")
    data = json.loads(body.decode())
    assert data["auth_required"] is True and "token" not in data


def test_cors_allow_list(public_server):
    _, headers, _ = _get(public_server + "/health", {"Origin": "https://evil.example"})
    assert headers.get("Access-Control-Allow-Origin") is None
    _, headers, _ = _get(public_server + "/health", {"Origin": "https://chk-jxcn.github.io"})
    assert headers.get("Access-Control-Allow-Origin") == "https://chk-jxcn.github.io"
    assert cors_origin("http://192.168.1.20:8080", "x", "/app/ping") == "http://192.168.1.20:8080"
    assert cors_origin("null", "x", "/app/ping") == "null"
    assert cors_origin("null", "x", "/api/admin/devices") is None


def test_forwarded_for_only_behind_a_local_reverse_proxy():
    hdrs = {"X-Forwarded-For": "1.2.3.4:5555"}
    assert client_ip("127.0.0.1", hdrs) == "1.2.3.4"
    assert client_ip("8.8.8.8", hdrs) == "8.8.8.8"


def test_static_paths_and_log_redaction():
    for p in ("/config.ini", "/minischeme_proxy.db", "/.git/config", "/proxy/server.py", "/server.py", "/tools/x"):
        assert static_path_blocked(p), p
    for p in ("/", "/index.html", "/js/app.js", "/wasm/minischeme_vm.wasm", "/apps/index.json"):
        assert not static_path_blocked(p), p
    assert "secret123" not in redact_url("/app/weather?loc=x&token=secret123&key=abc")


def test_market_app_info(tmp_path, monkeypatch):
    """/market/<id>.info.json: one app's entry (a device never reads the whole index);
    the file when the market has one, else cut from index.json; 404 for an unknown app."""
    import server
    from proxy.market_sync import app_info
    entry = {"id": "weather", "name": "天气", "version": "9", "api": "1.15", "file": "weather.msbc",
             "size": 10, "hash": "0badf00d"}
    (tmp_path / "index.json").write_text(json.dumps([{"id": "news", "file": "news.msbc"}, entry]), encoding="utf-8")
    assert json.loads(app_info([str(tmp_path / "none"), str(tmp_path)], "weather").decode("utf-8")) == entry
    assert app_info([str(tmp_path)], "nosuch") is None
    monkeypatch.setattr(server, "market_dirs", lambda: [str(tmp_path)])
    status, ctype, body = server.handle_proxy_endpoint("/market/weather.info.json")
    assert status == 200 and ctype == "application/json" and json.loads(body.decode("utf-8")) == entry
    assert server.handle_proxy_endpoint("/market/nosuch.info.json")[0] == 404
    (tmp_path / "news.info.json").write_text('{"id": "news", "version": "2"}', encoding="utf-8")
    assert json.loads(server.handle_proxy_endpoint("/market/news.info.json")[2].decode("utf-8"))["version"] == "2"


def test_market_app_info_converter(public_server):
    """The converter (app_proxy.py) cuts it from its apps/ index.json as well (CORS: the device page reads it)."""
    import server
    first = json.loads(open(os.path.join(os.path.dirname(server.__file__), "apps", "index.json"), encoding="utf-8").read())
    first = (first.get("apps") if isinstance(first, dict) else first)[0]
    page = "http://192.168.2.184"          # the device page, on the LAN
    status, headers, body = _get(public_server + f"/market/{first['id']}.info.json", {"Origin": page})
    assert status == 200 and json.loads(body.decode("utf-8"))["id"] == first["id"]
    assert headers.get("Access-Control-Allow-Origin") == page
    assert _get(public_server + "/market/nosuch.info.json")[0] == 404
