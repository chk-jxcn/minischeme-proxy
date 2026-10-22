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
button.secondary { background: #64748b; }
.tab-btn { background: #f1f5f9; color: #475569; border: 1px solid var(--border); border-radius: 6px 6px 0 0; padding: 8px 16px; margin-right: 6px; }
.tab-btn.active { background: var(--card); color: var(--primary); border-bottom-color: var(--card); font-weight: 600; }
table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }
th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); }
th { background: #f1f5f9; color: #475569; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 12px; background: #e0f2fe; color: #0369a1; }
.badge-sim { background: #f3e8ff; color: #7e22ce; }
.masked { font-family: monospace; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; color: #0369a1; }
.hidden { display: none; }
</style>
</head>
<body>
<div class="container">
  <h1>MiniScheme 设备用户控制台</h1>

  <div id="login-section" class="card">
    <div style="border-bottom: 1px solid var(--border); margin-bottom: 18px; display:flex;">
      <button class="tab-btn active" id="tab-hw-btn" onclick="switchLoginTab('hw')">📱 物理硬件设备 (MAC + 应用服务密码)</button>
      <button class="tab-btn" id="tab-sim-btn" onclick="switchLoginTab('sim')">🎮 WASM 模拟器 (UUID 免密登录)</button>
    </div>

    <!-- 硬件设备登录面板 -->
    <div id="login-hw-panel">
      <p style="margin-top:0;">请输入设备的 MAC 地址，以及设备上为这台服务器设置的应用服务密码（v1.0.65 以前的固件用的是硬件密码）：</p>
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <input type="text" id="mac-input" placeholder="MAC地址 (例如 C8F09EB36C90)" style="flex:1; min-width:200px;">
        <input type="password" id="pwd-input" placeholder="应用服务密码" style="flex:1; min-width:200px;">
        <button onclick="doHwLogin()">安全登录</button>
      </div>
    </div>

    <!-- 模拟器免密登录面板 -->
    <div id="login-sim-panel" class="hidden">
      <p style="margin-top:0;">输入 WebREPL 模拟器的独立 UUID（可在 WebREPL 后端服务连接与管理设置中查看与复制）：</p>
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <input type="text" id="sim-uuid-input" placeholder="模拟器 UUID (例如 SIM-A1B2-C3D4)" style="flex:1; min-width:240px; font-family:monospace;">
        <button onclick="doSimLogin()">免密直接登录</button>
      </div>
    </div>

    <div id="login-msg" style="margin-top:12px; color:#ef4444; font-size:14px;"></div>
  </div>

  <div id="dashboard-section" class="card hidden">
    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
      <h2>设备身份: <span id="disp-mac" class="badge"></span> <span id="disp-badge" class="badge"></span></h2>
      <button onclick="doLogout()" class="secondary">退出登录</button>
    </div>

    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:20px;">
      <h3 style="margin:0;">已存储的应用数据</h3>
      <button class="danger" style="padding:6px 14px; font-size:13px;" onclick="clearAllData()">🗑️ 一键清空全部数据</button>
    </div>
    <table id="data-table">
      <thead><tr><th>应用</th><th>键名</th><th>配置值 (敏感密钥已脱敏)</th><th>更新时间</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>

    <h3 style="margin-top:30px;">近期 API 访问审计日志</h3>
    <table id="log-table">
      <thead><tr><th>时间</th><th>来源 IP</th><th>方法</th><th>接口</th><th>状态</th><th>数据量</th></tr></thead>
      <tbody></tbody>
    </table>

    <h3 style="margin-top:30px; color:#ef4444;">危险区域</h3>
    <p>注销设备账户将彻底清除该设备在服务器的所有应用配置与密钥，无法复原：</p>
    <button class="danger" onclick="doDeleteAccount()">注销设备账户</button>
  </div>
</div>

<script>
let curMac = '', sessionToken = '', isSim = false;

function switchLoginTab(tab) {
  const hwBtn = document.getElementById('tab-hw-btn');
  const simBtn = document.getElementById('tab-sim-btn');
  const hwPanel = document.getElementById('login-hw-panel');
  const simPanel = document.getElementById('login-sim-panel');
  document.getElementById('login-msg').innerText = '';
  if (tab === 'sim') {
    hwBtn.classList.remove('active');
    simBtn.classList.add('active');
    hwPanel.classList.add('hidden');
    simPanel.classList.remove('hidden');
  } else {
    simBtn.classList.remove('active');
    hwBtn.classList.add('active');
    simPanel.classList.add('hidden');
    hwPanel.classList.remove('hidden');
  }
}

function doLogin() { return doHwLogin(); }

async function doHwLogin() {
  const macRaw = document.getElementById('mac-input').value.trim();
  const pwd = document.getElementById('pwd-input').value;
  if (!macRaw || !pwd) return alert('请输入 MAC 地址和应用服务密码');
  const mac = macRaw.replace(/[:-]/g, '').toUpperCase();
  const msgEl = document.getElementById('login-msg');
  msgEl.innerText = '正在验证挑战与凭据...';

  try {
    // 1. Get challenge nonce from server
    const rNonce = await fetch('/api/user/nonce?mac=' + encodeURIComponent(mac));
    const dNonce = await rNonce.json();
    if (!dNonce.success) {
      msgEl.innerText = dNonce.error || '获取登录挑战失败';
      return;
    }

    // 2. Prove the password without sending it: SHA-256(hardware hash key || nonce),
    //    the hash key derived here as the server does (HKDF of the password and MAC);
    //    plain JS SHA-256, since the public server is http (no crypto.subtle)
    const hashKey = MiniHash.hwHashKey(pwd, mac);
    const hashHex = MiniHash.hex(MiniHash.sha256(MiniHash.concat(hashKey, MiniHash.utf8(dNonce.nonce))));

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
      isSim = false;
      saveSession();
      showDashboardView();
      loadDashboard();
    } else {
      msgEl.innerText = dAuth.error || '登录失败，密码错误';
    }
  } catch (e) {
    msgEl.innerText = '登录请求异常: ' + e;
  }
}

async function doSimLogin(directUuid) {
  const uuid = (directUuid || document.getElementById('sim-uuid-input').value).trim().toUpperCase();
  if (!uuid) return alert('请输入模拟器 UUID');
  const msgEl = document.getElementById('login-msg');
  msgEl.innerText = '正在连接模拟器会话...';

  try {
    const r = await fetch('/api/user/sim_login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ uuid: uuid })
    });
    const d = await r.json();
    if (d.success) {
      curMac = d.mac || uuid;
      sessionToken = d.token;
      isSim = true;
      saveSession();
      showDashboardView();
      loadDashboard();
    } else {
      msgEl.innerText = d.error || '模拟器登录失败';
    }
  } catch (e) {
    msgEl.innerText = '请求异常: ' + e;
  }
}

function saveSession() {
  localStorage.setItem('minischeme_user_session', JSON.stringify({
    mac: curMac,
    token: sessionToken,
    isSim: isSim
  }));
}

function showDashboardView() {
  document.getElementById('login-section').classList.add('hidden');
  document.getElementById('dashboard-section').classList.remove('hidden');
  document.getElementById('disp-mac').innerText = curMac;
  const bEl = document.getElementById('disp-badge');
  if (isSim) {
    bEl.className = 'badge badge-sim';
    bEl.innerText = '🎮 WASM 模拟器 (UUID)';
  } else {
    bEl.className = 'badge';
    bEl.innerText = '📱 物理硬件设备';
  }
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function authHeaders(extra) {
  return Object.assign({'X-Session-Token': sessionToken}, extra || {});
}

async function loadDashboard() {
  // Load app data
  try {
    const rData = await fetch(`/api/user/data?mac=${encodeURIComponent(curMac)}`, { headers: authHeaders() });
    if (!rData.ok) {
      doLogout();
      return;
    }
    const dData = await rData.json();
    if (!dData.success) {
      doLogout();
      return;
    }
    const tbody = document.querySelector('#data-table tbody');
    tbody.innerHTML = '';
    if (dData.items && dData.items.length) {
      dData.items.forEach(it => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td><b>${esc(it.app_id)}</b></td><td>${esc(it.key)}</td><td><span class="masked">${esc(it.value)}</span></td><td>${new Date(it.updated_at*1000).toLocaleString()}</td><td><button style="padding:4px 8px;font-size:12px;background:#ef4444;">清空此应用</button></td>`;
        tr.querySelector('button').addEventListener('click', () => clearApp(it.app_id));
        tbody.appendChild(tr);
      });
    } else {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#94a3b8;">暂无存储数据</td></tr>';
    }

    // Load logs
    const rLogs = await fetch(`/api/user/logs?mac=${encodeURIComponent(curMac)}`, { headers: authHeaders() });
    const dLogs = await rLogs.json();
    const logTbody = document.querySelector('#log-table tbody');
    logTbody.innerHTML = '';
    if (dLogs.items && dLogs.items.length) {
      dLogs.items.forEach(l => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${new Date(l.timestamp*1000).toLocaleString()}</td><td>${esc(l.ip)}</td><td>${esc(l.method)}</td><td>${esc(l.endpoint)}</td><td>${Number(l.status_code) || 0}</td><td>${Number(l.bytes_transferred) || 0} B</td>`;
        logTbody.appendChild(tr);
      });
    } else {
      logTbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#94a3b8;">暂无访问记录</td></tr>';
    }
  } catch (e) {
    console.warn('Dashboard load error:', e);
  }
}

async function clearApp(appId) {
  if (!confirm(`确认清空应用 ${appId} 的所有数据？`)) return;
  await fetch(`/api/user/data/clear?mac=${encodeURIComponent(curMac)}&app_id=${encodeURIComponent(appId)}`, { method: 'POST', headers: authHeaders() });
  loadDashboard();
}

async function clearAllData() {
  if (!confirm(`确认一键清空该设备 (${curMac}) 的所有应用配置数据？`)) return;
  await fetch(`/api/user/data/clear?mac=${encodeURIComponent(curMac)}&app_id=all`, { method: 'POST', headers: authHeaders() });
  loadDashboard();
}

async function doDeleteAccount() {
  if (!confirm('警告：此操作将永久注销设备账户并清空所有记录，确认继续？')) return;
  await fetch(`/api/user/account/delete?mac=${encodeURIComponent(curMac)}`, { method: 'POST', headers: authHeaders() });
  alert('账户已注销');
  doLogout();
}

function doLogout() {
  localStorage.removeItem('minischeme_user_session');
  try {
    fetch('/api/user/logout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ token: sessionToken, mac: curMac })
    });
  } catch (e) {}
  sessionToken = '';
  curMac = '';
  location.reload();
}

window.addEventListener('DOMContentLoaded', () => {
  // Check URL query parameters for ?uuid=...
  const params = new URLSearchParams(window.location.search);
  const qUuid = params.get('uuid');
  if (qUuid) {
    switchLoginTab('sim');
    document.getElementById('sim-uuid-input').value = qUuid;
    doSimLogin(qUuid);
    return;
  }

  // Check persistent session in localStorage
  const saved = localStorage.getItem('minischeme_user_session');
  if (saved) {
    try {
      const sess = JSON.parse(saved);
      if (sess && sess.mac && sess.token) {
        curMac = sess.mac;
        sessionToken = sess.token;
        isSim = !!sess.isSim;
        showDashboardView();
        loadDashboard();
        return;
      }
    } catch (e) {
      localStorage.removeItem('minischeme_user_session');
    }
  }
});
</script>
</body>
</html>
"""

from .database import normalize_device_id
from .hwkey_js import HWKEY_JS

# The page's own SHA-256 / HKDF (hwkey_js.py) goes in before its script
USER_HTML = USER_HTML.replace("<script>", "<script>\n" + HWKEY_JS + "\n", 1)

_active_nonces: Dict[str, Tuple[str, float]] = {}
_user_sessions: Dict[str, Tuple[str, float]] = {}


def generate_user_nonce(mac: str) -> str:
    clean_mac = normalize_device_id(mac)
    nonce = secrets.token_hex(16)
    _active_nonces[clean_mac] = (nonce, time.time() + 60)
    return nonce


def create_user_session(mac: str) -> str:
    clean_mac = normalize_device_id(mac)
    token = secrets.token_hex(24)
    # 30-day persistent session until manual logout
    _user_sessions[token] = (clean_mac, time.time() + 86400 * 30)
    return token


def verify_user_login(mac: str, client_hash: str, nonce: str, db) -> Tuple[bool, str]:
    clean_mac = normalize_device_id(mac)
    if clean_mac not in _active_nonces:
        return False, "挑战已超时，请刷新重试"
    saved_nonce, exp = _active_nonces.pop(clean_mac)
    if exp < time.time() or saved_nonce != nonce:
        return False, "挑战不匹配或已过期"

    dev = db.get_device(clean_mac)
    if not dev:
        return False, "未找到该设备，请确认设备已首次联网激活"

    expected = hashlib.sha256(dev["hardware_hash_key"] + nonce.encode("ascii")).hexdigest()
    if not secrets.compare_digest(expected, client_hash):
        return False, "应用服务密码校验失败"

    token = create_user_session(clean_mac)
    return True, token


def verify_simulator_login(uuid: str, db, ip: str = "") -> Tuple[bool, str]:
    clean_uuid = normalize_device_id(uuid)
    if not clean_uuid:
        return False, "无效的模拟器 UUID"
    if not clean_uuid.startswith("SIM"):
        clean_uuid = f"SIM-{clean_uuid}"
    dev = db.get_device(clean_uuid)
    if not dev:
        db.register_simulator(clean_uuid, ip)
    else:
        db.update_device_seen(clean_uuid, ip)
    token = create_user_session(clean_uuid)
    return True, token


def check_user_session(token: str, mac: str) -> bool:
    clean_mac = normalize_device_id(mac)
    if token in _user_sessions:
        session_mac, exp = _user_sessions[token]
        if exp > time.time() and session_mac == clean_mac:
            return True
    return False


def remove_user_session(token: str):
    if token in _user_sessions:
        del _user_sessions[token]

