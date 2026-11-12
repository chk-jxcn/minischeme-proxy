"""/app/geo: the time zone of the asking address, as a TZ rule the board can use."""
import io
import json
import urllib.error

from proxy import endpoints, geo


def fake_urlopen(answers):
    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for part, body in answers.items():
            if part in url:
                if body is None:
                    raise urllib.error.URLError("timed out")
                return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        raise urllib.error.URLError("no such host in this test: " + url)
    return urlopen


def test_posix_rules():
    assert geo.posix_tz("Asia/Shanghai") == "CST-8"
    assert geo.posix_tz("America/New_York") == "EST5EDT,M3.2.0,M11.1.0"
    assert geo.posix_tz("Europe/Paris") == "CET-1CEST,M3.5.0,M10.5.0/3"
    assert geo.posix_tz("No/Such_Zone", 19800) == "<+0530>-5:30"
    assert geo.posix_tz("../../etc/passwd") == ""
    assert geo.fixed_posix(28800) == "<+08>-8"
    assert geo.fixed_posix(-18000) == "<-05>5"
    assert geo.fixed_posix(0) == "UTC0"


def test_geo_from_ip_api(monkeypatch):
    endpoints.cache.clear()
    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen({
        "ip-api.com/json/8.8.8.8": {"status": "success", "countryCode": "US", "city": "Ashburn",
                                    "timezone": "America/New_York", "offset": -18000}}))
    status, ctype, body = endpoints.handle_app_query("/app/geo", client_ip="8.8.8.8")
    d = json.loads(body)
    assert status == 200 and d["tz"] == "America/New_York" and d["posix"] == "EST5EDT,M3.2.0,M11.1.0"
    assert d["city"] == "Ashburn" and d["country"] == "US"
    # cached: no second lookup
    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen({}))
    assert json.loads(endpoints.handle_app_query("/app/geo", client_ip="8.8.8.8")[2])["posix"] == d["posix"]


def test_geo_second_source_and_local_client(monkeypatch):
    endpoints.cache.clear()
    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen({
        "myip.ipip.net": "当前 IP：1.2.3.4  来自于：中国 上海".encode("utf-8"),
        "ip-api.com": None,
        "ipwho.is/1.2.3.4": {"success": True, "country_code": "CN", "city": "Shanghai",
                             "timezone": {"id": "Asia/Shanghai", "offset": 28800}}}))
    status, _, body = endpoints.handle_app_query("/app/geo", client_ip="192.168.2.184")
    d = json.loads(body)
    assert status == 200 and d["posix"] == "CST-8" and d["tz"] == "Asia/Shanghai"


def test_geo_nothing_found_is_an_error_and_not_cached(monkeypatch):
    endpoints.cache.clear()
    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen({}))
    monkeypatch.setattr(endpoints.urllib.request, "urlopen", fake_urlopen({}))
    status, _, body = endpoints.handle_app_query("/app/geo", client_ip="8.8.4.4")
    assert status == 502 and "error" in json.loads(body)
    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen({
        "ip-api.com/json/8.8.4.4": {"status": "success", "countryCode": "US", "city": "X",
                                    "timezone": "America/Chicago", "offset": -21600}}))
    assert json.loads(endpoints.handle_app_query("/app/geo", client_ip="8.8.4.4")[2])["posix"] == "CST6CDT,M3.2.0,M11.1.0"
