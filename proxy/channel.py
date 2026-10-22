"""The MSC1 encrypted channel (minischeme_channel.h), server side.

Request: "MSC1" | id_len (1..64) | id | nonce (16) | size (u32 BE) | AES-256-GCM(k_req, IV 0^12,
plaintext, AAD = everything before the ciphertext) where plaintext is
time (u32 BE) | method_len | method | url_len (u16 BE) | url | body.
k_req, k_resp = HKDF-SHA256(master_key_2, salt=nonce, info="msc1 request keys").

Each (device, nonce) is accepted once within 2 x CHANNEL_SKEW; a request whose
time is more than CHANNEL_SKEW seconds off is answered (encrypted) with 401 so
the device learns its clock is wrong. The answer is records of at most 1 KB
(pack_channel_response).
"""

import struct
import threading
import time
import urllib.parse

from .crypto import aes_gcm_decrypt, derive_request_keys, pack_channel_response
from .security import FetchRefused, guarded_fetch

CHANNEL_SKEW = 300
CHANNEL_MAX = 64 * 1024
LOCAL_PATHS = ("/app/", "/market/")
LOCAL_EXACT = ("/health", "/fetch", "/relay")

_seen = {}
_seen_lock = threading.Lock()


class ChannelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def open_request(raw: bytes, master_key_2_of):
    """(device id, k_resp, method, url, body, clock problem or None) of a request.

    master_key_2_of(device id) returns the device's key or None."""
    if len(raw) < 5 or raw[:4] != b"MSC1" or not 1 <= raw[4] <= 64:
        raise ChannelError(400, "not a channel request")
    n_id = raw[4]
    head_len = 5 + n_id + 16 + 4
    if len(raw) < head_len + 16:
        raise ChannelError(400, "short request")
    try:
        device = raw[5:5 + n_id].decode("ascii")
    except UnicodeDecodeError:
        raise ChannelError(400, "bad device id")
    nonce = raw[5 + n_id:5 + n_id + 16]
    size = struct.unpack(">I", raw[5 + n_id + 16:head_len])[0]
    if size > CHANNEL_MAX:
        raise ChannelError(400, "request too large")
    if len(raw) != head_len + size + 16:
        raise ChannelError(400, "short request")
    m2 = master_key_2_of(device)
    # An unknown device and a wrong key look the same from outside.
    if m2 is None:
        raise ChannelError(403, "request failed authentication")
    k_req, k_resp = derive_request_keys(m2, nonce)
    try:
        plain = aes_gcm_decrypt(k_req, bytes(12), raw[head_len:], raw[:head_len])
    except Exception:
        raise ChannelError(403, "request failed authentication")
    if len(plain) < 8:
        raise ChannelError(400, "short request")
    sent = struct.unpack(">I", plain[:4])[0]
    m = plain[4]
    if len(plain) < 7 + m:
        raise ChannelError(400, "short request")
    method = plain[5:5 + m].decode("ascii", "replace").upper()
    u = struct.unpack(">H", plain[5 + m:7 + m])[0]
    if len(plain) < 7 + m + u:
        raise ChannelError(400, "short request")
    url = plain[7 + m:7 + m + u].decode("utf-8", "replace")
    body = plain[7 + m + u:]

    now = time.time()
    with _seen_lock:
        for k in [k for k, exp in _seen.items() if exp < now]:
            del _seen[k]
        if (device, nonce) in _seen:
            raise ChannelError(403, "replayed request")
        _seen[(device, nonce)] = now + 2 * CHANNEL_SKEW
    problem = None
    if sent == 0 or abs(now - sent) > CHANNEL_SKEW:
        problem = f"clock off by {int(sent - now)} s"
    return device, k_resp, method, url, body, problem


def answer(method: str, url: str, body: bytes, local_dispatch, allow_private: bool):
    """(status, body) for a device's request.

    The proxy's own API paths are answered by local_dispatch(path_with_query);
    any other URL is fetched (GET only), from the internet only unless
    allow_private."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        return 400, b"url must be http(s)://"
    path = p.path or "/"
    if path.startswith(LOCAL_PATHS) or path in LOCAL_EXACT:
        status, _, out = local_dispatch(path + ("?" + p.query if p.query else ""))
        return status, out
    if method != "GET":
        return 405, b"only GET to other hosts"
    try:
        _, out = guarded_fetch(url, allow_private=allow_private)
        return 200, out
    except FetchRefused as e:
        return 403, str(e).encode("utf-8")
    except Exception as e:
        return 502, f"fetch failed: {type(e).__name__}".encode("utf-8")


def seal(k_resp: bytes, status: int, body: bytes) -> bytes:
    return pack_channel_response(k_resp, status, body)


def register_device(db, master_key_1: bytes, mac: str, hw_hash_key: bytes, ip: str = ""):
    """("registered" | "recovered", master_key_2) for a device's hardware hash
    key, or None when the device is registered with another key.

    A new device is registered; a registered one only gets its own key back
    (after a factory reset) when it proves the same hash key: an existing
    device's keys are never replaced."""
    import hmac as _hmac
    from .crypto import derive_master_key_2
    dev = db.get_device(mac)
    if dev is None:
        m2 = derive_master_key_2(master_key_1, hw_hash_key, mac)
        if db.register_device(mac, hw_hash_key, m2, ip):
            return "registered", m2
        dev = db.get_device(mac)
        if dev is None:
            return None
    if dev.get("status") == "simulator" or not dev.get("hardware_hash_key"):
        return None
    if not _hmac.compare_digest(bytes(dev["hardware_hash_key"]), hw_hash_key):
        return None
    db.update_device_seen(mac, ip)
    return "recovered", bytes(dev["master_key_2"])
