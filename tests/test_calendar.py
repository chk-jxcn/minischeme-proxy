"""The calendar app's data is computed for the day asked (no fixed sample)."""
import datetime as dt

from proxy.calendar_data import cal_month, cal_today, countdowns, world_clocks


def kinds(items, t):
    return [i for i in items if i["t"] == t]


def test_today_follows_the_day_and_the_settings():
    day = dt.date(2026, 10, 20)
    items = cal_today("", "", today=day)
    assert kinds(items, "cdate")[0]["c"] == "2026-10-20"
    assert not kinds(items, "cd") and not kinds(items, "wclk")   # none asked, none given
    other = cal_today("", "", today=dt.date(2027, 1, 1))
    assert kinds(other, "lunar")[0]["a"] != kinds(items, "lunar")[0]["a"]
    asked = cal_today("春节=L01-01, 考试=2027-06-07, 旧=2020-01-01", "美西=PST, 新西兰=NZST, 火星=XYZ", today=day)
    cds = kinds(asked, "cd")
    assert [c["a"] for c in cds] == ["春节", "考试"]       # the past one-off date is left out
    assert cds[0]["c"] == "2027-02-06" and cds[0]["b"] == "109"
    assert [w["a"] for w in kinds(asked, "wclk")] == ["美西", "新西兰"]   # the unknown zone is skipped


def test_countdown_forms():
    day = dt.date(2026, 12, 25)
    got = {name: days for name, days, _ in countdowns("圣诞=12-25, 元旦=01-01, 中秋=L08-15", day)}
    assert got["圣诞"] == 0 and got["元旦"] == 7 and got["中秋"] > 200


def test_world_clock_offsets():
    now = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    clocks = world_clocks("纽约, UTC+8, 伦敦=Europe/London", now)
    assert [c[2] for c in clocks] == [-4 * 3600, 8 * 3600, 3600]


def test_month_has_real_lunar_days():
    items = cal_month("2026-02", today=dt.date(2026, 10, 20))
    head, days = items[0], kinds(items, "day")
    assert head["c"] == "28" and head["d"] == "0" and len(days) == 28
    assert any(d["b"] == "春节" or d["b"] == "正月" for d in days)   # 2026-02-17 is the lunar new year
