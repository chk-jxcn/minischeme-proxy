"""A weather answer with a failed part, or a place no lookup found, is not cached: the next
request asks again (a failure at 17:00 stayed on the board for 10 minutes)."""
import io
import json
import urllib.error

from proxy import endpoints


class FakeQWeather:
    """urlopen for QWeather: each path answers, or fails while it is in self.down."""

    def __init__(self):
        self.down = set()
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        path = url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        self.calls.append(path)
        if any(d in path for d in self.down):
            raise urllib.error.URLError("timed out")
        body = {"code": "200"}
        if "city/lookup" in path:
            body["location"] = [{"name": "海口", "id": "101310101", "lat": "20.0", "lon": "110.3"}]
        elif path.endswith("weather/now"):
            body["now"] = {"temp": "26", "text": "晴", "icon": "100"}
        elif path.endswith("weather/7d"):
            body["daily"] = [{"fxDate": "2026-11-05", "tempMax": "30", "tempMin": "22"}]
        elif path.endswith("weather/24h"):
            body["hourly"] = [{"fxTime": "2026-11-05T17:00+08:00", "temp": "26", "pop": "10", "precip": "0"}]
        elif "indices" in path:
            body["daily"] = [{"name": "运动指数", "category": "适宜", "text": "好"}]
        return io.BytesIO(json.dumps(body).encode("utf-8"))


def test_weather_with_a_failed_part_is_not_cached(monkeypatch):
    fake = FakeQWeather()
    monkeypatch.setattr(endpoints.urllib.request, "urlopen", fake)
    endpoints.cache.clear()
    fake.down = {"weather/7d"}
    first = endpoints.get_weather("110.15,19.98", token="t", host="x.re.qweatherapi.com")
    assert any(i["t"] == "now" for i in first["items"]) and not any(i["t"] == "day" for i in first["items"])
    fake.down = set()
    n = len(fake.calls)
    second = endpoints.get_weather("110.15,19.98", token="t", host="x.re.qweatherapi.com")
    assert len(fake.calls) > n, "asked again"
    assert any(i["t"] == "day" for i in second["items"])
    n = len(fake.calls)
    third = endpoints.get_weather("110.15,19.98", token="t", host="x.re.qweatherapi.com")
    assert len(fake.calls) == n and third == second, "a complete answer is cached"


def test_weather_failure_is_not_cached(monkeypatch):
    fake = FakeQWeather()
    monkeypatch.setattr(endpoints.urllib.request, "urlopen", fake)
    endpoints.cache.clear()
    fake.down = {"weather/now"}
    assert endpoints.get_weather("110.15,19.98", token="t", host="x.re.qweatherapi.com")["items"][0]["t"] == "error"
    fake.down = set()
    assert any(i["t"] == "now" for i in endpoints.get_weather("110.15,19.98", token="t", host="x.re.qweatherapi.com")["items"])


def test_place_not_found_is_not_cached(monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(endpoints.urllib.request, "urlopen", down)
    endpoints.cache.clear()
    assert endpoints.locate_place("auto", client_ip="112.66.140.64")["name"] == "北京"
    assert endpoints.cache.get("ip_geo_112.66.140.64") is None
