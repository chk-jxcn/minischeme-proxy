"""/app/tide's document from a synthetic hourly series (no network)."""
import math
from datetime import datetime, timedelta

from proxy import tide


def _series(day0):
    # a semidiurnal tide: 12.42 h period, 1.2 m amplitude, from yesterday to 3 days ahead
    start = day0 - timedelta(days=1)
    return [(start + timedelta(hours=h), 1.2 * math.cos(2 * math.pi * h / 12.42)) for h in range(24 * 5)]


def test_items_from_a_series():
    day0 = datetime(2026, 10, 22, tzinfo=tide.TZ)
    now = day0 + timedelta(hours=9, minutes=7)
    items = tide.build_items(20.02, 110.27, "海口", now=now, series=_series(day0), model_point=(20.04, 110.13))
    kinds = [i["t"] for i in items]
    assert kinds[:4] == ["meta", "now", "curve", "upd"]
    assert items[0]["a"] == "海口"
    curve = [int(x) for x in items[2]["a"].split(",")]
    assert len(curve) == 97 and max(curve) <= 121 and min(curve) >= -121
    ext = [i for i in items if i["t"] == "ext"]
    today = [e for e in ext if e["d"] == "0"]
    assert len(today) in (3, 4) and {e["c"] for e in today} == {"high", "low"}
    assert all(e["d"] in ("0", "1") for e in ext)
    assert items[1]["a"] == "09:07" and items[1]["d"] in ("0", "1")


def test_extrema_drop_small_wiggles():
    t0 = datetime(2026, 10, 22, tzinfo=tide.TZ)
    flat = [(t0 + timedelta(minutes=15 * i), 0.02 * math.sin(i)) for i in range(96)]
    assert tide.extrema(flat) == []


def test_place_names():
    assert tide.place_name(20.03, 110.26) == "海口"
    assert tide.place_name(40.0, 100.0) == ""
