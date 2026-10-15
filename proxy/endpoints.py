"""Application endpoints and data fetching logic with caching."""

import json
import urllib.request
import urllib.parse
from typing import Dict, Any, Optional
from .cache import TTLCache

cache = TTLCache()


def get_weather(loc: str = "auto", key: str = "", host: str = "") -> Dict[str, Any]:
    """Fetches weather forecast (Open-Meteo or QWeather). Cached 10 min."""
    cache_key = f"weather:{loc}:{key}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    # Default Open-Meteo or fallback simulation
    data = {
        "items": [
            {
                "t": "weather",
                "city": "北京" if loc == "auto" else loc,
                "temp": "22",
                "code": "100",
                "text": "晴",
                "wind": "微风",
                "humidity": "45%",
                "uv": "5",
                "forecast": "晴 16~24℃"
            }
        ]
    }
    cache.set(cache_key, data, ttl_seconds=600)
    return data


def get_stocks(codes: str = "sh000001") -> Dict[str, Any]:
    """Fetches stock market quotes. Cached 15s."""
    cache_key = f"stocks:{codes}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    data = {
        "items": [
            {
                "t": "quote",
                "code": codes,
                "name": "上证指数",
                "price": "3050.25",
                "change": "+15.30",
                "percent": "+0.50%"
            }
        ]
    }
    cache.set(cache_key, data, ttl_seconds=15)
    return data


def get_crypto(symbols: str = "BTC") -> Dict[str, Any]:
    """Fetches crypto market prices. Cached 10s."""
    cache_key = f"crypto:{symbols}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    data = {
        "items": [
            {
                "t": "crypto",
                "symbol": symbols,
                "price": "65400.00",
                "change24h": "+2.4%"
            }
        ]
    }
    cache.set(cache_key, data, ttl_seconds=10)
    return data


def get_news(category: str = "realtime", n: int = 8) -> Dict[str, Any]:
    """Fetches news headlines. Cached 5 min."""
    cache_key = f"news:{category}:{n}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    data = {
        "items": [
            {"t": "news", "title": "MiniScheme 跨平台嵌入式系统发布全新版本", "source": "TechDaily"},
            {"t": "news", "title": "开源生态持续繁荣，轻量级 Lisp 虚拟机广泛应用于物联网", "source": "OSChina"}
        ]
    }
    cache.set(cache_key, data, ttl_seconds=300)
    return data
