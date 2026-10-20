"""The WxPusher receiver's board document, the notifier and the QR encoder
(no network: no receiver thread is started)."""
import json

from proxy import qr, wxpusher as wx
from proxy.endpoints import handle_app_query


def test_qr_matches_a_known_symbol():
    # "HELLO" at level M: version 1, 21 modules; finder patterns in three corners
    m = qr.qr_matrix("HELLO", "M")
    assert len(m) == 21 and all(len(r) == 21 for r in m)
    for x0, y0 in ((0, 0), (14, 0), (0, 14)):
        assert [m[y0][x0 + i] for i in range(7)] == [1] * 7
        assert [m[y0 + 1][x0 + i] for i in range(7)] == [1, 0, 0, 0, 0, 0, 1]
    assert qr.qr_hex([1, 0, 1, 1, 0]) == "b0"
    # the WeChat login code: version 5 at level Q
    assert len(qr.qr_matrix("http://weixin.qq.com/q/02U2gIUDIQcYj1OS0nNGcK", "Q", min_version=5)) == 37


def test_ticket_carries_the_qr_text():
    import base64
    ticket = base64.urlsafe_b64encode(b"\x08\x01gQhttp://weixin.qq.com/q/abc\x10\x00").decode().rstrip("=")
    assert wx.qr_text_of_ticket(ticket) == "http://weixin.qq.com/q/abc"
    assert wx.qr_text_of_ticket("!!") == ""


def test_receiver_document(tmp_path):
    rx = wx.WxReceiver(str(tmp_path / "rx.json"))
    rx.state, rx.qr_rows, rx.qr_expires = "login", [[1, 0, 1], [0, 1, 0], [1, 1, 1]], 2e9
    items = rx.items()
    assert items[0]["t"] == "state" and items[0]["c"] == "3"
    assert items[1] == {"t": "qr", "a": "0", "b": "a,4,e"}
    rx.state = "ok"
    rx.add_message(7, "<b>标题</b>", "**正文** [链接](http://x)", 1_700_000_000_000, name="来源")
    items = rx.items()
    assert len(items) == 2 and items[1]["b"] == "标题" and items[1]["d"] == "正文 链接" and items[1]["topic"] == "来源"
    rx.delete(["7"])
    assert len(rx.items()) == 1
    rx.add_message(7, "again", "", 0)                 # deleted on a board: never back
    assert len(rx.items()) == 1
    assert json.load(open(tmp_path / "rx.json"))["del_pending"] == [7]


def test_notifier_local_and_public():
    st, _, body = wx.handle_request("/notify?title=T&text=hello&level=warn&from=t")
    assert st == 200 and json.loads(body)["success"]
    items = json.loads(handle_app_query("/app/notifier?n=3")[2])["items"]
    assert items[0]["t"] == "meta" and items[1]["c"] == "hello" and items[1]["d"] == "warn"
    # a public server: to= and the portal session of that device
    assert wx.handle_request("/notify?text=x", public=True)[0] == 400
    assert wx.handle_request("/notify?text=x&to=AABBCCDDEEFF", public=True)[0] == 401
    ok = wx.handle_request("/notify?to=aa:bb:cc:dd:ee:ff", b'{"text": "mine"}', public=True,
                           authorized=lambda mac: mac == "AABBCCDDEEFF")
    assert ok[0] == 200
    mine = json.loads(handle_app_query("/app/notifier", device="AABBCCDDEEFF", public=True)[2])["items"]
    assert [x["c"] for x in mine if x["t"] == "msg"] == ["mine"]
    other = json.loads(handle_app_query("/app/notifier", device="112233445566", public=True)[2])["items"]
    assert [x for x in other if x["t"] == "msg"] == []
    # plaintext on a public server: no device, no messages
    assert json.loads(handle_app_query("/app/wxpusher", public=True)[2])["items"][0]["t"] == "error"
    assert wx.handle_request("/wxpusher/send", b'{"content": "x"}', public=True)[0] == 401
    assert wx.handle_request("/wxpusher/logout", public=True)[0] == 404
