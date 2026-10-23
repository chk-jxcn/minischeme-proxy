#!/usr/bin/env python3
"""
Deploy the MiniScheme font server (font_server.py, fontsrv/) to the public server.
Remote Server: root@124.222.231.186
Port: 8090 (a service of its own, beside minischeme-proxy on 8087)
Service Name: minischeme-font.service
Directory: /opt/minischeme-font (fontdata/ uploaded fonts and their order, font_server.ini)

The uploaded fonts and the config are kept; a new config gets a random
administration password, readable on the server only (never printed here).
"""

import io
import os
import subprocess
import sys
import tarfile
import time
import urllib.request

REMOTE_HOST = "124.222.231.186"
REMOTE_USER = "root"
REMOTE_DIR = "/opt/minischeme-font"
SERVICE = "minischeme-font"
PORT = 8090
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UNIT = f"""[Unit]
Description=MiniScheme font server (glyphs for boards, no password)
After=network.target

[Service]
WorkingDirectory={REMOTE_DIR}
ExecStart=/usr/bin/python3 {REMOTE_DIR}/font_server.py --mode public --host 0.0.0.0 --port {PORT} --data {REMOTE_DIR}/fontdata --config {REMOTE_DIR}/font_server.ini
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def ssh(cmd, data=None):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                        f"{REMOTE_USER}@{REMOTE_HOST}", cmd], input=data, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"remote command failed: {cmd}\n{r.stderr.decode('utf-8', 'replace')}")
    return r.stdout.decode("utf-8", "replace")


def payload():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(os.path.join(BASE_DIR, "font_server.py"), arcname="font_server.py")
        for fn in sorted(os.listdir(os.path.join(BASE_DIR, "fontsrv"))):
            if fn.endswith(".py"):
                tar.add(os.path.join(BASE_DIR, "fontsrv", fn), arcname=f"fontsrv/{fn}")
    return buf.getvalue()


def main():
    print(f"[*] {REMOTE_HOST}: packages")
    ssh("python3 -c 'import PIL, fontTools' 2>/dev/null || "
        "(DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pil python3-fonttools >/dev/null)")
    print(f"[*] uploading to {REMOTE_DIR} (fonts and config kept)")
    ssh(f"mkdir -p {REMOTE_DIR}/fontdata && rm -rf {REMOTE_DIR}/fontsrv && tar -xzf - -C {REMOTE_DIR}", payload())
    made = ssh(f"test -f {REMOTE_DIR}/font_server.ini && echo kept || "
               f"(umask 077; printf '[security]\\nadmin_password = %s\\n' "
               f"\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')\" > {REMOTE_DIR}/font_server.ini; echo new)").strip()
    print(f"[*] config: {made}" + (f" (the administration password is in {REMOTE_DIR}/font_server.ini)" if made == "new" else ""))
    ssh(f"cat > /etc/systemd/system/{SERVICE}.service", UNIT.encode())
    ssh(f"systemctl daemon-reload && systemctl enable {SERVICE} >/dev/null 2>&1; systemctl restart {SERVICE}")
    time.sleep(3)
    state = ssh(f"systemctl is-active {SERVICE} || true").strip()
    print(f"[*] service {SERVICE}: {state}")
    print("[*] on the server:", ssh(f"curl -s -m 5 http://127.0.0.1:{PORT}/health || true").strip())
    try:
        with urllib.request.urlopen(f"http://{REMOTE_HOST}:{PORT}/health", timeout=8) as r:
            print("[*] from here:", r.read().decode())
    except Exception as e:
        print(f"[!] not reachable from here ({e}): open TCP {PORT} in the server's firewall / security group")
    return 0 if state == "active" else 1


if __name__ == "__main__":
    sys.exit(main())
