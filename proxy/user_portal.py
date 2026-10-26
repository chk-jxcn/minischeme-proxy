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

.nc { border:1px solid #e2e8f0; border-radius:10px; padding:12px; margin-top:12px; background:#f8fafc; }
.nc-row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px; }
.nc-row input { flex:1; min-width:180px; }
.nc-tools { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; align-items:center; }
.nc-tools button, .nc-tools select, .nc-tools label { padding:4px 10px; font-size:12px; }
.nc-cols { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-start; }
.nc-cols textarea { flex:1; min-width:260px; min-height:220px; font-family:inherit; font-size:14px; padding:8px; box-sizing:border-box; }
.nc-screen { width:320px; min-height:220px; max-height:420px; overflow:auto; background:#0b1118; color:#e6ecf3; border-radius:8px; padding:8px 10px; box-sizing:border-box; font-size:15px; line-height:20px; }
.nc-screen .nr-t { color:#62d0f0; margin-bottom:6px; }
.nr-h { color:#62d0f0; border-bottom:2px solid #62d0f0; padding-bottom:2px; margin:2px 0 4px; }
.nr-li { padding-left:14px; text-indent:-12px; }
.nr-big2 { font-size:30px; line-height:40px; }
.nr-big3 { font-size:45px; line-height:60px; }
.nr-li::before { content:'• '; }
.nr-hr { border:0; border-top:1px solid #688098; margin:6px 0; }
.nr-img { display:block; margin:4px 0; border-radius:4px; }
.nc-foot { display:flex; justify-content:space-between; align-items:center; margin-top:8px; gap:10px; flex-wrap:wrap; }
#note-table .nr-cell { max-width:420px; }
#note-table .nr-cell .nr-h { color:#0a6f9c; border-color:#0a6f9c; }
#note-table .nr-cell .nr-big2 { font-size:20px; line-height:26px; }
#note-table .nr-cell .nr-big3 { font-size:26px; line-height:32px; }
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

    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:30px; flex-wrap:wrap; gap:10px;">
      <h3 style="margin:0;">服药记录 <span id="med-total" class="badge"></span></h3>
      <button class="danger" style="padding:6px 14px; font-size:13px;" onclick="clearMedRecords()">清空服务器上的服药记录</button>
    </div>
    <p style="margin:6px 0 0; color:#64748b; font-size:13px;">吃药提醒应用打开“同步到应用服务器”后上传的记录，按日期从新到旧；以设备上的记录为准，设备上修改后会重新上传。</p>
    <table id="med-table">
      <thead><tr><th>日期</th><th>服药时间</th><th>药品</th><th>用量</th><th>结果</th><th>确认时间</th></tr></thead>
      <tbody></tbody>
    </table>
    <div style="margin-top:10px;"><button id="med-more" class="secondary hidden" onclick="loadMedRecords(true)">加载更多</button></div>

    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:30px; flex-wrap:wrap; gap:10px;">
      <h3 style="margin:0;">消息提示 <span id="note-total" class="badge"></span></h3>
      <button class="danger" style="padding:6px 14px; font-size:13px;" onclick="clearNotes()">清空服务器上的消息</button>
    </div>
    <p style="margin:6px 0 0; color:#64748b; font-size:13px;">推送给这台设备的消息（/notify），服务器全部保留，从新到旧；设备上的“消息提示”应用只保留它设定的最新几条。在下面写好发送，或者用脚本推送：<code>/notify?mac=设备ID&amp;password=应用服务密码&amp;title=标题&amp;text=内容&amp;level=info|warn|urgent</code>（内容可以用同样的标记）。</p>

    <div class="nc" id="note-compose">
      <div class="nc-row">
        <input id="nc-title" maxlength="60" placeholder="标题（可空）">
        <select id="nc-level"><option value="info">通知</option><option value="warn">提醒</option><option value="urgent">紧急（按键才关）</option></select>
      </div>
      <div class="nc-tools">
        <button class="secondary" type="button" onclick="ncLine('# ')" title="这一行作为标题">标题</button>
        <button class="secondary" type="button" onclick="ncLine('## ')" title="这一行两倍大">大字×2</button>
        <button class="secondary" type="button" onclick="ncLine('### ')" title="这一行三倍大">大字×3</button>
        <button class="secondary" type="button" onclick="ncWrap('**', '**')" title="强调（强调色）">强调</button>
        <select id="nc-color" onchange="ncColor(this)" title="给选中的文字上色">
          <option value="">颜色…</option><option value="red">红</option><option value="green">绿</option>
          <option value="yellow">黄</option><option value="blue">蓝</option><option value="gray">灰</option>
        </select>
        <button class="secondary" type="button" onclick="ncLine('- ')" title="列表项">列表</button>
        <button class="secondary" type="button" onclick="ncInsert('\\n---\\n')" title="分隔线">分隔线</button>
        <button class="secondary" type="button" onclick="ncImageUrl()" title="网络图片（服务器取来，按屏幕缩放）">图片链接</button>
        <label class="secondary" style="cursor:pointer; border:1px solid #cbd5e1; border-radius:6px;" title="上传一张图片">上传图片<input type="file" accept="image/*" style="display:none" onchange="ncUpload(this)"></label>
      </div>
      <div class="nc-cols">
        <textarea id="nc-text" maxlength="1000" oninput="ncPreview()" placeholder="# 标题&#10;**强调** 和 {red}红字{/}&#10;- 列表项&#10;---&#10;![](https://图片地址)"></textarea>
        <div><div style="font-size:12px; color:#64748b; margin-bottom:4px;">设备上的样子（预览）</div><div id="nc-preview" class="nc-screen"></div></div>
      </div>
      <div class="nc-foot">
        <span id="nc-status" style="font-size:13px; color:#64748b;">标记：# 标题、## 两倍大字、### 三倍大字、**强调**、{red}颜色{/}（红 绿 黄 蓝 灰）、- 列表、--- 分隔线、![](图片地址)；空行分段</span>
        <button type="button" onclick="ncSend()">发送到设备</button>
      </div>
    </div>
    <table id="note-table">
      <thead><tr><th>时间</th><th>级别</th><th>标题</th><th>内容</th><th>来源</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>
    <div style="margin-top:10px;"><button id="note-more" class="secondary hidden" onclick="loadNotes(true)">加载更多</button></div>

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

    await loadMedRecords(false);
    await loadNotes(false);

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

/* The medicine app's records, 100 at a time */
let medOffset = 0;
const MED_HOW = {ok: '已服', miss: '超时未服', skip: '未服', auto: '超时（记为已服）'};
function medDate(day) {
  const d = new Date(day * 86400000);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}
function medAmount(a, unit) {
  const n = Number(a) || 0;
  return (n % 10 ? (n / 10).toFixed(1) : String(n / 10)) + (unit ? ' ' + unit : '');
}
async function loadMedRecords(more) {
  if (!more) medOffset = 0;
  const r = await fetch(`/api/user/med_records?mac=${encodeURIComponent(curMac)}&off=${medOffset}&lim=100`, { headers: authHeaders() });
  const d = await r.json();
  if (!d.success) return;
  const tbody = document.querySelector('#med-table tbody');
  if (!more) tbody.innerHTML = '';
  (d.items || []).forEach(x => {
    const tr = document.createElement('tr');
    const how = MED_HOW[x.how] || x.how;
    tr.innerHTML = `<td>${medDate(x.day)}</td><td>${esc(x.slot)}</td><td>${esc(x.name)}</td><td>${esc(medAmount(x.amount, x.unit))}</td>` +
                   `<td style="color:${x.how === 'ok' || x.how === 'auto' ? '#16a34a' : '#dc2626'}">${esc(how)}</td><td>${esc(x.at || '')}</td>`;
    tbody.appendChild(tr);
  });
  medOffset += (d.items || []).length;
  if (!medOffset) tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#94a3b8;">没有服药记录</td></tr>';
  document.getElementById('med-total').innerText = d.total ? `共 ${d.total} 条` : '';
  document.getElementById('med-more').classList.toggle('hidden', medOffset >= (d.total || 0));
}

/* Rich messages (proxy/notes_rich.py): the markup -> the board's encoding -> HTML */
const NC_COLOR = {a: '#62d0f0', r: '#ee7c7c', g: '#72d696', y: '#f0bc55', b: '#5a96fa', m: '#a2b2c6', n: ''};
const NC_NAMES = {red: 'r', '红': 'r', green: 'g', '绿': 'g', yellow: 'y', '黄': 'y', blue: 'b', '蓝': 'b', gray: 'm', grey: 'm', '灰': 'm'};
const ncImages = {};   // uploaded pictures: id -> [width, height]
function ncInline(s) {
  const re = /\\*\\*|\\{\\/\\}|\\{([a-z]+|[红绿黄蓝灰])\\}/g;
  let out = '', pos = 0, color = 'n', strong = false, m;
  while ((m = re.exec(s))) {
    out += s.slice(pos, m.index).split('^').join('^^');
    if (m[0] === '**') strong = !strong;
    else if (m[0] === '{/}') color = 'n';
    else if (NC_NAMES[m[1]]) color = NC_NAMES[m[1]];
    else { out += m[0].split('^').join('^^'); pos = re.lastIndex; continue; }
    out += '^' + (strong ? 'a' : color);
    pos = re.lastIndex;
  }
  return out + s.slice(pos).split('^').join('^^');
}
/* a picture inside a line goes on a line of its own there (as the server does) */
function ncPictureLines(text) {
  const out = [];
  for (const line of text.split('\\n')) {
    const re = /!\\[[^\\]]*\\]\\(\\s*[^)\\s]+\\s*\\)/g;
    let pos = 0, m;
    while ((m = re.exec(line))) {
      if (line.slice(pos, m.index).trim()) out.push(line.slice(pos, m.index).replace(/\\s+$/, ''));
      out.push(m[0]);
      pos = re.lastIndex;
    }
    if (pos === 0) out.push(line);
    else if (line.slice(pos).trim()) out.push(line.slice(pos).trim());
  }
  return out;
}
/* the preview's encoding: pictures by URL stay URLs (^u...;), uploaded ones are ^i */
function ncEncode(text) {
  return '^!' + ncPictureLines(text).map(raw => {
    const s = raw.replace(/\\s+$/, ''), bare = s.trim();
    const img = /^!\\[[^\\]]*\\]\\(\\s*([^)\\s]+)\\s*\\)$/.exec(bare);
    if (['---', '***', '___'].includes(bare)) return '^=';
    if (img) {
      const src = img[1];
      if (src.startsWith('id:')) { const wh = ncImages[src.slice(3)] || [320, 180]; return `^i${src.slice(3)}:${wh[0]}x${wh[1]};`; }
      return /^https?:/.test(src) ? `^u${src};` : '^m' + ncInline('[图片无法载入]');
    }
    if (bare.startsWith('#')) {
      const level = Math.min(3, bare.length - bare.replace(/^#+/, '').length);
      return ['^#', '^2', '^3'][level - 1] + ncInline(bare.replace(/^#+/, '').trim());
    }
    if (/^(- |\\* |• )/.test(bare)) return '^-' + ncInline(bare.slice(2).trim());
    return ncInline(s);
  }).join('^/');
}
function ncSpans(s) {
  let out = '', cur = '', color = 'n';
  const flush = () => { if (cur) out += NC_COLOR[color] ? `<span style="color:${NC_COLOR[color]}">${esc(cur)}</span>` : esc(cur); cur = ''; };
  for (let i = 0; i < s.length; i++) {
    if (s[i] === '^' && i + 1 < s.length) {
      const c = s[++i];
      if (c === '^') { cur += '^'; continue; }
      flush(); color = c; continue;
    }
    cur += s[i];
  }
  flush();
  return out;
}
function richHtml(enc, width) {
  if (!String(enc || '').startsWith('^!')) return esc(enc);
  return enc.slice(2).split('^/').map(line => {
    if (line.startsWith('^=')) return '<hr class="nr-hr">';
    let m = /^\\^i([0-9a-f]{16}):(\\d+)x(\\d+);/.exec(line);
    if (m) {
      const w = width, h = Math.max(16, Math.min(200, Math.round(width * Number(m[3]) / Number(m[2]))));
      return `<img class="nr-img" src="/app/notifier/pic?id=${m[1]}&w=${w}&h=${h}" width="${w}" height="${h}" alt="图片">`;
    }
    m = /^\\^u(.+);$/.exec(line);
    if (m) return `<img class="nr-img" src="${esc(m[1])}" style="max-width:${width}px; max-height:200px;" alt="图片">`;
    if (line.startsWith('^#')) return `<div class="nr-h">${ncSpans(line.slice(2))}</div>`;
    if (line.startsWith('^2') || line.startsWith('^3')) return `<div class="nr-big${line[1]}">${ncSpans(line.slice(2)) || '&nbsp;'}</div>`;
    if (line.startsWith('^-')) return `<div class="nr-li">${ncSpans(line.slice(2))}</div>`;
    return `<div>${ncSpans(line) || '&nbsp;'}</div>`;
  }).join('');
}
function ncPreview() {
  const title = document.getElementById('nc-title').value.trim();
  const text = document.getElementById('nc-text').value;
  document.getElementById('nc-preview').innerHTML = (title ? `<div class="nr-t">${esc(title)}</div>` : '') +
    (text.trim() ? richHtml(ncEncode(text), 296) : '<div style="color:#64748b">（空）</div>');
}
function ncInsert(s) {
  const t = document.getElementById('nc-text'), a = t.selectionStart, b = t.selectionEnd;
  t.value = t.value.slice(0, a) + s + t.value.slice(b);
  t.selectionStart = t.selectionEnd = a + s.length;
  t.focus(); ncPreview();
}
function ncWrap(open, close) {
  const t = document.getElementById('nc-text'), a = t.selectionStart, b = t.selectionEnd;
  const sel = t.value.slice(a, b) || '文字';
  t.value = t.value.slice(0, a) + open + sel + close + t.value.slice(b);
  t.selectionStart = a + open.length; t.selectionEnd = a + open.length + sel.length;
  t.focus(); ncPreview();
}
function ncLine(prefix) {
  const t = document.getElementById('nc-text'), a = t.selectionStart;
  const start = t.value.lastIndexOf('\\n', a - 1) + 1;
  t.value = t.value.slice(0, start) + prefix + t.value.slice(start);
  t.selectionStart = t.selectionEnd = a + prefix.length;
  t.focus(); ncPreview();
}
function ncColor(sel) { if (sel.value) ncWrap(`{${sel.value}}`, '{/}'); sel.value = ''; }
function ncImageUrl() {
  const url = prompt('图片地址（http:// 或 https://，服务器取来后按屏幕缩放）');
  if (url) ncInsert(`![](${url.trim()})`);
}
async function ncUpload(input) {
  const f = input.files && input.files[0];
  input.value = '';
  if (!f) return;
  const st = document.getElementById('nc-status');
  st.textContent = '上传图片…';
  try {
    const r = await fetch(`/notify/image?mac=${encodeURIComponent(curMac)}`, { method: 'POST', headers: authHeaders(), body: f });
    const d = await r.json();
    if (!d.success) throw new Error(d.error || r.status);
    ncImages[d.id] = [d.width, d.height];
    ncInsert(`![](id:${d.id})`);
    st.textContent = `已上传图片 ${d.width}×${d.height}`;
  } catch (e) { st.textContent = '上传失败：' + (e.message || e); }
}
async function ncSend() {
  const st = document.getElementById('nc-status');
  const text = document.getElementById('nc-text').value.trim(), title = document.getElementById('nc-title').value.trim();
  if (!text && !title) { st.textContent = '标题和内容都是空的'; return; }
  st.textContent = '发送中…';
  try {
    const r = await fetch(`/notify?mac=${encodeURIComponent(curMac)}`, {
      method: 'POST', headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({title, text, level: document.getElementById('nc-level').value, from: '用户门户'})});
    const d = await r.json();
    if (!d.success) throw new Error(d.error || r.status);
    st.textContent = '已发送，设备几秒内收到';
    document.getElementById('nc-text').value = ''; document.getElementById('nc-title').value = '';
    ncPreview(); loadNotes(false);
  } catch (e) { st.textContent = '发送失败：' + (e.message || e); }
}
let noteOffset = 0;
const NOTE_LEVEL = {info: '通知', warn: '提醒', urgent: '紧急'};
function noteTime(sec) {
  const d = new Date(sec * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
         `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
async function loadNotes(more) {
  if (!more) noteOffset = 0;
  const r = await fetch(`/api/user/notes?mac=${encodeURIComponent(curMac)}&off=${noteOffset}&lim=50`, { headers: authHeaders() });
  const d = await r.json();
  if (!d.success) return;
  const tbody = document.querySelector('#note-table tbody');
  if (!more) tbody.innerHTML = '';
  (d.items || []).forEach(x => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${noteTime(x.created)}</td><td style="color:${x.level === 'urgent' ? '#dc2626' : x.level === 'warn' ? '#d97706' : '#2563eb'}">${esc(NOTE_LEVEL[x.level] || x.level)}</td>` +
                   `<td>${esc(x.title)}</td><td class="nr-cell">${richHtml(x.text, 160)}</td><td>${esc(x.source)}</td>` +
                   `<td><button class="secondary" style="padding:3px 10px;font-size:12px;" onclick="deleteNote(${Number(x.id)})">删除</button></td>`;
    tbody.appendChild(tr);
  });
  noteOffset += (d.items || []).length;
  if (!noteOffset) tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#94a3b8;">没有消息</td></tr>';
  document.getElementById('note-total').innerText = d.total ? `共 ${d.total} 条` : '';
  document.getElementById('note-more').classList.toggle('hidden', noteOffset >= (d.total || 0));
}
async function deleteNote(id) {
  await fetch(`/api/user/data/clear?mac=${encodeURIComponent(curMac)}&app_id=notifier&id=${id}`, { method: 'POST', headers: authHeaders() });
  loadNotes(false);
}
async function clearNotes() {
  if (!confirm('确认删除服务器上这台设备的全部消息？设备上已经收到的消息不受影响。')) return;
  await fetch(`/api/user/data/clear?mac=${encodeURIComponent(curMac)}&app_id=notifier`, { method: 'POST', headers: authHeaders() });
  loadNotes(false);
}

async function clearMedRecords() {
  if (!confirm('确认清空服务器上这台设备的全部服药记录？设备上的记录不受影响，但已经上传过的不会再上传。')) return;
  await fetch(`/api/user/data/clear?mac=${encodeURIComponent(curMac)}&app_id=medicine`, { method: 'POST', headers: authHeaders() });
  loadDashboard();
}

async function clearApp(appId) {
  if (!confirm(`确认清空应用 ${appId} 的所有数据？`)) return;
  await fetch(`/api/user/data/clear?mac=${encodeURIComponent(curMac)}&app_id=${encodeURIComponent(appId)}`, { method: 'POST', headers: authHeaders() });
  loadDashboard();
}

async function clearAllData() {
  if (!confirm(`确认一键清空该设备 (${curMac}) 的所有应用数据？服务器上的服药记录也会一起删除。`)) return;
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

