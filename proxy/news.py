"""Hot lists for the news app (/app/news?cat=&n=): Baidu's realtime,
livelihood, finance and sports boards (top.baidu.com), the Zhihu hot list
and the Toutiao hot board, one list per request.

Items: {"t": "item", "a": title, "c": rank, "d": tag} with the tag 新, 热 or
爆 when the source marks the entry ("" otherwise), then {"t": "upd", "a":
"HH:MM"}. Lists are cached for 5 minutes; when a source
fails the last list is kept (an error item says why), else the document is
just the error item and the device keeps what it shows.
"""

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Tuple

CACHE_S = 300
TIMEOUT_S = 6
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36")

# Baidu's hotTag: 1 new, 3 hot, 5 boiling (others: none)
BAIDU_TAGS = {"1": "新", "3": "热", "5": "爆"}

_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
_lock = threading.Lock()


class NewsError(Exception):
    pass


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def _clean(title: Any) -> str:
    """One line of text: no control characters or runs of spaces."""
    text = "".join(ch if ch >= " " else " " for ch in str(title or ""))
    return " ".join(text.split())


def parse_baidu(doc: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(title, tag) of a top.baidu.com board, in rank order; the pinned entry
    (isTop, no rank) left out."""
    try:
        content = doc["data"]["cards"][0]["content"]
    except (KeyError, IndexError, TypeError):
        raise NewsError("百度热搜: 没有榜单")
    if content and isinstance(content[0], dict) and "content" in content[0]:
        content = content[0]["content"]   # the realtime board nests its list once more
    out = []
    for entry in content:
        if not isinstance(entry, dict) or entry.get("isTop"):
            continue
        title = _clean(entry.get("word") or entry.get("query"))
        if title:
            out.append((title, BAIDU_TAGS.get(str(entry.get("hotTag", "")), "")))
    return out


def parse_zhihu(doc: Dict[str, Any]) -> List[Tuple[str, str]]:
    out = []
    for entry in doc.get("data") or []:
        title = _clean((entry.get("target") or {}).get("title"))
        if title:
            out.append((title, ""))
    return out


def parse_toutiao(doc: Dict[str, Any]) -> List[Tuple[str, str]]:
    labels = {"new": "新", "hot": "热"}
    out = []
    for entry in doc.get("data") or []:
        title = _clean(entry.get("Title"))
        if title:
            out.append((title, labels.get(str(entry.get("Label", "")).lower(), "")))
    return out


def _baidu(tab: str) -> Callable[[], List[Tuple[str, str]]]:
    return lambda: parse_baidu(_get_json(f"https://top.baidu.com/api/board?platform=wise&tab={tab}"))


# cat -> (list title, fetch)
SOURCES: Dict[str, Tuple[str, Callable[[], List[Tuple[str, str]]]]] = {
    "realtime": ("百度热搜", _baidu("realtime")),
    "livelihood": ("民生榜", _baidu("livelihood")),
    "finance": ("财经榜", _baidu("finance")),
    "sports": ("体育榜", _baidu("sports")),
    "zhihu": ("知乎热榜", lambda: parse_zhihu(_get_json("https://api.zhihu.com/topstory/hot-list?limit=30"))),
    "toutiao": ("今日头条", lambda: parse_toutiao(_get_json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"))),
}


def _items(title: str, rows: List[Tuple[str, str]], n: int, when: float) -> List[Dict[str, str]]:
    items = []
    for rank, (text, tag) in enumerate(rows[:n], 1):
        items.append({"t": "item", "a": text, "c": str(rank), "d": tag})
    items.append({"t": "upd", "a": time.strftime("%H:%M", time.localtime(when))})
    return items


def get_news(cat: str = "realtime", n: int = 10) -> Dict[str, Any]:
    cat = cat if cat in SOURCES else "realtime"
    n = max(1, min(30, n))
    title, fetch = SOURCES[cat]
    now = time.time()
    with _lock:
        hit = _cache.get(cat)
    if hit and now - hit[0] < CACHE_S:
        return {"items": _items(title, hit[1], n, hit[0])}
    try:
        rows = fetch()
        if not rows:
            raise NewsError(f"{title}: 榜单为空")
    except Exception as e:   # unreachable, changed format: the last list stays
        why = str(e) if isinstance(e, NewsError) else f"{title}: {type(e).__name__}"
        if hit:
            return {"items": _items(title, hit[1], n, hit[0]) + [{"t": "error", "a": why}]}
        return {"items": [{"t": "error", "a": why}]}
    with _lock:
        _cache[cat] = (now, rows)
    return {"items": _items(title, rows, n, now)}
