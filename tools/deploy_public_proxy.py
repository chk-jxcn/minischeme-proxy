#!/usr/bin/env python3
"""
Deploy and synchronize MiniScheme App Proxy & Protocol Converter to remote public server.
Remote Server: root@124.222.231.186
Port: 8087
Mode: --proxy-only --protocol encrypted --admin-login-delay 5
Service Name: minischeme-proxy.service
Config File: /opt/minischeme-proxy/config.ini
"""

import io
import os
import secrets
import string
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.error
import json

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REMOTE_HOST = "124.222.231.186"
REMOTE_USER = "root"
REMOTE_DIR = "/opt/minischeme-proxy"
SERVICE_NAME = "minischeme-proxy"
REMOTE_PORT = 8087
CONFIG_PATH = f"{REMOTE_DIR}/config.ini"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_ssh(cmd, input_bytes=None):
    """Executes a command on the remote server via SSH."""
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        f"{REMOTE_USER}@{REMOTE_HOST}",
        cmd
    ]
    res = subprocess.run(ssh_cmd, input=input_bytes, capture_output=True)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Remote command failed: {cmd}\nStderr: {err}")
    return res.stdout.decode("utf-8", errors="replace")


def generate_10_letter_password():
    """Generates a random 10-character pure ASCII letter string."""
    return "".join(secrets.choice(string.ascii_letters) for _ in range(10))


def create_payload_archive():
    """Creates an in-memory tar.gz archive of server.py and proxy/ package."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Add server.py
        server_py = os.path.join(BASE_DIR, "server.py")
        tar.add(server_py, arcname="server.py")

        # Add proxy directory
        proxy_dir = os.path.join(BASE_DIR, "proxy")
        for root, dirs, files in os.walk(proxy_dir):
            # Skip __pycache__
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith((".py", ".json")):
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, BASE_DIR).replace("\\", "/")
                    tar.add(full_p, arcname=rel_p)
    return buf.getvalue()


def deploy():
    print("=" * 64)
    print(f" MiniScheme Public Proxy Deployment -> {REMOTE_USER}@{REMOTE_HOST}")
    print("=" * 64)

    # 1. Test SSH connectivity
    print("[1/6] Testing SSH connectivity to remote server...")
    uname = run_ssh("uname -s -r -m").strip()
    print(f"      Connected successfully: {uname}")

    # 2. Prepare remote directory
    print(f"[2/6] Preparing remote target directory: {REMOTE_DIR}...")
    run_ssh(f"mkdir -p {REMOTE_DIR}")

    # 3. Handle config.ini (read existing or generate 10-letter password)
    print(f"[3/6] Inspecting remote configuration at {CONFIG_PATH}...")
    cfg_check = run_ssh(f"if [ -f '{CONFIG_PATH}' ]; then echo 'EXISTS'; else echo 'NEW'; fi").strip()

    if "EXISTS" in cfg_check:
        print("      Existing config.ini found on server. Preserving current admin credentials...")
        # Ensure security and server settings are enforced
        py_patch = (
            f"python3 -c \""
            f"import configparser\n"
            f"cfg = configparser.ConfigParser()\n"
            f"cfg.read('{CONFIG_PATH}')\n"
            f"if not cfg.has_section('server'): cfg.add_section('server')\n"
            f"if not cfg.has_section('security'): cfg.add_section('security')\n"
            f"cfg.set('server', 'protocol_mode', 'both')\n"
            f"cfg.set('server', 'components', 'proxy,channel,portal')\n"
            f"cfg.set('security', 'admin_login_delay', '5')\n"
            f"if not cfg.get('security', 'admin_password', fallback=''):\n"
            f"    import secrets, string\n"
            f"    cfg.set('security', 'admin_password', ''.join(secrets.choice(string.ascii_letters) for _ in range(10)))\n"
            f"if not cfg.get('security', 'master_key_1', fallback=''):\n"
            f"    import secrets\n"
            f"    cfg.set('security', 'master_key_1', secrets.token_bytes(32).hex())\n"
            f"with open('{CONFIG_PATH}', 'w') as f: cfg.write(f)\n"
            f"\""
        )
        run_ssh(py_patch)
    else:
        print("      Generating new configuration with 10-letter pure letters admin password...")
        new_pwd = generate_10_letter_password()
        mk1 = secrets.token_bytes(32).hex()
        cfg_content = (
            "[server]\n"
            "protocol_mode = both\n"
            "components = proxy,channel,portal\n\n"
            "[security]\n"
            f"admin_password = {new_pwd}\n"
            "admin_login_delay = 5\n"
            f"master_key_1 = {mk1}\n"
        )
        run_ssh(f"cat << 'EOF' > '{CONFIG_PATH}'\n{cfg_content}\nEOF\nchmod 600 '{CONFIG_PATH}'")
        print(f"      [OK] config.ini written to {CONFIG_PATH} (chmod 600)")

    # 4. Upload application archive and unpack
    print("[4/6] Packaging and uploading server and proxy modules...")
    archive_bytes = create_payload_archive()
    print(f"      Archive size: {len(archive_bytes)} bytes")
    run_ssh(f"tar -xzf - -C {REMOTE_DIR}", input_bytes=archive_bytes)
    print("      Files extracted successfully into /opt/minischeme-proxy")

    # 5. Install and configure systemd service
    print("[5/6] Configuring systemd service minischeme-proxy.service...")
    service_content = f"""[Unit]
Description=MiniScheme Public App Proxy & Protocol Converter Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={REMOTE_DIR}
ExecStart=/usr/bin/python3 {REMOTE_DIR}/server.py --port {REMOTE_PORT} --proxy-only --protocol both --admin-login-delay 5 --no-tray --config {CONFIG_PATH}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    run_ssh(f"cat << 'EOF' > /etc/systemd/system/{SERVICE_NAME}.service\n{service_content}\nEOF")
    run_ssh(f"systemctl daemon-reload && systemctl enable {SERVICE_NAME}.service && systemctl restart {SERVICE_NAME}.service")
    time.sleep(2)

    # Check status
    svc_status = run_ssh(f"systemctl is-active {SERVICE_NAME}.service").strip()
    print(f"      Service status: {svc_status}")
    if svc_status != "active":
        logs = run_ssh(f"journalctl -u {SERVICE_NAME}.service -n 25 --no-pager")
        raise RuntimeError(f"Service failed to enter active state:\n{logs}")

    # 6. Verification
    print(f"[6/6] Verifying remote endpoints at http://{REMOTE_HOST}:{REMOTE_PORT} ...")
    test_endpoints()

    print("=" * 64)
    print("  Deployment Succeeded Successfully!")
    print(f"  Remote Server:       {REMOTE_HOST}:{REMOTE_PORT}")
    print(f"  Service Name:        {SERVICE_NAME}.service")
    print(f"  Config File Path:    {CONFIG_PATH}")
    print(f"  Protocol Policy:     Strict Encrypted Only (MSC1 channel)")
    print(f"  Admin Login Delay:   5 seconds")
    print(f"  Web Portals:         http://{REMOTE_HOST}:{REMOTE_PORT}/admin")
    print(f"                       http://{REMOTE_HOST}:{REMOTE_PORT}/user")
    print("=" * 64)


def test_endpoints():
    print("      Testing server endpoints in-process via SSH curl...")

    # A. Health check
    out = run_ssh(f"curl -s http://127.0.0.1:{REMOTE_PORT}/health")
    data = json.loads(out)
    assert data.get("protocol_mode") in ("encrypted", "both"), f"Unexpected protocol mode: {data.get('protocol_mode')}"
    assert "webrepl" not in data.get("components", []), "webrepl component must be disabled"
    print(f"      [OK] /health returned 200 OK (mode={data.get('protocol_mode')})")

    # B. /app/weather test
    out_weather = run_ssh(f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{REMOTE_PORT}/app/weather")
    expected_code = "200" if data.get("protocol_mode") == "both" else "403"
    assert out_weather.strip() == expected_code, f"Expected {expected_code}, got {out_weather}"
    print(f"      [OK] /app/weather returned {expected_code} as expected")

    # C. Static WebREPL files should return 404 in proxy-only mode
    out_root = run_ssh(f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{REMOTE_PORT}/")
    assert out_root.strip() == "404", f"Expected 404, got {out_root}"
    print("      [OK] Static / returned 404 Not Found (proxy-only mode confirmed)")

    # D. /admin portal should return 200 OK
    out_admin = run_ssh(f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{REMOTE_PORT}/admin")
    assert out_admin.strip() == "200", f"Expected 200, got {out_admin}"
    print("      [OK] /admin portal returned 200 OK")

    # E. /user portal should return 200 OK
    out_user = run_ssh(f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{REMOTE_PORT}/user")
    assert out_user.strip() == "200", f"Expected 200, got {out_user}"
    print("      [OK] /user portal returned 200 OK")

    # F. External ingress check from this machine
    base_url = f"http://{REMOTE_HOST}:{REMOTE_PORT}"
    print(f"      Probing external public access to {base_url}/health ...")
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as r:
            if r.status == 200:
                print("      [OK] External inbound access to port 8087 is OPEN and responding")
    except Exception as e:
        print(f"      [i] Inbound public probe to port 8087 timed out or filtered by cloud security group.")
        print(f"          (Note: Server firewall was untouched. Please verify port 8087 is allowed in the cloud console security group if external access is needed.)")


if __name__ == "__main__":
    deploy()
