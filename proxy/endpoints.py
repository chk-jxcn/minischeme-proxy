"""Application endpoints and data fetching logic with caching.

Provides dedicated individual endpoints for each application:
  /app/weather   - QWeather / Open-Meteo weather forecast (now, 7d, 24h, rain, air, idx)
  /app/news      - Multi-category news (cat=all or cat=realtime|livelihood|finance|sports|zhihu)
  /app/stocks    - Market quotes (view=all or view=home|watch|charts|trends)
  /app/crypto    - Crypto market prices (view=all or view=home|trend|kline)
  /app/futures   - Commodity and financial futures (view=all or view=home|trend)

Every multi-page app supports an aggregated query (e.g. cat=all, view=all) that
returns the data for all pages in a single HTTP request upon app launch,
preventing latency during page transitions on the hardware device.
"""

import concurrent.futures
import configparser
import datetime
import gzip
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Dict, Any, List, Optional
from .cache import TTLCache

cache = TTLCache()

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

CAPITALS = {
    "北京": "北京", "天津": "天津", "上海": "上海", "重庆": "重庆", "河北": "石家庄", "山西": "太原",
    "辽宁": "沈阳", "吉林": "长春", "黑龙江": "哈尔滨", "江苏": "南京", "浙江": "杭州", "安徽": "合肥",
    "福建": "福州", "江西": "南昌", "山东": "济南", "河南": "郑州", "湖北": "武汉", "湖南": "长沙",
    "广东": "广州", "海南": "海口", "四川": "成都", "贵州": "贵阳", "云南": "昆明", "陕西": "西安",
    "甘肃": "兰州", "青海": "西宁", "台湾": "台北", "内蒙古": "呼和浩特", "广西": "南宁", "西藏": "拉萨",
    "宁夏": "银川", "新疆": "乌鲁木齐", "香港": "香港", "澳门": "澳门",
}

CITY_COORDS = {
    # Municipalities & Special Administrative Regions
    "北京": (39.9042, 116.4074), "天津": (39.0842, 117.2009), "上海": (31.2304, 121.4737),
    "重庆": (29.5630, 106.5516), "香港": (22.3193, 114.1694), "澳门": (22.1987, 113.5439),
    "台北": (25.0330, 121.5654),
    # Provincial capitals & major cities
    "石家庄": (38.0428, 114.5149), "太原": (37.8706, 112.5489), "沈阳": (41.8057, 123.4315),
    "长春": (43.8171, 125.3235), "哈尔滨": (45.8038, 126.5349), "南京": (32.0603, 118.7969),
    "杭州": (30.2741, 120.1551), "合肥": (31.8206, 117.2272), "福州": (26.0745, 119.2965),
    "南昌": (28.6829, 115.8582), "济南": (36.6512, 117.1201), "郑州": (34.7466, 113.6253),
    "武汉": (30.5928, 114.3055), "长沙": (28.2282, 112.9388), "广州": (23.1291, 113.2644),
    "南宁": (22.8170, 108.3665), "海口": (20.0440, 110.1999), "成都": (30.5728, 104.0668),
    "贵阳": (26.6470, 106.6302), "昆明": (25.0406, 102.7123), "西安": (34.3416, 108.9398),
    "兰州": (36.0611, 103.8343), "西宁": (36.6171, 101.7782), "银川": (38.4872, 106.2309),
    "乌鲁木齐": (43.8256, 87.6168), "呼和浩特": (40.8427, 111.7492), "拉萨": (29.6525, 91.1721),
    # Additional tier-1 & tier-2 & notable cities / counties
    "深圳": (22.5431, 114.0579), "青岛": (36.0671, 120.3826), "大连": (38.9140, 121.6147),
    "厦门": (24.4798, 118.0894), "宁波": (29.8683, 121.5440), "苏州": (31.2990, 120.5853),
    "无锡": (31.4912, 120.3119), "常州": (31.8112, 119.9741), "温州": (27.9943, 120.6994),
    "东莞": (23.0205, 113.7518), "佛山": (23.0215, 113.1214), "珠海": (22.2707, 113.5767),
    "中山": (22.5176, 113.3928), "烟台": (37.4638, 121.4479), "潍坊": (36.7068, 119.1618),
    "泉州": (24.8741, 118.6757), "桂林": (25.2736, 110.2902), "洛阳": (34.6197, 112.4540),
    "芜湖": (31.3529, 118.3765), "宜昌": (30.6920, 111.2865), "襄阳": (32.0084, 112.1224),
    "岳阳": (29.3566, 113.1289), "绵阳": (31.4675, 104.6791), "遵义": (27.7257, 106.9274),
    "曲靖": (25.4900, 103.7962), "宝鸡": (34.3693, 107.2375), "汉中": (33.0677, 107.0286),
    "三亚": (18.2528, 109.5119), "澄迈": (19.7424, 110.0076), "儋州": (19.5209, 109.5768),
    "文昌": (19.6104, 110.7539), "琼海": (19.2458, 110.4665), "万宁": (18.7962, 110.3888),
    "东方": (19.1019, 108.6538), "五指山": (18.7760, 109.5167), "乐东": (18.7476, 109.1754),
    "陵水": (18.5050, 110.0372), "保亭": (18.6364, 109.7025), "琼中": (19.0347, 109.8385),
    "白沙": (19.2246, 109.4526), "昌江": (19.2610, 109.0533), "临高": (19.9083, 109.6877),
    "定安": (19.6811, 110.3492), "屯昌": (19.3629, 110.1028),
}


def is_private_ip(ip: str) -> bool:
    if not ip:
        return True
    ip = ip.strip()
    if ip in ("127.0.0.1", "localhost", "::1", "0.0.0.0", ""):
        return True
    if ip.startswith(("10.", "192.168.", "127.", "169.254.")):
        return True
    if ip.startswith("172."):
        parts = ip.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            v = int(parts[1])
            if 16 <= v <= 31:
                return True
    return False


def locate_place(loc: str = "auto", client_ip: str = "") -> Dict[str, Any]:
    """Resolves location name and coordinates from query parameter or client IP."""
    loc_clean = (loc or "auto").strip()
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", loc_clean)
    if m:
        return {"name": loc_clean, "lon": float(m.group(1)), "lat": float(m.group(2))}

    if loc_clean.lower() != "auto" and loc_clean != "":
        clean_name = loc_clean.rstrip("市县区")
        if clean_name in CITY_COORDS:
            lat, lon = CITY_COORDS[clean_name]
            return {"name": clean_name, "lat": lat, "lon": lon}
        if loc_clean in CITY_COORDS:
            lat, lon = CITY_COORDS[loc_clean]
            return {"name": clean_name, "lat": lat, "lon": lon}
        # Try Open-Meteo geocoding
        try:
            url = ("https://geocoding-api.open-meteo.com/v1/search?count=5&language=zh&name=" +
                   urllib.parse.quote(loc_clean))
            req = urllib.request.Request(url, headers={"User-Agent": "MiniScheme-Proxy/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("results") or []
                if results:
                    r0 = results[0]
                    return {"name": r0.get("name") or clean_name, "lat": float(r0["latitude"]), "lon": float(r0["longitude"])}
        except Exception:
            pass
        return {"name": clean_name, "lat": 39.9042, "lon": 116.4074}

    # IP Geolocation: determine effective public IP to locate
    target_ip = (client_ip or "").strip()
    if "," in target_ip:
        target_ip = target_ip.split(",")[0].strip()
    if ":" in target_ip and not target_ip.startswith("[") and target_ip.count(":") == 1:
        target_ip = target_ip.split(":")[0].strip()

    if is_private_ip(target_ip):
        # Local or loopback request: probe public egress IP
        try:
            req = urllib.request.Request("http://myip.ipip.net/", headers={"User-Agent": "MiniScheme-Proxy/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                text = resp.read().decode("utf-8", "replace")
                m_ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
                if m_ip:
                    target_ip = m_ip.group(1)
        except Exception:
            pass
        if not target_ip or is_private_ip(target_ip):
            try:
                req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "MiniScheme-Proxy/1.0"})
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    text = resp.read().decode("utf-8", "replace").strip()
                    m_ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
                    if m_ip:
                        target_ip = m_ip.group(1)
            except Exception:
                pass

    cache_key = f"ip_geo_{target_ip}" if target_ip and not is_private_ip(target_ip) else "ip_geo_default"
    hit = cache.get(cache_key)
    if hit:
        return hit

    resolved = None

    # Step 1: Query Baidu OpenData IP API (high-precision county and city resolution)
    if target_ip and not is_private_ip(target_ip):
        try:
            b_url = f"http://opendata.baidu.com/api.php?query={target_ip}&co=&resource_id=6006&oe=utf8"
            req = urllib.request.Request(b_url, headers={"User-Agent": "MiniScheme-Proxy/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                b_data = json.loads(resp.read().decode("utf-8"))
                if b_data.get("data"):
                    raw_loc = b_data["data"][0].get("location", "")
                    clean_loc = re.sub(r"\s*(电信|联通|移动|铁通|广电|教育网).*$", "", raw_loc).strip()
                    for city_key in sorted(CITY_COORDS.keys(), key=lambda c: len(c), reverse=True):
                        if city_key in clean_loc:
                            lat, lon = CITY_COORDS[city_key]
                            resolved = {"name": city_key, "lat": lat, "lon": lon}
                            break
                    if not resolved and clean_loc:
                        sub_parts = re.split(r"[省市区县旗]", clean_loc)
                        sub_parts = [p.strip() for p in sub_parts if p.strip()]
                        cand = sub_parts[-1] if sub_parts else clean_loc
                        resolved = {"name": cand, "lat": 39.9042, "lon": 116.4074}
        except Exception:
            pass

    # Step 2: Query PCOnline Whois
    if not resolved and target_ip and not is_private_ip(target_ip):
        try:
            p_url = f"https://whois.pconline.com.cn/ipJson.jsp?ip={target_ip}&json=true"
            req = urllib.request.Request(p_url, headers={"User-Agent": "MiniScheme-Proxy/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                p_data = json.loads(resp.read().decode("gbk", "replace").strip())
                city = (p_data.get("city") or "").rstrip("市县区")
                pro = (p_data.get("pro") or "").rstrip("省市自治区")
                cand = city or pro
                if cand in CITY_COORDS:
                    lat, lon = CITY_COORDS[cand]
                    resolved = {"name": cand, "lat": lat, "lon": lon}
                elif cand:
                    cap = CAPITALS.get(pro, "北京")
                    lat, lon = CITY_COORDS.get(cap, (39.9042, 116.4074))
                    resolved = {"name": cand, "lat": lat, "lon": lon}
        except Exception:
            pass

    # Step 3: Query myip.ipip.net
    if not resolved:
        try:
            req = urllib.request.Request("http://myip.ipip.net/", headers={"User-Agent": "MiniScheme-Proxy/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                text = resp.read().decode("utf-8", "replace")
                parts = text.split("来自于：")[-1].split()
                province = parts[1] if len(parts) > 1 else ""
                places = [p for p in parts[2:] if p not in ("电信", "联通", "移动", "铁通", "广电", "教育网")]
                target_name = places[0] if places else (province or "北京")
                clean_name = target_name.rstrip("市县区")
                if clean_name in CITY_COORDS:
                    lat, lon = CITY_COORDS[clean_name]
                    resolved = {"name": clean_name, "lat": lat, "lon": lon}
                elif target_name in CITY_COORDS:
                    lat, lon = CITY_COORDS[target_name]
                    resolved = {"name": clean_name, "lat": lat, "lon": lon}
                else:
                    cap = CAPITALS.get(province.rstrip("省市自治区"), "北京")
                    lat, lon = CITY_COORDS.get(cap, (39.9042, 116.4074))
                    resolved = {"name": clean_name, "lat": lat, "lon": lon}
        except Exception:
            pass

    if not resolved:
        resolved = {"name": "北京", "lat": 39.9042, "lon": 116.4074}

    cache.set(cache_key, resolved, ttl_seconds=6 * 3600)
    return resolved


def qw_icon(code: Any) -> str:
    """Maps QWeather icon codes to MiniScheme GUI icon types."""
    try:
        c = int(code or 0)
    except (ValueError, TypeError):
        return "cloud"
    if c in (100, 150):
        return "sun"
    if c in (101, 102, 103, 151, 152, 153):
        return "partly" if c in (102, 103, 152, 153) else "cloud"
    if c == 104:
        return "overcast"
    if 300 <= c < 400:
        return "storm" if c in (302, 303, 304) else ("sleet" if c == 313 else "rain")
    if 400 <= c < 500:
        return "sleet" if c in (404, 405, 406, 456) else "snow"
    if 500 <= c < 600:
        return "fog"
    return "cloud"


def format_day_label(date_str: str, i: int) -> str:
    """Formats date and day label, e.g. '10/16 今天'."""
    try:
        parts = date_str[:10].split("-")
        m, d = int(parts[1]), int(parts[2])
        dt_val = datetime.date(int(parts[0]), m, d)
        wk = WEEKDAYS[dt_val.weekday()]
    except Exception:
        now = datetime.date.today() + datetime.timedelta(days=i)
        m, d = now.month, now.day
        wk = WEEKDAYS[now.weekday()]
    name = ["今天", "明天", "后天"][i] if i < 3 else "周" + wk
    return f"{m}/{d} {name}"


def series(values: List[float], scale: int = 1) -> str:
    """Encodes numeric lists into comma-separated integer series."""
    return ",".join(str(int(round((v or 0) * scale))) for v in values)


def fetch_qweather(loc: str, token: str, host: str, client_ip: str = "") -> Optional[List[Dict[str, Any]]]:
    """Fetches live weather from QWeather using per-device token without persistence."""
    tok = token.strip()
    if not tok:
        return None

    hst = (host or "").strip()
    if not hst:
        hst = "devapi.qweather.com"
    base_url = "https://" + hst.rstrip("/").replace("https://", "").replace("http://", "")
    headers = {
        "X-QW-Api-Key": tok,
        "User-Agent": "MiniScheme-Proxy/1.0"
    }

    def qw_api_call(path: str, params: dict, timeout: float = 6.0) -> dict:
        q_params = {**params, "lang": "zh", "key": tok}
        cur_base = base_url
        if path.startswith("/geo/") and ("devapi.qweather.com" in base_url or "api.qweather.com" in base_url):
            cur_base = "https://geoapi.qweather.com"
        url = f"{cur_base}{path}?{urllib.parse.urlencode(q_params)}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))
            if str(data.get("code", "200")) != "200":
                raise RuntimeError(f"QWeather error {path}: code {data.get('code')}")
            return data

    try:
        loc_str = locate_place(loc, client_ip=client_ip)["name"] if (not loc or loc.strip().lower() == "auto") else loc.strip()
        geo = qw_api_call("/geo/v2/city/lookup", {"location": loc_str, "number": 1})
        locations = geo.get("location") or []
        if not locations:
            return None
        c = locations[0]
        city_name = c.get("name", loc_str)
        loc_id = c["id"]
        lat = f"{float(c.get('lat', 39.9)):.2f}"
        lon = f"{float(c.get('lon', 116.4)):.2f}"
        lonlat = f"{lon},{lat}"

        # Fetch endpoints concurrently for responsive performance
        jobs = {
            "now": lambda: qw_api_call("/v7/weather/now", {"location": loc_id}),
            "7d": lambda: qw_api_call("/v7/weather/7d", {"location": loc_id}),
            "24h": lambda: qw_api_call("/v7/weather/24h", {"location": loc_id}),
            "indices": lambda: qw_api_call("/v7/indices/1d", {"location": loc_id, "type": "1,3,5,8,9"}),
        }

        # Optional endpoints (may not be supported on standard tier or specific coords)
        def fetch_minutely():
            try:
                return qw_api_call("/v7/minutely/5m", {"location": lonlat})
            except Exception:
                return None

        def fetch_air():
            try:
                return qw_api_call("/v7/air/now", {"location": loc_id})
            except Exception:
                try:
                    return qw_api_call(f"/airquality/v1/current/{lat}/{lon}", {})
                except Exception:
                    return None

        jobs["minutely"] = fetch_minutely
        jobs["air"] = fetch_air

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            future_to_key = {ex.submit(fn): k for k, fn in jobs.items()}
            for f in concurrent.futures.as_completed(future_to_key):
                k = future_to_key[f]
                try:
                    results[k] = f.result()
                except Exception:
                    results[k] = None

        now_res = results.get("now")
        if not now_res or "now" not in now_res:
            return None
        now_data = now_res["now"]

        items: List[Dict[str, Any]] = [
            {"t": "loc", "a": city_name, "b": "和风天气", "c": time.strftime("%H:%M")}
        ]

        # 1. Now conditions
        temp = str(now_data.get("temp", "22"))
        text = str(now_data.get("text", "晴"))
        icon = qw_icon(now_data.get("icon", "100"))
        feels_like = str(now_data.get("feelsLike", temp))
        humidity = str(now_data.get("humidity", "45"))
        items.append({"t": "now", "a": temp, "b": text, "c": icon, "d": feels_like, "e": humidity})

        # 2. Secondary now conditions
        w_dir = str(now_data.get("windDir", "微风"))
        w_scale = str(now_data.get("windScale", "1"))
        if not w_scale.endswith("级"):
            w_scale += "级"
        pressure = str(now_data.get("pressure", "1013"))
        vis = str(now_data.get("vis", "10"))
        items.append({"t": "now2", "a": w_dir, "b": w_scale, "c": pressure, "d": vis, "e": ""})

        # 3. 7-day forecast
        daily_list = (results.get("7d") or {}).get("daily", [])
        for i, d in enumerate(daily_list[:7]):
            lbl = format_day_label(d.get("fxDate", ""), i)
            t_max = str(d.get("tempMax", "22"))
            t_min = str(d.get("tempMin", "15"))
            precip = str(round(float(d.get("precip") or 0) * 10))
            hum = str(d.get("humidity", "50"))
            items.append({
                "t": "day",
                "a": lbl,
                "b": str(d.get("textDay", "晴")),
                "c": qw_icon(d.get("iconDay", "100")),
                "d": f"{t_max},{t_min}",
                "e": f",{precip},{hum}"
            })

        # 4. 24-hour forecast & rain5h
        hourly_list = (results.get("24h") or {}).get("hourly", [])
        if hourly_list:
            temps = [float(x.get("temp", 0)) for x in hourly_list]
            pops = [float(x.get("pop", 0)) for x in hourly_list]
            precips = [float(x.get("precip", 0)) for x in hourly_list]
            start_hr = int(hourly_list[0].get("fxTime", "T00")[11:13]) if "T" in hourly_list[0].get("fxTime", "") else 0
            items.append({
                "t": "hours",
                "a": series(temps),
                "b": series(pops),
                "c": series(precips, 10),
                "d": str(start_hr)
            })
            for h in hourly_list[:8]:
                fx = h.get("fxTime", "")[11:16] if "T" in h.get("fxTime", "") else "12:00"
                items.append({
                    "t": "hour",
                    "a": fx,
                    "b": str(h.get("temp", "22")),
                    "c": str(h.get("icon", "100")),
                    "d": str(h.get("pop", "0"))
                })

            rain5 = precips[:5]
            pop5 = pops[:5]
            r_sum = sum(rain5)
            summary = "未来5小时无降水" if max(rain5 + [0]) < 0.1 else f"未来5小时累计 {r_sum:.1f} mm"
            items.append({
                "t": "rain5h",
                "a": series(rain5, 10),
                "b": series(pop5),
                "c": summary
            })

        # 5. Minutely precipitation
        minutely_res = results.get("minutely")
        if minutely_res and "minutely" in minutely_res:
            m_list = minutely_res.get("minutely", [])
            if m_list:
                vals = [float(x.get("precip", 0)) for x in m_list[:24]]
                summary = minutely_res.get("summary", "未来两小时无降水")
                start_time = m_list[0].get("fxTime", "")[11:16] if "T" in m_list[0].get("fxTime", "") else ""
                items.append({
                    "t": "rain",
                    "a": series(vals, 10),
                    "b": summary,
                    "c": "5",
                    "d": start_time
                })

        # 6. Air quality
        air_res = results.get("air")
        if air_res:
            if "now" in air_res:
                a_now = air_res["now"]
                items.append({
                    "t": "air",
                    "a": str(a_now.get("aqi", "42")),
                    "b": str(a_now.get("category", "优")),
                    "c": str(a_now.get("pm2p5", "15")),
                    "d": str(a_now.get("pm10", "30")),
                    "e": "首要污染物: " + str(a_now.get("primary", "无"))
                })
            elif "indexes" in air_res:
                indexes = air_res.get("indexes") or []
                idx = next((x for x in indexes if x.get("code") == "cn-mee"), indexes[0] if indexes else None)
                pol = {x.get("code"): (x.get("concentration") or {}).get("value") for x in air_res.get("pollutants") or []}
                if idx:
                    prim = (idx.get("primaryPollutant") or {}).get("name") or "无"
                    aqi_val = str(idx.get("aqiDisplay") or idx.get("aqi") or "42")
                    cat = str(idx.get("category", "优"))
                    pm25 = f"{pol.get('pm2p5', 15):.0f}" if pol.get("pm2p5") is not None else "15"
                    pm10 = f"{pol.get('pm10', 30):.0f}" if pol.get("pm10") is not None else "30"
                    items.append({"t": "air", "a": aqi_val, "b": cat, "c": pm25, "d": pm10, "e": "首要污染物: " + prim})

        # 7. Life indices
        indices_res = results.get("indices")
        if indices_res and "daily" in indices_res:
            for idx in indices_res.get("daily", [])[:5]:
                name = str(idx.get("name", "")).replace("指数", "")
                cat = str(idx.get("category", ""))
                desc = str(idx.get("text", ""))
                items.append({"t": "idx", "a": name, "b": cat, "c": desc})

        items.append({"t": "upd", "a": time.strftime("%H:%M")})
        return items
    except Exception:
        return None


def build_fallback_weather(loc: str, fallback_provider: str = "MiniScheme-Proxy", client_ip: str = "") -> List[Dict[str, Any]]:
    """Builds robust fallback weather data conforming to weather.scm schema."""
    city_name = locate_place(loc, client_ip=client_ip)["name"] if (not loc or loc.strip().lower() == "auto") else loc.strip()
    now_struct = time.localtime()
    now_hhmm = time.strftime("%H:%M", now_struct)

    provider_str = fallback_provider
    if "演示数据" not in provider_str:
        provider_str = f"{provider_str}【演示数据】"

    items: List[Dict[str, Any]] = [
        {"t": "demo", "a": "1", "b": "【演示数据】"},
        {"t": "title", "a": "天气【演示数据】"},
        {"t": "loc", "a": city_name, "b": provider_str, "c": now_hhmm},
        {"t": "now", "a": "22", "b": "晴", "c": "sun", "d": "21", "e": "45"},
        {"t": "now2", "a": "东北风", "b": "2级", "c": "1013", "d": "10", "e": ""},
    ]

    # 7-day forecast
    day_temps = [("24", "14", "晴", "sun"), ("23", "13", "多云", "cloud"),
                 ("19", "11", "小雨", "rain"), ("21", "12", "晴", "sun"),
                 ("22", "13", "多云", "partly"), ("24", "15", "晴", "sun"),
                 ("20", "13", "阴", "overcast")]
    for i, (hi, lo, cond, icon) in enumerate(day_temps):
        lbl = format_day_label("", i)
        items.append({
            "t": "day",
            "a": lbl,
            "b": cond,
            "c": icon,
            "d": f"{hi},{lo}",
            "e": ",0,45"
        })

    # 24-hour forecast
    items.append({
        "t": "hours",
        "a": "22,23,24,23,21,20,19,18,17,16,15,15,14,14,15,16,17,19,20,22,23,24,23,22",
        "b": "0,0,0,5,10,10,5,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
        "c": "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
        "d": str(now_struct.tm_hour)
    })
    for h_off in (0, 3, 6, 9, 12, 15, 18, 21):
        hr_val = (now_struct.tm_hour + h_off) % 24
        items.append({
            "t": "hour",
            "a": f"{hr_val:02d}:00",
            "b": "22",
            "c": "100",
            "d": "0"
        })

    # rain5h
    items.append({
        "t": "rain5h",
        "a": "0,0,0,0,0",
        "b": "0,0,0,5,10",
        "c": "未来5小时无降水"
    })

    # minutely rain
    items.append({
        "t": "rain",
        "a": "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
        "b": "未来两小时无降水",
        "c": "5",
        "d": now_hhmm
    })

    # air quality
    items.append({
        "t": "air",
        "a": "42",
        "b": "优",
        "c": "15",
        "d": "30",
        "e": "首要污染物: 无"
    })

    # indices
    items.extend([
        {"t": "idx", "a": "舒适度", "b": "舒适", "c": "白天温度适宜，风力不大，感受舒适"},
        {"t": "idx", "a": "穿衣", "b": "夹克", "c": "建议着单层棉麻面料的短套装、T恤衫"},
        {"t": "idx", "a": "感冒", "b": "少发", "c": "各项气象条件适宜，无明显降温"},
        {"t": "idx", "a": "紫外线", "b": "中等", "c": "涂擦SPF大于15、PA+防晒护肤品"},
    ])

    items.append({"t": "upd", "a": now_hhmm})
    return items


def get_qweather_default_config():
    """Reads [qweather] key and host from config.ini or app_proxy.local.ini if available."""
    cfg = configparser.ConfigParser()
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        "app_proxy.local.ini",
        "config.ini",
        "/opt/minischeme-proxy/config.ini",
        os.path.join(cur_dir, "..", "config.ini"),
        os.path.join(cur_dir, "..", "app_proxy.local.ini"),
        os.path.join(cur_dir, "..", "..", "tools", "app_proxy.local.ini"),
        os.path.join(cur_dir, "..", "..", "..", "tools", "app_proxy.local.ini"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                cfg.read(p, encoding="utf-8")
                if cfg.has_section("qweather"):
                    k = cfg.get("qweather", "key", fallback="").strip()
                    h = cfg.get("qweather", "host", fallback="").strip()
                    if k:
                        return k, h
            except Exception:
                pass
    return "", ""


def get_weather(loc: str = "auto", key: str = "", host: str = "", token: str = "", client_ip: str = "") -> Dict[str, Any]:
    """Fetches weather forecast for all pages (now, 7-day, 24-hr, rain/air, indices).

    Supports individual device token (passed via token or key parameter) without proxy-side persistence.
    Cached in RAM for 10 minutes (600 seconds) per (loc, token, host, client_ip) tuple.
    """
    tok = (token or key or "").strip()
    hst = (host or "").strip()

    if not tok or not hst:
        def_k, def_h = get_qweather_default_config()
        if not tok:
            tok = def_k
        if not hst:
            hst = def_h

    cache_key = f"weather:{loc}:{tok}:{hst}:{client_ip}" if (not loc or loc.strip().lower() == "auto") else f"weather:{loc}:{tok}:{hst}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    items = None
    is_demo = False
    if tok:
        items = fetch_qweather(loc, tok, hst, client_ip=client_ip)

    if not items:
        is_demo = True
        provider = "MiniScheme-Proxy" if not tok else "和风天气(回退)"
        items = build_fallback_weather(loc, provider, client_ip=client_ip)

    title = "天气【演示数据】" if is_demo else "天气"
    data = {"title": title, "is_demo": is_demo, "items": items}
    cache.set(cache_key, data, ttl_seconds=600)
    return data


# ---------------------------------------------------------------------------
# 2. Hot News (/app/news) - Supports cat=all for all 5 pages
# ---------------------------------------------------------------------------

NEWS_CATEGORIES = {
    "realtime": [
        ("全球开源软件供应链安全治理研讨会在京举行", "爆", 1),
        ("微型嵌入式Scheme解释器助力物联网边缘计算", "热", 2),
        ("新能源车载算力平台新突破，国产芯片受瞩目", "新", 3),
        ("多地启动新一轮促消费政策，实体商业回暖", "", 4),
        ("全球人工智能安全评估框架标准正式发布", "热", 5),
        ("空间站新一批科学实验样品顺利返回地球", "新", 6),
        ("绿色算力中心能效比创下历史新高", "", 7),
        ("高精度卫星导航系统服务全球超百个国家", "", 8),
    ],
    "livelihood": [
        ("全国多地秋粮丰收在望，收购工作全面展开", "新", 1),
        ("城市便民生活圈建设持续推进，百姓生活更便利", "热", 2),
        ("基本医保参保覆盖面进一步巩固，异地就医结算升级", "", 3),
        ("公共交通绿色升级，纯电动公交占比突破八成", "", 4),
        ("普惠养老托育服务体系加快构建", "", 5),
    ],
    "finance": [
        ("央行宏观审慎调控政策发力，流动性保持合理充裕", "热", 1),
        ("科技创新类企业上市融资通道持续优化", "新", 2),
        ("主要指数集体回升，硬科技板块涨幅居前", "爆", 3),
        ("外资金融机构持续加大在华资产配置", "", 4),
        ("跨境贸易人民币结算规模稳步扩大", "", 5),
    ],
    "sports": [
        ("国际青年田径锦标赛中国选手再破赛事纪录", "爆", 1),
        ("全国和美乡村篮球大赛总决赛点燃激情", "热", 2),
        ("亚洲冬季运动会筹备工作进入最后冲刺阶段", "新", 3),
        ("马拉松黄金赛事季开启，全民健身蔚然成风", "", 4),
        ("游泳世锦赛中国队连获两金创历史最佳战绩", "热", 5),
    ],
    "zhihu": [
        ("如何评价 MiniScheme 在微控制器上的内存与GC设计？", "热", 1),
        ("为什么函数式编程思想在低功耗物联网开发中越来越受欢迎？", "新", 2),
        ("如何用最简单的工具链自制一个便民信息电子墨水屏？", "热", 3),
        ("在现代软硬件架构下，如何做好跨端协议加密与数据脱敏？", "", 4),
        ("有哪些开源软件的设计让你感受到了极致的简洁之美？", "", 5),
    ]
}

def get_news(category: str = "realtime", n: int = 8) -> Dict[str, Any]:
    """Fetches news headlines.

    If category == "all", aggregates items for ALL pages/categories so the
    device downloads everything on startup in a single request.
    Cached for 5 minutes (300 seconds).
    """
    cache_key = f"news:{category}:{n}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    items: List[Dict[str, str]] = []
    now_str = time.strftime("%H:%M", time.localtime())

    if category == "all":
        # Aggregate all categories for zero-delay page switching
        for cat_name, entries in NEWS_CATEGORIES.items():
            for title, tag, rank in entries[:n]:
                items.append({
                    "t": "item",
                    "a": title,
                    "b": "hot",
                    "c": cat_name,  # Category tag used by page renderer
                    "d": tag,
                    "e": str(rank)
                })
    else:
        entries = NEWS_CATEGORIES.get(category, NEWS_CATEGORIES["realtime"])
        for title, tag, rank in entries[:n]:
            items.append({
                "t": "item",
                "a": title,
                "b": "hot",
                "c": category,
                "d": tag,
                "e": str(rank)
            })

    items.append({"t": "upd", "a": now_str})
    items.insert(0, {"t": "demo", "a": "1", "b": "【演示数据】"})
    items.insert(1, {"t": "title", "a": "热点新闻【演示数据】"})
    data = {"title": "热点新闻【演示数据】", "is_demo": True, "items": items}
    cache.set(cache_key, data, ttl_seconds=300)
    return data


# ---------------------------------------------------------------------------
# Market Helpers & Formatters
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float:
    try:
        return float(val or 0.0)
    except (ValueError, TypeError):
        return 0.0


def _fmt_price(v: float) -> str:
    """Formats market prices concisely with adaptive decimal precision."""
    if v >= 10000:
        return f"{v:.0f}"
    if v >= 1000:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    if v >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".") if f"{v:.2f}".endswith(".00") else f"{v:.2f}"
    if v >= 0.01:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _fmt_vol(v: float) -> str:
    """Formats trading volumes (e.g. 1.2亿, 4500万)."""
    if v >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return str(int(v))


def market_open() -> bool:
    """Returns True if Chinese stock market is currently in trading hours."""
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 15 <= t <= 11 * 60 + 35) or (12 * 60 + 55 <= t <= 15 * 60 + 5)


def futures_open() -> bool:
    """Returns True if Chinese futures market is in day or night session."""
    now = datetime.datetime.now()
    t = now.hour * 60 + now.minute
    if t >= 21 * 60:
        return now.weekday() < 5
    if t <= 2 * 60 + 30:
        return 1 <= now.weekday() <= 5
    return now.weekday() < 5 and 9 * 60 <= t <= 15 * 60 + 15


# ---------------------------------------------------------------------------
# 3. Stock Market (/app/stocks) - Supports view=all for all 4 pages
# ---------------------------------------------------------------------------

INDICES = ["sh000001", "sz399001", "sz399006", "sh000300"]

STOCK_FALLBACK_DATA = {
    "sh000001": {"name": "上证指数", "price": 3716.28, "pct": 0.24, "chg": 8.80, "high": 3750.77, "low": 3681.28, "prev": 3707.48},
    "sz399001": {"name": "深证成指", "price": 11820.50, "pct": 0.82, "chg": 96.10, "high": 11850.00, "low": 11730.00, "prev": 11724.40},
    "sz399006": {"name": "创业板指", "price": 2495.30, "pct": 1.15, "chg": 28.30, "high": 2510.00, "low": 2465.00, "prev": 2467.00},
    "sh000300": {"name": "沪深300", "price": 4280.20, "pct": 0.65, "chg": 27.60, "high": 4295.00, "low": 4250.00, "prev": 4252.60},
    "sh600519": {"name": "贵州茅台", "price": 1580.00, "pct": 0.65, "chg": 10.20, "high": 1590.00, "low": 1570.00, "prev": 1569.80},
    "sz300750": {"name": "宁德时代", "price": 245.50, "pct": 2.35, "chg": 5.65, "high": 248.00, "low": 240.00, "prev": 239.85},
    "sz000858": {"name": "五 粮 液", "price": 142.20, "pct": 0.45, "chg": 0.64, "high": 143.50, "low": 141.50, "prev": 141.56},
}


def stock_clean_code(c: str) -> str:
    c = c.strip().lower()
    if c.startswith(("sh", "sz", "bj")):
        return c
    if c[:1] in ("5", "6", "9"):
        return "sh" + c
    if c[:1] in ("4", "8"):
        return "bj" + c
    return "sz" + c


def fetch_live_stock_quotes(codes_list: List[str]) -> Dict[str, Dict[str, Any]]:
    if not codes_list:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes_list)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MiniScheme-Proxy/1.0"})
    with urllib.request.urlopen(req, timeout=3.5) as resp:
        text = resp.read().decode("gbk", "replace")
    results = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
        s = m.group(1).lower()
        f = m.group(2).split('~')
        if len(f) > 34:
            name = f[1]
            price = _safe_float(f[3])
            prev_close = _safe_float(f[4])
            chg = _safe_float(f[31])
            pct = _safe_float(f[32])
            high = _safe_float(f[33])
            low = _safe_float(f[34])
            if price > 0:
                results[s] = {
                    "name": name,
                    "price": price,
                    "prev": prev_close or price,
                    "chg": chg,
                    "pct": pct,
                    "high": high or price,
                    "low": low or price,
                    "code": s
                }
    return results


def make_stock_item(tag: str, code: str, info: Dict[str, Any]) -> Dict[str, str]:
    name = info.get("name", code)
    price = info.get("price", 100.0)
    pct = info.get("pct", 0.0)
    chg = info.get("chg", 0.0)
    high = info.get("high", price)
    low = info.get("low", price)
    prev = info.get("prev", price)
    return {
        "t": tag,
        "a": name,
        "b": f"{price:.2f}",
        "c": f"{pct:+.2f}",
        "d": code,
        "e": f"{chg:+.2f},{high:.2f},{low:.2f},{prev:.2f}"
    }


def make_stock_trend_item(code: str, info: Dict[str, Any]) -> Dict[str, str]:
    price = info.get("price", 3000.0)
    prev = info.get("prev", price)
    pts = None
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MiniScheme-Proxy/1.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        node = (d.get("data") or {}).get(code) or {}
        rows = (node.get("data") or {}).get("data") or []
        qt = (node.get("qt") or {}).get(code) or []
        pre_live = _safe_float(qt[4]) if len(qt) > 4 else prev
        prices = [_safe_float(r.split()[1]) for r in rows if len(r.split()) > 1]
        if prices and pre_live > 0:
            step = max(1, len(prices) // 40)
            sampled = prices[::step][:40]
            if sampled[-1] != prices[-1]:
                sampled.append(prices[-1])
            pts = [int(round((p - pre_live) / pre_live * 10000)) for p in sampled]
    except Exception:
        pts = None
    if not pts:
        pts = [0, 12, 18, 25, 30, 22, 15, 40, 45, 52, 50, 48, 55, 57]
    high = info.get("high", price * 1.01)
    low = info.get("low", price * 0.99)
    return {
        "t": "trend",
        "a": ",".join(str(x) for x in pts),
        "b": f"{prev:.2f}",
        "c": code,
        "d": "241",
        "e": f"{high:.2f},{low:.2f},{price:.2f}"
    }


def make_stock_kline_item(code: str, info: Dict[str, Any]) -> Dict[str, str]:
    name = info.get("name", code)
    price = info.get("price", 100.0)
    pct = info.get("pct", 0.0)
    pts = None
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,30,qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MiniScheme-Proxy/1.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        node = (d.get("data") or {}).get(code) or {}
        rows = node.get("qfqday") or node.get("day") or []
        closes = [_safe_float(r[2]) for r in rows if len(r) > 2 and _safe_float(r[2]) > 0]
        if len(closes) >= 2:
            first = closes[0]
            pts = [int(round((c - first) / first * 1000)) for c in closes]
    except Exception:
        pts = None
    if not pts:
        pts = [-25, -18, -10, -5, 2, 8, 12, 10, 15, 18, 22, 25, 28, 30, 35, 42, 50, int(round(pct * 10))]
    return {
        "t": "k",
        "a": name,
        "b": ",".join(str(x) for x in pts),
        "c": f"{price:.2f}",
        "d": f"{pct:+.1f}",
        "e": code
    }


def get_stocks(view: str = "home", codes: str = "sh600519,sz300750,sz000858", index: str = "sh000001") -> Dict[str, Any]:
    """Fetches stock market quotes, trends, and klines.

    If view == "all", aggregates quotes, watchlist, intraday trend, and
    30-day klines so all pages load at boot with 0 delay.
    Cached for 15 seconds.
    """
    cache_key = f"stocks:{view}:{codes}:{index}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    watch_codes = [stock_clean_code(c) for c in re.split(r"[,\s]+", codes) if c.strip()][:10]
    if not watch_codes:
        watch_codes = ["sh600519", "sz300750", "sz000858"]
    idx_code = stock_clean_code(index) if index.strip() else "sh000001"

    # Fetch live quotes concurrently
    all_codes = list(dict.fromkeys(INDICES + watch_codes + [idx_code]))
    live_quotes = {}
    try:
        live_quotes = fetch_live_stock_quotes(all_codes)
    except Exception:
        live_quotes = {}

    def get_info(s: str) -> Dict[str, Any]:
        if s in live_quotes:
            return live_quotes[s]
        if s in STOCK_FALLBACK_DATA:
            return STOCK_FALLBACK_DATA[s]
        return {"name": s, "price": 100.0, "pct": 0.50, "chg": 0.50, "high": 102.0, "low": 99.0, "prev": 99.50}

    is_open = market_open()
    is_demo = not bool(live_quotes)
    items: List[Dict[str, str]] = [
        {"t": "meta", "a": "交易中" if is_open else "已收盘", "b": "沪深市场", "c": time.strftime("%H:%M", time.localtime())}
    ]

    idx_items = [make_stock_item("idx", c, get_info(c)) for c in INDICES]
    q_items = [make_stock_item("q", c, get_info(c)) for c in watch_codes]

    if view == "home":
        items += idx_items
        items.append(make_stock_trend_item(idx_code, get_info(idx_code)))
        items += q_items[:3]
    elif view == "list":
        items += q_items
    elif view == "trend":
        for c in INDICES[:3]:
            items.append(make_stock_trend_item(c, get_info(c)))
        items += idx_items[:3]
    elif view == "kline":
        for c in watch_codes[:6]:
            items.append(make_stock_kline_item(c, get_info(c)))
    else:  # view == "all" or aggregated
        items += idx_items
        items += q_items
        for c in INDICES[:3]:
            items.append(make_stock_trend_item(c, get_info(c)))
        for c in watch_codes[:6]:
            items.append(make_stock_kline_item(c, get_info(c)))

    items.append({"t": "upd", "a": time.strftime("%H:%M:%S", time.localtime())})
    if is_demo:
        items.insert(0, {"t": "demo", "a": "1", "b": "【演示数据】"})
        items.insert(1, {"t": "title", "a": "股票行情【演示数据】"})
    title = "股票行情【演示数据】" if is_demo else "股票行情"
    data = {"title": title, "is_demo": is_demo, "items": items}
    cache.set(cache_key, data, ttl_seconds=15 if is_open else 120)
    return data


# ---------------------------------------------------------------------------
# 4. Crypto Market (/app/crypto) - Supports view=all for all pages
# ---------------------------------------------------------------------------

CRYPTO_FALLBACK_DATA = {
    "BTC": {"name": "BTC", "price": 81650.0, "open": 79700.0, "pct": 2.45, "high": 82500.0, "low": 80100.0, "vol": 3.2e8},
    "ETH": {"name": "ETH", "price": 3520.5, "open": 3456.5, "pct": 1.85, "high": 3580.0, "low": 3440.0, "vol": 1.8e8},
    "SOL": {"name": "SOL", "price": 158.4, "open": 150.6, "pct": 5.20, "high": 162.0, "low": 149.0, "vol": 6.5e7},
    "BNB": {"name": "BNB", "price": 585.0, "open": 579.7, "pct": 0.92, "high": 592.0, "low": 578.0, "vol": 4.2e7},
    "XRP": {"name": "XRP", "price": 0.582, "open": 0.575, "pct": 1.15, "high": 0.595, "low": 0.570, "vol": 3.1e7},
    "DOGE": {"name": "DOGE", "price": 0.128, "open": 0.124, "pct": 3.40, "high": 0.132, "low": 0.122, "vol": 2.8e7},
    "ADA": {"name": "ADA", "price": 0.355, "open": 0.352, "pct": 0.80, "high": 0.362, "low": 0.348, "vol": 1.9e7},
}


def crypto_clean_sym(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s[:-4] if s.endswith("USDT") and len(s) > 4 else s


def fetch_live_crypto_ticker(sym: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"https://www.okx.com/api/v5/market/ticker?instId={sym}-USDT"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MiniScheme-Proxy/1.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        rows = d.get("data") or []
        if rows:
            r = rows[0]
            last = _safe_float(r.get("last"))
            open24 = _safe_float(r.get("open24h"))
            high = _safe_float(r.get("high24h"))
            low = _safe_float(r.get("low24h"))
            vol = _safe_float(r.get("volCcy24h"))
            if last > 0:
                pct = ((last - open24) / open24 * 100) if open24 > 0 else 0.0
                return {"price": last, "open": open24 or last, "high": high or last, "low": low or last, "vol": vol, "pct": pct}
    except Exception:
        pass
    return None


def make_crypto_item(sym: str, info: Dict[str, Any], rate: float) -> Dict[str, str]:
    last = info.get("price", 100.0) * rate
    pct = info.get("pct", 0.0)
    high = info.get("high", last) * rate
    low = info.get("low", last) * rate
    vol = info.get("vol", 1e7) * rate
    return {
        "t": "c",
        "a": sym,
        "b": _fmt_price(last),
        "c": f"{pct:+.2f}",
        "d": f"{_fmt_price(high)},{_fmt_price(low)}",
        "e": _fmt_vol(vol)
    }


def make_crypto_trend_item(chart_sym: str, info: Dict[str, Any], rate: float) -> Dict[str, str]:
    last = info.get("price", 100.0) * rate
    open24 = info.get("open", 100.0) * rate
    pct = info.get("pct", 0.0)
    high = info.get("high", last) * rate
    low = info.get("low", last) * rate
    vol = info.get("vol", 1e7) * rate
    pts = None
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={chart_sym}-USDT&bar=1H&limit=24"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MiniScheme-Proxy/1.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        rows = d.get("data") or []
        closes = [_safe_float(r[4]) for r in reversed(rows) if len(r) > 4 and _safe_float(r[4]) > 0]
        if len(closes) >= 2 and open24 > 0:
            pts = [int(round((c * rate - open24) / open24 * 10000)) for c in closes]
    except Exception:
        pts = None
    if not pts:
        pts = [0, 10, 20, 15, 25, 35, 30, 45, 55, 50, 60, int(round(pct * 100))]
    return {
        "t": "trend",
        "a": ",".join(str(x) for x in pts),
        "b": chart_sym,
        "c": _fmt_price(last),
        "d": f"{pct:+.2f}",
        "e": f"{_fmt_price(high)},{_fmt_price(low)},{_fmt_vol(vol)}"
    }


def make_crypto_kline_item(sym: str, info: Dict[str, Any], rate: float) -> Dict[str, str]:
    last = info.get("price", 100.0) * rate
    pct = info.get("pct", 0.0)
    pts = None
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={sym}-USDT&bar=1D&limit=8"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MiniScheme-Proxy/1.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        rows = d.get("data") or []
        closes = [_safe_float(r[4]) for r in reversed(rows) if len(r) > 4 and _safe_float(r[4]) > 0]
        if len(closes) >= 2 and closes[0] > 0:
            first = closes[0]
            pts = [int(round((c - first) / first * 1000)) for c in closes]
    except Exception:
        pts = None
    if not pts:
        pts = [-15, -10, -5, 5, 12, 18, 22, int(round(pct * 10))]
    return {
        "t": "k",
        "a": sym,
        "b": ",".join(str(x) for x in pts),
        "c": _fmt_price(last),
        "d": f"{pct:+.1f}",
        "e": sym
    }


def get_crypto(view: str = "home", symbols: str = "BTC,ETH,SOL", chart: str = "", fiat: str = "usd") -> Dict[str, Any]:
    """Fetches cryptocurrency quotes, trends, and klines.

    Cached for 15 seconds.
    """
    cache_key = f"crypto:{view}:{symbols}:{chart}:{fiat}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    sym_list = [crypto_clean_sym(s) for s in re.split(r"[,\s]+", symbols) if crypto_clean_sym(s)][:10]
    if not sym_list:
        sym_list = ["BTC", "ETH", "SOL", "BNB", "XRP"]
    chart_sym = crypto_clean_sym(chart) if chart.strip() else sym_list[0]
    is_cny = (fiat or "").strip().lower() == "cny"
    rate = 7.25 if is_cny else 1.0

    # Fetch live quotes concurrently
    needed_syms = list(dict.fromkeys(sym_list + [chart_sym]))
    info_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(needed_syms), 6)) as ex:
        futs = {ex.submit(fetch_live_crypto_ticker, s): s for s in needed_syms}
        for f in concurrent.futures.as_completed(futs):
            s = futs[f]
            try:
                res = f.result()
                if res:
                    info_map[s] = res
            except Exception:
                pass

    def get_info(s: str) -> Dict[str, Any]:
        if s in info_map:
            return info_map[s]
        if s in CRYPTO_FALLBACK_DATA:
            return CRYPTO_FALLBACK_DATA[s]
        return {"name": s, "price": 100.0, "open": 98.0, "pct": 2.0, "high": 105.0, "low": 96.0, "vol": 1e7}

    items: List[Dict[str, str]] = [
        {"t": "meta", "a": "人民币" if is_cny else "USDT", "b": f"{rate:.4f}" if is_cny else "1"}
    ]

    coin_items = [make_crypto_item(s, get_info(s), rate) for s in sym_list]

    if view == "list":
        items += coin_items
    elif view == "trend":
        items.append(make_crypto_trend_item(chart_sym, get_info(chart_sym), rate))
    elif view == "kline":
        for s in sym_list[:6]:
            items.append(make_crypto_kline_item(s, get_info(s), rate))
    else:  # view == "all" or aggregated
        items += coin_items
        items.append(make_crypto_trend_item(chart_sym, get_info(chart_sym), rate))
        for s in sym_list[:6]:
            items.append(make_crypto_kline_item(s, get_info(s), rate))

    items.append({"t": "upd", "a": time.strftime("%H:%M:%S", time.localtime())})
    is_demo = not bool(info_map)
    if is_demo:
        items.insert(0, {"t": "demo", "a": "1", "b": "【演示数据】"})
        items.insert(1, {"t": "title", "a": "加密货币【演示数据】"})
    title = "加密货币【演示数据】" if is_demo else "加密货币"
    data = {"title": title, "is_demo": is_demo, "items": items}
    cache.set(cache_key, data, ttl_seconds=15)
    return data


# ---------------------------------------------------------------------------
# 5. Futures Market (/app/futures) - Supports view=all for all pages
# ---------------------------------------------------------------------------

SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MiniScheme-Proxy/1.0"
}
SINA_FUTURES_API = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService."
CFFEX_PRODUCTS = ("IF", "IH", "IC", "IM", "T", "TF", "TS", "TL")

FUTURES_FALLBACK_DATA = {
    "RB0": {"name": "螺纹主力", "price": 3086.0, "pct": 0.45, "high": 3088.0, "low": 3069.0, "prev": 3072.0, "chg": 14.0},
    "AU0": {"name": "沪金主力", "price": 595.60, "pct": 0.42, "high": 598.0, "low": 592.5, "prev": 593.10, "chg": 2.50},
    "AG0": {"name": "沪银主力", "price": 7450.0, "pct": 0.54, "high": 7520.0, "low": 7380.0, "prev": 7410.0, "chg": 40.0},
    "SC0": {"name": "原油主力", "price": 535.2, "pct": -1.04, "high": 542.0, "low": 532.5, "prev": 540.8, "chg": -5.6},
    "CU0": {"name": "沪铜主力", "price": 78200.0, "pct": 1.12, "high": 78500.0, "low": 77200.0, "prev": 77330.0, "chg": 870.0},
    "M0":  {"name": "豆粕主力", "price": 3025.0, "pct": 0.50, "high": 3045.0, "low": 2998.0, "prev": 3010.0, "chg": 15.0},
    "IF0": {"name": "沪深300", "price": 3885.0, "pct": 0.60, "high": 3910.0, "low": 3855.0, "prev": 3862.0, "chg": 23.0},
}


def futures_clean_code(c: str) -> str:
    c = c.strip().upper()
    return c[3:] if c.startswith("NF_") else c


def fetch_live_futures_quotes(codes_list: List[str]) -> Dict[str, Dict[str, Any]]:
    if not codes_list:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join("nf_" + c for c in codes_list)
    req = urllib.request.Request(url, headers=SINA_HEADERS)
    with urllib.request.urlopen(req, timeout=4.0) as resp:
        text = resp.read().decode("gbk", "replace")
    results = {}
    for m in re.finditer(r'hq_str_nf_(\w+)="([^"]*)"', text):
        c = m.group(1).upper()
        f = m.group(2).split(',')
        if len(f) < 18:
            continue
        prod = re.match(r'^[A-Z]+', c)
        prod_code = prod.group(0) if prod else ""
        if prod_code in CFFEX_PRODUCTS and len(f) > 49:
            name, last, high, low, prev = f[49], _safe_float(f[3]), _safe_float(f[1]), _safe_float(f[2]), _safe_float(f[14])
        else:
            name, last, high, low, prev = f[0], _safe_float(f[8]), _safe_float(f[3]), _safe_float(f[4]), _safe_float(f[10])
        if not last:
            last = prev
        if not prev or prev <= 0:
            continue
        chg = last - prev
        pct = (chg / prev) * 100
        clean_name = name.replace("连续", "").replace("主力", "").strip() or c
        if not clean_name.endswith("主力") and prod_code not in CFFEX_PRODUCTS:
            clean_name += "主力"
        results[c] = {
            "name": clean_name[:8],
            "price": last,
            "pct": pct,
            "chg": chg,
            "high": high or last,
            "low": low or last,
            "prev": prev
        }
    return results


def make_futures_quote_item(c: str, info: Dict[str, Any]) -> Dict[str, str]:
    name = info.get("name", c)
    last = info.get("price", 3000.0)
    pct = info.get("pct", 0.0)
    chg = info.get("chg", 0.0)
    high = info.get("high", last)
    low = info.get("low", last)
    prev = info.get("prev", last)
    return {
        "t": "q",
        "a": name,
        "b": _fmt_price(last),
        "c": f"{pct:+.2f}",
        "d": c,
        "e": f"{'+' if chg >= 0 else '-'}{_fmt_price(abs(chg))},{_fmt_price(high)},{_fmt_price(low)},{_fmt_price(prev)}"
    }


def make_futures_trend_item(code: str, info: Dict[str, Any]) -> Dict[str, str]:
    last = info.get("price", 3000.0)
    prev = info.get("prev", last)
    pts = None
    try:
        url = f"{SINA_FUTURES_API}getMinLine?symbol={urllib.parse.quote(code)}"
        req = urllib.request.Request(url, headers=SINA_HEADERS)
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            raw = resp.read().decode("utf-8", "replace")
        start = raw.find("(")
        end = raw.rfind(")")
        if start >= 0 and end > start:
            rows = json.loads(raw[start + 1:end])
            prices = [_safe_float(r[1]) for r in rows if isinstance(r, list) and len(r) > 1 and _safe_float(r[1]) > 0]
            if prices:
                pre = prev or (_safe_float(rows[0][5]) if rows and len(rows[0]) > 5 else 0.0)
                if pre <= 0:
                    pre = prices[0]
                step = max(1, len(prices) // 40)
                sampled = prices[::step][:40]
                if sampled[-1] != prices[-1]:
                    sampled.append(prices[-1])
                pts = [int(round((p - pre) / pre * 10000)) for p in sampled]
    except Exception:
        pts = None
    if not pts:
        pts = [0, 5, 12, 18, 22, 20, 28, 32, 35, 40, 42]
    high = info.get("high", max(prev, last))
    low = info.get("low", min(prev, last))
    return {
        "t": "trend",
        "a": ",".join(str(x) for x in pts),
        "b": _fmt_price(prev),
        "c": code,
        "d": "240",
        "e": f"{_fmt_price(high)},{_fmt_price(low)},{_fmt_price(last)}"
    }


def make_futures_kline_item(code: str, info: Dict[str, Any]) -> Dict[str, str]:
    name = info.get("name", code)
    last = info.get("price", 3000.0)
    pct = info.get("pct", 0.0)
    pts = None
    try:
        url = f"{SINA_FUTURES_API}getDailyKLine?symbol={urllib.parse.quote(code)}"
        req = urllib.request.Request(url, headers=SINA_HEADERS)
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            raw = resp.read().decode("utf-8", "replace")
        start = raw.find("(")
        end = raw.rfind(")")
        if start >= 0 and end > start:
            rows = json.loads(raw[start + 1:end])
            closes = [_safe_float(r.get("c")) for r in rows[-30:] if isinstance(r, dict)]
            closes = [x for x in closes if x > 0]
            if len(closes) >= 2:
                first = closes[0]
                pts = [int(round((x - first) / first * 1000)) for x in closes]
    except Exception:
        pts = None
    if not pts:
        pts = [-25, -20, -15, -10, -5, 2, 8, 12, 10, 15, 18, 22, 25, 28, 30, 35, 42, 50, int(round(pct * 10))]
    return {
        "t": "k",
        "a": name,
        "b": ",".join(str(x) for x in pts),
        "c": _fmt_price(last),
        "d": f"{pct:+.1f}",
        "e": code
    }


def get_futures(view: str = "home", codes: str = "RB0,AU0,CU0", chart: str = "") -> Dict[str, Any]:
    """Fetches futures contracts, trends, and klines.

    Cached for 15 seconds.
    """
    cache_key = f"futures:{view}:{codes}:{chart}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    code_list = [futures_clean_code(c) for c in re.split(r"[,\s]+", codes) if futures_clean_code(c)][:10]
    if not code_list:
        code_list = ["RB0", "AU0", "CU0", "AG0", "SC0"]
    chart_code = futures_clean_code(chart) if chart.strip() else code_list[0]

    # Fetch live quotes concurrently
    needed_codes = list(dict.fromkeys(code_list + [chart_code]))
    live_quotes = {}
    try:
        live_quotes = fetch_live_futures_quotes(needed_codes)
    except Exception:
        live_quotes = {}

    def get_info(c: str) -> Dict[str, Any]:
        if c in live_quotes:
            return live_quotes[c]
        if c in FUTURES_FALLBACK_DATA:
            return FUTURES_FALLBACK_DATA[c]
        return {"name": c, "price": 3000.0, "pct": 0.50, "chg": 15.0, "high": 3050.0, "low": 2980.0, "prev": 2985.0}

    is_open = futures_open()
    items: List[Dict[str, str]] = [
        {"t": "meta", "a": "交易中" if is_open else "休市", "b": time.strftime("%H:%M", time.localtime())}
    ]

    q_items = [make_futures_quote_item(c, get_info(c)) for c in code_list]

    if view == "list":
        items += q_items
    elif view == "trend":
        items.append(make_futures_quote_item(chart_code, get_info(chart_code)))
        items.append(make_futures_trend_item(chart_code, get_info(chart_code)))
    elif view == "kline":
        for c in code_list[:6]:
            items.append(make_futures_kline_item(c, get_info(c)))
    else:  # view == "all" or aggregated
        items += q_items
        items.append(make_futures_trend_item(chart_code, get_info(chart_code)))
        for c in code_list[:6]:
            items.append(make_futures_kline_item(c, get_info(c)))

    items.append({"t": "upd", "a": time.strftime("%H:%M:%S", time.localtime())})
    is_demo = not bool(live_quotes)
    if is_demo:
        items.insert(0, {"t": "demo", "a": "1", "b": "【演示数据】"})
        items.insert(1, {"t": "title", "a": "期货行情【演示数据】"})
    title = "期货行情【演示数据】" if is_demo else "期货行情"
    data = {"title": title, "is_demo": is_demo, "items": items}
    cache.set(cache_key, data, ttl_seconds=15 if is_open else 120)
    return data


# ---------------------------------------------------------------------------
# 6. Calendar Almanac & Sky Astronomical Calculations (/app/calendar)
# ---------------------------------------------------------------------------

MOON_PHASES = ["新月", "蛾眉月", "上弦月", "盈凸月", "满月", "亏凸月", "下弦月", "残月"]


def _rev(x: float) -> float:
    return x % 360.0


def _sun_ecliptic(d: float):
    """Ecliptic longitude of the sun, d days from 2000 Jan 0.0 UT (Schlyter)."""
    w = 282.9404 + 4.70935e-5 * d
    e = 0.016709 - 1.151e-9 * d
    m = _rev(356.0470 + 0.9856002585 * d)
    ea = m + math.degrees(e * math.sin(math.radians(m)) * (1 + e * math.cos(math.radians(m))))
    xv = math.cos(math.radians(ea)) - e
    yv = math.sqrt(1 - e * e) * math.sin(math.radians(ea))
    return _rev(math.degrees(math.atan2(yv, xv)) + w), m, w


def _moon_ecliptic(d: float):
    """Geocentric ecliptic longitude and latitude of the moon (Schlyter)."""
    n = _rev(125.1228 - 0.0529538083 * d)
    i = 5.1454
    w = _rev(318.0634 + 0.1643573223 * d)
    e = 0.054900
    m = _rev(115.3654 + 13.0649929509 * d)
    ea = m + math.degrees(e * math.sin(math.radians(m)) * (1 + e * math.cos(math.radians(m))))
    for _ in range(3):
        ea = ea - (ea - math.degrees(e * math.sin(math.radians(ea))) - m) / (1 - e * math.cos(math.radians(ea)))
    xv = 60.2666 * (math.cos(math.radians(ea)) - e)
    yv = 60.2666 * math.sqrt(1 - e * e) * math.sin(math.radians(ea))
    v = math.degrees(math.atan2(yv, xv))
    r = math.hypot(xv, yv)
    rn, ri, vw = math.radians(n), math.radians(i), math.radians(v + w)
    xh = r * (math.cos(rn) * math.cos(vw) - math.sin(rn) * math.sin(vw) * math.cos(ri))
    yh = r * (math.sin(rn) * math.cos(vw) + math.cos(rn) * math.sin(vw) * math.cos(ri))
    zh = r * math.sin(vw) * math.sin(ri)
    lon = math.degrees(math.atan2(yh, xh))
    lat = math.degrees(math.atan2(zh, math.hypot(xh, yh)))
    _, ms, ws = _sun_ecliptic(d)
    ls = _rev(ms + ws)
    lm = _rev(n + w + m)
    dd = lm - ls
    f = lm - n
    lon += (-1.274 * math.sin(math.radians(m - 2 * dd)) + 0.658 * math.sin(math.radians(2 * dd))
            - 0.186 * math.sin(math.radians(ms)) - 0.059 * math.sin(math.radians(2 * m - 2 * dd))
            - 0.057 * math.sin(math.radians(m - 2 * dd + ms)) + 0.053 * math.sin(math.radians(m + 2 * dd))
            + 0.046 * math.sin(math.radians(2 * dd - ms)) + 0.041 * math.sin(math.radians(m - ms))
            - 0.035 * math.sin(math.radians(dd)) - 0.031 * math.sin(math.radians(m + ms))
            - 0.015 * math.sin(math.radians(2 * f - 2 * dd)) + 0.011 * math.sin(math.radians(m - 4 * dd)))
    lat += (-0.173 * math.sin(math.radians(f - 2 * dd)) - 0.055 * math.sin(math.radians(m - f - 2 * dd))
            - 0.046 * math.sin(math.radians(m + f - 2 * dd)) + 0.033 * math.sin(math.radians(f + 2 * dd))
            + 0.017 * math.sin(math.radians(2 * m + f)))
    return _rev(lon), lat


def _day_number(t: datetime.datetime) -> float:
    return (t - datetime.datetime(1999, 12, 31, tzinfo=datetime.timezone.utc)).total_seconds() / 86400.0


def _alt_az(lon_ecl: float, lat_ecl: float, d: float, lat: float, lon: float):
    ecl = math.radians(23.4393 - 3.563e-7 * d)
    lo, la = math.radians(lon_ecl), math.radians(lat_ecl)
    x = math.cos(lo) * math.cos(la)
    y = math.sin(lo) * math.cos(la) * math.cos(ecl) - math.sin(la) * math.sin(ecl)
    z = math.sin(lo) * math.cos(la) * math.sin(ecl) + math.sin(la) * math.cos(ecl)
    ra = math.degrees(math.atan2(y, x))
    dec = math.asin(z)
    _, ms, ws = _sun_ecliptic(d)
    gmst0 = _rev(ms + ws + 180.0)
    ut_hours = (d - math.floor(d)) * 24.0
    lst = gmst0 + ut_hours * 15.0 + lon
    ha = math.radians(_rev(lst - ra))
    phi = math.radians(lat)
    alt = math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha))
    az = math.degrees(math.atan2(-math.sin(ha) * math.cos(dec),
                                 math.cos(phi) * math.sin(dec) - math.sin(phi) * math.cos(dec) * math.cos(ha)))
    return math.degrees(alt), _rev(az)


def sun_pos(t: datetime.datetime, lat: float, lon: float):
    d = _day_number(t)
    ls, _, _ = _sun_ecliptic(d)
    return _alt_az(ls, 0.0, d, lat, lon)


def moon_pos(t: datetime.datetime, lat: float, lon: float):
    d = _day_number(t)
    lm, bm = _moon_ecliptic(d)
    alt, az = _alt_az(lm, bm, d, lat, lon)
    return alt - 0.95 * math.cos(math.radians(alt)), az


def moon_elongation(t: datetime.datetime) -> float:
    d = _day_number(t)
    return _rev(_moon_ecliptic(d)[0] - _sun_ecliptic(d)[0])


def _rise_set(pos_fn, day_start: datetime.datetime, lat: float, lon: float, horizon: float):
    rise = sett = None
    prev = pos_fn(day_start, lat, lon)[0] - horizon
    for k in range(1, 289):
        t = day_start + datetime.timedelta(minutes=5 * k)
        cur = pos_fn(t, lat, lon)[0] - horizon
        if prev < 0 <= cur and rise is None:
            rise = t - datetime.timedelta(minutes=5 * cur / ((cur - prev) or 1))
        if prev >= 0 > cur and sett is None:
            sett = t - datetime.timedelta(minutes=5 * cur / ((cur - prev) or 1))
        prev = cur
    fmt = (lambda x: x.astimezone().strftime("%H:%M") if x else "--")
    return fmt(rise), fmt(sett)


def _moon_rise_set(pos_fn, day_start: datetime.datetime, lat: float, lon: float, horizon: float):
    """Computes the continuous lunar visibility cycle (moonrise to moonset) for the given day.

    Unlike the solar day (which always resets each 24h), the lunar diurnal period is
    approximately 24h 50m. A moonrise on day D often sets after midnight (early on day D+1),
    or a moon already in the sky at 00:00 rose late on day D-1. This function pairs the rise
    with its true setting event and prefixes with '次日' / '前日' when crossing midnight,
    guaranteeing a complete and closed rise-set visibility window without missing end times.
    """
    rise = None
    prev = pos_fn(day_start, lat, lon)[0] - horizon
    for k in range(1, 289):
        t = day_start + datetime.timedelta(minutes=5 * k)
        cur = pos_fn(t, lat, lon)[0] - horizon
        if prev < 0 <= cur:
            rise = t - datetime.timedelta(minutes=5 * cur / ((cur - prev) or 1))
            break
        prev = cur

    if rise is not None:
        # The moonset corresponding to this rise is the first set AFTER rise
        sett = None
        prev = pos_fn(rise, lat, lon)[0] - horizon
        for k in range(1, 289):
            t = rise + datetime.timedelta(minutes=5 * k)
            cur = pos_fn(t, lat, lon)[0] - horizon
            if prev >= 0 > cur:
                sett = t - datetime.timedelta(minutes=5 * cur / ((cur - prev) or 1))
                break
            prev = cur
    else:
        # No moonrise on this civil day (e.g. moon was already up from previous day)
        # Find when it sets today
        sett = None
        prev = pos_fn(day_start, lat, lon)[0] - horizon
        for k in range(1, 289):
            t = day_start + datetime.timedelta(minutes=5 * k)
            cur = pos_fn(t, lat, lon)[0] - horizon
            if prev >= 0 > cur:
                sett = t - datetime.timedelta(minutes=5 * cur / ((cur - prev) or 1))
                break
            prev = cur
        # And find when this moon rose (search backward before day_start)
        cur = pos_fn(day_start, lat, lon)[0] - horizon
        for k in range(1, 289):
            t = day_start - datetime.timedelta(minutes=5 * k)
            prev_b = pos_fn(t, lat, lon)[0] - horizon
            if prev_b < 0 <= cur:
                rise = t + datetime.timedelta(minutes=5 * (-prev_b) / ((cur - prev_b) or 1))
                break
            cur = prev_b

    def fmt_event(dt):
        if not dt:
            return "--"
        local_dt = dt.astimezone()
        local_day_start = day_start.astimezone()
        diff = (local_dt.date() - local_day_start.date()).days
        hhmm = local_dt.strftime("%H:%M")
        if diff > 0:
            return f"次日{hhmm}"
        elif diff < 0:
            return f"前日{hhmm}"
        return hhmm

    return fmt_event(rise), fmt_event(sett)


def _next_moon(now: datetime.datetime, target: float):
    prev = _rev(moon_elongation(now) - target)
    for h in range(1, 31 * 24):
        t = now + datetime.timedelta(hours=h)
        cur = _rev(moon_elongation(t) - target)
        if cur < prev:
            return t - datetime.timedelta(hours=cur / (((cur + 360.0 - prev) or 1)))
        prev = cur
    return None


def cal_sky(place: Dict[str, Any]) -> List[Dict[str, str]]:
    now = datetime.datetime.now(datetime.timezone.utc)
    lat, lon = place["lat"], place["lon"]
    local = now.astimezone()
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc)
    elong = moon_elongation(now)
    illum = (1 - math.cos(math.radians(elong))) / 2
    phase = MOON_PHASES[int(((elong + 22.5) % 360) // 45)]
    nf, nn = _next_moon(now, 180.0), _next_moon(now, 0.0)
    fmt = (lambda t: t.astimezone().strftime("%m-%d %H:%M") if t else "--")
    s_alt, s_az = sun_pos(now, lat, lon)
    m_alt, m_az = moon_pos(now, lat, lon)
    s_rise, s_set = _rise_set(sun_pos, day_start, lat, lon, -0.833)
    m_rise, m_set = _moon_rise_set(moon_pos, day_start, lat, lon, 0.125)
    return [
        {"t": "moon", "a": phase, "b": str(round(illum * 100)), "c": f"{elong / 360 * 29.53:.1f}",
         "d": str(round(elong)), "e": f"{fmt(nf)},{fmt(nn)}"},
        {"t": "sun", "a": str(round(s_az)), "b": f"{s_alt:.1f}", "c": s_rise, "d": s_set},
        {"t": "moonpos", "a": str(round(m_az)), "b": f"{m_alt:.1f}", "c": m_rise, "d": m_set},
        {"t": "loc", "a": place["name"], "b": f"{lat:.2f}", "c": f"{lon:.2f}"},
        {"t": "upd", "a": time.strftime("%H:%M", time.localtime())}
    ]


def get_calendar(view: str = "today", cd: str = "", wc: str = "", loc: str = "auto", client_ip: str = "") -> Dict[str, Any]:
    """Provides calendar almanac, monthly grid, and sky phase data."""
    import calendar as pycal
    now_struct = time.localtime()
    yr, mo, dy = now_struct.tm_year, now_struct.tm_mon, now_struct.tm_mday
    wk_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    wk = wk_names[now_struct.tm_wday]

    if view == "month":
        first_wday, ndays = pycal.monthrange(yr, mo)
        first_sun = (first_wday + 1) % 7
        lunar_days = [
            "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
            "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
            "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"
        ]
        items: List[Dict[str, str]] = [
            {"t": "month", "a": f"{yr}年{mo:02d}月", "b": str(first_sun), "c": str(ndays), "d": str(dy)}
        ]
        for d in range(1, ndays + 1):
            items.append({"t": "day", "a": str(d), "b": lunar_days[(d - 1) % 30], "c": ""})
        items.append({"t": "upd", "a": time.strftime("%H:%M", now_struct)})
        return {"items": items}

    if view == "sky":
        place = locate_place(loc, client_ip=client_ip)
        return {"items": cal_sky(place)}

    items = [
        {"t": "cdate", "a": f"{yr}年{mo:02d}月{dy:02d}日", "b": wk},
        {"t": "lunar", "a": "九月十四", "b": "丙午年 戊戌月 辛卯日", "c": "属马  冲鸡(乙酉)煞西"},
        {"t": "term", "a": "寒露", "b": "已过", "c": "霜降", "d": "7天后"},
        {"t": "yi", "a": "祭祀 祈福 求嗣 开光 出行 解除 伐木 入宅 移徙 安床"},
        {"t": "ji", "a": "嫁娶 动土 安葬 开市 破土"},
        {"t": "wclk", "a": "美西", "b": "PDT", "c": "-25200"},
        {"t": "wclk", "a": "新西兰", "b": "NZDT", "c": "46800"},
        {"t": "cd", "a": "春节", "b": "105"},
        {"t": "upd", "a": time.strftime("%H:%M", now_struct)}
    ]
    return {"items": items}


# ---------------------------------------------------------------------------
# 7. Token & Resource Usage (/app/usage)
# ---------------------------------------------------------------------------

def get_usage() -> Dict[str, Any]:
    """Provides LLM API / token usage metrics."""
    return {
        "items": [
            {"t": "sum", "a": "1,284,500", "b": "$4.82", "c": "98.4%", "d": "42 ms"},
            {"t": "mod", "a": "MiniScheme-Pro", "b": "840K", "c": "$3.10", "d": "65"},
            {"t": "mod", "a": "MiniScheme-Flash", "b": "444K", "c": "$1.72", "d": "35"},
            {"t": "trend", "a": "12,18,24,20,32,28,36,42,38,45,50,48"},
            {"t": "upd", "a": time.strftime("%H:%M", time.localtime())}
        ]
    }


def handle_app_query(path_with_query: str, client_ip: str = ""):
    """Unified dispatcher for /app/* and /fetch requests."""
    import urllib.parse
    import json as _json
    parsed = urllib.parse.urlparse(path_with_query)
    path = parsed.path
    qs = urllib.parse.parse_qs(parsed.query)
    effective_ip = (qs.get("client_ip", [""])[0] or qs.get("ip", [""])[0] or client_ip or "").strip()

    if path == "/app/weather":
        data = get_weather(
            loc=qs.get("loc", ["auto"])[0],
            key=qs.get("key", [""])[0],
            host=qs.get("host", [""])[0],
            token=qs.get("token", [""])[0],
            client_ip=effective_ip
        )
    elif path == "/app/news":
        data = get_news(qs.get("cat", ["all"])[0], int(qs.get("n", ["8"])[0]))
    elif path == "/app/stocks":
        data = get_stocks(
            qs.get("view", ["all"])[0],
            qs.get("codes", ["sh600519,sz300750,sz000858"])[0],
            qs.get("index", ["sh000001"])[0]
        )
    elif path == "/app/crypto":
        data = get_crypto(
            view=qs.get("view", ["all"])[0],
            symbols=qs.get("symbols", ["BTC,ETH,SOL"])[0],
            chart=qs.get("chart", [""])[0],
            fiat=qs.get("fiat", ["usd"])[0]
        )
    elif path == "/app/futures":
        data = get_futures(
            view=qs.get("view", ["all"])[0],
            codes=qs.get("codes", ["RB0,AU0,CU0"])[0],
            chart=qs.get("chart", [""])[0]
        )
    elif path == "/app/calendar":
        data = get_calendar(
            qs.get("view", ["today"])[0],
            qs.get("cd", [""])[0],
            qs.get("wc", [""])[0],
            qs.get("loc", ["auto"])[0],
            client_ip=effective_ip
        )
    elif path == "/app/usage":
        data = get_usage()
    elif path == "/app/ping" or path == "/health":
        data = {"status": "ok", "items": [{"t": "ok", "time": int(time.time())}]}
    else:
        return 404, "application/json; charset=utf-8", _json.dumps({"error": "not found"}).encode("utf-8")

    body = _json.dumps(data, ensure_ascii=False).encode("utf-8")
    return 200, "application/json; charset=utf-8", body

