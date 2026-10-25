"""/app/bilibili for the B站热门视频 app: Bilibili's public "popular video" lists
turned into the device's items format, plus a small cover-picture endpoint.

Five categories:
  popular  综合热门   /x/web-interface/popular                      (no auth needed)
  precious 入站必刷   /x/web-interface/popular/precious             (no auth needed)
  rank     排行榜     /x/web-interface/ranking/v2?rid=<partition>   (no auth needed)
  weekly   每周必看   /x/web-interface/popular/series/one?number=N  (WBI-signed; this web
           endpoint applies much stricter risk control than the three above and can
           refuse a server with no logged-in browser session, especially under load)
  up       UP主最新视频 /x/v2/space/archive?vmid=&order=pubdate on app.bilibili.com, the
           official mobile apps' own API, app-key signed (not WBI): checked live against
           several different UP主, repeatedly, with no failures; with
           MINISCHEME_BILIBILI_UP_APP_API=0 the web's /x/space/wbi/arc/search instead
           (WBI-signed, Bilibili's most heavily protected endpoint of the five used here,
           refused almost every time from a server with no login). Resolving a name typed by the user to a uid always uses
           the web's /x/web-interface/wbi/search/type?search_type=bili_user, which is
           WBI-signed but reliable in practice regardless of the switch above.

The first three are read straight from the upstream JSON; popular (综合热门) and
排行榜 need no auth at all. weekly, up (by default) and the name search are
WBI-signed (Bilibili's 2023 anti-scraping scheme for the web: a request
parameter signed with a key mixed from two image names given out by the
anonymous-ok nav endpoint) and can still be refused by risk control with no
session; up uses the separate, app-key signed mobile API instead (the
default: see MINISCHEME_BILIBILI_UP_APP_API below), which holds up much
better with no login. Either way, same policy as any other
upstream failure here: keep the app's last data, tell the user why, never
invent a list.

Document: {"items": [{"t":"meta","a":subject}?, {"t":"v","a":title,"b":aid,"c":views,"d":UP主,"e":day}...,
                     {"t":"upd","a":"HH:MM"}]}
(day: the upload's Beijing date as days since 1970-01-01, "" when unknown; the
board turns it into "3天前" against its own date, so a kept document stays right)
Pictures: /app/bilibili/pic?id=<aid>&w=<px>&h=<px> answers a small baseline JPEG cover,
cropped and resized to exactly the box the device asked for (board_jpeg-style, 11.7).
"""

import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .cache import TTLCache

BJT = timezone(timedelta(hours=8))   # Beijing time: "today" for the 【新】 mark is this zone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"
API = "https://api.bilibili.com"

DOC_CACHE_S = 300            # the list changes slowly: 5 minutes is plenty
PIC_URL_CACHE_S = 1800       # aid -> cover url, kept longer than the list document
PIC_JPEG_CACHE_S = 3600      # the converted JPEG itself, per (aid, w, h)
FETCH_N_DEFAULT = 30         # videos fetched per category; the device paginates locally
FETCH_N_MAX = 40
PIC_BOX_MIN, PIC_BOX_MAX = 24, 200
PIC_W_DEFAULT, PIC_H_DEFAULT = 120, 68

CATEGORIES = ("popular", "precious", "rank", "weekly", "up")

# The top-level content zones ranking/v2 accepts (rid, name); 0 = the whole site.
# Checked against the live API (2026-10-23): each rid below answers real items
# whose own sub-tags belong to that zone.
PARTITIONS = [
    ("0", "全站"), ("1", "动画"), ("3", "音乐"), ("4", "游戏"), ("5", "娱乐"),
    ("36", "知识"), ("119", "鬼畜"), ("129", "舞蹈"), ("155", "时尚"), ("160", "生活"),
    ("168", "国创相关"), ("177", "纪录片"), ("181", "影视"), ("188", "科技"),
    ("211", "美食"), ("217", "动物圈"), ("223", "汽车"), ("234", "运动"),
]
PARTITION_NAMES = dict(PARTITIONS)

cache = TTLCache()                               # list documents, keyed by "cat:rid"
pic_cache = TTLCache()                           # converted JPEGs, keyed by "aid:w:h"
_pic_urls: Dict[str, Tuple[float, str]] = {}      # aid -> (expiry, cover url)
_pic_lock = threading.Lock()


class BilibiliError(Exception):
    pass


# ---------------------------------------------------------------------------
# The app declares api = 1.11 (meta.ini): the board's font server fetches any
# glyph its own fonts lack (emoji, Korean, traditional characters, kanji
# outside GB2312) and keeps it, redrawing the text once it arrives (section
# 9). So titles - full of exactly that kind of character - go to the device
# as they are; this just collapses whitespace runs a scraped title may have.
# (Pre-1.11 or offline-board apps instead drop characters the font ROM lacks:
# see the board_text sample in the guide's section 9 for that version.)
# ---------------------------------------------------------------------------

def board_text(s: str) -> str:
    return " ".join(s.split())


def _fmt_count(n: float) -> str:
    """Bilibili-style play/view counts: 1.2亿, 34.5万, 820."""
    n = float(n or 0)
    if n >= 1e8:
        return f"{n / 1e8:.1f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return str(int(n))


def _pub_day(ts: Any) -> str:
    """An upload time (unix seconds) -> its Beijing date as days since 1970-01-01, or ""."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        return ""
    try:
        return str((datetime.fromtimestamp(ts, BJT).date() - date(1970, 1, 1)).days)
    except (OverflowError, OSError, ValueError):
        return ""


def _full_pic_url(pic: str) -> str:
    """Some endpoints (the user search preview) give a protocol-relative URL."""
    if pic.startswith("//"):
        return "https:" + pic
    return pic


def _get_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# WBI signing: popular/series/one (and a few other newer endpoints) require a
# signed w_rid + wts since Bilibili's 2023 change. The keys come from the
# (anonymous-ok) nav endpoint and are mixed through a fixed permutation table.
# ---------------------------------------------------------------------------

_MIXIN_KEY_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28,
    14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21,
    56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_wbi_cache: Optional[Tuple[float, str]] = None   # (fetched-at, 32-char mixin key)
_wbi_lock = threading.Lock()


def _wbi_mixin_key() -> str:
    global _wbi_cache
    now = time.time()
    with _wbi_lock:
        if _wbi_cache and now - _wbi_cache[0] < 3600:
            return _wbi_cache[1]
    nav = _get_json(f"{API}/x/web-interface/nav")
    img = nav["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = nav["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    mixin = "".join(raw[i] for i in _MIXIN_KEY_TAB if i < len(raw))[:32]
    with _wbi_lock:
        _wbi_cache = (now, mixin)
    return mixin


def _wbi_sign(params: Dict[str, Any]) -> Dict[str, str]:
    mixin = _wbi_mixin_key()
    p = dict(params)
    p["wts"] = int(time.time())
    p = dict(sorted(p.items()))
    p = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in p.items()}
    query = urllib.parse.urlencode(p)
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p


# ---------------------------------------------------------------------------
# The four sources
# ---------------------------------------------------------------------------

def _fetch_popular(n: int) -> List[Dict[str, Any]]:
    d = _get_json(f"{API}/x/web-interface/popular?ps={n}&pn=1")
    if d.get("code") != 0:
        raise BilibiliError(f"综合热门: {d.get('message') or d.get('code')}")
    return (d.get("data") or {}).get("list") or []


def _fetch_precious(n: int) -> List[Dict[str, Any]]:
    d = _get_json(f"{API}/x/web-interface/popular/precious?page_size={n}&page=1")
    if d.get("code") != 0:
        raise BilibiliError(f"入站必刷: {d.get('message') or d.get('code')}")
    return ((d.get("data") or {}).get("list") or [])[:n]


def _fetch_rank(rid: str, n: int) -> List[Dict[str, Any]]:
    rid = rid if rid in PARTITION_NAMES else "0"
    d = _get_json(f"{API}/x/web-interface/ranking/v2?rid={rid}&type=all")
    if d.get("code") != 0:
        raise BilibiliError(f"排行榜: {d.get('message') or d.get('code')}")
    return ((d.get("data") or {}).get("list") or [])[:n]


def _latest_week() -> Tuple[int, str]:
    d = _get_json(f"{API}/x/web-interface/popular/series/list")
    rows = (d.get("data") or {}).get("list") or []
    if d.get("code") != 0 or not rows:
        raise BilibiliError(f"每周必看: {d.get('message') or d.get('code')}")
    first = rows[0]
    return int(first["number"]), str(first.get("subject") or "")


def _fetch_weekly(n: int) -> Tuple[List[Dict[str, Any]], str]:
    number, subject = _latest_week()
    qs = urllib.parse.urlencode(_wbi_sign({"number": number}))
    d = _get_json(f"{API}/x/web-interface/popular/series/one?{qs}")
    if d.get("code") != 0:
        # Bilibili's risk control on this one endpoint: keep the app's last data,
        # show why (section 11.3's error item), never invent a list.
        raise BilibiliError(f"第{number}期暂不可获取（风控 {d.get('code')}，请稍后重试）")
    rows = (d.get("data") or {}).get("list") or []
    return rows[:n], subject


# ---------------------------------------------------------------------------
# App-key signing: the official mobile apps' own API, a different surface
# from the web's WBI scheme and far less aggressively risk-controlled for a
# server with no login (checked live, 2026-10-24: steady across many calls
# and several different UP主, where the web's WBI-signed equivalent could
# not succeed even once). APP_KEY / APP_SECRET are not a secret of ours:
# they are the values countless open-source Bilibili clients (BBDown,
# bilibili-API-collect and others) have published and used openly for
# years, but they are Bilibili's own mobile apps' signing keys: the owner
# of this project chose to use them (2026-10-24). ON by default; set
# MINISCHEME_BILIBILI_UP_APP_API=0 on a server that must not use them, and
# "up" falls back to the web's WBI-signed endpoint, with the same "keep the
# app's last data, say why" policy on failure.
# ---------------------------------------------------------------------------

APP_KEY = "1d8b6e7d45233436"
APP_SECRET = "560c52ccd288fed045859ed18bffd973"
APP_UA = "bilibili-freedoooooom/MA"


def up_uses_app_api() -> bool:
    return os.environ.get("MINISCHEME_BILIBILI_UP_APP_API", "1").strip().lower() not in ("0", "false", "no", "off")


def _app_sign(params: Dict[str, Any]) -> str:
    p = dict(params)
    p["appkey"] = APP_KEY
    p["ts"] = int(time.time())
    p = dict(sorted(p.items()))
    qs = urllib.parse.urlencode(p)
    return qs + "&sign=" + hashlib.md5((qs + APP_SECRET).encode()).hexdigest()


def _get_app_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": APP_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# UP主最新视频: resolve whatever the user typed (a uid, a space.bilibili.com
# link, or a name) to a mid, then the uploader's own newest videos.
# ---------------------------------------------------------------------------

_UID_RE = re.compile(r"space\.bilibili\.com/(\d+)")


def _fetch_card_name(mid: str) -> str:
    d = _get_json(f"{API}/x/web-interface/card?mid={mid}")
    if d.get("code") != 0:
        raise BilibiliError(f"找不到这个 UP 主（UID {mid}）")
    return str(((d.get("data") or {}).get("card") or {}).get("name") or mid)


def _search_user(name: str) -> Tuple[str, str]:
    """A name typed by the user -> (mid, exact uname), through the public user
    search (unlike arc/search this one is not blocked for a server with no
    login - checked live 2026-10-24)."""
    qs = urllib.parse.urlencode(_wbi_sign({"search_type": "bili_user", "keyword": name}))
    d = _get_json(f"{API}/x/web-interface/wbi/search/type?{qs}")
    if d.get("code") != 0:
        raise BilibiliError(f"搜索 UP 主失败: {d.get('message') or d.get('code')}")
    results = (d.get("data") or {}).get("result") or []
    if not results:
        raise BilibiliError(f"没有找到名叫“{name}”的 UP 主")
    best = results[0]
    return str(best.get("mid") or ""), str(best.get("uname") or name)


def _resolve_up(raw: str) -> Tuple[str, str]:
    """Whatever the user put in the web page's UP主 field -> (mid, display name):
    a bare uid, a space.bilibili.com/<uid>... link, or a name to search for."""
    raw = (raw or "").strip()
    if not raw:
        raise BilibiliError("请先在应用网页里填写 UP 主的 UID、空间链接或名字")
    m = _UID_RE.search(raw)
    mid = m.group(1) if m else (raw if raw.isdigit() else None)
    if mid:
        return mid, _fetch_card_name(mid)
    return _search_user(raw)


def _fetch_up_videos_app(mid: str, n: int) -> List[Dict[str, Any]]:
    """The official mobile apps' own API (app-key signed): opt-in, see up_uses_app_api()."""
    qs = _app_sign({
        "vmid": mid, "ps": min(max(n, 1), 20), "pn": 1, "order": "pubdate",
        "platform": "android", "mobi_app": "android", "build": "6830200", "channel": "bili",
    })
    d = _get_app_json(f"https://app.bilibili.com/x/v2/space/archive?{qs}")
    if d.get("code") != 0:
        raise BilibiliError(f"UP 主投稿获取失败（{d.get('code')}: {d.get('message') or ''}，请稍后重试）")
    raw = (d.get("data") or {}).get("item") or []
    # normalized to the same shape _make_up_items expects from the web endpoint below
    return [{"aid": v.get("param"), "title": v.get("title"), "pic": v.get("cover"),
             "play": v.get("play"), "created": v.get("ctime")} for v in raw[:n]]


def _fetch_up_videos_web(mid: str, n: int) -> List[Dict[str, Any]]:
    """The web's WBI-signed endpoint: the default until app-key signing is approved.
    Bilibili's most heavily protected endpoint of the five used here - a server with
    no login is refused almost every time (checked live, 2026-10-24)."""
    qs = urllib.parse.urlencode(_wbi_sign({"mid": mid, "ps": min(n, 30), "pn": 1, "order": "pubdate"}))
    d = _get_json(f"{API}/x/space/wbi/arc/search?{qs}")
    if d.get("code") != 0:
        raise BilibiliError(f"UP 主投稿获取失败（风控 {d.get('code')}，请稍后重试）")
    return (((d.get("data") or {}).get("list") or {}).get("vlist") or [])[:n]


def _fetch_up_videos(mid: str, n: int) -> List[Dict[str, Any]]:
    return _fetch_up_videos_app(mid, n) if up_uses_app_api() else _fetch_up_videos_web(mid, n)


def _make_up_items(vlist: List[Dict[str, Any]], n: int, up: str = "") -> List[Dict[str, str]]:
    today = datetime.now(BJT).date()
    items = []
    for v in vlist[:n]:
        aid = str(v.get("aid") or "")
        if not aid:
            continue
        is_new = False
        created = v.get("created")
        if isinstance(created, (int, float)):
            try:
                is_new = datetime.fromtimestamp(created, BJT).date() == today
            except (OverflowError, OSError, ValueError):
                is_new = False
        title = board_text(str(v.get("title") or ""))[:56] or "(无标题)"
        if is_new:
            title = "【新】" + title
        pic = _full_pic_url(str(v.get("pic") or ""))
        views = _fmt_count(v.get("play") or 0)
        if pic:
            _remember_pic_url(aid, pic)
        items.append({"t": "v", "a": title, "b": aid, "c": views, "d": board_text(up)[:24], "e": _pub_day(created)})
    return items


# ---------------------------------------------------------------------------
# aid -> cover url, remembered for the picture endpoint
# ---------------------------------------------------------------------------

def _remember_pic_url(aid: str, url: str):
    if not (aid and url):
        return
    with _pic_lock:
        _pic_urls[aid] = (time.time() + PIC_URL_CACHE_S, url)
        if len(_pic_urls) > 400:
            stale = sorted(_pic_urls.items(), key=lambda kv: kv[1][0])[:200]
            for k, _ in stale:
                _pic_urls.pop(k, None)


def _pic_url_for(aid: str) -> Optional[str]:
    with _pic_lock:
        hit = _pic_urls.get(aid)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _make_items(raw: List[Dict[str, Any]], n: int) -> List[Dict[str, str]]:
    items = []
    for v in raw[:n]:
        aid = str(v.get("aid") or "")
        title = board_text(str(v.get("title") or ""))[:60] or "(无标题)"
        pic = _full_pic_url(str(v.get("pic") or ""))
        stat = v.get("stat") or {}
        views = _fmt_count(stat.get("view") if stat.get("view") is not None else v.get("view") or 0)
        up = board_text(str((v.get("owner") or {}).get("name") or v.get("author") or ""))[:24]
        if aid and pic:
            _remember_pic_url(aid, pic)
        if aid:
            items.append({"t": "v", "a": title, "b": aid, "c": views, "d": up, "e": _pub_day(v.get("pubdate"))})
    return items


# ---------------------------------------------------------------------------
# Entry points used by handle_app_query
# ---------------------------------------------------------------------------

def _build_document(category: str, rid: str, up: str, n: int) -> Tuple[List[Dict[str, str]], str]:
    """-> (items, subject). subject is the week's theme (weekly) or the
    resolved UP主's display name (up), shown as a "meta" item; "" otherwise."""
    if category == "popular":
        return _make_items(_fetch_popular(n), n), ""
    if category == "precious":
        return _make_items(_fetch_precious(n), n), ""
    if category == "rank":
        return _make_items(_fetch_rank(rid, n), n), ""
    if category == "up":
        mid, name = _resolve_up(up)
        items = _make_up_items(_fetch_up_videos(mid, n), n, name)
        if not items:
            raise BilibiliError(f"{name} 最近没有投稿")
        return items, name
    raw, subject = _fetch_weekly(n)
    return _make_items(raw, n), subject


def get_bilibili(category: str = "popular", rid: str = "0", n: int = FETCH_N_DEFAULT, up: str = "") -> Dict[str, Any]:
    category = category if category in CATEGORIES else "popular"
    rid = rid if rid in PARTITION_NAMES else "0"
    n = max(1, min(FETCH_N_MAX, n or FETCH_N_DEFAULT))
    cache_key = f"{category}:{rid if category == 'rank' else ''}:{up if category == 'up' else ''}:{n}"
    hit = cache.get(cache_key)
    if hit:
        return hit
    try:
        items, subject = _build_document(category, rid, up, n)
        if not items:
            raise BilibiliError("列表为空")
    except BilibiliError as e:
        return {"items": [{"t": "error", "a": str(e)}]}
    except (urllib.error.URLError, OSError) as e:
        return {"items": [{"t": "error", "a": f"B站接口无法访问: {type(e).__name__}"}]}
    except Exception as e:  # noqa: BLE001 - never crash the request over an upstream surprise
        return {"items": [{"t": "error", "a": f"B站: {type(e).__name__}"}]}
    doc_items: List[Dict[str, str]] = []
    if subject:
        doc_items.append({"t": "meta", "a": board_text(subject)[:40]})
    doc_items.extend(items)
    doc_items.append({"t": "upd", "a": time.strftime("%H:%M", time.localtime())})
    data = {"items": doc_items}
    cache.set(cache_key, data, ttl_seconds=DOC_CACHE_S)
    return data


def _cover_crop(im, w: int, h: int):
    from PIL import Image
    resample = getattr(Image, "Resampling", Image).LANCZOS
    sw, sh = im.size
    target, src = w / h, sw / sh
    if src > target:
        new_w = max(1, int(sh * target))
        x0 = (sw - new_w) // 2
        im = im.crop((x0, 0, x0 + new_w, sh))
    elif src < target:
        new_h = max(1, int(sw / target))
        y0 = (sh - new_h) // 2
        im = im.crop((0, y0, sw, y0 + new_h))
    return im.resize((w, h), resample)


def board_cover_jpeg(raw: bytes, w: int, h: int) -> bytes:
    """A small baseline JPEG, center-cropped and resized to exactly w x h (11.7)."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im = _cover_crop(im, w, h)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=72, progressive=False, optimize=True)
    return out.getvalue()


def picture(aid: str, w: int = PIC_W_DEFAULT, h: int = PIC_H_DEFAULT) -> Tuple[int, str, bytes]:
    """(status, content_type, body) for /app/bilibili/pic?id=<aid>&w=&h= (section 11.7):
    only ids the device learned from a list document are ever fetched, never an open proxy."""
    aid = (aid or "").strip()
    if not aid.isdigit():
        return 404, "text/plain; charset=utf-8", b"bad id"
    w = max(PIC_BOX_MIN, min(PIC_BOX_MAX, w or PIC_W_DEFAULT))
    h = max(PIC_BOX_MIN, min(PIC_BOX_MAX, h or PIC_H_DEFAULT))
    pic_key = f"{aid}:{w}:{h}"
    hit = pic_cache.get(pic_key)
    if hit:
        return 200, "image/jpeg", hit
    url = _pic_url_for(aid)
    if not url:
        return 404, "text/plain; charset=utf-8", b"unknown id (not in a recent list document)"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read()
        jpeg = board_cover_jpeg(raw, w, h)
    except Exception as e:  # noqa: BLE001 - the device keeps the placeholder box either way
        return 502, "text/plain; charset=utf-8", f"cover: {type(e).__name__}".encode("utf-8")
    pic_cache.set(pic_key, jpeg, ttl_seconds=PIC_JPEG_CACHE_S)
    return 200, "image/jpeg", jpeg
