"""The medicine app's records (proxy/medicine.py) and the devices' plain
HTTP password header (proxy/device_auth.py), on proxy/server.py."""
import base64
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from proxy.medicine import parse_name, parse_record
from proxy.security import redact_url
from proxy.server import ProxyServer

MK1 = b"master_level_1_test_key_32bytes!"


def _start(port, public):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    server = ProxyServer(("127.0.0.1", port), MK1, "admin-pw-for-tests", db_path=db_path, public=public)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return server, db_path


def _stop(server, db_path):
    server.shutdown()
    server.server_close()
    time.sleep(0.1)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass


@pytest.fixture(scope="module")
def local_server():
    server, db_path = _start(18091, False)
    yield "http://127.0.0.1:18091", server
    _stop(server, db_path)


@pytest.fixture(scope="module")
def public_server():
    server, db_path = _start(18092, True)
    yield "http://127.0.0.1:18092", server
    _stop(server, db_path)


def _get(url, device="", password=None):
    headers = {}
    if device:
        headers["X-MiniScheme-Device"] = device
    if password is not None:
        headers["X-MiniScheme-Password"] = base64.b64encode(password.encode("utf-8")).decode("ascii")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _med(base, query, device="", password=None):
    st, doc = _get(f"{base}/app/medicine?{query}", device, password)
    assert st == 200, doc
    return doc["items"]


def test_parsing():
    assert parse_record("20384.0800.1.ok.0805.10") == (20384, "08:00", 1, "ok", "08:05", 10)
    assert parse_record("20384.0800.2.miss.-.5") == (20384, "08:00", 2, "miss", "", 5)
    for bad in ("20384.0800.1.ok.0805", "20384.2500.1.ok.-.10", "x.0800.1.ok.-.10", "20384.0800.1.eaten.-.10",
                "20384.0800.0.ok.-.10", "20384.0800.1.ok.9999.10"):
        assert parse_record(bad) is None, bad
    assert parse_name("3~阿司匹林~片") == (3, "阿司匹林", "片")
    assert parse_name("3~~片") is None
    assert parse_name("3~" + "药" * 40 + "~片")[1] == "药" * 20     # 60 bytes, whole characters


def test_records_round_trip_on_a_local_server(local_server):
    base, _ = local_server
    dev = "SIM-AAAA-0001"
    assert _med(base, "op=names&m=" + urllib.parse.quote("1~阿司匹林~片") + "&m=2~B~%E7%B2%92", dev)[0]["n"] == "2"
    assert _med(base, "op=put&r=20380.0800.1.ok.0805.10,20380.0800.2.miss.-.5,20381.2000.1.skip.-.10", dev)[0] == \
        {"t": "ok", "n": "3"}
    # the device's copy wins: a record put again replaces the stored one
    assert _med(base, "op=put&r=20380.0800.2.ok.0930.5", dev)[0]["n"] == "1"
    items = _med(base, "op=get&from=20380&off=0&lim=2", dev)
    assert [i["t"] for i in items] == ["r", "r", "end"]
    assert items[0] == {"t": "r", "d": "20380", "h": "08:00", "m": "1", "w": "ok", "a": "08:05", "n": "10",
                        "nm": "阿司匹林", "u": "片"}
    assert items[1]["w"] == "ok" and items[1]["a"] == "09:30" and items[1]["u"] == "粒"
    assert items[2] == {"t": "end", "n": "3", "more": "1"}
    rest = _med(base, "op=get&from=20380&off=2&lim=2", dev)
    assert rest[0]["d"] == "20381" and rest[0]["a"] == "-" and rest[-1]["more"] == "0"
    assert _med(base, "op=get&from=20381", dev)[-1]["n"] == "1"
    assert _med(base, "op=info", dev)[0] == {"t": "info", "n": "3", "first": "20380", "last": "20381"}
    # another device sees nothing of it
    assert _med(base, "op=info", "SIM-AAAA-0002")[0]["n"] == "0"
    # a local server takes a board's MAC without a password too
    assert _med(base, "op=put&r=20380.0800.1.ok.-.10", "C0FFEE00AA01")[0]["n"] == "1"
    assert _med(base, "op=info")[0]["t"] == "err"          # no device id: no records


def test_public_server_needs_the_password_for_a_board(public_server):
    base, server = public_server
    mac = "C0FFEE00BB01"
    err = _med(base, "op=info", mac)[0]
    assert err["t"] == "err" and "密码" in err["e"]          # a MAC is no proof
    assert _med(base, "op=info", "SIM-BBBB-0001")[0]["t"] == "info"    # a simulator's id is
    # the first request with a password registers the device
    st, r = _get(f"{base}/device/check", mac, "svc-pw-1")
    assert r["status"] == "new" and server.db.get_device(mac) is None
    assert _med(base, "op=put&r=20380.0800.1.ok.0805.10", mac, "svc-pw-1")[0] == {"t": "ok", "n": "1"}
    assert server.db.get_device(mac) is not None
    assert _get(f"{base}/device/check", mac, "svc-pw-1")[1]["status"] == "ok"
    # another password is refused, for any request
    assert _get(f"{base}/device/check", mac, "attacker")[1]["status"] == "password"
    st, r = _get(f"{base}/app/medicine?op=get&from=0", mac, "attacker")
    assert st == 403 and "wrong" in r["error"]
    st, r = _get(f"{base}/app/ping", mac, "attacker")
    assert st == 403
    # not base64
    st, r = _get(f"{base}/app/ping", mac)
    assert st == 200
    req = urllib.request.Request(f"{base}/app/ping", headers={"X-MiniScheme-Device": mac,
                                                              "X-MiniScheme-Password": "%%%"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
    # the right one reads the records back
    items = _med(base, "op=get&from=20000", mac, "svc-pw-1")
    assert items[0]["w"] == "ok" and items[-1]["n"] == "1"
    assert _get(f"{base}/device/check", mac)[1]["status"] == "plain"


def test_records_are_kept_out_of_the_access_log():
    url = "http://h:8087/app/medicine?op=put&r=20380.0800.1.ok.0805.10&m=1~x~y"
    assert redact_url(url) == "http://h:8087/app/medicine?op=put"


def test_clearing_and_deleting_a_device_removes_its_records(local_server):
    base, server = local_server
    dev = "SIM-CCCC-0001"
    _med(base, "op=put&r=20380.0800.1.ok.-.10", dev)
    server.db.clear_all_app_data(dev)
    assert server.db.med_summary(dev)[0] == 0
    _med(base, "op=put&r=20380.0800.1.ok.-.10", dev)
    server.db.delete_device_account(dev)
    assert server.db.med_summary(dev)[0] == 0


def test_the_user_portal_lists_and_clears_the_records(local_server):
    base, server = local_server
    dev = "SIM-DDDD-0001"
    _med(base, "op=names&m=1~" + urllib.parse.quote("维生素") + "~" + urllib.parse.quote("片"), dev)
    _med(base, "op=put&r=20380.0800.1.ok.0805.10,20381.0800.1.miss.-.5", dev)
    req = urllib.request.Request(f"{base}/api/user/sim_login", data=json.dumps({"uuid": dev}).encode(),
                                 headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())["token"]
    hdr = {"X-Session-Token": token}
    url = f"{base}/api/user/med_records?mac={dev}&off=0&lim=1"
    page = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=5).read().decode())
    assert page["total"] == 2 and page["items"][0]["day"] == 20381 and page["items"][0]["name"] == "维生素"
    with pytest.raises(urllib.error.HTTPError):          # another device's session: refused
        urllib.request.urlopen(urllib.request.Request(url.replace(dev, "SIM-DDDD-0002"), headers=hdr), timeout=5)
    clear = urllib.request.Request(f"{base}/api/user/data/clear?mac={dev}&app_id=medicine", data=b"", headers=hdr)
    urllib.request.urlopen(clear, timeout=5).read()
    assert server.db.med_summary(dev)[0] == 0
