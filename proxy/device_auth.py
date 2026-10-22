"""Devices on plain HTTP (firmware 1.0.69+): every request to an app server
carries

  X-MiniScheme-Device    the WiFi MAC (12 hex digits) or a simulator's SIM-... id
  X-MiniScheme-Password  base64 of the app service password set on the device
                         for this server (UTF-8), absent without one

The first request with a password registers the device (the server keeps
HKDF(the MAC, the password), never the password itself, as the encrypted
channel did); later ones must give the same password, a wrong one is
refused (403). A simulator has no password: its id alone names it. No
password: the id is only a hint (plain_device), never proof of who it is.

Plain HTTP shows the header to anyone on the way: base64 is no encryption.
The encrypted channel (proxy/channel.py) still serves older firmware.
"""

import base64
import binascii
import threading
import time
from typing import Dict, Tuple

from .crypto import derive_hardware_hash_key
from .database import normalize_device_id
from .security import device_header

PASSWORD_MAX = 128          # bytes
FAIL_LIMIT = 20             # wrong passwords from one address ...
FAIL_WINDOW = 15 * 60       # ... in this many seconds, then it waits that long

_fails: Dict[str, Tuple[int, float]] = {}
_fails_lock = threading.Lock()


def decode_password(value: str) -> Tuple[bool, str]:
    """(ok, password) of an X-MiniScheme-Password value; (True, '') without one."""
    v = (value or "").strip()
    if not v:
        return True, ""
    try:
        raw = base64.b64decode(v, validate=True)
        if len(raw) > PASSWORD_MAX:
            return False, ""
        return True, raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False, ""


def _blocked(ip: str) -> bool:
    with _fails_lock:
        n, since = _fails.get(ip, (0, 0.0))
        if since and time.time() - since > FAIL_WINDOW:
            _fails.pop(ip, None)
            return False
        return n >= FAIL_LIMIT


def _failed(ip: str):
    with _fails_lock:
        n, since = _fails.get(ip, (0, 0.0))
        if not since or time.time() - since > FAIL_WINDOW:
            n, since = 0, time.time()
        _fails[ip] = (n + 1, since)
        if len(_fails) > 10000:            # many addresses: forget the oldest
            for k in sorted(_fails, key=lambda k: _fails[k][1])[:5000]:
                _fails.pop(k, None)


def authenticate(db, master_key_1: bytes, headers, ip: str = "") -> Tuple[str, str, str]:
    """(device, plain_device, problem) of a request's headers: device the id the
    password proved (registering a new device), plain_device the id it names
    (proof or not), problem a reason to refuse it ('' to go on)."""
    plain = device_header(headers.get("X-MiniScheme-Device", ""))
    plain = normalize_device_id(plain) if plain else ""
    ok, password = decode_password(headers.get("X-MiniScheme-Password", ""))
    if not ok:
        return "", plain, "X-MiniScheme-Password: base64 of the app service password expected"
    if not password or not plain or plain.startswith("SIM"):
        return "", plain, ""
    if _blocked(ip):
        return "", plain, "too many wrong passwords: try again later"
    from .channel import register_device
    got = register_device(db, master_key_1, plain, derive_hardware_hash_key(password, plain), ip)
    if not got:
        _failed(ip)
        return "", plain, "wrong app service password for this device"
    return plain, plain, ""


def check(db, headers, ip: str = "") -> Tuple[str, str]:
    """The device page's test button (GET /device/check): registers nothing.
    (status, detail): ok (registered here with this password), new (unknown:
    its first request registers it), password (another password), plain (no
    password sent), error."""
    plain = device_header(headers.get("X-MiniScheme-Device", ""))
    if not plain:
        return "error", "no X-MiniScheme-Device header"
    plain = normalize_device_id(plain)
    ok, password = decode_password(headers.get("X-MiniScheme-Password", ""))
    if not ok:
        return "error", "X-MiniScheme-Password is not base64"
    if not password:
        return "plain", "no app service password: the server knows the device by its id only"
    if _blocked(ip):
        return "error", "too many wrong passwords: try again later"
    dev = db.get_device(plain)
    if not dev:
        return "new", "the device is not registered yet: its first request registers it with this password"
    import hmac
    stored = bytes(dev.get("hardware_hash_key") or b"")
    if dev.get("status") == "simulator" or not hmac.compare_digest(stored, derive_hardware_hash_key(password, plain)):
        _failed(ip)
        return "password", "the device is registered here with another app service password"
    return "ok", "registered with this password"
