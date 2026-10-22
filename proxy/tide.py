"""/app/tide for the 潮汐 app: Open-Meteo's global tide model (sea level above
mean sea level, hourly) for the device's place, resampled to 15 minutes, with
today's and tomorrow's highs and lows.

Ported from the app's own tide-server (deliverables/tide/server.py): the place
comes from the proxy's locate_place (loc=auto: the device's public IP, else a
city or "lon,lat"), so the app needs no server of its own.

Document: {"items": [meta, now, curve, upd, ext...]}
  meta  a station name, b lat, c lon, d place note, e model point
  now   a HH:MM, b cm, c cm/h (signed), d 1 rising / 0 falling
  curve a 97 comma-separated cm values, 00:00..24:00 every 15 min
  ext   a HH:MM, b cm, c high|low, d day offset (0 today, 1 tomorrow)
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any, Dict, List, Tuple

TZ = timezone(timedelta(hours=8))          # the firmware's default zone, CST-8
OPEN_METEO = "https://marine-api.open-meteo.com/v1/marine"
UA = "MiniScheme-Proxy/1.0 (tide)"
DOC_CACHE_S = 600
STATION_RADIUS_KM = 25.0

# Named places so the screen says 海口 rather than "N20.04 E110.13" (labels only)
PLACES = [
    (20.02, 110.27, "海口"), (19.55, 110.20, "澄迈"), (18.25, 109.51, "三亚"), (19.52, 109.58, "儋州"),
    (19.75, 110.95, "文昌"), (18.78, 110.39, "万宁"), (19.10, 108.65, "东方"), (19.24, 110.46, "琼海"),
    (23.35, 116.68, "汕头"), (24.48, 118.09, "厦门"), (26.07, 119.30, "福州"), (36.07, 120.32, "青岛"),
    (31.23, 121.47, "上海"), (22.54, 114.06, "深圳"), (22.27, 114.17, "香港"), (38.91, 121.60, "大连"),
    (39.13, 117.20, "天津"), (29.87, 121.55, "宁波"), (21.48, 109.12, "北海"), (21.20, 110.36, "湛江"),
]

_cache: Dict[Any, Tuple[float, Any]] = {}
_lock = threading.Lock()


class TideError(Exception):
    pass


def _json(url: str, timeout: float = 12) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _haversine(lat1, lon1, lat2, lon2):
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


def place_name(lat: float, lon: float) -> str:
    best, best_d = "", 1e9
    for plat, plon, name in PLACES:
        d = _haversine(lat, lon, plat, plon)
        if d < best_d:
            best, best_d = name, d
    return best if best_d <= STATION_RADIUS_KM else ""


def fetch_series(lat: float, lon: float):
    """[(datetime, metres)] hourly, UTC+8, one past day and three forecast days."""
    q = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "hourly": "sea_level_height_msl", "timezone": "Asia/Shanghai",
        "past_days": 1, "forecast_days": 3,
    })
    d = _json(f"{OPEN_METEO}?{q}")
    h = d.get("hourly") or {}
    out = []
    for ts, v in zip(h.get("time") or [], h.get("sea_level_height_msl") or []):
        if v is not None:
            out.append((datetime.fromisoformat(ts).replace(tzinfo=TZ), float(v)))
    if len(out) < 8:
        raise TideError("模型没有这个位置的潮位（内陆或高纬）：在应用设置里填沿海城市或经纬度")
    return out, (d.get("latitude"), d.get("longitude"))


def resample(points, step_min: int = 15):
    """Catmull-Rom through the hourly samples, step_min apart."""
    out, n = [], len(points)
    for i in range(n - 1):
        t0, v0 = points[i]
        vm1 = points[i - 1][1] if i > 0 else v0
        v1 = points[i + 1][1]
        v2 = points[i + 2][1] if i + 2 < n else v1
        for k in range(0, 60, step_min):
            u = k / 60.0
            v = 0.5 * ((2 * v0) + (-vm1 + v1) * u + (2 * vm1 - 5 * v0 + 4 * v1 - v2) * u * u +
                       (-vm1 + 3 * v0 - 3 * v1 + v2) * u * u * u)
            out.append((t0 + timedelta(minutes=k), v))
    out.append(points[-1])
    return out


def extrema(series, prominence_m: float = 0.08):
    """[(datetime, metres, 'high'|'low')]: a plateau is one candidate, and one
    standing less than prominence_m from its opposite neighbour is dropped
    (the 3-5 cm wiggles of a dead-water tide)."""
    cands, n, i = [], len(series), 1
    while i < n - 1:
        j = i
        while j + 1 < n - 1 and series[j + 1][1] == series[i][1]:
            j += 1
        v, left, right = series[i][1], series[i - 1][1], series[j + 1][1]
        if v > left and v > right:
            cands.append((i, j, "high"))
        elif v < left and v < right:
            cands.append((i, j, "low"))
        i = j + 1
    out = []
    for k, (i, j, kind) in enumerate(cands):
        v = series[i][1]
        prev = cands[k - 1] if k > 0 else None
        nxt = cands[k + 1] if k + 1 < len(cands) else None
        if prev and prev[2] != kind and abs(v - series[prev[0]][1]) < prominence_m:
            continue
        if nxt and nxt[2] != kind and abs(v - series[nxt[0]][1]) < prominence_m:
            continue
        out.append((series[(i + j) // 2][0], v, kind))
    return out


def value_at(series, when):
    if not series:
        return None
    if when <= series[0][0]:
        return series[0][1]
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= when <= t1:
            span = (t1 - t0).total_seconds() or 1
            return v0 + (v1 - v0) * ((when - t0).total_seconds() / span)
    return series[-1][1]


def build_items(lat: float, lon: float, note: str, now: datetime = None, series=None, model_point=None) -> List[Dict[str, str]]:
    now = now or datetime.now(TZ)
    if series is None:
        series, model_point = fetch_series(lat, lon)
    fine = resample(series, 15)
    ex = extrema(fine)
    day0 = datetime(now.year, now.month, now.day, tzinfo=TZ)
    day1 = day0 + timedelta(days=1)
    today = [(t, v) for t, v in fine if day0 <= t <= day1]
    if len(today) < 90:
        raise TideError("今天的潮位数据不完整")
    curve = [str(int(round(v * 100))) for _, v in today[:97]]
    curve += [curve[-1]] * (97 - len(curve))
    two_days = [e for e in ex if day0 <= e[0] < day0 + timedelta(days=2)]
    now_v = value_at(fine, now)
    before, after = value_at(fine, now - timedelta(minutes=10)), value_at(fine, now + timedelta(minutes=10))
    rate = (after - before) * 100 * 3 if before is not None and after is not None else 0.0
    station = place_name(lat, lon) or f"N{lat:.2f} E{lon:.2f}"
    mp = model_point or (lat, lon)
    items = [
        {"t": "meta", "a": station, "b": f"{lat:.2f}", "c": f"{lon:.2f}", "d": note,
         "e": f"open-meteo {mp[0]:.2f},{mp[1]:.2f}"},
        {"t": "now", "a": now.strftime("%H:%M"), "b": str(int(round(now_v * 100))), "c": f"{rate:+.0f}",
         "d": "1" if rate > 0 else "0"},
        {"t": "curve", "a": ",".join(curve)},
        {"t": "upd", "a": now.strftime("%H:%M")},
    ]
    for t, v, kind in two_days:
        items.append({"t": "ext", "a": t.strftime("%H:%M"), "b": str(int(round(v * 100))), "c": kind,
                      "d": str((t.date() - day0.date()).days)})
    return items


def get_tide(place: Dict[str, Any]) -> Dict[str, Any]:
    """The document for a place from locate_place ({name, lat, lon}); cached 10 min."""
    lat, lon = float(place["lat"]), float(place["lon"])
    key = (round(lat, 2), round(lon, 2))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < DOC_CACHE_S:
            return {"items": hit[1]}
    try:
        items = build_items(lat, lon, str(place.get("name") or ""))
    except TideError as e:
        return {"items": [{"t": "error", "a": str(e)}]}
    except Exception as e:   # the model unreachable: the app keeps its last data
        return {"items": [{"t": "error", "a": f"潮汐模型: {type(e).__name__}"}]}
    with _lock:
        _cache[key] = (now, items)
        for k in [k for k, (t, _) in _cache.items() if now - t > DOC_CACHE_S * 6]:
            del _cache[k]
    return {"items": items}
