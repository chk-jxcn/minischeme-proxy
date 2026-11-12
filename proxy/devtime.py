"""The asking device's clock: dates and times in its answers are in its time zone.

A device says how far its local time is from UTC with each request: the header
X-MiniScheme-UTC-Offset (minutes east of UTC: 480 in China, -300 in New York in
winter; the firmware since v1.0.99, the simulator from the browser's zone), or
the query parameter tzo= of an /app request. Without either (older firmware),
the server's own zone, as before.

The offset is kept for the request being answered (a context variable, set by
the dispatcher); the shared cache keeps answers of other zones apart from the
server's own (cache_tag), since they hold formatted times and dates.
"""
import contextvars
import datetime
from typing import Any, Optional

HEADER = "X-MiniScheme-UTC-Offset"
CHINA = datetime.timezone(datetime.timedelta(hours=8))   # the Chinese markets' clock

_offset: contextvars.ContextVar = contextvars.ContextVar("device_utc_offset", default=None)


def parse_offset(text: Any) -> Optional[int]:
    """Minutes east of UTC from a header or parameter value; None when absent or not one."""
    try:
        minutes = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return minutes if -14 * 60 <= minutes <= 14 * 60 else None


def set_offset(minutes: Optional[int]):
    """The offset of the request being answered; the token for reset()."""
    return _offset.set(minutes)


def reset(token) -> None:
    _offset.reset(token)


def offset() -> Optional[int]:
    return _offset.get()


def _server_offset() -> int:
    delta = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
    return int(delta.total_seconds() // 60)


def tz() -> datetime.tzinfo:
    """The device's zone (a fixed offset), or the server's own."""
    minutes = _offset.get()
    if minutes is None:
        return datetime.datetime.now().astimezone().tzinfo
    return datetime.timezone(datetime.timedelta(minutes=minutes))


def now() -> datetime.datetime:
    """Now on the device's clock (aware)."""
    return datetime.datetime.now(tz())


def today() -> datetime.date:
    return now().date()


def local(when: datetime.datetime) -> datetime.datetime:
    """An instant on the device's clock (a naive one is the server's local time)."""
    return when.astimezone(tz())


def strftime(fmt: str, when: Optional[float] = None) -> str:
    """Now, or the Unix time when, formatted on the device's clock."""
    moment = now() if when is None else datetime.datetime.fromtimestamp(when, tz())
    return moment.strftime(fmt)


def china_now() -> datetime.datetime:
    """Now in China (the stock and futures markets' hours), wherever the server and the device are."""
    return datetime.datetime.now(CHINA)


def cache_tag() -> str:
    """"" for the server's own zone (and devices that say nothing); else a key suffix."""
    minutes = _offset.get()
    return "" if minutes is None or minutes == _server_offset() else f"|utc{minutes:+d}"
