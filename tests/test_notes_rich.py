"""Rich notifier messages (proxy/notes_rich.py): the markup, its encoding for the board,
the pictures (stored by content, resized for the board) and POST /notify/image."""
import io
import json

import pytest

from proxy import notes_rich as rich
from proxy import wxpusher


def _png(w=64, h=48, color=(200, 30, 30)):
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (w, h), color).save(out, 'PNG')
    return out.getvalue()


@pytest.fixture(autouse=True)
def _images_here(tmp_path, monkeypatch):
    monkeypatch.setattr(wxpusher, '_state_dir', str(tmp_path / 'wxpusher'))


def test_plain_text_stays_plain():
    assert rich.encode('just a line') == 'just a line'
    assert rich.encode('^!looks like the mark') == ' ^!looks like the mark'
    assert rich.plain('just a line') == 'just a line'


def test_markup_is_encoded_line_by_line():
    e = rich.encode('# 标题\n**重点** 和 {red}红字{/} a^b\n- 项目\n---\n\n第二段')
    assert e.startswith(rich.MARK)
    assert e[len(rich.MARK):].split('^/') == ['^#标题', '^a重点^n 和 ^r红字^n a^^b', '^-项目', '^=', '', '第二段']


def test_picture_inside_a_line_gets_its_own_line():
    pic = lambda src: ('0123456789abcdef', 64, 48)
    e = rich.encode('看这张 ![](https://example.invalid/a.jpg) 好看吧', pic)
    assert e[len(rich.MARK):].split('^/') == ['看这张', '^i0123456789abcdef:64x48;', '好看吧']
    e = rich.encode('前面\n![](id:0123456789abcdef)\n\n后面', pic)
    assert e[len(rich.MARK):].split('^/') == ['前面', '^i0123456789abcdef:64x48;', '', '后面']


def test_large_text_lines():
    e = rich.encode('## 大字\n### {red}更大{/}\n#### 最大三倍')
    assert e[len(rich.MARK):].split('^/') == ['^2大字', '^3^r更大^n', '^3最大三倍']
    assert rich.plain(e) == '大字\n更大\n最大三倍'


def test_colors_have_chinese_names_and_unknown_braces_stay():
    assert rich.encode('{绿}好{/}\n{x}y') == rich.MARK + '^g好^n^/{x}y'


def test_plain_of_encoded():
    e = rich.encode('# 标题\n**重点** a^b\n- 项目\n---\n![](id:0123456789abcdef)', lambda s: ('0123456789abcdef', 4, 3))
    assert rich.plain(e) == '标题\n重点 a^b\n• 项目\n[图片]'


def test_picture_lines_resolved_or_noted():
    e = rich.encode('![x](https://example.invalid/p.jpg)', lambda s: ('0123456789abcdef', 640, 480))
    assert e == rich.MARK + '^i0123456789abcdef:640x480;'
    def fail(src):
        raise OSError('unreachable')
    assert '图片无法载入' in rich.encode('![x](https://example.invalid/p.jpg)', fail)


def test_store_and_board_picture():
    pid, w, h = rich.store_image(_png(64, 48))
    assert (w, h) == (64, 48) and len(pid) == 16
    assert rich.store_image(_png(64, 48))[0] == pid                    # by content
    assert rich.resolve_picture('id:' + pid) == (pid, 64, 48)
    status, ctype, body = rich.picture(pid, 40, 20)
    from PIL import Image
    im = Image.open(io.BytesIO(body))
    assert status == 200 and ctype == 'image/jpeg' and im.size == (40, 20) and im.format == 'JPEG'
    assert rich.picture('nothex', 10, 10)[0] == 400
    assert rich.picture('ffffffffffffffff', 10, 10)[0] == 404


def test_not_a_picture_is_refused():
    with pytest.raises(Exception):
        rich.store_image(b'not an image')


def test_notify_image_endpoint_local():
    status, _, body = wxpusher.handle_request('/notify/image', _png(), public=False)
    d = json.loads(body)
    assert status == 200 and d['success'] and d['width'] == 64
    status, _, body = wxpusher.handle_request('/notify/image', b'junk', public=False)
    assert status == 400


def test_notify_image_endpoint_public_needs_rights():
    status, _, _ = wxpusher.handle_request('/notify/image?mac=AABBCCDDEEFF', _png(), public=True,
                                           authorized=lambda mac: False)
    assert status == 401
    status, _, body = wxpusher.handle_request('/notify/image?mac=AABBCCDDEEFF', _png(), public=True,
                                              authorized=lambda mac: mac == 'AABBCCDDEEFF')
    assert status == 200 and json.loads(body)['success']


def test_notify_with_markup_is_kept_encoded():
    wxpusher._notes.clear()
    pid = rich.store_image(_png())[0]
    out = wxpusher.notify_push({'text': f'**hi**\n![](id:{pid})', 'title': 't'}, 'local')
    assert out['success']
    items = wxpusher.app_notifier({'n': '5'}, 'local')
    msg = [i for i in items if i['t'] == 'msg'][-1]
    assert msg['c'] == rich.MARK + f'^ahi^n^/^i{pid}:64x48;'
