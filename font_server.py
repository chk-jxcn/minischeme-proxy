#!/usr/bin/env python3
"""MiniScheme font server: glyphs for the characters a board's font lacks.

A service of its own, beside the app proxy: boards ask it without a password
(config.ini [fonts] font_server), and it is useful without any app server.

Usage:
    python font_server.py [--mode local|public] [--host 0.0.0.0] [--port 8090]
                          [--data fontdata] [--config font_server.ini] [--no-system-fonts]

Fonts are uploaded on the administration page (http://host:8090/); on a test
PC (local mode) the installed fonts can be enabled too. The administration
password: config [security] admin_password or MINISCHEME_FONT_ADMIN_PASSWORD
(local mode: "admin" when unset; public mode refuses to start without one).
"""

import argparse
import configparser
import os
import sys

from fontsrv.library import Library
from fontsrv.server import FontServer

DEFAULT_LOCAL_PASSWORD = "admin"


def main(argv=None):
    ap = argparse.ArgumentParser(description="MiniScheme font server")
    ap.add_argument("--mode", choices=["local", "public"], default="local")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--data", default="fontdata", help="uploaded fonts and the font order")
    ap.add_argument("--config", default="font_server.ini")
    ap.add_argument("--no-system-fonts", action="store_true",
                    help="do not offer the installed fonts (default in public mode)")
    args = ap.parse_args(argv)

    cfg = configparser.ConfigParser()
    if os.path.exists(args.config):
        cfg.read(args.config, encoding="utf-8")
    password = os.getenv("MINISCHEME_FONT_ADMIN_PASSWORD") or cfg.get("security", "admin_password", fallback="")
    public = args.mode == "public"
    if not password:
        if public:
            print("[FATAL] public mode needs [security] admin_password in the config or "
                  "MINISCHEME_FONT_ADMIN_PASSWORD", file=sys.stderr)
            return 1
        password = DEFAULT_LOCAL_PASSWORD
        print(f"[*] Administration password: the default '{DEFAULT_LOCAL_PASSWORD}' "
              f"(set [security] admin_password in {args.config})")
    system_fonts = not args.no_system_fonts and not public
    library = Library(os.path.abspath(args.data), system_fonts=system_fonts)
    server = FontServer((args.host, args.port), library, password,
                        login_delay=3 if public else 0, public=public)
    print("=" * 60)
    print(f" MiniScheme font server [{args.mode.upper()}]")
    print(f" Listening on  http://{args.host}:{args.port}  (administration: /)")
    print(f" Fonts:        {library.upload_dir}  ({len(library.order)} enabled)")
    print(f" System fonts: {'offered' if system_fonts else 'off'}")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
