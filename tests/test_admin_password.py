"""The local service's admin password: the default "admin", a change from
the portal written into config.ini, and an edit of config.ini applying at
the next login (server.py and app_proxy.py)."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def call(port, path, body=None, token=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json", "X-Session-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class AdminPasswordBase:
    """Subclasses set command(port, config) for the server under test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.ini")
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("; kept comment\n[security]\n; the portal password\nadmin_password =\n")
        self.port = free_port()
        env = dict(os.environ)
        env.pop("MINISCHEME_ADMIN_PASSWORD", None)
        self.proc = subprocess.Popen(self.command(self.port, self.config), cwd=ROOT_DIR, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 8:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1):
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def test_default_change_and_file_edit(self):
        st, d = call(self.port, "/api/admin/status")
        self.assertTrue(d["auth_required"])
        self.assertTrue(d["default_password"])
        self.assertTrue(d["can_change_password"])
        self.assertEqual(call(self.port, "/api/admin/login", {"password": "wrong"})[0], 403)
        st, d = call(self.port, "/api/admin/login", {"password": "admin"})
        self.assertTrue(d["success"])
        token = d["token"]
        # weak, default and wrong-old passwords are refused
        self.assertEqual(call(self.port, "/api/admin/password", {"old": "admin", "new": "abc"}, token)[0], 400)
        self.assertEqual(call(self.port, "/api/admin/password", {"old": "admin", "new": "admin"}, token)[0], 400)
        self.assertEqual(call(self.port, "/api/admin/password", {"old": "nope", "new": "s3cret-pw"}, token)[0], 403)
        self.assertEqual(call(self.port, "/api/admin/password", {"old": "admin", "new": "s3cret-pw"})[0], 401)
        st, d = call(self.port, "/api/admin/password", {"old": "admin", "new": "s3cret-pw"}, token)
        self.assertTrue(d["success"], d)
        text = open(self.config, encoding="utf-8").read()
        self.assertIn("admin_password = s3cret-pw", text)
        self.assertIn("; kept comment", text)
        self.assertIn("; the portal password", text)
        self.assertEqual(call(self.port, "/api/admin/login", {"password": "admin"})[0], 403)
        self.assertTrue(call(self.port, "/api/admin/login", {"password": "s3cret-pw"})[1]["success"])
        self.assertFalse(call(self.port, "/api/admin/status")[1]["default_password"])
        # an edit of config.ini applies at the next login, no restart
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("[security]\nadmin_password = edited-pw\n")
        self.assertEqual(call(self.port, "/api/admin/login", {"password": "s3cret-pw"})[0], 403)
        self.assertTrue(call(self.port, "/api/admin/login", {"password": "edited-pw"})[1]["success"])


class TestWebReplServerAdminPassword(AdminPasswordBase, unittest.TestCase):
    def command(self, port, config):
        return [sys.executable, "server.py", "--port", str(port), "--no-tray", "--config", config]


class TestPublicDeploymentHasNoDefault(AdminPasswordBase, unittest.TestCase):
    """The proxy-only service (the public server) never takes "admin"."""

    def command(self, port, config):
        return [sys.executable, "server.py", "--port", str(port), "--no-tray", "--config", config,
                "--proxy-only", "--protocol", "both"]

    def test_default_change_and_file_edit(self):
        d = call(self.port, "/api/admin/status")[1]
        self.assertFalse(d["default_password"])
        self.assertFalse(d["can_change_password"])
        self.assertEqual(call(self.port, "/api/admin/login", {"password": "admin"})[0], 403)
        self.assertEqual(call(self.port, "/api/admin/login", {"password": ""})[0], 403)


class TestAppProxyAdminPassword(AdminPasswordBase, unittest.TestCase):
    def command(self, port, config):
        return [sys.executable, "app_proxy.py", "--mode", "local", "--port", str(port), "--config", config,
                "--db", os.path.join(os.path.dirname(config), "test.db")]


if __name__ == "__main__":
    unittest.main()
