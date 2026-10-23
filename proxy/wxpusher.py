"""WxPusher receiver (/app/wxpusher), notifier (/app/notifier, /notify) and
WxPusher sender (/wxpusher/send, /notify wx=1), ported from the original
single-file app proxy.

The receiver logs in to WxPusher as a desktop client of the user (a login
QR code scanned with WeChat) and keeps the messages pushed to them. Each
device has its own receiver and login: the encrypted channel's device id
names it (a local server's plaintext requests share "local"; a public
server answers plaintext requests without one). The login QR code is
drawn by the proxy itself (proxy/qr.py) from the content WeChat's ticket
carries, so the board shows it with gui-bitmap and no image library is
needed.
"""

import base64
import datetime as dt
import html as htmlmod
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .qr import qr_hex, qr_matrix

WX_BASE = 'https://wxpusher.zjiecode.com'
WX_WS = 'wss://wxpusher.zjiecode.com/ws'
WX_CLIENT_VERSION = '1.1.3'          # the desktop client this speaks as
WX_PLATFORM = 'desktop_win'
WX_DEVICE_NAME = '桌面屏'
WX_KEEP = 40                         # messages kept
WX_DOC_MESSAGES = 8                  # messages per /app/wxpusher document
WX_TEXT_BYTES = 1500                 # of a message's text (the board shows a screenful)
WX_DELETED_KEEP = 500                # ids deleted on a board, remembered so history never brings them back
WX_DELETE_CHUNK = 200                # ids per delete request (as the desktop client)
WX_QR_IDLE = 120                     # seconds after the board's last request: no new login QR codes
WX_HEARTBEAT = 25                    # seconds between the application heartbeats (msgType 101)
WX_READ_TIMEOUT = 90                 # seconds without a frame: reconnect
UA = 'Mozilla/5.0 (MiniScheme proxy)'

_state_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wxpusher')


def set_state_dir(path: str):
    """Where the receivers keep their login (one JSON file per device)."""
    global _state_dir
    _state_dir = path


class ProxyError(Exception):
    pass


class WxError(ProxyError):
    def __init__(self, code, msg):
        super().__init__(f'WxPusher: {msg} ({code})')
        self.code = code


def clip(text: Any, n: Optional[int] = None) -> str:
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    if n is not None and len(text) > n:
        text = text[:n - 1] + '…'
    return text


def hhmm() -> str:
    return dt.datetime.now().strftime('%H:%M')


def wx_text(content: str) -> str:
    """Message text for the board: HTML tags and markdown marks dropped,
    entities decoded, whitespace folded."""
    t = re.sub(r'<(br|/p|/div|/li)\s*/?>', ' ', content or '', flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    t = htmlmod.unescape(t)
    t = re.sub(r'(^|\s)#{1,6}\s+', r'\1', t)              # markdown headings
    t = re.sub(r'\*{1,3}|`+|~~', '', t)                  # emphasis and code marks (not "_": snake_case)
    t = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', t)      # links and images: their text
    return re.sub(r'\s+', ' ', t).strip()


def utf8_parts(text: str, size: int, count: int) -> List[str]:
    """text cut into at most count parts of at most size UTF-8 bytes (never
    inside a character); the last part ends with "…" when text was longer."""
    data = text.encode('utf-8')
    parts = []
    while data and len(parts) < count:
        n = min(size, len(data))
        while n < len(data) and (data[n] & 0xC0) == 0x80:
            n -= 1
        parts.append(data[:n].decode('utf-8'))
        data = data[n:]
    if data and parts:
        last = parts[-1].encode('utf-8')
        while len(last) + 3 > size:
            last = last[:-1]
            while last and (last[-1] & 0xC0) == 0x80:
                last = last[:-1]
            if last and last[-1] >= 0xC0:
                last = last[:-1]
        parts[-1] = last.decode('utf-8', 'ignore') + '…'
    return parts


def qr_text_of_ticket(ticket: str) -> str:
    """The text a WeChat QR ticket carries (http://weixin.qq.com/q/...), '' if none."""
    try:
        raw = base64.urlsafe_b64decode(ticket + '=' * (-len(ticket) % 4))
    except ValueError:
        return ''
    m = re.search(rb'https?://[\x21-\x7e]+', raw)
    return m.group(0).decode('ascii') if m else ''


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def wx_qr_rows(code: str) -> List[List[int]]:
    """The modules of a login code's QR code: the WeChat ticket the QR image
    URL redirects to carries its content, encoded here again."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(f'{WX_BASE}/api/qrcode/{code}.jpg', headers={'User-Agent': UA}), timeout=15)
        raise ProxyError('WxPusher: the QR code did not redirect to WeChat')
    except urllib.error.HTTPError as e:
        location = e.headers.get('Location', '') if e.code in (301, 302, 303, 307, 308) else ''
    except OSError as e:
        raise ProxyError(f'WxPusher: {e}')
    ticket = (urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get('ticket') or [''])[0]
    text = qr_text_of_ticket(ticket)
    if not text:
        raise ProxyError('WxPusher: no QR content in the WeChat ticket')
    return qr_matrix(text, 'Q', min_version=5)   # as WeChat draws it (version 5, level Q)


class WsClient:
    """A small WebSocket client (RFC 6455) on the standard library: text
    frames out (masked), text frames in, ping answered, fragments joined."""

    def __init__(self, url, headers=None, timeout=15):
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == 'wss' else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == 'wss':
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        self.sock.settimeout(timeout)
        self.timeout = timeout
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or '/') + ('?' + u.query if u.query else '')
        lines = [f'GET {path} HTTP/1.1', f'Host: {u.hostname}', 'Upgrade: websocket', 'Connection: Upgrade',
                 f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13']
        lines += [f'{k}: {v}' for k, v in (headers or {}).items()]
        self.sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8'))
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ProxyError('websocket: closed during the handshake')
            buf += chunk
            if len(buf) > 16384:
                raise ProxyError('websocket: handshake too long')
        head, self.buf = buf.split(b'\r\n\r\n', 1)
        status = head.split(b'\r\n', 1)[0].decode('latin-1')
        if ' 101' not in status:
            raise ProxyError(f'websocket: {status}')

    def _send(self, opcode, payload):
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += n.to_bytes(2, 'big')
        else:
            head.append(0x80 | 127)
            head += n.to_bytes(8, 'big')
        mask = os.urandom(4)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send_text(self, text):
        self._send(0x1, text.encode('utf-8'))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError('websocket closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, wait=1.0):
        """The next text message, or None when nothing came within wait seconds."""
        message = b''
        while True:
            if not self.buf:
                self.sock.settimeout(wait)
                try:
                    chunk = self.sock.recv(4096)
                except (socket.timeout, TimeoutError):
                    return None
                finally:
                    self.sock.settimeout(self.timeout)
                if not chunk:
                    raise ConnectionError('websocket closed')
                self.buf += chunk
            b0, b1 = self._read(2)
            n = b1 & 0x7f
            if n == 126:
                n = int.from_bytes(self._read(2), 'big')
            elif n == 127:
                n = int.from_bytes(self._read(8), 'big')
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            opcode = b0 & 0x0f
            if opcode == 0x9:                       # ping
                self._send(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode == 0x8:
                raise ConnectionError('websocket closed by the server')
            message += payload
            if b0 & 0x80:                           # the last fragment
                return message.decode('utf-8', 'replace')

    def close(self):
        try:
            self._send(0x8, b'')
            self.sock.close()
        except OSError:
            pass


class WxReceiver:
    """One device's WxPusher client; run() is its thread."""

    def __init__(self, state_file):
        self.state_file = state_file
        self.lock = threading.Lock()
        self.state = 'starting'          # starting, login, ok, error
        self.info = ''
        self.qr_code = ''
        self.qr_rows = None
        self.qr_expires = 0.0
        self.messages = {}               # id -> {id, title, content, time, read, name}
        self.history_loaded = False
        self.thread = None
        self.last_seen = 0.0             # the board's last /app/wxpusher request
        self.ws = None
        try:
            with open(self.state_file, encoding='utf-8') as f:
                self.cred = json.load(f)
        except (OSError, ValueError):
            self.cred = {}
        # deleted on a board: never shown again; del_pending still to delete on the service
        self.deleted = set(self.cred.get('deleted', []))
        self.del_pending = list(self.cred.get('del_pending', []))

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.cred, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self.run, name='wxpusher', daemon=True)
            self.thread.start()

    # ---- the service's HTTP API -------------------------------------------------------
    def api(self, method, path, body=None):
        headers = {'Content-Type': 'application/json;charset=utf-8', 'version': WX_CLIENT_VERSION,
                   'platform': WX_PLATFORM, 'User-Agent': 'WxPusher-Desktop'}
        if self.cred.get('deviceToken'):
            headers['deviceToken'] = self.cred['deviceToken']
        data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        req = urllib.request.Request(WX_BASE + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                result = json.loads(r.read().decode('utf-8', 'replace'))
        except (OSError, ValueError) as e:
            raise ProxyError(f'WxPusher: {e}')
        code = result.get('code')
        if code == 1000:
            return result.get('data')
        if code == 1002:                         # the login expired: scan again
            self.logout_local('登录已过期，请重新扫码')
        raise WxError(code, result.get('msg') or 'error')

    def logout_local(self, info=''):
        with self.lock:
            self.cred.pop('deviceToken', None)
            self.save()
            self.state, self.info = 'login', info
            self.qr_code, self.qr_rows, self.qr_expires = '', None, 0
            self.history_loaded = False

    def logout(self):
        """The board's 退出登录: this receiver leaves the user's WxPusher devices."""
        if self.cred.get('deviceToken'):
            try:
                self.api('POST', '/api/need-login/device/logout')
            except ProxyError:
                pass
        with self.lock:
            self.messages.clear()
        self.logout_local('已退出登录')

    # ---- what the service pushes ---------------------------------------------------------
    def add_message(self, mid, title, content, when_ms, read=False, name=''):
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            return
        with self.lock:
            if mid in self.deleted or (mid in self.messages and self.messages[mid].get('content')):
                return
            self.messages[mid] = {'id': mid, 'title': wx_text(title) or '（无标题）', 'read': bool(read),
                                  'name': wx_text(name), 'content': wx_text(content),
                                  'time': (when_ms or time.time() * 1000) / 1000}
            for old in sorted(self.messages)[:-WX_KEEP]:
                del self.messages[old]

    def handle(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        t = msg.get('msgType')
        if t == 202 and msg.get('pushToken'):                 # DEVICE_INIT
            with self.lock:
                self.cred['pushToken'] = msg['pushToken']
                self.save()
            if self.cred.get('deviceToken'):
                self.report_push_token()
        elif t == 20001:                                      # PUSH_NOTE
            self.add_message(msg.get('mid'), msg.get('summary') or msg.get('content') or msg.get('title'),
                             msg.get('content') or msg.get('summary') or '', msg.get('createTime'),
                             name=msg.get('name') or '')
        elif t in (203, 204):                                 # ERROR_MSG, UPDATE_CLIENT
            with self.lock:
                self.info = str(msg.get('msg') or ('the service wants a newer client' if t == 204 else ''))

    def delete(self, ids):
        """Messages a board deleted: gone from here, never loaded again, and
        deleted on the service (flush_deletes, from the receiver thread)."""
        ids = [int(i) for i in ids if str(i).strip().isdigit()]
        if not ids:
            return
        with self.lock:
            for mid in ids:
                self.messages.pop(mid, None)
                self.deleted.add(mid)
            self.del_pending = sorted(set(self.del_pending) | set(ids))
            keep = sorted(self.deleted)[-WX_DELETED_KEEP:]
            self.deleted = set(keep)
            self.cred['deleted'] = keep
            self.cred['del_pending'] = self.del_pending
            self.save()

    def flush_deletes(self):
        with self.lock:
            chunk = self.del_pending[:WX_DELETE_CHUNK]
        if not chunk or not self.cred.get('deviceToken'):
            return
        try:
            self.api('DELETE', '/api/need-login/device/message/delete?messageIds=' + ','.join(map(str, chunk)))
        except WxError as e:
            if e.code == 1002:          # the login expired: after the next login
                return
        except ProxyError:
            return
        with self.lock:
            self.del_pending = [i for i in self.del_pending if i not in set(chunk)]
            self.cred['del_pending'] = self.del_pending
            self.save()

    def report_push_token(self):
        try:
            self.api('PUT', '/api/need-login/device/update-device-info',
                     {'pushToken': self.cred['pushToken'], 'deviceUuid': self.cred.get('deviceUuid'),
                      'platform': WX_PLATFORM})
        except WxError as e:
            if e.code == 1001:                                # this device is gone: log in again
                self.logout_local('设备已注销，请重新扫码')
        except ProxyError:
            pass

    # ---- login by QR code -------------------------------------------------------------------
    def login_step(self):
        """One round of the login: a QR code while there is none (or it expired),
        then one try to register this device (the user may have scanned it)."""
        if not self.cred.get('pushToken'):
            return                                         # the WebSocket gives it first
        now = time.time()
        if not self.qr_code or now > self.qr_expires:
            if now - self.last_seen > WX_QR_IDLE:          # nobody looks at it: no new codes
                with self.lock:
                    self.state, self.qr_code, self.qr_rows = 'login', '', None
                return
            data = self.api('GET', '/api/device/create-login-qrcode')
            rows, info = None, ''
            try:
                rows = wx_qr_rows(data['code'])
            except ProxyError as e:
                info = f'无法显示二维码：{e}'
            with self.lock:
                self.qr_code, self.qr_rows = data['code'], rows
                self.qr_expires = (data.get('expires') or (now + 300) * 1000) / 1000 - 5
                self.state, self.info = 'login', info
        body = {'code': self.qr_code, 'deviceName': WX_DEVICE_NAME, 'platform': WX_PLATFORM,
                'pushToken': self.cred['pushToken']}
        if self.cred.get('deviceUuid'):
            body['deviceUuid'] = self.cred['deviceUuid']
        try:
            data = self.api('POST', '/api/device/register-device', body)
        except WxError as e:
            if e.code != 10000:                            # 10000: not scanned yet
                with self.lock:
                    self.qr_code = ''                      # a new one next round
            return
        if data and data.get('deviceToken'):
            with self.lock:
                self.cred['deviceToken'] = data['deviceToken']
                self.cred['deviceUuid'] = data.get('deviceUuid') or self.cred.get('deviceUuid')
                self.save()
                self.state, self.info = 'ok', ''
                self.qr_code, self.qr_rows = '', None
            self.report_push_token()

    def load_history(self):
        rows = self.api('GET', f'/api/need-login/device/message/list-v2?messageId={2 ** 53 - 1}&key=&scene=1') or []
        for m in rows:
            self.add_message(m.get('messageId'), m.get('summary') or m.get('name'), m.get('summary') or '',
                             m.get('createTime'), m.get('read'), name=m.get('name') or '')
        self.history_loaded = True

    # ---- the connection -------------------------------------------------------------------
    def session(self):
        url = f'{WX_WS}?version={WX_CLIENT_VERSION}&platform={WX_PLATFORM}'
        if self.cred.get('pushToken'):
            url += '&pushToken=' + urllib.parse.quote(self.cred['pushToken'])
        self.ws = WsClient(url, {'User-Agent': 'WxPusher-Desktop', 'Origin': WX_BASE,
                                 'version': WX_CLIENT_VERSION, 'platform': WX_PLATFORM})
        self.history_loaded = False                         # catch up with what came while away
        with self.lock:
            if self.state in ('starting', 'error'):
                self.state = 'ok' if self.cred.get('deviceToken') else 'login'
                self.info = ''
        heart = last_frame = last_step = time.time()
        while True:
            raw = self.ws.recv(1.0)
            now = time.time()
            if raw is not None:
                last_frame = now
                self.handle(raw)
            if now - last_frame > WX_READ_TIMEOUT:
                raise ConnectionError('no frame for 90 s')
            if now - heart >= WX_HEARTBEAT:
                self.ws.send_text(json.dumps({'msgType': 101, 'createTime': int(now * 1000)}))
                heart = now
            if now - last_step >= 3:
                last_step = now
                if not self.cred.get('deviceToken'):
                    self.login_step()
                elif not self.history_loaded:
                    self.load_history()
                elif self.del_pending:
                    self.flush_deletes()

    def run(self):
        delay = 5
        while True:
            began = time.time()
            try:
                self.session()
            except Exception as e:                # the network, the service: again a little later
                with self.lock:
                    if self.state != 'login':
                        self.state = 'error'
                    self.info = str(e)[:80]
            finally:
                if self.ws:
                    self.ws.close()
                    self.ws = None
            delay = 5 if time.time() - began > 60 else min(delay * 2, 120)   # a steady session: start over
            time.sleep(delay)

    # ---- the board's document -------------------------------------------------------------------
    def items(self):
        """/app/wxpusher: {t: state, a: starting|login|ok|error, b: info, c: QR modules per side, d: seconds
        the QR code stays valid}; the QR code {t: qr, a: 0, b: its rows in hex, comma-separated};
        the newest messages {t: m (unread) or h (read on the service before), a: id, b: title,
        c: MM-DD HH:MM, d: the text, topic: the app or topic it came from}."""
        self.last_seen = time.time()
        with self.lock:
            state, info = self.state, self.info
            rows = self.qr_rows if state == 'login' else None
            left = max(0, int(self.qr_expires - time.time())) if rows else 0
            msgs = [self.messages[k] for k in sorted(self.messages, reverse=True)[:WX_DOC_MESSAGES]]
        items = [{'t': 'state', 'a': state, 'b': clip(info, 60), 'c': str(len(rows)) if rows else '',
                  'd': str(left)}]
        if rows:
            items.append({'t': 'qr', 'a': '0', 'b': ','.join(qr_hex(r) for r in rows)})
        for m in msgs:
            item = {'t': 'h' if m['read'] else 'm', 'a': str(m['id']), 'b': m['title'],
                    'c': dt.datetime.fromtimestamp(m['time']).strftime('%m-%d %H:%M')}
            text = (utf8_parts(m['content'], WX_TEXT_BYTES, 1) or [''])[0]
            if text:
                item['d'] = text
            if m['name']:
                item['topic'] = m['name']
            items.append(item)
        return items


_receivers: Dict[str, WxReceiver] = {}
_receivers_lock = threading.Lock()


def _key(device: str) -> str:
    return re.sub(r'[^A-Za-z0-9_-]', '', device or '')[:32] or 'local'


def receiver(device: str) -> WxReceiver:
    key = _key(device)
    with _receivers_lock:
        rx = _receivers.get(key)
        if rx is None:
            rx = WxReceiver(os.path.join(_state_dir, f'{key}.json'))
            _receivers[key] = rx
    rx.start()
    return rx


def app_wxpusher(qs: Dict[str, str], device: str) -> List[Dict[str, str]]:
    """del=<ids>: messages the board deleted (it sends them until a document
    comes back); logout=1: the board's 退出登录."""
    rx = receiver(device)
    if qs.get('logout') == '1':
        rx.logout()
    if qs.get('del'):
        rx.delete(qs['del'].split(','))
    return rx.items()


# ---------------------------------------------------------------------------
# Notifier: messages for a board, pushed here by scripts and phones
#   GET/POST /notify?text=...&title=...&level=info|warn|urgent&from=...&wx=1
# The board's notifier app polls /app/notifier. A local server keeps one
# list for the boards of its network; a public one a list per device (to=).
# ---------------------------------------------------------------------------

NOTIFY_KEEP = 30
_notes: Dict[str, list] = {}
_note_seq = 0
_note_lock = threading.Lock()


def notify_push(fields: Dict[str, str], device: str = '') -> Dict[str, Any]:
    global _note_seq
    text = clip(fields.get('text') or fields.get('content') or '', 70)
    title = clip(fields.get('title') or '', 16)
    if not text and not title:
        raise ProxyError('empty message: text= or title=')
    level = fields.get('level', 'info')
    level = level if level in ('info', 'warn', 'urgent') else 'info'
    with _note_lock:
        _note_seq += 1
        note = {'id': _note_seq, 'time': hhmm(), 'title': title, 'text': text, 'level': level,
                'source': clip(fields.get('from') or fields.get('source') or '', 10)}
        notes = _notes.setdefault(_key(device), [])
        notes.append(note)
        del notes[:-NOTIFY_KEEP]
    result = {'success': True, 'id': note['id']}
    if str(fields.get('wx', '')).lower() in ('1', 'true', 'yes'):
        try:
            result['wx'] = wxpusher_send((title + '\n' if title else '') + text, title or text)
        except ProxyError as e:
            result['wx'] = {'ok': False, 'message': str(e)}
    return result


def app_notifier(qs: Dict[str, str], device: str) -> List[Dict[str, str]]:
    try:
        n = max(1, min(int(qs.get('n', '8')), 12))
    except ValueError:
        n = 8
    with _note_lock:
        notes = list(reversed(_notes.get(_key(device), [])[-n:]))
        seq = _note_seq
    items = [{'t': 'meta', 'a': str(seq), 'b': hhmm()}]
    for x in notes:
        items.append({'t': 'msg', 'a': str(x['id']), 'b': x['title'], 'c': x['text'], 'd': x['level'],
                      'e': f'{x["time"]} {x["source"]}'.strip()})
    return items


# ---------------------------------------------------------------------------
# WxPusher sender (/notify wx=1, /wxpusher/send): the server's own app token
#   config.ini [wxpusher] app_token = AT_..., uids = UID_a,UID_b (or topic_ids = 123)
# ---------------------------------------------------------------------------

_send_cfg: Dict[str, Any] = {}


def set_sender(app_token: str = '', uids: str = '', topic_ids: str = ''):
    split = lambda v: [x for x in re.split(r'[,\s]+', v or '') if x]   # noqa: E731
    _send_cfg.update(app_token=app_token.strip(), uids=split(uids),
                     topic_ids=[int(x) for x in split(topic_ids) if x.isdigit()])


def wxpusher_send(content: str, summary: str = '', content_type: int = 1, uids=None, topic_ids=None,
                  url: str = '') -> Dict[str, Any]:
    token = _send_cfg.get('app_token')
    if not token:
        raise ProxyError('WxPusher: no [wxpusher] app_token in the server config.ini')
    body = {'appToken': token, 'content': content, 'summary': clip(summary or content, 99),
            'contentType': content_type, 'uids': uids or _send_cfg.get('uids') or [],
            'topicIds': topic_ids or _send_cfg.get('topic_ids') or []}
    if url:
        body['url'] = url
    if not body['uids'] and not body['topicIds']:
        raise ProxyError('WxPusher: no receivers ([wxpusher] uids or topic_ids)')
    req = urllib.request.Request(f'{WX_BASE}/api/send/message', method='POST',
                                 data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode('utf-8'))
    except (OSError, ValueError) as e:
        raise ProxyError(f'WxPusher: {e}')
    return {'ok': result.get('code') == 1000, 'message': result.get('msg', ''), 'data': result.get('data')}


# ---------------------------------------------------------------------------
# HTTP: GET/POST /notify, POST /wxpusher/send and /wxpusher/logout
# ---------------------------------------------------------------------------

HTTP_PATHS = ('/notify', '/wxpusher/send', '/wxpusher/logout')


def device_id(text: str) -> str:
    """A device id as the channel names it (12 uppercase hex digits), '' if not one."""
    t = re.sub(r'[^0-9A-Fa-f]', '', text or '').upper()
    return t if len(t) == 12 else ''


def handle_request(path_with_query: str, body: bytes = b'', public: bool = False, authorized=None):
    """(status, content type, body) for /notify, /wxpusher/send and /wxpusher/logout.
    The body is a form or JSON; query fields count too. A local server takes
    them from its network (one message list and one WxPusher login for its
    boards); a public one only with a portal session: authorized(device id)
    tells whether the request has the admin's or that device's own session
    (device id '': the admin's). /notify then needs to=<device id>."""
    url = urllib.parse.urlparse(path_with_query)
    fields = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
    text = body.decode('utf-8', 'replace') if body else ''
    if text.lstrip().startswith('{'):
        try:
            fields.update({k: v if isinstance(v, str) else json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                           for k, v in json.loads(text).items()})
        except (ValueError, AttributeError):
            return _reply(400, {'success': False, 'error': 'bad JSON body'})
    elif text:
        fields.update({k: v[0] for k, v in urllib.parse.parse_qs(text, keep_blank_values=True).items()})
    allowed = authorized or (lambda mac: False)
    try:
        if url.path == '/notify':
            if public:
                to = device_id(fields.get('to', ''))
                if not to:
                    return _reply(400, {'success': False, 'error': 'to=<device id> is required on this server'})
                if not allowed(to):
                    return _reply(401, {'success': False, 'error': 'unauthorized: log in to the portal'})
                out = notify_push(fields, to)
            else:
                out = notify_push(fields, 'local')
        elif url.path == '/wxpusher/logout':
            if public:                  # a board logs out through its app (/app/wxpusher?logout=1)
                return _reply(404, {'success': False, 'error': 'unknown endpoint'})
            receiver('local').logout()
            out = {'success': True}
        elif url.path == '/wxpusher/send':
            if public and not allowed(''):
                return _reply(401, {'success': False, 'error': 'unauthorized'})
            split = lambda v: [x for x in re.split(r'[,\s]+', v or '') if x]   # noqa: E731
            kind = str(fields.get('type', '1'))
            r = wxpusher_send(fields.get('content', ''), fields.get('summary', ''),
                              int(kind) if kind.isdigit() else 1,
                              split(fields.get('uids')), [int(x) for x in split(fields.get('topics')) if x.isdigit()],
                              fields.get('url', ''))
            out = dict(r, success=r['ok'])
            if not r['ok']:
                out['error'] = r['message']
        else:
            return _reply(404, {'success': False, 'error': 'unknown endpoint'})
    except ProxyError as e:
        out = {'success': False, 'error': str(e)}
    return _reply(200, out)


def _reply(status, obj):
    return status, 'application/json; charset=utf-8', json.dumps(obj, ensure_ascii=False).encode('utf-8')


def app_items(path: str, qs: Dict[str, str], device: str, public: bool) -> List[Dict[str, str]]:
    """/app/wxpusher or /app/notifier for a request: a public server answers
    a device on the encrypted channel (its own login and messages); a local
    one has one of each for its boards."""
    key = device if public else 'local'
    if not key:
        return [{'t': 'error', 'a': 'encrypted channel required (register the device)'}]
    try:
        if path == '/app/wxpusher':
            return app_wxpusher(qs, key)
        return app_notifier(qs, key)
    except ProxyError as e:
        return [{'t': 'error', 'a': clip(str(e), 60)}]
