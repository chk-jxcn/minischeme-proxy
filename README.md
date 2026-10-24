# MiniScheme App Proxy & Protocol Converter

A high-performance application proxy, protocol converter, and device management service built with Python 3 and SQLite for the MiniScheme desktop info card ecosystem.

## Core Capabilities
- **Devices on plain HTTP (firmware 1.0.69+)**: no TLS (30-45 KB of RAM on the board) and no encryption (its code and buffers are gone from the firmware too). Every request carries `X-MiniScheme-Device` (the WiFi MAC, or a simulator's `SIM-...` id) and, when the device has an app service password for this server, `X-MiniScheme-Password` (base64 of it, UTF-8). The first request with a password registers the device (the server keeps `HKDF(MAC, password)`, never the password); later ones need the same password or get 403 (20 wrong passwords from one address in 15 minutes: refused for 15 minutes). `GET /device/check` with the same headers registers nothing (the device page's 测试连接 button): `ok`, `new`, `password` or `plain`. Base64 is no encryption: anyone on the way can read the password, so it should be used nowhere else. `proxy/device_auth.py`.
- **AES-256-GCM encrypted channel (MSC1), for firmware up to 1.0.68**: requests over HTTP (`POST /channel`), kept so older boards keep working.
- **Device keys (the encrypted channel)**:
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
  - **User portal (`/user`)**: MAC + the app service password the device has for this server (its `<key>_password`; firmware before 1.0.65 used the hardware password); the page derives the hash key itself and sends only `SHA-256(hash key || nonce)`. Simulator users log in with their UUID. Shows access logs and masked data; clears data or deletes the account.
  - **Admin portal (`/admin`)**: all devices and their data in plain text. Needs `[security] admin_password`; logins wait `admin_login_delay` seconds; the session token travels in the `X-Session-Token` header.
- **Safety rules**:
  - `/market/<file>` serves only files inside `apps/`; `/fetch?url=` reaches internet hosts only (every redirect re-checked).
  - CORS answers only for the published WebREPL, pages on the same network and the server itself.
  - `X-Forwarded-For` is trusted only from a reverse proxy on the same machine.
  - Secret query values (`token`, `key`, ...) are masked in logs and the access log; the database file is mode 0600.
  - The server's own QWeather key (`[qweather]` in config.ini, or `MINISCHEME_QWEATHER_KEY`) is used only for devices without a token and only with QWeather hosts.
- **Real data only**: market endpoints never invent values; e.g. `/app/crypto` takes the first of OKX, Binance (`data-api.binance.vision`, reachable from mainland China) and Huobi that answers (a failing source is skipped for 5 minutes), USD/CNY from OKX or Sina, and answers 502 when no source responds, so the board keeps its last data.

- **WxPusher and notifier**: `/app/wxpusher` is a WxPusher client (`proxy/wxpusher.py`): it logs in with a QR code scanned in WeChat (drawn by `proxy/qr.py`, sent to the board as bitmap rows) and keeps the messages pushed to the user; the board deletes (`del=`) and logs out (`logout=1`) through its feed URL. `/app/notifier` shows what was pushed to `GET/POST /notify?text=&title=&level=info|warn|urgent&from=&wx=1`. A local server keeps one login and one message list for the boards of its network; a public one keeps them per device (the encrypted channel names the device; plaintext requests get none), and `/notify` there needs `to=<device id>` and the admin's or that device's portal session (`X-Session-Token`). `wx=1` and `POST /wxpusher/send` use the server's own `[wxpusher] app_token` and `uids` (or `topic_ids`) from config.ini. Logins are kept in `wxpusher/` next to the database.

- **Medicine records**: `/app/medicine` keeps the medicine app's dose records per device (`med_records`, `med_names` in the database) when the app's sync is on: the board uploads them (`op=names`, `op=put`; its own copy wins), downloads them only on a new install or when asked (`op=get&from=<day>`), `op=info` counts them. A public server takes them only from a device its password proved (or a simulator). The user portal lists them and can clear them; the access log keeps only `op` of these URLs. `proxy/medicine.py`.

- **App servers without encryption**: `/relay?url=<url>` fetches an app's own server for a device (an app server configured without an app service password and `proxy_url_relay = 1`) and passes the device id on in `X-MiniScheme-Device` (the authenticated one over the channel, the device's own header over plain HTTP). `/channel/auth` with `"check": true` registers nothing (the device's 测试连接 button): an unknown device gets `{"status": "new"}`. The simulator's `/api/fetch` of an app's own server (not this server's endpoints) carries the simulator's id the same way.

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

## Font server (`font_server.py`)
A service of its own, beside the app proxy: boards ask it, **without a password**, for the glyphs of the characters their fonts lack (emoji, Korean, traditional and rare characters), or for every character of their system text when `font_server_first = 1` (firmware API 1.11, config.ini `[fonts]`). It needs no app server.

```bash
python font_server.py                       # local: http://127.0.0.1:8090, password "admin", the PC's fonts offered
python font_server.py --mode public --port 8090 --data /opt/minischeme-font/fontdata --config font_server.ini
```

- **Fonts.** On the administration page (`http://host:8090/`, the password from `[security] admin_password` in the config or `MINISCHEME_FONT_ADMIN_PASSWORD`): upload `.ttf` / `.ttc` / `.otf` files, enable them and put them in order. In local mode the installed fonts (Windows: `C:\Windows\Fonts`) can be enabled too, e.g. Malgun Gothic for Korean and Segoe UI Emoji for emoji. Public mode needs a password and offers only uploads. Only upload fonts you may use on a server.
- **Which font draws a character:** the face the board asks for (its system font, `simsun`, or `font_server_face`) when the server has it, uploaded or installed, then the enabled fonts in order. Glyphs are rendered like the board's font ROM (`minischeme-webrepl/tools/make_font.py`): a SimSun glyph from the server is the same as the ROM's; a glyph taller than the cell (emoji) is drawn at the largest size that fits.
- **API:** `GET /font/glyphs?face=simsun&size=16&cps=4e2d,1f600` (hex code points, at most 64) answers an `MGLY` reply (`fontsrv/glyphs.py`): per code point its advance and 1-bit rows, or "absent"; the reply carries an identity hash that changes with the fonts, so boards drop old glyphs. `GET /font/faces`, `GET /font/preview?face=&size=&text=` (PNG), `GET /health`. Any origin may call it (the WebREPL page does).
- **Code and packaging:** it lives in this repository but stands alone: `font_server.py` and the `fontsrv/` package (it does not use `proxy/`), `tests/test_font_server.py` and `tools/deploy_font_server.py`. Every Release carries `minischeme-font-server.zip` with just these files. It needs `pillow` and `fonttools` (both in `requirements.txt`).
- **Windows exe:** every Release also carries `MiniScheme-FontServer.exe` (built by CI with `tools/build_font_exe.py`, PyInstaller): double-click it and it runs in local mode on port 8090 (the next free port when that one is taken) with a tray icon. A left click opens the administration page. The right-click menu shows the PC's address, copies the address a board puts in `[fonts] font_server`, and opens the fonts folder or the config file. `fontdata/`, `font_server.ini` and `font_server.log` sit next to the exe. `--no-tray` runs it without the icon.
- **Public server:** `tools/deploy_font_server.py` installs it as the systemd service `minischeme-font` in `/opt/minischeme-font` (port 8090), keeping its fonts and config; a new config gets a random administration password (read it on the server: `/opt/minischeme-font/font_server.ini`).
- Tests: `tests/test_font_server.py` builds its own font (fontTools), no system fonts or network.

## Running Tests
```bash
python -m pytest -v tests
```

## License
MIT License
