"""The calendar app's data (/app/calendar view=today, view=month): the date,
the lunar date, solar terms, festivals, 宜忌 (lunar_python), the countdowns
and world clocks the device asks for. Ported from the original
single-file app proxy; every value is computed for the actual day (no fixed
sample data).

lunar_python (MIT, pure Python) is vendored in proxy/vendor/ so a server
without pip has it; an installed copy comes first.
"""

import datetime as dt
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    from lunar_python import Lunar, Solar
except ImportError:   # the vendored copy
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
    try:
        from lunar_python import Lunar, Solar
    except ImportError:
        Lunar = Solar = None

WEEK_CN = "日一二三四五六"


class CalendarUnavailable(Exception):
    pass


def _need_lunar():
    if Solar is None:
        raise CalendarUnavailable("万年历需要 lunar_python")


def clip(text: Any, n: Optional[int] = None) -> str:
    """Whitespace folded; cut with '…' past n characters."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if n is not None and len(text) > n:
        text = text[:n - 1] + "…"
    return text


def _upd() -> Dict[str, str]:
    return {"t": "upd", "a": dt.datetime.now().strftime("%H:%M")}


def countdowns(text: str, today: dt.date) -> List[Tuple[str, int, dt.date]]:
    """'春节=L01-01, 生日=12-01, 考试=2027-06-07' -> [(name, days, date)], the
    nearest first. YYYY-MM-DD once; MM-DD every year; L before MM-DD: the
    lunar date every year. Dates already past (once) are left out."""
    out = []
    for part in re.split(r"[,，、]", text or ""):
        if "=" not in part:
            continue
        name, when = (x.strip() for x in part.split("=", 1))
        if not name or not when:
            continue
        try:
            lunar = when[:1] in ("L", "l")
            nums = [int(x) for x in re.findall(r"\d+", when)]
            if len(nums) == 3 and not lunar:
                date = dt.date(nums[0], nums[1], nums[2])
            elif len(nums) == 2:
                cands = []
                for year in (today.year - 1, today.year, today.year + 1):
                    if lunar:
                        _need_lunar()
                        s = Lunar.fromYmd(year, nums[0], nums[1]).getSolar()
                        cands.append(dt.date(s.getYear(), s.getMonth(), s.getDay()))
                    else:
                        cands.append(dt.date(year, nums[0], nums[1]))
                date = min((c for c in cands if c >= today), default=None)
            else:
                continue
        except CalendarUnavailable:
            raise
        except Exception:   # an impossible date (Feb 30, a lunar 三十 that month lacks)
            continue
        if date is None or date < today:
            continue
        out.append((name, (date - today).days, date))
    out.sort(key=lambda x: x[1])
    return out


# World clocks: the device keeps them running from the UTC offsets given here
# (DST included), so the proxy only answers which offset each place has now.
WORLD_ZONES = {
    # abbreviations (the DST and standard names of a zone both work)
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "MST": "America/Denver", "MDT": "America/Denver", "MT": "America/Denver",
    "CST": "America/Chicago", "CDT": "America/Chicago", "CT": "America/Chicago",
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "AKST": "America/Anchorage", "AKDT": "America/Anchorage", "HST": "Pacific/Honolulu",
    "BRT": "America/Sao_Paulo", "GMT": "Europe/London", "BST": "Europe/London", "WET": "Europe/Lisbon",
    "CET": "Europe/Paris", "CEST": "Europe/Paris", "EET": "Europe/Athens", "EEST": "Europe/Athens",
    "MSK": "Europe/Moscow", "GST": "Asia/Dubai", "IST": "Asia/Kolkata", "ICT": "Asia/Bangkok",
    "WIB": "Asia/Jakarta", "SGT": "Asia/Singapore", "HKT": "Asia/Hong_Kong", "BJT": "Asia/Shanghai",
    "CCT": "Asia/Shanghai", "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "AWST": "Australia/Perth",
    "ACST": "Australia/Adelaide", "ACDT": "Australia/Adelaide", "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney", "NZST": "Pacific/Auckland", "NZDT": "Pacific/Auckland",
    # place names
    "美西": "America/Los_Angeles", "洛杉矶": "America/Los_Angeles", "旧金山": "America/Los_Angeles",
    "硅谷": "America/Los_Angeles", "西雅图": "America/Los_Angeles", "温哥华": "America/Vancouver",
    "美中": "America/Chicago", "芝加哥": "America/Chicago", "美东": "America/New_York",
    "纽约": "America/New_York", "华盛顿": "America/New_York", "多伦多": "America/Toronto",
    "夏威夷": "Pacific/Honolulu", "伦敦": "Europe/London", "巴黎": "Europe/Paris", "柏林": "Europe/Berlin",
    "莫斯科": "Europe/Moscow", "迪拜": "Asia/Dubai", "印度": "Asia/Kolkata", "曼谷": "Asia/Bangkok",
    "新加坡": "Asia/Singapore", "北京": "Asia/Shanghai", "香港": "Asia/Hong_Kong", "台北": "Asia/Taipei",
    "东京": "Asia/Tokyo", "日本": "Asia/Tokyo", "首尔": "Asia/Seoul", "韩国": "Asia/Seoul",
    "悉尼": "Australia/Sydney", "墨尔本": "Australia/Melbourne", "珀斯": "Australia/Perth",
    "新西兰": "Pacific/Auckland", "奥克兰": "Pacific/Auckland", "UTC": "UTC",
}


def world_zone(spec: str, now: dt.datetime) -> Optional[Tuple[int, str]]:
    """(UTC offset in seconds, abbreviation) of a zone named by an
    abbreviation (PST), a place (美西), an IANA name (America/Chicago) or a
    fixed offset (UTC+8, GMT-3:30); None when unknown."""
    import zoneinfo
    spec = spec.strip()
    m = re.fullmatch(r"(?i)(?:UTC|GMT)\s*([+-])(\d{1,2})(?::?(\d{2}))?", spec)
    if m:
        off = (int(m.group(2)) * 3600 + int(m.group(3) or 0) * 60) * (1 if m.group(1) == "+" else -1)
        return off, "UTC" + m.group(1) + m.group(2) + (":" + m.group(3) if m.group(3) else "")
    name = WORLD_ZONES.get(spec.upper()) or WORLD_ZONES.get(spec) or spec
    try:
        local = now.astimezone(zoneinfo.ZoneInfo(name))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None
    abbr = local.tzname() or ""
    if abbr[:1] in "+-":                # zones without an abbreviation: "+08"
        abbr = ""
    return int(local.utcoffset().total_seconds()), abbr


def world_clocks(text: str, now: Optional[dt.datetime] = None) -> List[Tuple[str, str, int]]:
    """The places of `text` ("美西=PST, 新西兰=NZST", "CST", "东京",
    "伦敦=Europe/London", "UTC+8"; separated by , ， 、 or ;) as
    (label, abbreviation, offset seconds), at most 4; unknown zones skipped."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for part in re.split(r"[,，、;；]", text or ""):
        part = part.strip()
        if not part:
            continue
        label, _, zone = part.partition("=")
        label, zone = label.strip(), (zone.strip() or label.strip())
        got = world_zone(zone, now)
        if not got:
            continue
        off, abbr = got
        if label == zone and "/" in label:      # an IANA name alone: its city
            label = label.rsplit("/", 1)[1].replace("_", " ")
        out.append((clip(label, 8), "" if abbr.upper() == label.upper() else abbr, off))
        if len(out) == 4:
            break
    return out


def cal_today(cd: str, wc: str = "", today: Optional[dt.date] = None) -> List[Dict[str, str]]:
    _need_lunar()
    today = today or dt.date.today()
    s = Solar.fromYmd(today.year, today.month, today.day)
    lu = s.getLunar()
    items = [{"t": "cdate", "a": f"{today.year}年{today.month}月{today.day}日", "b": "星期" + WEEK_CN[s.getWeek()],
              "c": today.isoformat()}]
    # a: 九月十一, b: 丙午马年, c: 戊戌月 丁卯日, d: 属马  冲鸡(辛酉)煞西 (the year's
    # animal; the animal and the stem-branch the day clashes with, its 煞 direction)
    items.append({"t": "lunar", "a": f"{lu.getMonthInChinese()}月{lu.getDayInChinese()}",
                  "b": f"{lu.getYearInGanZhi()}{lu.getYearShengXiao()}年",
                  "c": f"{lu.getMonthInGanZhi()}月 {lu.getDayInGanZhi()}日",
                  "d": f"属{lu.getYearShengXiao()}  冲{lu.getDayChongShengXiao()}"
                       f"({lu.getDayChongGan()}{lu.getDayChong()})煞{lu.getDaySha()}"})
    term = lu.getJieQi()
    nxt = lu.getNextJieQi()
    nd = nxt.getSolar()
    next_date = dt.date(nd.getYear(), nd.getMonth(), nd.getDay())
    if term:
        items.append({"t": "term", "a": term, "b": "今日", "c": nxt.getName(),
                      "d": f"{next_date.month}月{next_date.day}日", "e": str((next_date - today).days)})
    else:
        prev = lu.getPrevJieQi()
        pd = prev.getSolar()
        prev_date = dt.date(pd.getYear(), pd.getMonth(), pd.getDay())
        items.append({"t": "term", "a": prev.getName(), "b": f"第{(today - prev_date).days + 1}天", "c": nxt.getName(),
                      "d": f"{next_date.month}月{next_date.day}日", "e": str((next_date - today).days)})
    fests = list(lu.getFestivals()) + list(s.getFestivals())
    if fests:
        items.append({"t": "fest", "a": clip(" ".join(fests))})
    items.append({"t": "yi", "a": clip(" ".join(lu.getDayYi()))})
    items.append({"t": "ji", "a": clip(" ".join(lu.getDayJi()))})
    for name, days, date in countdowns(cd, today)[:3]:
        items.append({"t": "cd", "a": clip(name, 12), "b": str(days), "c": date.isoformat()})
    for label, abbr, off in world_clocks(wc):
        items.append({"t": "wclk", "a": label, "b": abbr, "c": str(off)})
    items.append(_upd())
    return items


def _day_label(lu, s) -> Tuple[str, str]:
    """The small text under a day: a solar term, a lunar festival, the month
    on the 1st (初一), else the lunar day; two characters fit a cell."""
    term = lu.getJieQi()
    if term:
        return term, "1"
    fests = [f for f in lu.getFestivals() if len(f) <= 3]
    if fests:
        return fests[0][:2], "1"
    if lu.getDay() == 1:
        return lu.getMonthInChinese() + "月", "1"
    return lu.getDayInChinese(), ""


def cal_month(ym: str = "", today: Optional[dt.date] = None) -> List[Dict[str, str]]:
    _need_lunar()
    today = today or dt.date.today()
    year, month = today.year, today.month
    m = re.match(r"^(\d{4})-(\d{1,2})$", ym or "")
    if m:
        year, month = int(m.group(1)), int(m.group(2))
    first = dt.date(year, month, 1)
    days = ((first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - first).days
    items = [{"t": "month", "a": f"{year}年{month}月", "b": str((first.weekday() + 1) % 7), "c": str(days),
              "d": str(today.day) if (today.year, today.month) == (year, month) else "0"}]
    for d in range(1, days + 1):
        s = Solar.fromYmd(year, month, d)
        label, mark = _day_label(s.getLunar(), s)
        items.append({"t": "day", "a": str(d), "b": label, "c": mark})
    # the days of the months before and after that fill six weeks, as a
    # wall calendar shows them ({"t": "xday", d: "p" before, "n" after})
    lead = (first.weekday() + 1) % 7
    for k in range(-lead, 6 * 7 - lead):
        if 0 <= k < days:
            continue
        day = first + dt.timedelta(days=k)
        s = Solar.fromYmd(day.year, day.month, day.day)
        label, mark = _day_label(s.getLunar(), s)
        items.append({"t": "xday", "a": str(day.day), "b": label, "c": mark, "d": "p" if k < 0 else "n"})
    items.append(_upd())
    return items
