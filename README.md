# MiniScheme App Proxy & Protocol Converter

A high-performance application proxy, protocol converter, and device management service built with Python 3 and SQLite for the MiniScheme desktop info card ecosystem.

## Core Capabilities
- **AES-256-GCM encrypted channel (MSC1)**: avoids the 30-45 KB of RAM TLS needs on the board; requests go over HTTP (`POST /channel`).
- **Device keys**:
  1. **Registration**: the WebREPL setup posts the board's MAC and hardware password to `/channel/auth` once (in the clear, by design). The server derives the hardware hash key and `master_key_2 = HKDF(hash key, master_key_1, MAC)`, stores both and returns `master_key_2`, which the setup writes to the board's `[channel] channel_key`.
  2. **No take-over**: a registered device is never re-keyed. Registering it again returns its key only with the same password (e.g. after a factory reset); anything else gets 403. The challenge flow (`hardware_hash_key`, then `HMAC(hash key, challenge)`) does the same without sending the password.
  3. **Requests**: each request carries 16 random nonce bytes; `HKDF(salt = nonce, ikm = master_key_2, info = "msc1 request keys")` gives that request's own request and response keys. A nonce seen in the last 10 minutes is refused; the board's clock is not enforced (it may send time 0 before NTP).
  4. **What a device may ask**: the proxy's own `/app/...` and `/market/...` (answered in-process), or an internet `http(s)` URL (GET, fetched by the server; never a private or cloud-metadata address on a public server).
- **Dual Deployment Modes**:
  - `local`: Designed for home/LAN use. Without `[security] admin_password` the admin password is `admin`; change it in the admin portal (written into config.ini) or in config.ini (read again at every login).
  - `public`: Designed for cloud servers. Requires admin password in configuration/env; refuses passwordless startup.
- **SQLite Multi-Tenant Storage**:
  - Per-device isolated key-value store for app settings.
  - Detailed access logs and IP tracking.
  - Multi-tier in-memory TTL caching (weather 10m, news 5m, stocks 15s).
- **Web Portals**:
  - **User portal (`/user`)**: MAC + hardware password; the page derives the hash key itself and sends only `SHA-256(hash key || nonce)`. Simulator users log in with their UUID. Shows access logs and masked data; clears data or deletes the account.
  - **Admin portal (`/admin`)**: all devices and their data in plain text. Needs `[security] admin_password`; logins wait `admin_login_delay` seconds; the session token travels in the `X-Session-Token` header.
- **Safety rules**:
  - `/market/<file>` serves only files inside `apps/`; `/fetch?url=` reaches internet hosts only (every redirect re-checked).
  - CORS answers only for the published WebREPL, pages on the same network and the server itself.
  - `X-Forwarded-For` is trusted only from a reverse proxy on the same machine.
  - Secret query values (`token`, `key`, ...) are masked in logs and the access log; the database file is mode 0600.
  - The server's own QWeather key (`[qweather]` in config.ini, or `MINISCHEME_QWEATHER_KEY`) is used only for devices without a token and only with QWeather hosts.
- **Real data only**: market endpoints never invent values; e.g. `/app/crypto` takes the first of OKX, Binance (`data-api.binance.vision`, reachable from mainland China) and Huobi that answers (a failing source is skipped for 5 minutes), USD/CNY from OKX or Sina, and answers 502 when no source responds, so the board keeps its last data.

## Installation & Running

```bash
pip install -r requirements.txt
```

### Start in Local Mode (Default)
```bash
python app_proxy.py --mode local --port 8087
```

### Start in Public Mode (Production)
```bash
python app_proxy.py --mode public --port 8087 --config config.ini
```

### The public server
`tools/deploy_public_proxy.py` deploys `server.py` (the same code the WebREPL's local server runs) as the systemd service `minischeme-proxy` in `/opt/minischeme-proxy`: `server.py --proxy-only --protocol both` (plaintext `/app/*` for the online simulator, MSC1 for boards). An existing `/opt/minischeme-proxy/config.ini` is kept untouched; a new one gets a random admin password and `master_key_1`, and an empty `[qweather]` section.

## Running Tests
```bash
python -m pytest -v tests
```

## License
MIT License
