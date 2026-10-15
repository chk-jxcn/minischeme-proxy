# MiniScheme App Proxy & Protocol Converter

A high-performance application proxy, protocol converter, and device management service built with Python 3 and SQLite for the MiniScheme desktop info card ecosystem.

## Core Capabilities
- **AES-256-GCM Encrypted Channel**: Eliminates the 30-45 KB RAM overhead of TLS on microcontrollers by encrypting HTTP/TCP traffic using per-session symmetric keys.
- **5-Step Key Lifecycle**:
  1. **Provisioning**: User enters WiFi credentials and a secure Hardware Password on the device.
  2. **First Handshake**: Device derives `hardware_hash_key` and sends it to the server. Server combines its `master_key_1` and the device's `hardware_hash_key` to derive `master_key_2` (session master). Keys appear on the wire only once.
  3. **Runtime Encryption**: Every request derives single-use subkeys from `master_key_2` with 16-byte random nonces and timestamp verification (anti-replay window: ±5 minutes).
  4. **Factory Reset Recovery**: If a reset device reconnects, the server issues a random challenge nonce. The device computes `HMAC(hardware_hash_key, challenge)`. If verified, `master_key_2` is restored. Failed attempts are rejected immediately (device retries once per boot).
  5. **Roundtrip Optimization**: Supports batch endpoints (`/app/batch`) packing multiple queries into one roundtrip for lightning-fast boot.
- **Dual Deployment Modes**:
  - `local`: Designed for home/LAN use. Prompts the user to set an admin password on first run.
  - `public`: Designed for cloud servers. Requires admin password in configuration/env; refuses passwordless startup.
- **SQLite Multi-Tenant Storage**:
  - Per-device isolated key-value store for app settings.
  - Detailed access logs and IP tracking.
  - Multi-tier in-memory TTL caching (weather 10m, news 5m, stocks 15s).
- **Web Portals**:
  - **User Portal (`/user`)**: Login with MAC address + hardware password via client-side nonce hashing (no plaintext passwords over the wire). Inspect logs, view masked sensitive data (`sk-****1234`), clear app data, or delete device account.
  - **Admin Portal (`/admin`)**: Audit all registered devices, view plaintext data for debugging, modify keys, and preset configuration data for devices before they first connect.

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

## Running Tests
```bash
python -m pytest -v tests
```

## License
MIT License
