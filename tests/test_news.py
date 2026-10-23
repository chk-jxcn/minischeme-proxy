"""proxy/news.py: the hot lists of the news app, without network."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxy import news  # noqa: E402

BAIDU_REALTIME = {"data": {"cards": [{"content": [{"content": [
    {"isTop": True, "word": "置顶的话题"},
    {"index": 1, "word": "第一条\n热搜", "hotTag": "3"},
    {"index": 2, "word": "第二条 😀 與 한국", "hotTag": "0"},
    {"index": 3, "query": "第三条", "hotTag": "1"},
]}]}]}}
BAIDU_FLAT = {"data": {"cards": [{"content": [
    {"index": 1, "word": "民生一", "hotTag": "5"},
    {"index": 2, "word": ""},
]}]}}
ZHIHU = {"data": [{"target": {"title": "知乎问题一？"}}, {"target": {}}, {"target": {"title": "问题二"}}]}
TOUTIAO = {"data": [{"Title": "头条一", "Label": "new"}, {"Title": "头条二", "Label": "onSite"}]}


class NewsTest(unittest.TestCase):
    def setUp(self):
        news._cache.clear()

    def test_baidu(self):
        self.assertEqual(news.parse_baidu(BAIDU_REALTIME),
                         [("第一条 热搜", "热"), ("第二条 😀 與 한국", ""), ("第三条", "新")])
        self.assertEqual(news.parse_baidu(BAIDU_FLAT), [("民生一", "爆")])
        with self.assertRaises(news.NewsError):
            news.parse_baidu({"data": {}})

    def test_zhihu_toutiao(self):
        self.assertEqual(news.parse_zhihu(ZHIHU), [("知乎问题一？", ""), ("问题二", "")])
        self.assertEqual(news.parse_toutiao(TOUTIAO), [("头条一", "新"), ("头条二", "")])

    def test_document_ranks_cache_and_failure(self):
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) > 1:
                raise OSError("down")
            return news.parse_baidu(BAIDU_REALTIME)

        saved = news.SOURCES["finance"]
        news.SOURCES["finance"] = ("财经榜", fetch)
        try:
            doc = news.get_news("finance", 2)
            rows = [it for it in doc["items"] if it["t"] == "item"]
            self.assertEqual([(it["a"], it["c"], it["d"]) for it in rows], [("第一条 热搜", "1", "热"), ("第二条 😀 與 한국", "2", "")])
            self.assertEqual(doc["items"][-1]["t"], "upd")
            news.get_news("finance", 2)            # cached: no second fetch
            self.assertEqual(len(calls), 1)
            news._cache["finance"] = (0, news._cache["finance"][1])   # expired
            doc = news.get_news("finance", 2)      # the source fails: the last list and an error
            self.assertEqual(len([it for it in doc["items"] if it["t"] == "item"]), 2)
            self.assertEqual(doc["items"][-1], {"t": "error", "a": "财经榜: OSError"})
            news._cache.clear()
            self.assertEqual(news.get_news("finance", 2)["items"], [{"t": "error", "a": "财经榜: OSError"}])
        finally:
            news.SOURCES["finance"] = saved

    def test_unknown_category_is_realtime(self):
        saved = news.SOURCES["realtime"]
        news.SOURCES["realtime"] = ("百度热搜", lambda: [("a", "")])
        try:
            self.assertEqual(news.get_news("nosuch", 5)["items"][0]["a"], "a")
        finally:
            news.SOURCES["realtime"] = saved


if __name__ == "__main__":
    unittest.main()
