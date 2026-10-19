#!/usr/bin/env python3
"""MiniScheme App Proxy & Protocol Converter Service

Usage:
    python app_proxy.py [--mode local|public] [--port 8087] [--host 0.0.0.0] [--config config.ini]
"""

import argparse
import configparser
import os
import secrets
import sys

from proxy.admin_password import DEFAULT_LOCAL_PASSWORD, write_ini_value
from proxy.server import ProxyServer


def main():
    parser = argparse.ArgumentParser(description="MiniScheme App Proxy & Protocol Converter")
    parser.add_argument("--mode", choices=["local", "public"], default="local", help="Deployment mode")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind")
    parser.add_argument("--port", type=int, default=8087, help="Port to listen on")
    parser.add_argument("--protocol", "--protocol-mode", "-P", dest="protocol_mode", choices=["unencrypted", "encrypted", "both"], default=None, help="Protocol mode: unencrypted, encrypted, or both")
    parser.add_argument("--admin-login-delay", type=int, default=None, help="Delay in seconds before returning admin login responses (anti-bruteforce)")
    parser.add_argument("--config", default="config.ini", help="Configuration file path")
    parser.add_argument("--db", default="minischeme_proxy.db", help="SQLite database path")
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    if os.path.exists(args.config):
        cfg.read(args.config, encoding="utf-8")

    # 1. Master Key 1
    master_key_1_hex = os.getenv("MINISCHEME_MASTER_KEY_1") or cfg.get("security", "master_key_1", fallback="")
    if not master_key_1_hex:
        master_key_1 = secrets.token_bytes(32)
        write_ini_value(args.config, "security", "master_key_1", master_key_1.hex())   # comments kept
        print(f"[*] Generated new master_key_1 and saved to {args.config}")
    else:
        master_key_1 = bytes.fromhex(master_key_1_hex)

    # 2. Admin Password handling
    admin_password = os.getenv("MINISCHEME_ADMIN_PASSWORD") or cfg.get("security", "admin_password", fallback="")

    if args.mode == "public":
        if not admin_password:
            print("[FATAL] Public server mode requires admin_password in config.ini or MINISCHEME_ADMIN_PASSWORD env var.", file=sys.stderr)
            print("Passwordless start is strictly prohibited in public mode.", file=sys.stderr)
            sys.exit(1)
    else:  # local mode
        if not admin_password:
            # no prompt: the default, changed in the admin portal or in config.ini
            # [security] admin_password (read again at every login)
            admin_password = DEFAULT_LOCAL_PASSWORD
            print(f"[*] Admin portal password: the default '{DEFAULT_LOCAL_PASSWORD}'. Change it in the portal "
                  f"(/admin) or in {args.config} [security] admin_password.")

    db_path = cfg.get("server", "db_path", fallback=args.db)

    default_proto = "encrypted" if args.mode == "public" else "unencrypted"
    proto_mode = (
        args.protocol_mode
        or os.getenv("MINISCHEME_PROTOCOL_MODE")
        or cfg.get("security", "protocol_mode", fallback="")
        or default_proto
    ).lower().strip()
    if proto_mode not in ("unencrypted", "encrypted", "both"):
        proto_mode = default_proto

    default_delay = 5 if args.mode == "public" else 0
    if args.admin_login_delay is not None:
        login_delay = max(0, args.admin_login_delay)
    else:
        try:
            login_delay = int(
                os.getenv("MINISCHEME_ADMIN_LOGIN_DELAY")
                or cfg.get("security", "admin_login_delay", fallback="")
                or default_delay
            )
        except Exception:
            login_delay = default_delay

    print("=" * 60)
    print(f" MiniScheme App Proxy & Protocol Converter [{args.mode.upper()} MODE]")
    print(f" Protocol Mode: {proto_mode.upper()}")
    print(f" Login Delay:   {login_delay}s")
    print(f" Listening on   http://{args.host}:{args.port}")
    print(f" SQLite DB:     {os.path.abspath(db_path)}")
    print(f" User Portal:   http://{args.host}:{args.port}/user")
    print(f" Admin Portal:  http://{args.host}:{args.port}/admin")
    print("=" * 60)

    server = ProxyServer((args.host, args.port), master_key_1, admin_password, db_path=db_path, protocol_mode=proto_mode, admin_login_delay=login_delay,
                         public=args.mode == "public", config_path=os.path.abspath(args.config))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
