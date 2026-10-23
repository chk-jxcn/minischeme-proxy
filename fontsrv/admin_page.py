"""The font server's administration page (served at /)."""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MiniScheme 字体服务器</title>
<style>
:root{--bg:#0b1118;--card:#121b26;--line:#233244;--text:#dce6f0;--muted:#8a9bb0;--accent:#4fc3f7;--bad:#ef6b6b;--ok:#5fd38d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Microsoft YaHei",sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 2px}
.sub{color:var(--muted);margin:0 0 14px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
h2{font-size:15px;margin:0 0 10px;color:var(--accent)}
input,select,button{font:inherit;color:var(--text);background:#0e151e;border:1px solid var(--line);border-radius:6px;padding:6px 9px}
button{cursor:pointer;background:#16324a;border-color:#2b5a80}
button:hover{border-color:var(--accent)}
button.plain{background:transparent}
button.danger{background:#3a1c1c;border-color:#7a3030}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:500;font-size:13px}
.tag{display:inline-block;font-size:12px;padding:0 6px;border-radius:4px;border:1px solid var(--line);color:var(--muted);margin-right:3px}
.tag.on{color:var(--ok);border-color:#2f6b48}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.muted{color:var(--muted)}
#msg{min-height:20px;margin-top:6px}
#msg.bad{color:var(--bad)} #msg.ok{color:var(--ok)}
#preview{margin-top:10px;max-width:100%;image-rendering:pixelated;border:1px solid var(--line);border-radius:6px}
.scroll{overflow-x:auto}
@media (max-width:640px){.hide-s{display:none}}
</style>
</head>
<body>
<main>
<h1>MiniScheme 字体服务器</h1>
<p class="sub">板子缺字时到这里取字形：先用板子报告的字体（如 simsun），再按下面"启用"的顺序找。取字形不需要密码；管理字体需要登录。</p>

<section id="login-box">
  <h2>登录</h2>
  <div class="row"><input id="pw" type="password" placeholder="管理员密码"><button onclick="login()">登录</button></div>
</section>

<section id="fonts-box" hidden>
  <h2>字体</h2>
  <p class="muted">勾选"启用"的字体按顺序补字：一个字依次找，第一个有这个字的字体给出字形。改动后点"保存顺序"。板子发现服务器的字体变了，会自动丢掉旧字形重新取。</p>
  <div class="row" style="margin-bottom:8px">
    <input id="file" type="file" accept=".ttf,.ttc,.otf,.otc">
    <button onclick="upload()">上传字体</button>
    <button class="plain" onclick="save()">保存顺序</button>
    <label class="muted"><input id="show-system" type="checkbox" onchange="render()"> 显示本机字体</label>
  </div>
  <div class="scroll"><table>
    <thead><tr><th>启用</th><th>字体</th><th class="hide-s">文件</th><th>字数</th><th>包含</th><th></th></tr></thead>
    <tbody id="list"></tbody>
  </table></div>
</section>

<section>
  <h2>预览</h2>
  <div class="row">
    <input id="face" value="simsun" size="10" title="板子报告的字体">
    <select id="size"><option>12</option><option selected>16</option><option>24</option></select>
    <input id="text" value="中文 繁體 한국어 日本語 😀⚡" style="flex:1;min-width:180px">
    <button onclick="preview()">预览</button>
  </div>
  <img id="preview" alt="">
</section>
<div id="msg" role="status"></div>
</main>
<script>
let token = sessionStorage.getItem('fs_token') || '';
let fonts = [], order = [];
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function say(text, kind) { $('msg').textContent = text || ''; $('msg').className = kind || ''; }

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'Authorization': 'Bearer ' + token}, opts.headers || {});
  const r = await fetch(path, opts);
  const v = await r.json().catch(() => ({success: false, error: 'HTTP ' + r.status}));
  if (r.status === 401) { showLogin(); }
  if (!r.ok || v.success === false) throw new Error(v.error || ('HTTP ' + r.status));
  return v;
}

function showLogin() { $('login-box').hidden = false; $('fonts-box').hidden = true; }

async function login() {
  try {
    const r = await fetch('/admin/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                           body: JSON.stringify({password: $('pw').value})});
    const v = await r.json();
    if (!v.success) throw new Error(v.error || '登录失败');
    token = v.token; sessionStorage.setItem('fs_token', token);
    $('pw').value = '';
    await load();
  } catch (e) { say(e.message, 'bad'); }
}

async function load() {
  say('读取字体列表…（第一次要扫描本机字体，可能要几秒）');
  const v = await api('/admin/fonts');
  fonts = v.fonts; order = v.order.slice();
  $('login-box').hidden = true; $('fonts-box').hidden = false;
  render(); say('');
}

function render() {
  const sys = $('show-system').checked;
  const enabled = new Set(order);
  const shown = fonts.filter((f) => enabled.has(f.id) || f.source === 'upload' || sys);
  shown.sort((a, b) => {
    const ia = order.indexOf(a.id), ib = order.indexOf(b.id);
    if (ia >= 0 || ib >= 0) return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib);
    return 0;
  });
  $('list').innerHTML = shown.map((f) => {
    const on = enabled.has(f.id);
    const tags = [f.cjk ? '中文' : '', f.hangul ? '韩文' : '', f.emoji ? 'emoji' : ''].filter(Boolean)
      .map((t) => '<span class="tag on">' + t + '</span>').join('');
    return '<tr><td><input type="checkbox" ' + (on ? 'checked' : '') + ' onchange="toggle(\'' + esc(f.id) + '\', this.checked)"></td>' +
      '<td>' + esc(f.family_zh || f.family) + (f.family_zh && f.family ? ' <span class="muted">' + esc(f.family) + '</span>' : '') +
      ' <span class="tag">' + (f.source === 'upload' ? '上传' : '本机') + '</span></td>' +
      '<td class="hide-s muted">' + esc(f.file) + '</td><td>' + f.glyphs + '</td><td>' + tags + '</td><td class="row">' +
      (on ? '<button class="plain" onclick="move(\'' + esc(f.id) + '\', -1)">上移</button><button class="plain" onclick="move(\'' + esc(f.id) + '\', 1)">下移</button>' : '') +
      (f.source === 'upload' ? '<button class="danger" onclick="removeFont(\'' + esc(f.id) + '\')">删除</button>' : '') +
      '</td></tr>';
  }).join('') || '<tr><td colspan="6" class="muted">还没有字体：上传一个，或勾选"显示本机字体"后启用。</td></tr>';
}

function toggle(id, on) {
  order = order.filter((x) => x !== id);
  if (on) order.push(id);
  render();
}

function move(id, d) {
  const i = order.indexOf(id), j = i + d;
  if (i < 0 || j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  render();
}

async function save() {
  try {
    const v = await api('/admin/order', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({order})});
    order = v.order; say('已保存顺序', 'ok'); render();
  } catch (e) { say(e.message, 'bad'); }
}

async function upload() {
  const f = $('file').files[0];
  if (!f) { say('先选一个字体文件', 'bad'); return; }
  say('上传 ' + f.name + '…');
  try {
    await api('/admin/upload?name=' + encodeURIComponent(f.name), {method: 'POST', body: f});
    $('file').value = '';
    await load(); say('已上传 ' + f.name + '，并启用在最后', 'ok');
  } catch (e) { say(e.message, 'bad'); }
}

async function removeFont(id) {
  if (!confirm('删除上传的字体 ' + id + '？')) return;
  try { await api('/admin/remove?id=' + encodeURIComponent(id), {method: 'POST'}); await load(); say('已删除', 'ok'); }
  catch (e) { say(e.message, 'bad'); }
}

function preview() {
  $('preview').src = '/font/preview?face=' + encodeURIComponent($('face').value) + '&size=' + $('size').value +
    '&text=' + encodeURIComponent($('text').value) + '&t=' + Date.now();
}

if (token) load().catch(() => showLogin());
preview();
</script>
</body>
</html>
"""
