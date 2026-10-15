"""Administrator Web Portal for MiniScheme proxy management."""

import json
import secrets
import time
from typing import Dict, Tuple

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MiniScheme 代理服务管理后台</title>
<style>
:root { --primary: #4f46e5; --bg: #f8fafc; --card: #ffffff; --text: #1e293b; --border: #e2e8f0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
.container { max-width: 1100px; margin: 0 auto; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
h1, h2, h3 { margin-top: 0; color: #0f172a; }
input, button, select { padding: 9px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; }
button { background: var(--primary); color: white; border: none; cursor: pointer; font-weight: 500; }
button:hover { opacity: 0.9; }
button.secondary { background: #64748b; }
table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }
th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); }
th { background: #f1f5f9; color: #475569; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 12px; background: #e0e7ff; color: #3730a3; }
.hidden { display: none; }
pre { background: #f1f5f9; padding: 10px; border-radius: 6px; font-size: 13px; overflow-x: auto; }
</style>
</head>
<body>
<div class="container">
  <h1>MiniScheme 代理服务管理后台</h1>

  <div id="login-section" class="card">
    <h2>管理员登录</h2>
    <div style="display:flex; gap:10px; max-width:400px;">
      <input type="password" id="admin-pwd" placeholder="管理员主密码" style="flex:1;">
      <button onclick="doAdminLogin()">登录后台</button>
    </div>
    <div id="login-msg" style="margin-top:10px; color:#ef4444;"></div>
  </div>

  <div id="dashboard-section" class="card hidden">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <h2>全量设备与租户审计</h2>
      <div>
        <button onclick="showPresetModal()">预设用户数据</button>
        <button class="secondary" onclick="doLogout()">退出</button>
      </div>
    </div>

    <table id="device-table">
      <thead><tr><th>设备 MAC</th><th>注册时间</th><th>最后在线</th><th>最后来源 IP</th><th>状态</th><th>数据占用</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>

    <div id="detail-modal" class="card hidden" style="margin-top:25px; border-top: 2px solid var(--primary);">
      <h3>设备明文数据查看与管理: <span id="detail-mac" class="badge"></span></h3>
      <table id="detail-table">
        <thead><tr><th>应用</th><th>配置键</th><th>明文数据</th><th>更新时间</th></tr></thead>
        <tbody></tbody>
      </table>
      <button class="secondary" style="margin-top:15px;" onclick="closeDetail()">关闭详情</button>
    </div>

    <div id="preset-modal" class="card hidden" style="margin-top:25px; border-top: 2px solid #10b981;">
      <h3>预设用户数据 (设备上线前配置)</h3>
      <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px; max-width:600px;">
        <input type="text" id="preset-mac" placeholder="目标设备 MAC (例如 C8F09EB36C90)">
        <input type="text" id="preset-app" placeholder="应用 ID (例如 weather)">
        <input type="text" id="preset-key" placeholder="配置键 (例如 api_key)">
        <input type="text" id="preset-val" placeholder="配置明文值">
      </div>
      <div style="margin-top:15px;">
        <button onclick="savePreset()">保存预设数据</button>
        <button class="secondary" onclick="closePreset()">取消</button>
      </div>
    </div>
  </div>
</div>

<script>
let adminToken = '';

async function doAdminLogin() {
  const pwd = document.getElementById('admin-pwd').value;
  const r = await fetch('/api/admin/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ password: pwd })
  });
  const d = await r.json();
  if (d.success) {
    adminToken = d.token;
    document.getElementById('login-section').classList.add('hidden');
    document.getElementById('dashboard-section').classList.remove('hidden');
    loadDevices();
  } else {
    document.getElementById('login-msg').innerText = d.error || '登录失败，密码错误';
  }
}

async function loadDevices() {
  const r = await fetch('/api/admin/devices?token=' + adminToken);
  const d = await r.json();
  const tbody = document.querySelector('#device-table tbody');
  tbody.innerHTML = '';
  if (d.devices && d.devices.length) {
    d.devices.forEach(dev => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><b>${dev.mac}</b></td><td>${new Date(dev.created_at*1000).toLocaleString()}</td><td>${new Date(dev.last_seen_at*1000).toLocaleString()}</td><td>${dev.last_ip || '-'}</td><td><span class="badge">${dev.status}</span></td><td>${dev.data_bytes} B</td><td><button style="padding:4px 8px;font-size:12px;" onclick="viewDetail('${dev.mac}')">明文审阅</button></td>`;
      tbody.appendChild(tr);
    });
  } else {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#94a3b8;">暂无注册设备</td></tr>';
  }
}

async function viewDetail(mac) {
  const r = await fetch(`/api/admin/device/data?mac=${mac}&token=${adminToken}`);
  const d = await r.json();
  document.getElementById('detail-mac').innerText = mac;
  const tbody = document.querySelector('#detail-table tbody');
  tbody.innerHTML = '';
  if (d.items && d.items.length) {
    d.items.forEach(it => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><b>${it.app_id}</b></td><td>${it.key}</td><td><code>${it.value}</code></td><td>${new Date(it.updated_at*1000).toLocaleString()}</td>`;
      tbody.appendChild(tr);
    });
  } else {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#94a3b8;">该设备无存储数据</td></tr>';
  }
  document.getElementById('detail-modal').classList.remove('hidden');
}

function closeDetail() {
  document.getElementById('detail-modal').classList.add('hidden');
}

function showPresetModal() {
  document.getElementById('preset-modal').classList.remove('hidden');
}

function closePreset() {
  document.getElementById('preset-modal').classList.add('hidden');
}

async function savePreset() {
  const mac = document.getElementById('preset-mac').value.trim();
  const app = document.getElementById('preset-app').value.trim();
  const key = document.getElementById('preset-key').value.trim();
  const val = document.getElementById('preset-val').value.trim();
  if (!mac || !app || !key) return alert('请完整填写预设信息');

  const r = await fetch('/api/admin/preset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ token: adminToken, mac, app_id: app, key, value: val })
  });
  const d = await r.json();
  if (d.success) {
    alert('预设保存成功');
    closePreset();
    loadDevices();
  } else {
    alert(d.error || '保存失败');
  }
}

function doLogout() {
  location.reload();
}
</script>
</body>
</html>
"""

_admin_sessions: Dict[str, float] = {}


def create_admin_session() -> str:
    token = secrets.token_hex(24)
    _admin_sessions[token] = time.time() + 7200
    return token


def verify_admin_session(token: str) -> bool:
    if token in _admin_sessions:
        if _admin_sessions[token] > time.time():
            return True
        del _admin_sessions[token]
    return False
