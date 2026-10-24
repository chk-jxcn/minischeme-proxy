#!/usr/bin/env python3
"""MiniScheme font server: glyphs for the characters a board's font lacks.

A service of its own, beside the app proxy: boards ask it without a password
(config.ini [fonts] font_server), and it is useful without any app server.

Usage:
    python font_server.py [--mode local|public] [--host 0.0.0.0] [--port 8090]
                          [--data fontdata] [--config font_server.ini] [--no-system-fonts]
                          [--no-tray]

Fonts are uploaded on the administration page (http://host:8090/); on a test
PC (local mode) the installed fonts can be enabled too. The administration
password: config [security] admin_password or MINISCHEME_FONT_ADMIN_PASSWORD
(local mode: "admin" when unset; public mode refuses to start without one).

The Windows exe (MiniScheme-FontServer.exe, tools/build_font_exe.py) keeps
fontdata/ and font_server.ini next to itself, writes its output to
font_server.log there, takes the next free port when 8090 is busy and shows
a tray icon (left click: the administration page; right click: a menu).
"""

import argparse
import configparser
import os
import socket
import subprocess
import sys
import threading
import webbrowser

from fontsrv.library import Library
from fontsrv.server import FontServer

DEFAULT_LOCAL_PASSWORD = "admin"
RELEASES_URL = "https://github.com/chk-jxcn/minischeme-proxy/releases/latest"
CONFIG_TEMPLATE = """; MiniScheme font server
[security]
; password of the administration page (empty: "admin" on this PC; required in public mode)
admin_password =
"""

FROZEN = getattr(sys, "frozen", False)          # the PyInstaller exe
HOME = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))


def _log_to_file():
    """The exe has no console: print and tracebacks go to font_server.log next to it."""
    if sys.stdout is None or sys.stderr is None:
        try:
            log = open(os.path.join(HOME, "font_server.log"), "a", encoding="utf-8", buffering=1)
        except OSError:
            log = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


def primary_ip():
    """This PC's address on its LAN (what a board puts in [fonts] font_server)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))        # no packet is sent: it picks the route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def bind_server(host, port, library, password, public, tries):
    """The server on port, or on the next free one (tries ports in all)."""
    for p in range(port, port + max(1, tries)):
        try:
            return FontServer((host, p), library, password, login_delay=3 if public else 0, public=public), p
        except OSError as e:
            last = e
    raise last


def run_tray(port, data_dir, config_path):
    from fontsrv.tray import Tray
    ip = primary_ip()
    local_url, lan_url = f"http://127.0.0.1:{port}/", f"http://{ip}:{port}"

    def copy(text):
        subprocess.run(["clip"], input=text.encode("utf-8"), check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def open_config():
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(CONFIG_TEMPLATE)
        os.startfile(config_path)

    menu = [
        (f"🔤 打开字体服务器管理页 ({local_url})", lambda: webbrowser.open(local_url)),
        None,
        (f"📡 本机 IP: {ip}  |  端口: {port}", lambda: webbrowser.open(lan_url + "/")),
        (f"📋 复制设备上填写的字体服务器地址 ({lan_url})", lambda: copy(lan_url)),
        (f"📁 打开字体目录 ({data_dir})", lambda: os.startfile(data_dir)),
        (f"📝 打开配置文件 ({config_path})", open_config),
        ("🔄 检查并下载最新 Release 版本", lambda: webbrowser.open(RELEASES_URL)),
    ]
    Tray("MiniScheme 字体服务器",
         f"MiniScheme 字体服务器\n{lan_url}\n点击打开管理页",
         menu, on_click=lambda: webbrowser.open(local_url),
         balloon=("MiniScheme 字体服务器已启动",
                  f"设备的字体服务器地址填 {lan_url}\n左键点击托盘图标打开管理页")).run()


def main(argv=None):
    if FROZEN:
        _log_to_file()
    ap = argparse.ArgumentParser(description="MiniScheme font server")
    ap.add_argument("--mode", choices=["local", "public"], default="local")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--data", default=os.path.join(HOME, "fontdata") if FROZEN else "fontdata",
                    help="uploaded fonts and the font order")
    ap.add_argument("--config", default=os.path.join(HOME, "font_server.ini") if FROZEN else "font_server.ini")
    ap.add_argument("--no-system-fonts", action="store_true",
                    help="do not offer the installed fonts (default in public mode)")
    ap.add_argument("--no-tray", action="store_true", help="no tray icon (the exe shows one on Windows)")
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
    # the exe on a PC: the next port when 8090 is taken; a server keeps its port or fails
    server, port = bind_server(args.host, args.port, library, password, public, 10 if FROZEN else 1)
    print("=" * 60)
    print(f" MiniScheme font server [{args.mode.upper()}]")
    print(f" Listening on  http://{args.host}:{port}  (administration: /)")
    print(f" Fonts:        {library.upload_dir}  ({len(library.order)} enabled)")
    print(f" System fonts: {'offered' if system_fonts else 'off'}")
    print("=" * 60)
    tray = FROZEN and os.name == "nt" and not args.no_tray
    try:
        if tray:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            run_tray(port, os.path.abspath(args.data), os.path.abspath(args.config))   # until its exit entry
            server.shutdown()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down")
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
