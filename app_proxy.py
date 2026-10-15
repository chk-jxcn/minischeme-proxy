#!/usr/bin/env python3
"""MiniScheme App Proxy & Protocol Converter Service

Usage:
    python app_proxy.py [--mode local|public] [--port 8087] [--host 0.0.0.0] [--config config.ini]
"""

import argparse
import configparser
import getpass
import os
import secrets
import sys

from proxy.server import ProxyServer


def main():
    parser = argparse.ArgumentParser(description="MiniScheme App Proxy & Protocol Converter")
    parser.add_argument("--mode", choices=["local", "public"], default="local", help="Deployment mode")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind")
    parser.add_argument("--port", type=int, default=8087, help="Port to listen on")
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
        if not cfg.has_section("security"):
            cfg.add_section("security")
        cfg.set("security", "master_key_1", master_key_1.hex())
        with open(args.config, "w", encoding="utf-8") as f:
            cfg.write(f)
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
            print("=" * 60)
            print(" MiniScheme Local Proxy - First Launch Wizard")
            print("=" * 60)
            if sys.stdin.isatty():
                pwd = getpass.getpass("Please set an Administrator password for this local instance: ").strip()
                while not pwd:
                    pwd = getpass.getpass("Password cannot be empty. Please enter: ").strip()
            else:
                pwd = secrets.token_urlsafe(12)
                print(f"[*] Non-interactive environment: auto-generated admin password: {pwd}")
            admin_password = pwd
            if not cfg.has_section("security"):
                cfg.add_section("security")
            cfg.set("security", "admin_password", admin_password)
            with open(args.config, "w", encoding="utf-8") as f:
                cfg.write(f)
            print(f"[*] Admin password saved to {args.config}")

    db_path = cfg.get("server", "db_path", fallback=args.db)

    print("=" * 60)
    print(f" MiniScheme App Proxy & Protocol Converter [{args.mode.upper()} MODE]")
    print(f" Listening on http://{args.host}:{args.port}")
    print(f" SQLite DB:     {os.path.abspath(db_path)}")
    print(f" User Portal:   http://{args.host}:{args.port}/user")
    print(f" Admin Portal:  http://{args.host}:{args.port}/admin")
    print("=" * 60)

    server = ProxyServer((args.host, args.port), master_key_1, admin_password, db_path=db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
