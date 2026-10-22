"""The medicine app's records on its app server (GET /app/medicine).

The board keeps its records itself; with sync on it uploads them here, where
they stay (the device never deletes them). It downloads them back only when
it starts with no data (a new install) or when the user asks for N days.
Requests come through the board's JSON feed (GET, a short URL: a plain
HTTP request line has to fit 256 bytes), answers are {"items": [...]}:

  op=names&m=<id>~<name>~<unit>[&m=...]     the medicines' names (URL-encoded)
      -> {"t":"ok","n":"<stored>"}
  op=put&r=<rec>,<rec>...                   records; the device's copy wins
      rec: day.HHMM.med.how.HHMM.amount     day: days since 1970-01-01 (local),
                                            the time point, the medicine id,
                                            how ok|miss|skip|auto, the confirm
                                            time (- for none), tenths
      -> {"t":"ok","n":"<stored>"}
  op=get&from=<day>&off=<k>&lim=<n>         records from that day on, oldest first
      -> {"t":"r","d","h","m","w","a","n","nm","u"} ... then
         {"t":"end","n":"<total>","more":"0|1"}
  op=info                                   -> {"t":"info","n","first","last"}
  any error                                 -> {"t":"err","e":"<message>"}

Who the device is: the id its app service password proved
(X-MiniScheme-Password, proxy/device_auth.py; the encrypted channel's
device for older firmware); without a password the X-MiniScheme-Device
header, which a public server takes only from a simulator (SIM-...): a
MAC is no secret, so a board's records there need its password.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .database import normalize_device_id

HOWS = ("ok", "miss", "skip", "auto")
NAME_MAX = 60          # bytes, as the app keeps them
UNIT_MAX = 12
GET_LIMIT = 50
PUT_MAX = 200          # records per request
DAY_MAX = 200000       # beyond year 2500

def _err(msg: str) -> List[Dict[str, str]]:
    return [{"t": "err", "e": msg}]


def resolve_device(device: str, plain_device: str, public: bool) -> Tuple[str, str]:
    """(device id, '') or ('', why not)."""
    if device:
        return normalize_device_id(device), ""
    d = normalize_device_id(plain_device or "")
    if not d:
        return "", "设备没有发送编号（固件需 1.0.65 以上）"
    if public and not d.startswith("SIM"):
        return "", "公网服务器保存服药记录需要应用服务密码：请在设备设置里为应用服务器填写应用服务密码"
    return d, ""


def _clip(text: str, limit: int) -> str:
    """At most limit UTF-8 bytes, never cutting a character; | and ~ removed."""
    text = re.sub(r"[|~\x00-\x1f]", " ", text or "").strip()
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


def _hm(s: str) -> str:
    """'0805' -> '08:05', else ''."""
    if not re.fullmatch(r"\d{4}", s or ""):
        return ""
    h, m = int(s[:2]), int(s[2:])
    return f"{h:02d}:{m:02d}" if h < 24 and m < 60 else ""


def parse_record(rec: str) -> Optional[Tuple[int, str, int, str, str, int]]:
    """'20384.0800.1.ok.0805.10' -> (day, '08:00', med, how, '08:05', amount)."""
    f = rec.split(".")
    if len(f) != 6:
        return None
    day, slot, med, how, at, amount = f
    if not (day.isdigit() and med.isdigit() and amount.isdigit()):
        return None
    slot = _hm(slot)
    at = "" if at == "-" else _hm(at)
    if not slot or how not in HOWS or (f[4] != "-" and not at):
        return None
    d, m, a = int(day), int(med), int(amount)
    if not (0 < d < DAY_MAX and 0 < m < 1000000 and 0 <= a <= 1000000):
        return None
    return d, slot, m, how, at, a


def parse_name(item: str) -> Optional[Tuple[int, str, str]]:
    """'<id>~<name>~<unit>' -> (id, name, unit)."""
    f = item.split("~")
    if len(f) != 3 or not f[0].isdigit():
        return None
    med = int(f[0])
    name, unit = _clip(f[1], NAME_MAX), _clip(f[2], UNIT_MAX)
    if not (0 < med < 1000000) or not name:
        return None
    return med, name, unit


def _int(qs: Dict[str, List[str]], key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int((qs.get(key) or [str(default)])[0])))
    except ValueError:
        return default


def portal_page(db, mac: str, qs: Dict[str, List[str]]) -> Dict[str, Any]:
    """The user portal's page of records: newest first, off / lim (at most 500)."""
    off = _int(qs, "off", 0, 0, 10 ** 7)
    lim = _int(qs, "lim", 100, 1, 500)
    rows, total = db.med_get_records(mac, 0, off, lim, newest_first=True)
    return {"success": True, "total": total,
            "items": [{k: r[k] for k in ("day", "slot", "med", "how", "at", "amount", "name", "unit", "updated_at")}
                      for r in rows]}


def app_items(qs: Dict[str, List[str]], device: str, plain_device: str, public: bool, db) -> List[Dict[str, str]]:
    """The items of one /app/medicine request (qs: parse_qs of its query; db:
    the answering server's Database)."""
    mac, why = resolve_device(device, plain_device, public)
    if not mac:
        return _err(why)
    if db is None:
        return _err("服务器没有数据库")
    op = (qs.get("op") or [""])[0]
    if op == "names":
        names = [n for n in (parse_name(x) for x in (qs.get("m") or [])[:PUT_MAX]) if n]
        db.med_put_names(mac, names)
        return [{"t": "ok", "n": str(len(names))}]
    if op == "put":
        recs = []
        for part in qs.get("r") or []:
            recs.extend(part.split(","))
        good = [r for r in (parse_record(x) for x in recs[:PUT_MAX]) if r]
        db.med_put_records(mac, good)
        return [{"t": "ok", "n": str(len(good))}]
    if op == "get":
        start = _int(qs, "from", 0, 0, DAY_MAX)
        off = _int(qs, "off", 0, 0, 10 ** 7)
        lim = _int(qs, "lim", GET_LIMIT, 1, GET_LIMIT)
        rows, total = db.med_get_records(mac, start, off, lim)
        items = [{"t": "r", "d": str(r["day"]), "h": r["slot"], "m": str(r["med"]), "w": r["how"],
                  "a": r["at"] or "-", "n": str(r["amount"]), "nm": r["name"], "u": r["unit"]} for r in rows]
        items.append({"t": "end", "n": str(total), "more": "1" if off + len(rows) < total else "0"})
        return items
    if op == "info":
        n, first, last = db.med_summary(mac)
        return [{"t": "info", "n": str(n), "first": str(first), "last": str(last)}]
    return _err("unknown op")
