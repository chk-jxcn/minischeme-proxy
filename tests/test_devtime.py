"""Answers on the asking device's clock: the X-MiniScheme-UTC-Offset header or tzo= of the request."""
import datetime
import json

import server
from proxy import calendar_data, devtime, endpoints
from proxy.cache import TTLCache


def at(offset_minutes):
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(minutes=offset_minutes)))


def upd_of(body):
    return next(i["a"] for i in json.loads(body)["items"] if i["t"] == "upd")


def near(hhmm, moment):
    """hhmm is moment's time, or the minute before (the answer was made a moment earlier)."""
    return hhmm in (moment.strftime("%H:%M"), (moment - datetime.timedelta(minutes=1)).strftime("%H:%M"))


def test_parse_offset():
    assert devtime.parse_offset("480") == 480 and devtime.parse_offset("-300") == -300
    assert devtime.parse_offset(" 330 ") == 330
    for bad in ("", None, "x", "9000", "-841", "8.5"):
        assert devtime.parse_offset(bad) is None


def test_no_offset_is_the_servers_clock():
    assert devtime.offset() is None and devtime.cache_tag() == ""
    assert devtime.today() == datetime.date.today()


def test_query_parameter_and_header():
    for minutes in (-600, 345, 780):
        status, _, body = endpoints.handle_app_query(f"/app/usage?tzo={minutes}")
        assert status == 200 and near(upd_of(body), at(minutes))
        status, _, body = server.handle_proxy_endpoint("/app/usage", utc_offset=str(minutes))
        assert status == 200 and near(upd_of(body), at(minutes))
    assert devtime.offset() is None   # nothing left over after a request


def test_today_follows_the_device():
    for minutes in (-720, 840):
        token = devtime.set_offset(minutes)
        try:
            assert devtime.today() == at(minutes).date()
            if calendar_data.Lunar is not None:
                items = calendar_data.cal_today("", "")
                d = at(minutes).date()
                assert any(f"{d.month}" in json.dumps(i, ensure_ascii=False) and f"{d.day}" in json.dumps(i, ensure_ascii=False)
                           for i in items)
        finally:
            devtime.reset(token)


def test_cache_keeps_zones_apart():
    c = TTLCache()
    c.set("k", "server", 60)
    server_offset = int(datetime.datetime.now().astimezone().utcoffset().total_seconds() // 60)
    other = server_offset - 300 if server_offset > 0 else server_offset + 300
    token = devtime.set_offset(other)
    try:
        assert c.get("k") is None
        c.set("k", "other", 60)
        assert c.get("k") == "other"
    finally:
        devtime.reset(token)
    assert c.get("k") == "server"
    token = devtime.set_offset(server_offset)   # a device in the server's zone shares its answers
    try:
        assert c.get("k") == "server"
    finally:
        devtime.reset(token)


def test_markets_keep_chinas_clock():
    assert devtime.china_now().utcoffset() == datetime.timedelta(hours=8)
    token = devtime.set_offset(-480)
    try:
        assert devtime.china_now().utcoffset() == datetime.timedelta(hours=8)
        assert isinstance(endpoints.market_open(), bool) and isinstance(endpoints.futures_open(), bool)
    finally:
        devtime.reset(token)
