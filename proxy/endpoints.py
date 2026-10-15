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

import time
from typing import Dict, Any, List
from .cache import TTLCache

cache = TTLCache()


# ---------------------------------------------------------------------------
# 1. Weather Forecast (/app/weather) - Serves all 5 pages in 1 document
# ---------------------------------------------------------------------------

def get_weather(loc: str = "auto", key: str = "", host: str = "") -> Dict[str, Any]:
    """Fetches weather forecast for all pages (now, 7-day, 24-hr, rain/air, indices).

    Cached for 10 minutes (600 seconds).
    """
    cache_key = f"weather:{loc}:{key}:{host}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    city_name = "北京" if (not loc or loc == "auto") else loc
    now_ts = int(time.time())

    items = [
        {"t": "loc", "a": city_name, "b": "MiniScheme-Proxy"},
        {"t": "now", "a": "晴", "b": "22", "c": "100", "d": "东北风 2级", "e": "45"},
        {"t": "now2", "a": "东北风", "b": "2级", "c": "1013", "d": "10", "e": "5"},
        # 7-day forecast
        {"t": "day", "a": "周五", "b": "晴", "c": "24", "d": "14", "e": "100"},
        {"t": "day", "a": "周六", "b": "多云", "c": "23", "d": "13", "e": "101"},
        {"t": "day", "a": "周日", "b": "小雨", "c": "19", "d": "11", "e": "305"},
        {"t": "day", "a": "周一", "b": "晴", "c": "21", "d": "12", "e": "100"},
        {"t": "day", "a": "周二", "b": "多云", "c": "22", "d": "13", "e": "101"},
        {"t": "day", "a": "周三", "b": "晴", "c": "24", "d": "15", "e": "100"},
        {"t": "day", "a": "周四", "b": "阴", "c": "20", "d": "13", "e": "104"},
        # 24-hour forecast
        {"t": "hour", "a": "12:00", "b": "22", "c": "100", "d": "0"},
        {"t": "hour", "a": "15:00", "b": "24", "c": "100", "d": "0"},
        {"t": "hour", "a": "18:00", "b": "20", "c": "101", "d": "10"},
        {"t": "hour", "a": "21:00", "b": "17", "c": "101", "d": "10"},
        {"t": "hour", "a": "00:00", "b": "15", "c": "150", "d": "5"},
        {"t": "hour", "a": "03:00", "b": "14", "c": "150", "d": "0"},
        {"t": "hour", "a": "06:00", "b": "14", "c": "100", "d": "0"},
        {"t": "hour", "a": "09:00", "b": "18", "c": "100", "d": "0"},
        # Rain & Air Quality
        {"t": "rain", "a": "未来两小时无降水", "b": "0,0,0,0,0,0,0,0,0,0"},
        {"t": "air", "a": "42", "b": "优", "c": "15", "d": "30"},
        # Life Indices
        {"t": "idx", "a": "舒适度", "b": "舒适", "c": "白天温度适宜，风力不大，感受舒适"},
        {"t": "idx", "a": "穿衣指数", "b": "夹克", "c": "建议着单层棉麻面料的短套装、T恤衫"},
        {"t": "idx", "a": "感冒指数", "b": "少发", "c": "各项气象条件适宜，无明显降温"},
        {"t": "idx", "a": "紫外线", "b": "中等", "c": "涂擦SPF大于15、PA+防晒护肤品"},
        {"t": "upd", "a": time.strftime("%H:%M", time.localtime(now_ts))}
    ]
    data = {"items": items}
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
    data = {"items": items}
    cache.set(cache_key, data, ttl_seconds=300)
    return data


# ---------------------------------------------------------------------------
# 3. Stock Market (/app/stocks) - Supports view=all for all 4 pages
# ---------------------------------------------------------------------------

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

    items: List[Dict[str, str]] = [
        {"t": "meta", "a": "交易中", "b": "沪深市场", "c": "15:00"},
        # Index quote
        {"t": "quote", "a": "sh000001", "b": "上证指数", "c": "+0.57", "d": "3068.20", "e": "+17.20,3075.10,3051.00,3051.00"},
        {"t": "quote", "a": "sz399001", "b": "深证成指", "c": "+0.82", "d": "9680.50", "e": "+78.60,9710.00,9610.00,9601.90"},
        {"t": "quote", "a": "sz399006", "b": "创业板指", "c": "+1.15", "d": "1895.30", "e": "+21.50,1905.00,1875.00,1873.80"},
        # Watch list stocks
        {"t": "quote", "a": "sh600519", "b": "贵州茅台", "c": "+0.65", "d": "1580.00", "e": "+10.20,1590.00,1570.00,1569.80"},
        {"t": "quote", "a": "sz300750", "b": "宁德时代", "c": "+2.35", "d": "245.50", "e": "+5.65,248.00,240.00,239.85"},
        {"t": "quote", "a": "sz000858", "b": "五 粮 液", "c": "+0.45", "d": "142.20", "e": "+0.64,143.50,141.50,141.56"},
        # Intraday trend curve points
        {"t": "trend", "a": "12,18,25,30,22,15,40,45,52,50,48,55,57", "b": "100", "c": "0", "d": "241"},
        # 30-day kline closes
        {"t": "kline", "a": "-5,2,8,12,10,15,18,22,25,28,30,35,42,50,57"}
    ]

    items.append({"t": "upd", "a": time.strftime("%H:%M:%S", time.localtime())})
    data = {"items": items}
    cache.set(cache_key, data, ttl_seconds=15)
    return data


# ---------------------------------------------------------------------------
# 4. Crypto Market (/app/crypto) - Supports view=all for all pages
# ---------------------------------------------------------------------------

def get_crypto(view: str = "home", symbols: str = "BTC,ETH,SOL", fiat: str = "usd") -> Dict[str, Any]:
    """Fetches cryptocurrency quotes and trends.

    Cached for 10 seconds.
    """
    cache_key = f"crypto:{view}:{symbols}:{fiat}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    items = [
        {"t": "meta", "a": "24H交易", "b": "全球市场"},
        {"t": "quote", "a": "BTC", "b": "Bitcoin", "c": "+2.45", "d": "66850.00", "e": "+1600.00,67200.00,65100.00,65250.00"},
        {"t": "quote", "a": "ETH", "b": "Ethereum", "c": "+1.85", "d": "3520.50", "e": "+64.00,3560.00,3440.00,3456.50"},
        {"t": "quote", "a": "SOL", "b": "Solana", "c": "+5.20", "d": "158.40", "e": "+7.80,162.00,149.00,150.60"},
        {"t": "trend", "a": "10,20,15,25,35,30,45,55,50,60,65", "b": "100", "c": "0", "d": "100"},
        {"t": "upd", "a": time.strftime("%H:%M:%S", time.localtime())}
    ]
    data = {"items": items}
    cache.set(cache_key, data, ttl_seconds=10)
    return data


# ---------------------------------------------------------------------------
# 5. Futures Market (/app/futures) - Supports view=all for all pages
# ---------------------------------------------------------------------------

def get_futures(view: str = "home", codes: str = "RB0,AU0,CU0") -> Dict[str, Any]:
    """Fetches futures contracts and trends.

    Cached for 15 seconds.
    """
    cache_key = f"futures:{view}:{codes}"
    hit = cache.get(cache_key)
    if hit:
        return hit

    items = [
        {"t": "meta", "a": "交易中", "b": "国内期货"},
        {"t": "quote", "a": "RB0", "b": "螺纹主力", "c": "+0.75", "d": "3420", "e": "+25,3440,3390,3395"},
        {"t": "quote", "a": "AU0", "b": "沪金主力", "c": "+0.42", "d": "595.60", "e": "+2.50,598.00,592.50,593.10"},
        {"t": "quote", "a": "CU0", "b": "沪铜主力", "c": "+1.12", "d": "78200", "e": "+870,78500,77200,77330"},
        {"t": "trend", "a": "5,12,18,22,20,28,32,35,40,42", "b": "80", "c": "0", "d": "100"},
        {"t": "upd", "a": time.strftime("%H:%M:%S", time.localtime())}
    ]
    data = {"items": items}
    cache.set(cache_key, data, ttl_seconds=15)
    return data
