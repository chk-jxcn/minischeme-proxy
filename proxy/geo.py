"""/app/geo: where a device is and its time zone, from the address it asks from.

The firmware (config.ini [system] timezone = auto, the default) asks once after a
boot and sets its clock's zone from "posix", the TZ rule with the daylight saving
changes (the board has no time zone database). Sources: ip-api.com, then ipwho.is;
when neither answers, the Chinese lookups of the weather (a place in China:
Asia/Shanghai). Nothing found: an error, and the device keeps the zone it has.
"""
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

GEO_TTL = 6 * 3600
_UA = {"User-Agent": "MiniScheme-Proxy/1.0"}


def _tzif_bytes(name: str) -> Optional[bytes]:
    """The compiled zone file of an IANA name: the system's, else the tzdata package's."""
    if not re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}", name or ""):
        return None
    try:
        import zoneinfo
        for root in zoneinfo.TZPATH:
            path = os.path.join(root, *name.split("/"))
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read()
    except Exception:
        pass
    try:
        import importlib.resources
        package = "tzdata.zoneinfo." + ".".join(name.split("/")[:-1]) if "/" in name else "tzdata.zoneinfo"
        return importlib.resources.files(package).joinpath(name.split("/")[-1]).read_bytes()
    except Exception:
        return None


def fixed_posix(offset_seconds: int) -> str:
    """A zone without daylight saving as a TZ rule: 28800 -> "<+08>-8" (POSIX counts west)."""
    sign = "+" if offset_seconds >= 0 else "-"
    a = abs(int(offset_seconds))
    h, m = a // 3600, (a % 3600) // 60
    name = f"<{sign}{h:02d}{m:02d}>" if m else f"<{sign}{h:02d}>"
    west = "-" if offset_seconds >= 0 else ""
    return f"{name}{west}{h}" + (f":{m:02d}" if m else "") if a else "UTC0"


def posix_tz(name: str, offset_seconds: Optional[int] = None) -> str:
    """The TZ rule of an IANA zone (the footer of its zone file: "EST5EDT,M3.2.0,M11.1.0");
    without the file, a fixed offset when one is known, else ""."""
    data = _tzif_bytes(name)
    if data and data[:4] == b"TZif" and data.endswith(b"\n"):
        footer = data[:-1].rsplit(b"\n", 1)[-1].decode("ascii", "replace").strip()
        if footer and re.fullmatch(r"[A-Za-z0-9<>+\-.,/:]+", footer) and len(footer) < 48:
            return footer
    return fixed_posix(offset_seconds) if offset_seconds is not None else ""


def _get_json(url: str, timeout: float = 4.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _from_ip_api(ip: str) -> Optional[Dict[str, Any]]:
    d = _get_json("http://ip-api.com/json/" + urllib.parse.quote(ip) +
                  "?fields=status,countryCode,city,timezone,offset&lang=zh-CN")
    if d.get("status") != "success" or not d.get("timezone"):
        return None
    return {"tz": d["timezone"], "offset": int(d.get("offset") or 0), "city": d.get("city") or "",
            "country": d.get("countryCode") or ""}


def _from_ipwho(ip: str) -> Optional[Dict[str, Any]]:
    d = _get_json("https://ipwho.is/" + urllib.parse.quote(ip) + "?fields=success,country_code,city,timezone")
    zone = d.get("timezone") or {}
    if not d.get("success") or not zone.get("id"):
        return None
    return {"tz": zone["id"], "offset": int(zone.get("offset") or 0), "city": d.get("city") or "",
            "country": d.get("country_code") or ""}


def _public_ip(client_ip: str) -> str:
    """The address to look up: the client's, or this server's own public one for a client on
    its local network (a WebREPL or a proxy at home)."""
    from .endpoints import is_private_ip
    ip = (client_ip or "").split(",")[0].strip()
    if ip.count(":") == 1:
        ip = ip.split(":")[0].strip()
    if ip and not is_private_ip(ip):
        return ip
    for url in ("http://myip.ipip.net/", "https://api.ipify.org"):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)", resp.read().decode("utf-8", "replace"))
            if m and not is_private_ip(m.group(1)):
                return m.group(1)
        except Exception:
            pass
    return ""


def get_geo(client_ip: str = "") -> Optional[Dict[str, Any]]:
    """{"tz", "posix", "offset", "city", "country"} for the address, or None (nothing found:
    not cached, asked again next time)."""
    from .endpoints import cache, locate_place
    ip = _public_ip(client_ip)
    key = "geo_" + (ip or "default")
    hit = cache.get(key)
    if hit:
        return hit
    found = None
    if ip:
        for source in (_from_ip_api, _from_ipwho):
            try:
                found = source(ip)
            except Exception:
                found = None
            if found:
                break
    if not found:
        # the weather's lookups know places in China only, and answer 北京 when they find
        # nothing: any other name is a place in China
        try:
            place = locate_place("auto", client_ip)
        except Exception:
            place = None
        if ip and place and place.get("name") and place["name"] != "北京":
            found = {"tz": "Asia/Shanghai", "offset": 28800, "city": place["name"], "country": "CN"}
    if not found:
        return None
    posix = posix_tz(found["tz"], found.get("offset"))
    if not posix:
        return None
    found["posix"] = posix
    cache.set(key, found, ttl_seconds=GEO_TTL)
    return found
