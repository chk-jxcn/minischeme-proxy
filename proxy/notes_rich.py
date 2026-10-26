"""Rich notifier messages: a light markup, encoded for the board, and their pictures.

The markup (what /notify and the user portal's editor take):

    # 标题                    a heading line
    ## 大字 / ### 大字         a line of text twice / three times as large
    **强调**                  emphasis (the accent color)
    {red}文字{/}              a color: red green yellow blue gray (红 绿 黄 蓝 灰)
    - 项目                    a list item ("* " and "• " too)
    ---                       a divider
    ![说明](https://...)      a picture on its own line (or ![](id:<id>), an uploaded one)
    an empty line             a paragraph gap

A message using any of it is stored and sent to the board encoded (MARK first):
lines joined by "^/"; a line starts with "^#" (heading), "^2" / "^3" (text twice /
three times as large), "^-" (list item), "^=" (divider) or "^i<id>:<width>x<height>;" (picture), else it is a paragraph; inside a
line "^a" emphasis, "^r" "^g" "^y" "^b" "^m" a color and "^n" the normal color
switch the color of the text that follows; "^^" is a "^". The board (notifier.scm)
and the user portal lay it out. Other messages stay plain text.

Pictures: fetched once from the internet (never this network) or uploaded on the
portal, kept by content (id = the first 16 hex digits of their SHA-1) in
notes_images/ next to the WxPusher logins; /app/notifier/pic?id=&w=&h= answers a
baseline JPEG of exactly w x h (center-cropped) for the board.
"""

import hashlib
import io
import os
import re
from typing import Callable, Optional, Tuple

MARK = '^!'
COLORS = {'red': 'r', '红': 'r', 'green': 'g', '绿': 'g', 'yellow': 'y', '黄': 'y', 'blue': 'b', '蓝': 'b',
          'gray': 'm', 'grey': 'm', '灰': 'm'}
IMAGE_MAX_BYTES = 6 * 1024 * 1024
IMAGE_MAX_SIDE = 8000
_IMAGE_LINE = re.compile(r'^\s*!\[[^\]]*\]\(\s*([^)\s]+)\s*\)\s*$')
_INLINE = re.compile(r'\*\*|\{/\}|\{([a-z]+|[红绿黄蓝灰])\}')
_IMAGE_ANY = re.compile(r'!\[[^\]]*\]\(\s*[^)\s]+\s*\)')
_MARKUP_HINT = re.compile(r'\n|\*\*|\{/\}|\{([a-z]+|[红绿黄蓝灰])\}|^\s*(#|- |\* |• |---)|!\[[^\]]*\]\(', re.M)


def _picture_lines(text: str) -> list:
    """The lines of text, a picture inside a line on a line of its own there (the text
    before it, the picture, the text after it): the board draws pictures between lines."""
    out = []
    for line in text.split('\n'):
        pos = 0
        for m in _IMAGE_ANY.finditer(line):
            if line[pos:m.start()].strip():
                out.append(line[pos:m.start()].rstrip())
            out.append(m.group(0))
            pos = m.end()
        if pos == 0 or line[pos:].strip():
            out.append(line[pos:].strip() if pos else line)
    return out


def has_markup(text: str) -> bool:
    return bool(_MARKUP_HINT.search(text or ''))


def _inline(s: str) -> str:
    """One line's text with its emphasis and colors as switches."""
    out, pos, color, strong = [], 0, 'n', False
    for m in _INLINE.finditer(s):
        out.append(s[pos:m.start()].replace('^', '^^'))
        tok = m.group(0)
        if tok == '**':
            strong = not strong
        elif tok == '{/}':
            color = 'n'
        elif m.group(1) in COLORS:
            color = COLORS[m.group(1)]
        else:
            out.append(tok.replace('^', '^^'))     # not one of ours: the text as written
            pos = m.end()
            continue
        out.append('^' + ('a' if strong else color))
        pos = m.end()
    out.append(s[pos:].replace('^', '^^'))
    return ''.join(out)


def encode(text: str, picture: Optional[Callable[[str], Optional[Tuple[str, int, int]]]] = None) -> str:
    """The stored form of a message: the board's encoding when it uses markup, else the text.
    picture(src) -> (id, width, height) of a picture, or None (shown as a note)."""
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    if not has_markup(text):
        return (' ' + text) if text.startswith(MARK) else text
    lines = []
    for raw in _picture_lines(text):
        s = raw.rstrip()
        bare = s.strip()
        img = _IMAGE_LINE.match(s)
        if bare in ('---', '***', '___'):
            lines.append('^=')
        elif img:
            got = None
            if picture is not None:
                try:
                    got = picture(img.group(1))
                except Exception:   # noqa: BLE001 - an unreachable picture is a note in the text
                    got = None
            lines.append(f'^i{got[0]}:{got[1]}x{got[2]};' if got else '^m' + _inline('[图片无法载入]'))
        elif bare.startswith('#'):
            level = min(3, len(bare) - len(bare.lstrip('#')))
            lines.append(('^#', '^2', '^3')[level - 1] + _inline(bare.lstrip('#').strip()))
        elif bare.startswith(('- ', '* ', '• ')):
            lines.append('^-' + _inline(bare[2:].strip()))
        else:
            lines.append(_inline(s))
    return MARK + '^/'.join(lines)


def plain(stored: str) -> str:
    """The text of a stored message without its encoding (WxPusher, one-line lists)."""
    if not (stored or '').startswith(MARK):
        return stored or ''
    out = []
    for line in stored[len(MARK):].split('^/'):
        if line.startswith('^='):
            continue
        if line.startswith('^i'):
            out.append('[图片]')
            continue
        if line[:2] in ('^#', '^2', '^3', '^-'):
            line = ('• ' if line[1] == '-' else '') + line[2:]
        out.append(re.sub(r'\^([\^anrgybm])', lambda m: '^' if m.group(1) == '^' else '', line))
    return '\n'.join(x for x in out if x is not None)


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

def image_dir() -> str:
    from . import wxpusher
    path = os.path.join(os.path.dirname(wxpusher._state_dir), 'notes_images')
    os.makedirs(path, exist_ok=True)
    return path


def _check_image(raw: bytes) -> Tuple[int, int]:
    from PIL import Image
    if not raw or len(raw) > IMAGE_MAX_BYTES:
        raise ValueError('picture empty or larger than 6 MB')
    im = Image.open(io.BytesIO(raw))
    w, h = im.size
    if w < 1 or h < 1 or w > IMAGE_MAX_SIDE or h > IMAGE_MAX_SIDE:
        raise ValueError('picture size out of range')
    im.verify()
    return w, h


def store_image(raw: bytes) -> Tuple[str, int, int]:
    """(id, width, height) of a picture, kept by its content."""
    w, h = _check_image(raw)
    pid = hashlib.sha1(raw).hexdigest()[:16]
    path = os.path.join(image_dir(), pid + '.img')
    if not os.path.exists(path):
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(raw)
        os.replace(tmp, path)
    return pid, w, h


def image_size(pid: str) -> Optional[Tuple[int, int]]:
    from PIL import Image
    if not re.fullmatch(r'[0-9a-f]{16}', pid or ''):
        return None
    path = os.path.join(image_dir(), pid + '.img')
    if not os.path.exists(path):
        return None
    with Image.open(path) as im:
        return im.size


def resolve_picture(src: str, allow_private: bool = False) -> Optional[Tuple[str, int, int]]:
    """A markup picture: id:<id> (uploaded) or an http(s) URL fetched now."""
    src = (src or '').strip()
    if src.startswith('id:'):
        pid = src[3:]
        size = image_size(pid)
        return (pid, size[0], size[1]) if size else None
    if not src.startswith(('http://', 'https://')):
        return None
    from .security import guarded_fetch
    _, raw = guarded_fetch(src, allow_private=allow_private, max_bytes=IMAGE_MAX_BYTES, timeout=10.0)
    return store_image(raw)


def picture(pid: str, w: int, h: int) -> Tuple[int, str, bytes]:
    """(status, content type, body) of /app/notifier/pic?id=&w=&h=."""
    if not re.fullmatch(r'[0-9a-f]{16}', pid or ''):
        return 400, 'text/plain', b'bad id'
    w, h = max(16, min(int(w), 480)), max(16, min(int(h), 480))
    path = os.path.join(image_dir(), pid + '.img')
    if not os.path.exists(path):
        return 404, 'text/plain', b'no such picture'
    from .bilibili import board_cover_jpeg
    with open(path, 'rb') as f:
        return 200, 'image/jpeg', board_cover_jpeg(f.read(), w, h)
