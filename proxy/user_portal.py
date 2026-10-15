"""User Web Portal for MiniScheme device owners."""

import hashlib
import json
import secrets
import time
from typing import Dict, Tuple

USER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MiniScheme 设备用户控制台</title>
<style>
:root { --primary: #0284c7; --bg: #f8fafc; --card: #ffffff; --text: #1e293b; --border: #e2e8f0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
.container { max-width: 900px; margin: 0 auto; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
h1, h2, h3 { margin-top: 0; color: #0f172a; }
input, button, select { padding: 10px 14px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; }
button { background: var(--primary); color: white; border: none; cursor: pointer; font-weight: 500; }
button:hover { opacity: 0.9; }
button.danger { background: #ef4444; }
table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }
th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); }
th { background: #f1f5f9; color: #475569; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 12px; background: #e0f2fe; color: #0369a1; }
.masked { font-family: monospace; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
.hidden { display: none; }
</style>
</head>
<body>
<div class="container">
  <h1>MiniScheme 设备用户控制台</h1>

  <div id="login-section" class="card">
    <h2>设备登录</h2>
    <p>请输入硬件背面的 MAC 地址与初次配网设置的硬件密码：</p>
    <div style="display:flex; gap:10px; flex-wrap:wrap;">
      <input type="text" id="mac-input" placeholder="MAC地址 (例如 C8F09EB36C90)" style="flex:1;">
      <input type="password" id="pwd-input" placeholder="硬件密码" style="flex:1;">
      <button onclick="doLogin()">安全登录</button>
    </div>
    <div id="login-msg" style="margin-top:10px; color:#ef4444;"></div>
  </div>

  <div id="dashboard-section" class="card hidden">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h2>设备信息: <span id="disp-mac" class="badge"></span></h2>
      <button onclick="doLogout()" style="background:#64748b;">退出登录</button>
    </div>

    <h3>已存储的应用数据</h3>
    <table id="data-table">
      <thead><tr><th>应用</th><th>键名</th><th>配置值 (敏感信息已脱敏)</th><th>更新时间</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>

    <h3 style="margin-top:30px;">近期 API 访问审计日志</h3>
    <table id="log-table">
      <thead><tr><th>时间</th><th>来源 IP</th><th>方法</th><th>接口</th><th>状态</th><th>数据量</th></tr></thead>
      <tbody></tbody>
    </table>

    <h3 style="margin-top:30px; color:#ef4444;">危险区域</h3>
    <p>注销设备账户将彻底清除该设备在公共服务器的所有应用配置与密钥，无法复原：</p>
    <button class="danger" onclick="doDeleteAccount()">注销设备账户</button>
  </div>
</div>

<script>
let curMac = '', sessionToken = '';

async function doLogin() {
  const mac = document.getElementById('mac-input').value.trim().toUpperCase().replace(/[:-]/g, '');
  const pwd = document.getElementById('pwd-input').value;
  if (!mac || !pwd) return alert('请输入 MAC 地址和硬件密码');

  // 1. Get challenge nonce from server
  const rNonce = await fetch('/api/user/nonce?mac=' + mac);
  const dNonce = await rNonce.json();
  if (!dNonce.success) {
    document.getElementById('login-msg').innerText = dNonce.error || '获取登录挑战失败';
    return;
  }

  // 2. Hash password with nonce client-side (no plaintext on wire)
  const enc = new TextEncoder();
  const data = enc.encode(pwd + dNonce.nonce);
  const hashBuf = await crypto.subtle.digest('SHA-256', data);
  const hashHex = Array.from(new Uint8Array(hashBuf)).map(b => b.toString(16).padStart(2, '0')).join('');

  // 3. Authenticate
  const rAuth = await fetch('/api/user/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ mac: mac, hash: hashHex, nonce: dNonce.nonce })
  });
  const dAuth = await rAuth.json();
  if (dAuth.success) {
    curMac = mac;
    sessionToken = dAuth.token;
    document.getElementById('login-section').classList.add('hidden');
    document.getElementById('dashboard-section').classList.remove('hidden');
    document.getElementById('disp-mac').innerText = mac;
    loadDashboard();
  } else {
    document.getElementById('login-msg').innerText = dAuth.error || '登录失败，密码错误';
  }
}

async function loadDashboard() {
  // Load data
  const rData = await fetch(`/api/user/data?mac=${curMac}&token=${sessionToken}`);
  const dData = await rData.json();
  const tbody = document.querySelector('#data-table tbody');
  tbody.innerHTML = '';
  if (dData.items && dData.items.length) {
    dData.items.forEach(it => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><b>${it.app_id}</b></td><td>${it.key}</td><td><span class="masked">${it.value}</span></td><td>${new Date(it.updated_at*1000).toLocaleString()}</td><td><button style="padding:4px 8px;font-size:12px;background:#ef4444;" onclick="clearApp('${it.app_id}')">清空此应用</button></td>`;
      tbody.appendChild(tr);
    });
  } else {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#94a3b8;">暂无存储数据</td></tr>';
  }

  // Load logs
  const rLogs = await fetch(`/api/user/logs?mac=${curMac}&token=${sessionToken}`);
  const dLogs = await rLogs.json();
  const logTbody = document.querySelector('#log-table tbody');
  logTbody.innerHTML = '';
  if (dLogs.items && dLogs.items.length) {
    dLogs.items.forEach(l => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${new Date(l.timestamp*1000).toLocaleString()}</td><td>${l.ip}</td><td>${l.method}</td><td>${l.endpoint}</td><td>${l.status_code}</td><td>${l.bytes_transferred} B</td>`;
      logTbody.appendChild(tr);
    });
  } else {
    logTbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#94a3b8;">暂无访问记录</td></tr>';
  }
}

async function clearApp(appId) {
  if (!confirm(`确认清空应用 ${appId} 的所有数据？`)) return;
  await fetch(`/api/user/data/clear?mac=${curMac}&app_id=${appId}&token=${sessionToken}`, { method: 'POST' });
  loadDashboard();
}

async function doDeleteAccount() {
  if (!confirm('警告：此操作将永久注销设备账户并清空所有记录，确认继续？')) return;
  await fetch(`/api/user/account/delete?mac=${curMac}&token=${sessionToken}`, { method: 'POST' });
  alert('账户已注销');
  location.reload();
}

function doLogout() {
  location.reload();
}
</script>
</body>
</html>
"""

_active_nonces: Dict[str, Tuple[str, float]] = {}
_user_sessions: Dict[str, Tuple[str, float]] = {}


def generate_user_nonce(mac: str) -> str:
    clean_mac = mac.replace(":", "").replace("-", "").upper()
    nonce = secrets.token_hex(16)
    _active_nonces[clean_mac] = (nonce, time.time() + 60)
    return nonce


def verify_user_login(mac: str, client_hash: str, nonce: str, db) -> Tuple[bool, str]:
    clean_mac = mac.replace(":", "").replace("-", "").upper()
    if clean_mac not in _active_nonces:
        return False, "挑战已超时，请刷新重试"
    saved_nonce, exp = _active_nonces.pop(clean_mac)
    if exp < time.time() or saved_nonce != nonce:
        return False, "挑战不匹配或已过期"

    dev = db.get_device(clean_mac)
    if not dev:
        return False, "未找到该设备，请确认设备已首次联网激活"

    # Verify client hash against device's hardware hash key
    # client_hash was SHA256(pwd + nonce). On server:
    # We can check HMAC of hw_hash_key with nonce
    expected = hashlib.sha256(dev["hardware_hash_key"] + nonce.encode("ascii")).hexdigest()
    if not secrets.compare_digest(expected, client_hash):
        return False, "硬件密码校验失败"

    token = secrets.token_hex(24)
    _user_sessions[token] = (clean_mac, time.time() + 3600)
    return True, token


def check_user_session(token: str, mac: str) -> bool:
    clean_mac = mac.replace(":", "").replace("-", "").upper()
    if token in _user_sessions:
        session_mac, exp = _user_sessions[token]
        if exp > time.time() and session_mac == clean_mac:
            return True
    return False
