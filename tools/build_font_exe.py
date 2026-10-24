#!/usr/bin/env python3
"""Packages the font server (font_server.py + fontsrv/) into one Windows
executable, dist/MiniScheme-FontServer.exe, with PyInstaller.

    pip install pyinstaller pillow fonttools
    python tools/build_font_exe.py

The exe has no console: it shows a tray icon and keeps fontdata/,
font_server.ini and font_server.log next to itself (font_server.py).
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "MiniScheme-FontServer"


def main():
    os.chdir(ROOT)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", NAME,
        "--collect-submodules", "fontsrv",
        # library.py imports fontTools inside a function (reading a font's names)
        "--collect-submodules", "fontTools.ttLib",
        "--hidden-import", "PIL.ImageFont",
        "font_server.py",
    ]
    print("[*] Running PyInstaller:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    exe = os.path.join(ROOT, "dist", NAME + (".exe" if os.name == "nt" else ""))
    if not os.path.exists(exe):
        sys.exit(f"[!] Expected output not found: {exe}")
    print(f"[+] Built {exe} ({os.path.getsize(exe)} bytes)")


if __name__ == "__main__":
    main()
