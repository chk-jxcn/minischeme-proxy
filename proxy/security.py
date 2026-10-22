"""Request-safety helpers shared by the proxy server.

- safe_join: a file name inside a directory, never outside it (no "..",
  absolute paths, drive letters or backslashes).
- guarded_fetch: an outgoing HTTP(S) GET that refuses hosts of this network
  (loopback, private, link-local such as cloud metadata, multicast, reserved)
  unless the caller allows them, re-checking every redirect hop, with a size cap.
- redact_url: a URL with secret query values (token, key, auth, ...) masked,
  for logs.
- origin_allowed: the CORS allow-list.
- RateLimiter: a small sliding-window counter.
"""

import ipaddress
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_join(base_dir: str, rel: str):
    """The path of rel inside base_dir, or None when rel could leave it.

    rel is a '/'-separated relative path whose parts are plain names
    ([A-Za-z0-9._-], not '.' or '..')."""
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    for p in parts:
        if not p or p in (".", "..") or not _NAME_RE.match(p):
            return None
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, *parts))
    if os.path.commonpath([base, path]) != base:
        return None
    return path


def _address_is_public(ip) -> bool:
    return ip.is_global and not ip.is_multicast


def host_is_public(host: str) -> bool:
    """Whether every address of host is on the internet."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if not _address_is_public(ip):
            return False
    return True


def url_is_public(url: str) -> bool:
    p = urllib.parse.urlparse(url)
    return p.scheme in ("http", "https") and host_is_public(p.hostname or "")


class FetchRefused(Exception):
    """The URL (or a redirect) points at a host that is not allowed."""


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool):
        super().__init__()
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlparse(newurl)
        if p.scheme not in ("http", "https"):
            raise FetchRefused("redirect to a non-http(s) URL")
        if not self.allow_private and not host_is_public(p.hostname or ""):
            raise FetchRefused("redirect to a host of this network")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_fetch(url: str, allow_private: bool = False, max_bytes: int = 512 * 1024,
                  timeout: float = 5.0, headers=None):
    """(content type, body) of a GET of url.

    Raises FetchRefused for a URL this server must not fetch, and
    urllib/OSError exceptions for network failures. A body larger than
    max_bytes raises FetchRefused."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchRefused("url must be http(s)://host/...")
    if not allow_private and not host_is_public(p.hostname):
        raise FetchRefused("not an internet host")
    opener = urllib.request.build_opener(_CheckedRedirects(allow_private))
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "MiniScheme-Proxy/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise FetchRefused(f"answer larger than {max_bytes} bytes")
        return resp.headers.get("Content-Type", "application/octet-stream"), body


def device_header(value: str) -> str:
    """A device id from an X-MiniScheme-Device header (12 hex digits, or a
    simulator's id of letters, digits and -), '' when it is not one."""
    v = (value or "").strip()
    return v if re.fullmatch(r"[0-9A-Za-z-]{4,40}", v) else ""


def relay_fetch(target: str, device: str, allow_private: bool):
    """(status, content type, body) of GET /relay?url=target: an app server
    reached through this converter for a device; it gets the device's id in
    X-MiniScheme-Device (over the encrypted channel the authenticated one,
    over plain HTTP the device's own header)."""
    if not target.startswith(("http://", "https://")):
        return 400, "text/plain", b"missing or invalid url"
    headers = {"User-Agent": "MiniScheme-Proxy/1.0"}
    if device_header(device):
        headers["X-MiniScheme-Device"] = device_header(device)
    try:
        ctype, body = guarded_fetch(target, allow_private=allow_private, headers=headers)
        return 200, ctype, body
    except FetchRefused as e:
        return 403, "text/plain", f"relay refused: {e}".encode("utf-8")
    except Exception as e:
        return 502, "text/plain", f"relay error: {type(e).__name__}".encode("utf-8")


_SECRET_PARAM_RE = re.compile(r"(token|key|auth|pass|pwd|secret|sig|uid)", re.I)


def redact_url(url: str) -> str:
    """url with the values of secret-looking query parameters replaced by ***."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0]
    if not p.query:
        return url
    pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    if p.path.endswith("/app/medicine"):     # health records: only what was asked
        pairs = [(k, v) for k, v in pairs if k == "op"]
    safe = [(k, "***" if (_SECRET_PARAM_RE.search(k) and v) else v) for k, v in pairs]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(safe, safe="*,:"), p.fragment))


def _ip_or_none(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    ip = _ip_or_none(host)
    return bool(ip and ip.is_loopback)


def is_private_host(host: str) -> bool:
    """Loopback, private or link-local literal address (or localhost)."""
    if is_loopback_host(host):
        return True
    ip = _ip_or_none(host)
    return bool(ip and (ip.is_private or ip.is_link_local))


DEFAULT_ALLOWED_ORIGINS = ("https://chk-jxcn.github.io",)


def origin_allowed(origin: str, request_host: str, allow_private_origins: bool, extra=()) -> bool:
    """Whether a browser page from origin may read this server's answers.

    Allowed: the server's own origin, the WebREPL's GitHub Pages origin,
    the extra list, and (when allow_private_origins) pages served from a
    loopback or private address, i.e. a WebREPL on this network."""
    if not origin:
        return False
    if origin in DEFAULT_ALLOWED_ORIGINS or origin in extra:
        return True
    try:
        p = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if request_host and p.netloc.lower() == request_host.lower():
        return True
    return allow_private_origins and is_private_host(p.hostname)


class RateLimiter:
    """At most `limit` events per `window` seconds for each key."""

    def __init__(self, limit: int, window: float, max_keys: int = 10000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._events = {}
        self._lock = threading.Lock()

    def allow_peek(self, key: str) -> bool:
        """Whether one more event would be allowed (records nothing)."""
        now = time.time()
        with self._lock:
            return len([t for t in self._events.get(key, []) if t > now - self.window]) < self.limit

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            if len(self._events) > self.max_keys:
                for k in [k for k, v in self._events.items() if not v or v[-1] < now - self.window]:
                    del self._events[k]
            events = [t for t in self._events.get(key, []) if t > now - self.window]
            if len(events) >= self.limit:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True


def client_ip(peer: str, headers) -> str:
    """The client's address: X-Forwarded-For / X-Real-IP only when the
    connection itself comes from this machine (a reverse proxy in front),
    else the peer address (a header from the internet is not believed)."""
    peer = (peer or "").strip()
    if is_loopback_host(peer):
        for name in ("X-Forwarded-For", "X-Real-IP"):
            v = (headers.get(name) or "").split(",")[0].strip()
            if v.count(":") == 1:          # "a.b.c.d:port"
                v = v.split(":")[0]
            if v and _ip_or_none(v) is not None:
                return v.strip("[]")
    return peer


_BLOCKED_SUFFIXES = (".ini", ".db", ".db-wal", ".db-shm", ".py", ".pyc", ".log", ".pem", ".key")
_BLOCKED_DIRS = ("proxy", "tools", "tests", "__pycache__", "build", "dist")


def static_path_blocked(url_path: str) -> bool:
    """Whether a static file request must not be served: dot files and
    folders (.git), configuration, databases, the server's own sources."""
    parts = [p for p in urllib.parse.unquote(url_path.split("?", 1)[0]).replace("\\", "/").split("/") if p]
    if any(p.startswith(".") for p in parts):
        return True
    if parts and parts[0].lower() in _BLOCKED_DIRS:
        return True
    return bool(parts) and parts[-1].lower().endswith(_BLOCKED_SUFFIXES)


def cors_origin(origin: str, request_host: str, path: str):
    """The Access-Control-Allow-Origin value for a request, or None (no header).

    Allowed: the WebREPL (GitHub Pages), pages on this network or this
    server, and "null" (a sandboxed app page) for the public app data paths."""
    if not origin:
        return None
    if origin == "null":
        return "null" if path.startswith(("/app/", "/market/", "/health")) else None
    return origin if origin_allowed(origin, request_host, True) else None
