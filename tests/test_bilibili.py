"""/app/bilibili and /app/bilibili/pic built from synthetic upstream data (no network)."""
import io
from datetime import datetime, timedelta

import pytest

from proxy import bilibili


def _video(aid, title, views=123456, pic="http://i0.hdslb.com/bfs/archive/x.jpg", owner="UP一号"):
    return {"aid": aid, "title": title, "pic": pic, "stat": {"view": views}, "owner": {"name": owner}}


def _up_video(aid, title, days_ago=0, play=1000, pic="http://i0.hdslb.com/bfs/archive/x.jpg"):
    """_make_up_items' normalized shape (aid/pic/created): both _fetch_up_videos_web and
    _fetch_up_videos_app (which translates the mobile API's own param/cover/ctime) return this."""
    when = datetime.now(bilibili.BJT) - timedelta(days=days_ago)
    return {"aid": str(aid), "title": title, "pic": pic, "play": play, "created": int(when.timestamp())}


def _fresh_cache():
    bilibili.cache.clear()
    bilibili.pic_cache.clear()
    with bilibili._pic_lock:
        bilibili._pic_urls.clear()


def test_board_text_only_collapses_whitespace():
    # api = 1.11: the board's font server fills any glyph gap (emoji, Korean,
    # traditional characters, kanji outside GB2312), so titles are no longer
    # filtered here - only whitespace runs from a scraped title are collapsed.
    assert bilibili.board_text("Hello 世界") == "Hello 世界"
    assert bilibili.board_text("emoji\U0001F600here \U0001F600") == "emoji\U0001F600here \U0001F600"
    assert bilibili.board_text("한글, 繁體字") == "한글, 繁體字"
    assert bilibili.board_text("  a   b  ") == "a b"


def test_fmt_count_matches_bilibili_style():
    assert bilibili._fmt_count(820) == "820"
    assert bilibili._fmt_count(34_500) == "3.5万"
    assert bilibili._fmt_count(123_000_000) == "1.2亿"


def test_popular_items(monkeypatch):
    _fresh_cache()
    rows = [_video(1, "标题一", views=123), _video(2, "标题二", views=20_000)]
    monkeypatch.setattr(bilibili, "_fetch_popular", lambda n: rows)
    doc = bilibili.get_bilibili("popular", "0", 10)
    items = doc["items"]
    assert items[0] == {"t": "v", "a": "标题一", "b": "1", "c": "123", "d": "UP一号", "e": ""}
    assert [i["t"] for i in items] == ["v", "v", "upd"]
    assert items[1]["c"] == "2.0万"


def test_unknown_category_falls_back_to_popular(monkeypatch):
    _fresh_cache()
    monkeypatch.setattr(bilibili, "_fetch_popular", lambda n: [_video(1, "x")])
    doc = bilibili.get_bilibili("not-a-category", "0", 5)
    assert doc["items"][0]["t"] == "v"


def test_rank_unknown_partition_falls_back_to_all_site(monkeypatch):
    _fresh_cache()
    seen = {}

    def fake_rank(rid, n):
        seen["rid"] = rid
        return [_video(1, "x")]

    monkeypatch.setattr(bilibili, "_fetch_rank", fake_rank)
    bilibili.get_bilibili("rank", "9999-does-not-exist", 5)
    assert seen["rid"] == "0"  # get_bilibili clamps an unknown rid before fetching


def test_weekly_adds_a_meta_item_with_the_subject(monkeypatch):
    _fresh_cache()
    monkeypatch.setattr(bilibili, "_fetch_weekly", lambda n: ([_video(1, "x")], "本期主题"))
    doc = bilibili.get_bilibili("weekly", "0", 5)
    assert doc["items"][0] == {"t": "meta", "a": "本期主题"}
    assert doc["items"][1]["t"] == "v"


def test_upstream_failure_is_an_error_item_not_invented_data(monkeypatch):
    _fresh_cache()

    def boom(n):
        raise bilibili.BilibiliError("风控：暂不可用")

    monkeypatch.setattr(bilibili, "_fetch_popular", boom)
    doc = bilibili.get_bilibili("popular", "0", 5)
    assert doc["items"] == [{"t": "error", "a": "风控：暂不可用"}]


def test_empty_upstream_list_is_also_an_error_item(monkeypatch):
    _fresh_cache()
    monkeypatch.setattr(bilibili, "_fetch_popular", lambda n: [])
    doc = bilibili.get_bilibili("popular", "0", 5)
    assert doc["items"][0]["t"] == "error"


def test_document_is_cached_between_calls(monkeypatch):
    _fresh_cache()
    calls = {"n": 0}

    def fake_popular(n):
        calls["n"] += 1
        return [_video(1, "x")]

    monkeypatch.setattr(bilibili, "_fetch_popular", fake_popular)
    bilibili.get_bilibili("popular", "0", 5)
    bilibili.get_bilibili("popular", "0", 5)
    assert calls["n"] == 1


def test_cache_key_is_specific_to_the_requested_count(monkeypatch):
    # two lists of the same category asking for a different number of videos (as the
    # multi-list app now does) must not serve each other's cached document
    _fresh_cache()
    calls = []

    def fake_popular(n):
        calls.append(n)
        return [_video(k, f"v{k}") for k in range(1, n + 1)]

    monkeypatch.setattr(bilibili, "_fetch_popular", fake_popular)
    doc5 = bilibili.get_bilibili("popular", "0", 5)
    doc10 = bilibili.get_bilibili("popular", "0", 10)
    assert calls == [5, 10]
    assert len([i for i in doc5["items"] if i["t"] == "v"]) == 5
    assert len([i for i in doc10["items"] if i["t"] == "v"]) == 10


def test_picture_rejects_an_id_the_app_never_named(monkeypatch):
    _fresh_cache()
    status, ctype, body = bilibili.picture("999999", 100, 60)
    assert status == 404


def test_picture_rejects_a_non_numeric_id():
    _fresh_cache()
    status, ctype, body = bilibili.picture("'; DROP TABLE x", 100, 60)
    assert status == 404


def _solid_jpeg_bytes(w=400, h=300):
    from PIL import Image
    im = Image.new("RGB", (w, h), (10, 120, 200))
    out = io.BytesIO()
    im.save(out, "JPEG")
    return out.getvalue()


def test_picture_crops_and_resizes_to_the_exact_box(monkeypatch):
    _fresh_cache()
    monkeypatch.setattr(bilibili, "_fetch_popular", lambda n: [_video(42, "封面")])
    bilibili.get_bilibili("popular", "0", 5)   # learns aid 42 -> its cover url

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _solid_jpeg_bytes()

    monkeypatch.setattr(bilibili.urllib.request, "urlopen", lambda req, timeout=6: FakeResponse())

    status, ctype, body = bilibili.picture("42", 120, 68)
    assert status == 200 and ctype == "image/jpeg"

    from PIL import Image
    im = Image.open(io.BytesIO(body))
    assert im.size == (120, 68)
    assert im.format == "JPEG"


def test_picture_is_cached_by_aid_and_box_size(monkeypatch):
    _fresh_cache()
    monkeypatch.setattr(bilibili, "_fetch_popular", lambda n: [_video(7, "封面")])
    bilibili.get_bilibili("popular", "0", 5)

    calls = {"n": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            calls["n"] += 1
            return _solid_jpeg_bytes()

    monkeypatch.setattr(bilibili.urllib.request, "urlopen", lambda req, timeout=6: FakeResponse())
    bilibili.picture("7", 100, 60)
    bilibili.picture("7", 100, 60)
    assert calls["n"] == 1   # the second call hits pic_cache, no re-fetch


def test_partitions_cover_the_documented_ranking_zones():
    names = dict(bilibili.PARTITIONS)
    for rid in ("0", "1", "4", "119", "181", "188", "211", "217", "223", "234"):
        assert rid in names


# ---------------------------------------------------------------------------
# UP主最新视频
# ---------------------------------------------------------------------------

def test_make_up_items_marks_only_todays_videos_new():
    vlist = [_up_video(1, "今天发的", days_ago=0), _up_video(2, "三天前发的", days_ago=3)]
    items = bilibili._make_up_items(vlist, 10)
    assert items[0]["a"] == "【新】今天发的"
    assert items[1]["a"] == "三天前发的"


def test_make_up_items_protocol_relative_pic_becomes_https():
    vlist = [_up_video(1, "x", pic="//i0.hdslb.com/bfs/archive/y.jpg")]
    bilibili._make_up_items(vlist, 10)
    assert bilibili._pic_url_for("1") == "https://i0.hdslb.com/bfs/archive/y.jpg"


def test_resolve_up_accepts_a_bare_uid(monkeypatch):
    monkeypatch.setattr(bilibili, "_fetch_card_name", lambda mid: "某UP主")
    mid, name = bilibili._resolve_up("  546195  ")
    assert mid == "546195" and name == "某UP主"


def test_resolve_up_accepts_a_space_link(monkeypatch):
    monkeypatch.setattr(bilibili, "_fetch_card_name", lambda mid: "某UP主")
    mid, name = bilibili._resolve_up("https://space.bilibili.com/546195/video?tab=upload")
    assert mid == "546195"


def test_resolve_up_searches_a_name(monkeypatch):
    monkeypatch.setattr(bilibili, "_search_user", lambda name: ("546195", "老番茄"))
    mid, name = bilibili._resolve_up("老番茄")
    assert (mid, name) == ("546195", "老番茄")


def test_resolve_up_rejects_empty_input():
    with pytest.raises(bilibili.BilibiliError, match="UP 主"):
        bilibili._resolve_up("   ")


def test_up_category_builds_a_meta_item_with_the_display_name(monkeypatch):
    _fresh_cache()
    monkeypatch.setattr(bilibili, "_resolve_up", lambda raw: ("546195", "老番茄"))
    monkeypatch.setattr(bilibili, "_fetch_up_videos", lambda mid, n: [_up_video(1, "新视频")])
    doc = bilibili.get_bilibili("up", "0", 10, "546195")
    assert doc["items"][0] == {"t": "meta", "a": "老番茄"}
    assert doc["items"][1]["a"] == "【新】新视频"
    assert doc["items"][1]["d"] == "老番茄"   # the UP主 line of every row


def test_up_category_with_no_videos_is_an_error_not_an_empty_screen(monkeypatch):
    _fresh_cache()
    monkeypatch.setattr(bilibili, "_resolve_up", lambda raw: ("1", "空号"))
    monkeypatch.setattr(bilibili, "_fetch_up_videos", lambda mid, n: [])
    doc = bilibili.get_bilibili("up", "0", 10, "1")
    assert doc["items"][0]["t"] == "error"


def test_up_category_upstream_failure_keeps_the_message_not_invented_data(monkeypatch):
    _fresh_cache()

    def boom(raw):
        raise bilibili.BilibiliError("UP 主投稿获取失败（风控 -352，请稍后重试）")

    monkeypatch.setattr(bilibili, "_resolve_up", boom)
    doc = bilibili.get_bilibili("up", "0", 10, "546195")
    assert doc["items"] == [{"t": "error", "a": "UP 主投稿获取失败（风控 -352，请稍后重试）"}]


def test_up_cache_key_is_specific_to_the_raw_up_value(monkeypatch):
    _fresh_cache()
    calls = []

    def fake_resolve(raw):
        calls.append(raw)
        return "1", "甲"

    monkeypatch.setattr(bilibili, "_resolve_up", fake_resolve)
    monkeypatch.setattr(bilibili, "_fetch_up_videos", lambda mid, n: [_up_video(1, "v")])
    bilibili.get_bilibili("up", "0", 10, "546195")
    bilibili.get_bilibili("up", "0", 10, "999999")
    assert calls == ["546195", "999999"]   # a different UP主 is never served from the other's cache


def test_up_app_api_is_on_unless_the_env_var_turns_it_off(monkeypatch):
    monkeypatch.delenv("MINISCHEME_BILIBILI_UP_APP_API", raising=False)
    assert bilibili.up_uses_app_api() is True
    for v in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv("MINISCHEME_BILIBILI_UP_APP_API", v)
        assert bilibili.up_uses_app_api() is True
    for v in ("0", "false", "no", "off"):
        monkeypatch.setenv("MINISCHEME_BILIBILI_UP_APP_API", v)
        assert bilibili.up_uses_app_api() is False


def test_fetch_up_videos_dispatches_on_the_switch(monkeypatch):
    calls = {"web": 0, "app": 0}
    monkeypatch.setattr(bilibili, "_fetch_up_videos_web", lambda mid, n: (calls.__setitem__("web", calls["web"] + 1), [])[1])
    monkeypatch.setattr(bilibili, "_fetch_up_videos_app", lambda mid, n: (calls.__setitem__("app", calls["app"] + 1), [])[1])

    monkeypatch.setenv("MINISCHEME_BILIBILI_UP_APP_API", "0")
    bilibili._fetch_up_videos("546195", 5)
    assert calls == {"web": 1, "app": 0}

    monkeypatch.delenv("MINISCHEME_BILIBILI_UP_APP_API", raising=False)
    bilibili._fetch_up_videos("546195", 5)
    assert calls == {"web": 1, "app": 1}


def test_fetch_up_videos_app_normalizes_to_the_same_shape_as_web(monkeypatch):
    def fake_app_json(url, timeout=6.0):
        return {"code": 0, "data": {"item": [
            {"param": "123", "title": "t", "cover": "//i0.hdslb.com/x.jpg", "play": 99, "ctime": 1000},
        ]}}

    monkeypatch.setattr(bilibili, "_get_app_json", fake_app_json)
    rows = bilibili._fetch_up_videos_app("546195", 5)
    assert rows == [{"aid": "123", "title": "t", "pic": "//i0.hdslb.com/x.jpg", "play": 99, "created": 1000}]


def test_items_carry_the_upload_day_in_beijing_time():
    # 2026-10-24 23:30 Beijing time is still the 24th there (15:30 UTC)
    ts = int(datetime(2026, 10, 24, 15, 30, tzinfo=bilibili.timezone.utc).timestamp())
    day = str((datetime(2026, 10, 24) - datetime(1970, 1, 1)).days)
    v = dict(_video(1, "A"), pubdate=ts)
    assert bilibili._make_items([v], 1)[0]["e"] == day
    assert bilibili._make_items([_video(2, "B")], 1)[0]["e"] == ""      # unknown: no age shown
    up = bilibili._make_up_items([_up_video(3, "C", days_ago=3)], 1)[0]["e"]
    today = (datetime.now(bilibili.BJT).date() - datetime(1970, 1, 1).date()).days
    assert int(up) == today - 3
