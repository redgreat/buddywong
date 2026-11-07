/* buddywong 可视化看板 —— 原生 JS，无构建、无外部依赖 */
'use strict';

const TOKEN_KEY = 'buddywong_token';
const PAGE_META = {
  dashboard: ['仪表盘', '账号池健康度与近 24 小时调用概览'],
  accounts:  ['账号管理', 'WorkBuddy 扫码登录、自动签到与连通性探测'],
  keys:      ['密钥分发', '为下游客户端签发独立 API Key'],
  logs:      ['调用日志', '每一笔反代请求的模型、状态、延迟与用量'],
  checkins:  ['签到流水', '每日自动签到与手动签到的执行结果'],
  settings:  ['系统设置', '签到计划、调度参数、模型别名与管理密码'],
};
const STATUS_TEXT = {
  healthy: '正常', unknown: '未探测', cooling: '冷却中',
  error: '异常', disabled: '已停用', expired: '已过期',
};

const state = {
  token: localStorage.getItem(TOKEN_KEY) || '',
  page: 'dashboard',
  version: '-',
  logs: { limit: 50, offset: 0, model: '', status: '' },
  wb: null,          // 当前扫码会话（不含任何 token）
  wbPoll: null,      // 轮询定时器
  wbErrors: 0,
};

const pageEl = document.getElementById('page');
const modalRoot = document.getElementById('modal-root');
const toastRoot = document.getElementById('toast-root');

/* ── 基础工具 ─────────────────────────────────────── */
function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const pad = n => String(n).padStart(2, '0');
function nfmt(n) { return (Number(n) || 0).toLocaleString('zh-CN'); }
function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(Number(ts) * 1000);
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
         pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function badge(status) {
  const cls = ['healthy', 'unknown', 'cooling', 'error', 'expired', 'disabled'].includes(status) ? status : 'neutral';
  return '<span class="badge ' + cls + '">' + esc(STATUS_TEXT[status] || status || '-') + '</span>';
}
function toast(message, type) {
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'ok');
  el.textContent = message;
  toastRoot.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
function kpi(label, value, foot, cls) {
  return '<div class="kpi"><div class="label">' + esc(label) + '</div>' +
         '<div class="value ' + (cls || '') + '">' + esc(value) + '</div>' +
         '<div class="foot">' + esc(foot) + '</div></div>';
}
function empty(title, text) {
  return '<div class="empty"><strong>' + esc(title) + '</strong>' + esc(text || '') + '</div>';
}

async function api(path, opts) {
  const options = opts || {};
  const headers = Object.assign({}, options.headers || {});
  if (options.body) headers['Content-Type'] = 'application/json';
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  let resp;
  try {
    resp = await fetch('/api' + path, Object.assign({}, options, { headers }));
  } catch (err) {
    throw new Error('无法连接服务：' + err.message);
  }
  let data = {};
  try { data = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (!resp.ok) {
    if (resp.status === 401 && path !== '/login') doLogout();
    throw new Error(data.detail || ('HTTP ' + resp.status));
  }
  return data;
}

/* ── 登录 / 布局 ─────────────────────────────────── */
function showLogin() {
  document.getElementById('login-view').classList.remove('hidden');
  document.getElementById('app-view').classList.add('hidden');
}
function showApp() {
  document.getElementById('login-view').classList.add('hidden');
  document.getElementById('app-view').classList.remove('hidden');
  document.getElementById('side-version').textContent = state.version;
}
function doLogout() {
  clearWbPoll();
  state.wb = null;
  state.token = '';
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
  pageEl.innerHTML = '';
}

/* ── WorkBuddy 页面扫码登录 ────────────────────────────────────
   管理后台登录（上面的账密表单）与 WorkBuddy 账号授权是两层：
   先进后台，再在「账号管理」里扫码把腾讯账号纳管进来并自动签到。
   二维码由服务端生成 PNG data URI，凭据全程不经过浏览器。            */
function clearWbPoll() {
  if (state.wbPoll) { clearInterval(state.wbPoll); state.wbPoll = null; }
}
function setWbStatus(text, kind) {
  const el = document.getElementById('wb-status');
  if (el) { el.textContent = text; el.className = 'wb-status' + (kind ? ' ' + kind : ''); }
}
function wbLoginModal() {
  openModal('扫码登录 WorkBuddy 账号',
    '<div class="wb-login">' +
      '<label class="field"><span>账号版本</span><select id="wb-realm">' +
        '<option value="cn">国内版 · codebuddy.cn（支持每日签到）</option>' +
        '<option value="global">国际版 · workbuddy.ai（无签到体系）</option>' +
      '</select></label>' +
      '<div id="wb-qr" class="qr-box"><span class="muted">选择版本后点「生成二维码」</span></div>' +
      '<p id="wb-status" class="wb-status">等待生成二维码</p>' +
      '<p class="wb-hint">微信 / QQ 扫码，或打开授权页用手机号 + 验证码登录（授权页支持切回扫码）。' +
        '授权成功后自动纳管账号、同步模型并立即签到一次。</p>' +
      '<a id="wb-link" class="wb-link hidden" href="#" target="_blank" rel="noopener">在浏览器打开授权页 ↗</a>' +
    '</div>',
    '<button class="btn" data-action="modal-close">取消</button>' +
    '<button class="btn ghost" data-action="wb-copy-link">复制链接</button>' +
    '<button class="btn primary" data-action="wb-start">生成二维码</button>', true);
}
async function startWbLogin() {
  clearWbPoll();
  state.wb = null;
  state.wbErrors = 0;
  const realm = (document.getElementById('wb-realm') || {}).value || 'cn';
  setWbStatus('正在申请腾讯授权链接…');
  try {
    const r = await api('/wb/auth/start', { method: 'POST', body: JSON.stringify({ realm }) });
    state.wb = { state: r.state, realm: r.realm || realm, ttl: r.ttl || 900,
                 authUrl: r.auth_url || '', started: Date.now() / 1000 };
    const qr = document.getElementById('wb-qr');
    qr.innerHTML = r.qr
      ? '<img src="' + esc(r.qr) + '" alt="WorkBuddy 登录二维码">'
      : '<span class="muted">二维码生成失败，请用下方链接在浏览器打开授权页</span>';
    const link = document.getElementById('wb-link');
    if (state.wb.authUrl) { link.href = state.wb.authUrl; link.classList.remove('hidden'); }
    else { link.classList.add('hidden'); }
    setWbStatus(r.hint || '请用微信 / QQ 扫码授权');
    startWbPoll();
  } catch (err) {
    setWbStatus(err.message, 'err');
  }
}
function startWbPoll() {
  clearWbPoll();
  state.wbPoll = setInterval(pollWbOnce, 2000);
  pollWbOnce();
}
async function pollWbOnce() {
  const w = state.wb;
  if (!w || !w.state || !state.wbPoll) return;
  const left = Math.max(0, Math.round(w.started + w.ttl - Date.now() / 1000));
  try {
    const r = await api('/wb/auth/poll?state=' + encodeURIComponent(w.state) +
                        '&realm=' + encodeURIComponent(w.realm));
    state.wbErrors = 0;
    if (r.status === 'waiting') {
      setWbStatus('等待扫码授权…（二维码剩余 ' + left + ' 秒）');
      return;
    }
    if (r.status === 'ready') {
      clearWbPoll();
      setWbStatus('授权成功：' + (r.nickname || r.uid) + '，正在保存并签到…');
      const f = await api('/wb/auth/finish', {
        method: 'POST', body: JSON.stringify({ state: w.state, auto_checkin: true }),
      });
      const c = f.checkin;
      state.wb = null;
      closeModal();
      toast(f.message + (c ? (c.ok ? '，签到成功' : '，签到：' + (c.message || '未完成')) : ''),
            c && !c.ok ? 'warn' : 'ok');
      await navigate('accounts');
      return;
    }
    if (r.status === 'expired' || r.status === 'invalid') {
      clearWbPoll(); setWbStatus('二维码已失效，请点「生成二维码」重新获取', 'err'); return;
    }
    if (r.status === 'realm_mismatch') {
      clearWbPoll(); setWbStatus('账号版本不一致，请重新生成二维码', 'err'); return;
    }
  } catch (err) {
    state.wbErrors += 1;
    setWbStatus('轮询失败：' + err.message, 'err');
    if (state.wbErrors >= 5) {
      clearWbPoll();
      setWbStatus('连续失败已停止轮询，请重新生成二维码', 'err');
    }
  }
}

document.getElementById('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const errorEl = document.getElementById('login-error');
  errorEl.classList.add('hidden');
  const button = e.target.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const result = await api('/login', {
      method: 'POST',
      body: JSON.stringify({
        username: document.getElementById('login-user').value.trim(),
        password: document.getElementById('login-pass').value,
      }),
    });
    state.token = result.token;
    state.version = result.version || '-';
    localStorage.setItem(TOKEN_KEY, result.token);
    document.getElementById('login-pass').value = '';
    showApp();
    await navigate('dashboard');
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.classList.remove('hidden');
  } finally {
    button.disabled = false;
  }
});

document.getElementById('logout-btn').addEventListener('click', doLogout);
document.getElementById('refresh-btn').addEventListener('click', () => navigate(state.page));
document.getElementById('nav').addEventListener('click', e => {
  const btn = e.target.closest('[data-page]');
  if (btn) navigate(btn.dataset.page);
});

async function navigate(name) {
  state.page = name;
  document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === name));
  document.getElementById('page-title').textContent = PAGE_META[name][0];
  document.getElementById('page-sub').textContent = PAGE_META[name][1];
  pageEl.innerHTML = '<div class="loading">加载中…</div>';
  try {
    await RENDERERS[name]();
  } catch (err) {
    pageEl.innerHTML = '<div class="card">' + empty('加载失败', err.message) + '</div>';
  }
}

function updateClock() {
  document.getElementById('top-time').textContent = fmtTime(Date.now() / 1000);
}

/* ── 图表与表格片段 ───────────────────────────────── */
function barChart(series) {
  const W = 720, H = 180, padL = 30, padR = 12, padT = 12, baseY = 150;
  const max = Math.max(1, ...series.map(s => s.total));
  const bw = (W - padL - padR) / Math.max(1, series.length);
  let grid = '';
  for (let i = 0; i <= 3; i++) {
    const y = padT + (baseY - padT) * i / 3;
    grid += '<line class="grid" x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y + '"/>';
  }
  const bars = series.map((s, i) => {
    const h = Math.round((s.total / max) * (baseY - padT));
    const okH = s.total ? Math.round(h * (s.ok / s.total)) : 0;
    const x = padL + i * bw + 2, w = Math.max(2, bw - 4);
    let out = '<rect class="bar-bg" x="' + x + '" y="' + (baseY - h) + '" width="' + w + '" height="' + h +
             '" rx="2"><title>' + esc(s.label) + ' 共 ' + s.total + ' 次</title></rect>';
    if (okH > 0) {
      out += '<rect class="bar-ok" x="' + x + '" y="' + (baseY - okH) + '" width="' + w + '" height="' + okH +
             '" rx="2"><title>成功 ' + s.ok + ' 次</title></rect>';
    }
    return out;
  }).join('');
  const labels = series.map((s, i) => i % 4 === 0
    ? '<text x="' + (padL + i * bw + bw / 2) + '" y="' + (H - 8) + '" text-anchor="middle">' + esc(s.label) + '</text>'
    : '').join('');
  return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '">' + grid + bars + labels + '</svg>';
}

function statusBars(byStatus, total) {
  const keys = ['healthy', 'unknown', 'cooling', 'error', 'expired', 'disabled'];
  const colors = { healthy: 'var(--ok)', unknown: 'var(--muted)', cooling: 'var(--warn)',
                   error: 'var(--err)', expired: 'var(--err)', disabled: 'var(--dim)' };
  const rows = keys.filter(k => (byStatus[k] || 0) > 0).map(k => {
    const n = byStatus[k] || 0;
    const pct = total ? Math.max(3, Math.round(n * 100 / total)) : 0;
    return '<div class="status-row"><span class="name">' + esc(STATUS_TEXT[k]) + '</span>' +
           '<span class="track"><span class="fill" style="width:' + pct + '%;background:' + colors[k] + '"></span></span>' +
           '<span class="num">' + n + '</span></div>';
  }).join('');
  return rows || empty('暂无账号', '到「账号管理」添加第一个上游账号');
}

function logsTable(items) {
  if (!items || !items.length) return empty('暂无调用记录', '通过 /v1/chat/completions 发起一次请求后，这里会出现明细');
  const rows = items.map(r =>
    '<tr>' +
      '<td class="mono">' + fmtTime(r.ts) + '</td>' +
      '<td class="mono">' + esc(r.key_prefix || '-') + '</td>' +
      '<td>' + esc(r.account_name || '-') + '</td>' +
      '<td><span class="tag">' + esc(r.model || '-') + '</span></td>' +
      '<td><span class="badge ' + (r.ok ? 'ok' : 'fail') + '">' + esc(r.status_code) + '</span></td>' +
      '<td class="mono">' + nfmt(r.latency_ms) + ' ms</td>' +
      '<td class="mono">' + (r.ttft_ms ? nfmt(r.ttft_ms) + ' ms' : '-') + '</td>' +
      '<td class="mono">' + nfmt(r.total_tokens) + '</td>' +
      '<td class="mono">' + esc(r.client_ip || '-') + '</td>' +
    '</tr>').join('');
  return '<div class="card table-wrap"><table><thead><tr>' +
    '<th>时间</th><th>密钥</th><th>账号</th><th>模型</th><th>状态</th>' +
    '<th>耗时</th><th>首字</th><th>Tokens</th><th>来源 IP</th></tr></thead><tbody>' +
    rows + '</tbody></table></div>';
}

/* ── 仪表盘 ───────────────────────────────────────── */
async function renderDashboard() {
  const d = await api('/overview');
  const t = d.today || {};
  pageEl.innerHTML =
    '<div class="kpi-grid">' +
      kpi('账号总数', d.accounts.total, '启用 ' + d.accounts.enabled + ' · 可用 ' + d.accounts.healthy) +
      kpi('今日调用', t.calls, '成功 ' + t.ok + ' · 失败 ' + t.failed) +
      kpi('成功率', (t.success_rate || 0) + '%', '平均延迟 ' + nfmt(t.avg_latency_ms) + ' ms',
          (t.success_rate >= 95 ? 'ok' : 'warn')) +
      kpi('今日 Tokens', nfmt(t.tokens), '已签发密钥 ' + d.keys.total + ' 把') +
    '</div>' +
    '<div class="grid-2">' +
      '<section class="card"><h3>近 24 小时调用<span class="sub">深色为总量，蓝色为成功</span></h3>' +
        barChart(d.hourly || []) + '</section>' +
      '<section class="card"><h3>账号状态分布</h3>' + statusBars(d.accounts.by_status || {}, d.accounts.total) +
        '<div class="info-grid" style="margin-top:18px">' +
          '<dt>版本</dt><dd class="mono">' + esc(d.version) + '</dd>' +
          '<dt>服务器时间</dt><dd class="mono">' + esc(d.server_time) + '</dd>' +
        '</div></section>' +
    '</div>' +
    '<section><h3 style="font-size:14px;margin:0 0 12px">最近调用</h3>' + logsTable(d.recent_logs) + '</section>';
}

/* ── 账号管理 ─────────────────────────────────────── */
function accountForm(a) {
  const isEdit = !!a;
  const methods = ['POST', 'GET', 'PUT', 'PATCH'];
  const methodOptions = methods.map(m =>
    '<option value="' + m + '"' + ((a && a.checkin_method === m) || (!a && m === 'POST') ? ' selected' : '') + '>' + m + '</option>').join('');
  const wbNote = (a && a.source === 'workbuddy')
    ? '<div class="note">该账号由 WorkBuddy 扫码登录纳管：Token 与刷新令牌自动维护，签到走腾讯官方接口。' +
      '这里只改分组、权重、模型等展示字段；换凭据请用「刷新授权」或重新扫码。</div>'
    : '';
  return wbNote + '<form id="modal-form">' +
    '<div class="form-grid">' +
      '<label class="field"><span>账号名称 *</span><input name="name" value="' + esc(a ? a.name : '') + '" required></label>' +
      '<label class="field"><span>分组</span><input name="group_name" value="' + esc(a ? a.group_name : 'default') + '"></label>' +
      '<label class="field full"><span>上游地址 *（OpenAI 兼容根地址）</span>' +
        '<input name="base_url" placeholder="https://api.example.com/v1" value="' + esc(a ? a.base_url : '') + '" required></label>' +
      '<label class="field full"><span>API Key / Token ' + (isEdit ? '（留空保持不变）' : '*') + '</span>' +
        '<input name="api_key" placeholder="' + (isEdit ? '不修改请留空' : 'sk-...') + '"></label>' +
      '<label class="field full"><span>可用模型（逗号分隔，留空 = 不限制）</span>' +
        '<input name="models" value="' + esc(a ? (a.models || []).join(', ') : '') + '"></label>' +
      '<label class="field"><span>权重（1-20，越大轮询越多）</span>' +
        '<input name="weight" type="number" min="1" max="20" value="' + (a ? a.weight : 1) + '"></label>' +
      '<label class="field"><span>积分 / 余额</span>' +
        '<input name="credits" type="number" step="0.01" value="' + (a ? a.credits : 0) + '"></label>' +
      '<label class="field"><span>到期日</span><input name="expires_at" type="date" value="' + esc(a ? a.expires_at : '') + '"></label>' +
      '<div class="field"><span>启用状态</span><label class="checkbox-field">' +
        '<input name="enabled" type="checkbox"' + (a && a.enabled === 0 ? '' : ' checked') + '>' +
        '<span>参与反代轮询与自动签到</span></label></div>' +
      '<label class="field full"><span>签到地址（留空 = 不参与自动签到）</span>' +
        '<input name="checkin_url" placeholder="https://api.example.com/api/checkin" value="' + esc(a ? a.checkin_url : '') + '"></label>' +
      '<label class="field"><span>签到方法</span><select name="checkin_method">' + methodOptions + '</select></label>' +
      '<label class="field"><span>附加请求头（JSON，默认自动带 Bearer Token）</span>' +
        '<textarea name="checkin_headers">' + esc(JSON.stringify((a && a.checkin_headers) || {}, null, 2)) + '</textarea></label>' +
      '<label class="field full"><span>签到请求体（JSON）</span>' +
        '<textarea name="checkin_body">' + esc(JSON.stringify((a && a.checkin_body) || {}, null, 2)) + '</textarea></label>' +
    '</div></form>';
}

function openModal(title, body, footer, narrow) {
  modalRoot.innerHTML = '<div class="modal-mask"><div class="modal' + (narrow ? ' narrow' : '') + '">' +
    '<div class="modal-head"><h3>' + esc(title) + '</h3>' +
      '<button class="close-x" data-action="modal-close" aria-label="关闭">&times;</button></div>' +
    '<div class="modal-body">' + body + '</div>' +
    '<div class="modal-foot">' + footer + '</div></div></div>';
}
function closeModal() { clearWbPoll(); modalRoot.innerHTML = ''; }
function formObject(scope) {
  const obj = {};
  scope.querySelectorAll('[name]').forEach(el => {
    obj[el.name] = el.type === 'checkbox' ? el.checked : String(el.value || '').trim();
  });
  return obj;
}

async function renderAccounts() {
  const d = await api('/accounts');
  const items = d.items || [];
  const rows = items.map(a => {
    const models = (a.models || []).slice(0, 3).map(m => '<span class="tag">' + esc(m) + '</span>').join('') ||
                   '<span class="muted">不限</span>';
    const sourceTag = a.source === 'workbuddy'
      ? '<span class="tag wb">WorkBuddy ' + esc(a.realm === 'global' ? '国际版' : '国内版') + '</span>'
      : '<span class="tag">手工录入</span>';
    return '<tr>' +
      '<td><strong>' + esc(a.name) + '</strong><span class="sub">' + esc(a.group_name || 'default') +
        ' ' + sourceTag + '</span></td>' +
      '<td><span class="ellipsis mono" title="' + esc(a.base_url) + '">' + esc(a.base_url) + '</span>' +
        '<span class="sub mono">' + esc(a.api_key_masked) + '</span></td>' +
      '<td>' + badge(a.effective_status) + '</td>' +
      '<td>' + models + ((a.models || []).length > 3 ? '<span class="sub">+' + (a.models.length - 3) + '</span>' : '') + '</td>' +
      '<td class="mono">' + a.weight + '</td>' +
      '<td class="mono">' + nfmt(a.credits) + '</td>' +
      '<td class="mono">' + nfmt(a.today_calls) + '<span class="sub">成功 ' + nfmt(a.today_ok) + '</span></td>' +
      '<td class="mono">' + fmtTime(a.last_checkin_at) + '</td>' +
      '<td><div class="row-actions">' +
        '<button class="btn ghost sm" data-action="account-edit" data-id="' + a.id + '">编辑</button>' +
        '<button class="btn ghost sm" data-action="account-probe" data-id="' + a.id + '">探测</button>' +
        (a.source === 'workbuddy'
          ? '<button class="btn ghost sm" data-action="wb-refresh" data-id="' + a.id + '">刷新授权</button>' : '') +
        (a.checkin_supported !== false
          ? '<button class="btn ghost sm" data-action="account-checkin" data-id="' + a.id + '">签到</button>' : '') +
        '<button class="btn ghost sm" data-action="account-toggle" data-id="' + a.id + '" data-enabled="' + a.enabled + '">' +
          (a.enabled ? '停用' : '启用') + '</button>' +
        '<button class="btn ghost sm danger" data-action="account-del" data-id="' + a.id + '">删除</button>' +
      '</div></td></tr>';
  }).join('');
  pageEl.innerHTML =
    '<div class="toolbar">' +
      '<button class="btn primary" data-action="wb-login">扫码登录 WorkBuddy</button>' +
      '<button class="btn" data-action="account-new">手工新增账号</button>' +
      '<button class="btn" data-action="checkin-all">全部签到</button>' +
      '<button class="btn" data-action="reload">刷新</button>' +
      '<span class="spacer"></span><span class="muted">共 ' + items.length + ' 个账号</span>' +
    '</div>' +
    (items.length
      ? '<div class="card table-wrap"><table><thead><tr>' +
        '<th>账号 / 分组</th><th>上游地址</th><th>状态</th><th>模型</th><th>权重</th>' +
        '<th>积分</th><th>今日调用</th><th>上次签到</th><th>操作</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>'
      : '<div class="card">' + empty('还没有账号', '点「扫码登录 WorkBuddy」直接在页面授权并自动签到；也可以手工录入一组 OpenAI 兼容凭据') + '</div>');
}

/* ── 密钥分发 ─────────────────────────────────────── */
function keyForm(k) {
  return '<form id="modal-form"><div class="form-grid">' +
    '<label class="field full"><span>密钥名称 *</span><input name="name" value="' + esc(k ? k.name : '') + '" required></label>' +
    '<label class="field"><span>调用额度（0 = 不限）</span><input name="quota" type="number" min="0" value="' + (k ? k.quota : 0) + '"></label>' +
    '<label class="field"><span>到期日</span><input name="expires_at" type="date" value="' + esc(k ? k.expires_at : '') + '"></label>' +
    '<label class="field full"><span>IP 白名单（逗号分隔，支持 CIDR；留空 = 不限）</span>' +
      '<input name="ip_whitelist" placeholder="1.2.3.4, 10.0.0.0/8" value="' + esc(k ? (k.ip_whitelist || []).join(', ') : '') + '"></label>' +
    '<label class="field full"><span>模型白名单（逗号分隔；留空 = 不限）</span>' +
      '<input name="model_whitelist" placeholder="gpt-4o, claude-sonnet-4" value="' + esc(k ? (k.model_whitelist || []).join(', ') : '') + '"></label>' +
    '<div class="field full"><span>启用状态</span><label class="checkbox-field">' +
      '<input name="enabled" type="checkbox"' + (k && k.enabled === 0 ? '' : ' checked') + '>' +
      '<span>允许该密钥调用反代接口</span></label></div>' +
    '</div></form>';
}

async function renderKeys() {
  const d = await api('/keys');
  const items = d.items || [];
  const rows = items.map(k =>
    '<tr>' +
      '<td><strong>' + esc(k.name) + '</strong><span class="sub mono">' + esc(k.prefix) + '</span></td>' +
      '<td>' + (k.enabled ? '<span class="badge ok">已启用</span>' : '<span class="badge disabled">已停用</span>') + '</td>' +
      '<td class="mono">' + nfmt(k.used_count) + ' / ' + (k.quota ? nfmt(k.quota) : '不限') + '</td>' +
      '<td>' + ((k.ip_whitelist || []).length ? k.ip_whitelist.map(i => '<span class="tag">' + esc(i) + '</span>').join('') : '<span class="muted">不限</span>') + '</td>' +
      '<td>' + ((k.model_whitelist || []).length ? k.model_whitelist.map(i => '<span class="tag">' + esc(i) + '</span>').join('') : '<span class="muted">不限</span>') + '</td>' +
      '<td class="mono">' + esc(k.expires_at || '永久') + '</td>' +
      '<td class="mono">' + fmtTime(k.last_used_at) + '</td>' +
      '<td><div class="row-actions">' +
        '<button class="btn ghost sm" data-action="key-edit" data-id="' + k.id + '">编辑</button>' +
        '<button class="btn ghost sm" data-action="key-toggle" data-id="' + k.id + '" data-enabled="' + k.enabled + '">' +
          (k.enabled ? '停用' : '启用') + '</button>' +
        '<button class="btn ghost sm danger" data-action="key-del" data-id="' + k.id + '">删除</button>' +
      '</div></td></tr>').join('');
  pageEl.innerHTML =
    '<div class="toolbar">' +
      '<button class="btn primary" data-action="key-new">签发新密钥</button>' +
      '<button class="btn" data-action="reload">刷新</button>' +
      '<span class="spacer"></span><span class="muted">共 ' + items.length + ' 把密钥</span>' +
    '</div>' +
    '<div class="card"><pre class="code-block"># 下游客户端接入（OpenAI 兼容）\nbase_url: http://&lt;服务器&gt;:3002/v1\napi_key : bw-xxxxxxxxxxxxxxxxxxxx</pre></div>' +
    (items.length
      ? '<div class="card table-wrap"><table><thead><tr>' +
        '<th>名称 / 前缀</th><th>状态</th><th>已用 / 额度</th><th>IP 白名单</th>' +
        '<th>模型白名单</th><th>到期日</th><th>最近使用</th><th>操作</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>'
      : '<div class="card">' + empty('还没有密钥', '签发一把密钥后，下游客户端就能通过 /v1 调用你的账号池') + '</div>');
}

/* ── 调用日志 ─────────────────────────────────────── */
async function renderLogs() {
  const q = state.logs;
  const params = new URLSearchParams({ limit: q.limit, offset: q.offset, model: q.model, status: q.status });
  const d = await api('/logs?' + params.toString());
  const start = d.total ? d.offset + 1 : 0;
  const end = Math.min(d.offset + d.limit, d.total);
  pageEl.innerHTML =
    '<form class="filter-bar" id="log-filter">' +
      '<input class="filter-input" name="model" placeholder="按模型筛选" value="' + esc(q.model) + '">' +
      '<select name="status">' +
        '<option value=""' + (q.status === '' ? ' selected' : '') + '>全部状态</option>' +
        '<option value="ok"' + (q.status === 'ok' ? ' selected' : '') + '>仅成功</option>' +
        '<option value="fail"' + (q.status === 'fail' ? ' selected' : '') + '>仅失败</option>' +
      '</select>' +
      '<button class="btn" type="submit">筛选</button>' +
      '<button class="btn ghost" type="button" data-action="logs-reset">重置</button>' +
      '<span style="flex:1"></span><span class="muted">共 ' + nfmt(d.total) + ' 条</span>' +
    '</form>' +
    logsTable(d.items) +
    '<div class="pager">' +
      '<span>' + start + ' - ' + end + ' / ' + nfmt(d.total) + '</span>' +
      '<button class="btn sm" data-action="logs-prev"' + (d.offset <= 0 ? ' disabled' : '') + '>上一页</button>' +
      '<button class="btn sm" data-action="logs-next"' + (end >= d.total ? ' disabled' : '') + '>下一页</button>' +
    '</div>';
  document.getElementById('log-filter').addEventListener('submit', e => {
    e.preventDefault();
    state.logs.model = e.target.querySelector('[name="model"]').value.trim();
    state.logs.status = e.target.querySelector('[name="status"]').value;
    state.logs.offset = 0;
    navigate('logs');
  });
}

/* ── 签到流水 ─────────────────────────────────────── */
async function renderCheckins() {
  const d = await api('/checkin-logs?limit=200');
  const items = d.items || [];
  const rows = items.map(r =>
    '<tr>' +
      '<td class="mono">' + fmtTime(r.ts) + '</td>' +
      '<td>' + esc(r.account_name || '-') + '</td>' +
      '<td><span class="badge ' + (r.ok ? 'ok' : 'fail') + '">' + (r.ok ? '成功' : '失败') + '</span></td>' +
      '<td class="mono">' + esc(r.status_code || '-') + '</td>' +
      '<td>' + esc(r.trigger === 'auto' ? '自动' : r.trigger === 'skip' ? '跳过'
                   : r.trigger === 'login' ? '登录后' : '手动') + '</td>' +
      '<td class="ellipsis" title="' + esc(r.message) + '">' + esc(r.message || '-') + '</td>' +
    '</tr>').join('');
  pageEl.innerHTML =
    '<div class="toolbar"><button class="btn" data-action="reload">刷新</button>' +
      '<span class="spacer"></span><span class="muted">最近 ' + items.length + ' 条</span></div>' +
    (items.length
      ? '<div class="card table-wrap"><table><thead><tr>' +
        '<th>时间</th><th>账号</th><th>结果</th><th>业务码</th><th>触发方式</th><th>说明</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table></div>'
      : '<div class="card">' + empty('暂无签到记录', '给账号配置签到地址后，可在账号页手动签到或等待每日自动签到') + '</div>');
}

/* ── 系统设置 ─────────────────────────────────────── */
async function renderSettings() {
  const d = await api('/settings');
  const s = d.items || {};
  const w = d.workbuddy || {};
  const v = w.versions || {};
  const num = (key, fallback) => esc(s[key] === undefined ? fallback : s[key]);
  const fullVer = (short, full) => esc(short || '-') +
    (full && full !== short ? ' <span class="muted">(' + esc(full) + ')</span>' : '');
  pageEl.innerHTML =
    '<div class="grid-2">' +
      '<section class="card"><h3>调度与反代参数</h3><form id="settings-form">' +
        '<div class="form-grid">' +
          '<div class="field full"><span>自动签到</span><label class="checkbox-field">' +
            '<input name="checkin_enabled" type="checkbox"' + (s.checkin_enabled === false ? '' : ' checked') + '>' +
            '<span>每天到点自动为所有配置了签到地址的账号签到</span></label></div>' +
          '<label class="field"><span>签到时刻（时 0-23）</span><input name="checkin_hour" type="number" min="0" max="23" value="' + num('checkin_hour', 8) + '"></label>' +
          '<label class="field"><span>签到时刻（分 0-59）</span><input name="checkin_minute" type="number" min="0" max="59" value="' + num('checkin_minute', 5) + '"></label>' +
          '<label class="field"><span>失败冷却（秒）</span><input name="cool_seconds" type="number" min="0" max="86400" value="' + num('cool_seconds', 300) + '"></label>' +
          '<label class="field"><span>单次请求最多换号次数</span><input name="max_account_retries" type="number" min="1" max="10" value="' + num('max_account_retries', 3) + '"></label>' +
          '<label class="field"><span>上游请求超时（秒）</span><input name="request_timeout" type="number" min="5" max="600" value="' + num('request_timeout', 120) + '"></label>' +
          '<label class="field"><span>日志保留天数（0 = 永久）</span><input name="log_retention_days" type="number" min="0" max="3650" value="' + num('log_retention_days', 30) + '"></label>' +
          '<label class="field full"><span>模型别名（JSON 对象：下游名称 → 上游实际模型）</span>' +
            '<textarea name="model_alias">' + esc(JSON.stringify(s.model_alias || {}, null, 2)) + '</textarea></label>' +
        '</div>' +
        '<button class="btn primary" type="button" data-action="settings-save">保存设置</button>' +
      '</form></section>' +
      '<div>' +
        '<section class="card"><h3>修改管理密码</h3><form id="password-form">' +
          '<label class="field"><span>原密码</span><input name="old_password" type="password" required></label>' +
          '<label class="field"><span>新密码（至少 8 位）</span><input name="new_password" type="password" minlength="8" required></label>' +
          '<button class="btn" type="button" data-action="password-save">修改并重新登录</button>' +
        '</form></section>' +
        '<section class="card"><h3>运行信息</h3><dl class="info-grid">' +
          '<dt>版本</dt><dd class="mono">' + esc(state.version) + '</dd>' +
          '<dt>配置文件</dt><dd class="mono">' + esc(d.config_file || '-') + '</dd>' +
          '<dt>出站代理</dt><dd class="mono">' + esc(d.http_proxy || '直连') + '</dd>' +
          '<dt>会话有效期</dt><dd class="mono">' + nfmt(d.session_timeout) + ' 秒</dd>' +
        '</dl></section>' +
        '<section class="card"><h3>WorkBuddy 真实签到</h3><dl class="info-grid">' +
          '<dt>登录端点</dt><dd class="mono">' + esc(w.cn_chat_base || '-') + '</dd>' +
          '<dt>签到端点</dt><dd class="mono">' + esc(w.cn_billing_base || '-') + '/v2/billing/meter/daily-checkin</dd>' +
          '<dt>国际版</dt><dd class="mono">' + esc(w.global_base || '-') + '（无签到体系）</dd>' +
          '<dt>二维码有效期</dt><dd class="mono">' + nfmt(w.state_ttl) + ' 秒</dd>' +
          '<dt>登录后自动签到</dt><dd>' + (w.auto_checkin_on_login ? '开启' : '关闭') + '</dd>' +
        '</dl><p class="muted" style="font-size:12px;margin:12px 0 0">' +
          '在「账号管理 → 扫码登录 WorkBuddy」直接授权；签到按腾讯 code=0 / 10001（今日已签到）口径记账，' +
          '10001「稍后重试」不会被误记成成功。</p></section>' +
        '<section class="card"><h3>官方客户端版本 <span class="tag">' +
          esc(v.source_text || '内置默认') + '</span></h3>' +
          '<div class="version-bar"><span class="muted">' +
            (v.mode === 'pinned' ? '部分版本已手动锁定' :
              '自动跟随官方发布 · 每 ' + nfmt(v.refresh_hours || 6) + ' 小时同步') + '</span>' +
            '<button class="btn ghost sm" data-action="wb-versions-refresh">立即同步</button></div>' +
          '<dl class="info-grid">' +
            '<dt>国内版客户端</dt><dd class="mono">' + fullVer(v.client_cn, v.full_cn) + '</dd>' +
            '<dt>国际版客户端</dt><dd class="mono">' + fullVer(v.client_global, v.full_global) + '</dd>' +
            '<dt>CLI 版本</dt><dd class="mono">' + esc(v.cli || '-') + '</dd>' +
            '<dt>上次同步</dt><dd class="mono">' + esc(v.fetched_at_text || '从未') + '</dd>' +
            '<dt>客户端来源</dt><dd class="mono" style="font-size:12px">' +
              esc(w.cn_billing_base || '-') + esc(v.client_update_path || '') + '</dd>' +
            '<dt>CLI 来源</dt><dd class="mono" style="font-size:12px">' +
              esc(v.cli_registry || '-') + '</dd>' +
          '</dl>' +
          (v.last_error ? '<p class="form-error" style="margin:12px 0 0">部分源未取到版本，' +
            '已沿用上次成功值：' + esc(v.last_error) + '</p>' : '') +
          '<p class="muted" style="font-size:12px;margin:12px 0 0">' +
            '版本不写在 config.yml 里：客户端取官网 /v2/update，CLI 取 npm @tencent-ai/codebuddy-code 的 latest。' +
            '官方升级后自动跟随，无需改配置或重新打包；探测失败沿用上次成功值，不影响签到。' +
            '排查问题时可用 BW_WB_CLIENT_VERSION / BW_WB_CLI_VERSION 临时钉死。</p></section>' +
        '<section class="card"><h3>反代接入</h3>' +
          '<pre class="code-block">from openai import OpenAI\n\nclient = OpenAI(\n    base_url="http://&lt;服务器&gt;:3002/v1",\n    api_key="bw-xxxxxxxxxxxxxxxx",\n)\nresp = client.chat.completions.create(\n    model="你的别名或实际模型名",\n    messages=[{"role": "user", "content": "hi"}],\n)</pre>' +
          '<p class="muted" style="font-size:12px;margin:12px 0 0">管理端口只监听 127.0.0.1，公网访问请在 Nginx / Caddy 上做 HTTPS 反代与访问控制。</p>' +
        '</section>' +
      '</div>' +
    '</div>';
}

/* ── 交互：统一事件委托 ───────────────────────────── */
document.addEventListener('click', async e => {
  const btn = e.target.closest('[data-action]');
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action;
  const id = Number(btn.dataset.id || 0);
  const enabled = Number(btn.dataset.enabled || 0);

  try {
    switch (action) {
      case 'modal-close': closeModal(); break;
      case 'reload': await navigate(state.page); break;

      /* 账号：WorkBuddy 页面扫码登录 */
      case 'wb-login':
        clearWbPoll(); state.wb = null; wbLoginModal(); break;
      case 'wb-start':
        await startWbLogin(); break;
      case 'wb-copy-link': {
        const href = (document.getElementById('wb-link') || {}).href || '';
        if (!href || href.slice(-1) === '#') { toast('请先生成二维码', 'warn'); break; }
        try { await navigator.clipboard.writeText(href); toast('授权链接已复制'); }
        catch (_) { toast('复制失败，请手动复制链接', 'warn'); }
        break;
      }
      case 'wb-refresh': {
        toast('正在刷新授权与实时积分…');
        const r = await api('/wb/accounts/' + id + '/refresh', { method: 'POST' });
        toast(r.message || (r.ok ? '刷新成功' : '刷新失败'), r.ok ? 'ok' : 'err');
        await navigate('accounts');
        break;
      }
      case 'wb-versions-refresh': {
        toast('正在向官方发布源同步版本…');
        const r = await api('/wb/versions/refresh', { method: 'POST' });
        toast(r.ok ? '版本已同步' : '同步未完成，已沿用上次成功值', r.ok ? 'ok' : 'warn');
        await navigate('settings');
        break;
      }

      /* 账号 */
      case 'account-new':
        openModal('新增账号', accountForm(null),
          '<button class="btn" data-action="modal-close">取消</button>' +
          '<button class="btn primary" data-action="account-save" data-id="0">保存</button>');
        break;
      case 'account-edit': {
        const d = await api('/accounts');
        const item = (d.items || []).find(x => x.id === id);
        if (!item) { toast('账号不存在', 'err'); break; }
        openModal('编辑账号 · ' + item.name, accountForm(item),
          '<button class="btn" data-action="modal-close">取消</button>' +
          '<button class="btn primary" data-action="account-save" data-id="' + id + '">保存</button>');
        break;
      }
      case 'account-save': {
        const data = formObject(document.getElementById('modal-form'));
        for (const key of ['checkin_headers', 'checkin_body']) {
          if (data[key]) {
            try { JSON.parse(data[key]); } catch (_) { toast(key + ' 不是合法 JSON', 'err'); return; }
          }
        }
        await api(id ? '/accounts/' + id : '/accounts',
                  { method: id ? 'PUT' : 'POST', body: JSON.stringify(data) });
        closeModal(); toast('账号已保存'); await navigate('accounts');
        break;
      }
      case 'account-del':
        if (!confirm('确定删除这个账号？历史日志会保留。')) break;
        await api('/accounts/' + id, { method: 'DELETE' });
        toast('账号已删除'); await navigate('accounts');
        break;
      case 'account-toggle':
        await api('/accounts/' + id + '/toggle', { method: 'POST', body: JSON.stringify({ enabled: !enabled }) });
        toast(enabled ? '已停用' : '已启用'); await navigate('accounts');
        break;
      case 'account-probe': {
        toast('正在探测上游连通性…');
        const r = await api('/accounts/' + id + '/probe', { method: 'POST' });
        toast(r.ok ? ('连通正常，发现 ' + (r.models || []).length + ' 个模型') : (r.message || '探测失败'),
              r.ok ? 'ok' : 'err');
        await navigate('accounts');
        break;
      }
      case 'account-checkin': {
        toast('正在执行签到…');
        const r = await api('/accounts/' + id + '/checkin', { method: 'POST' });
        toast(r.message || (r.ok ? '签到成功' : '签到失败'), r.ok ? 'ok' : 'warn');
        await navigate('accounts');
        break;
      }
      case 'checkin-all': {
        toast('正在批量签到…');
        const r = await api('/accounts/checkin-all', { method: 'POST' });
        toast('批量签到完成：' + r.succeeded + '/' + r.total + ' 成功', r.succeeded === r.total ? 'ok' : 'warn');
        await navigate('accounts');
        break;
      }

      /* 密钥 */
      case 'key-new':
        openModal('签发新密钥', keyForm(null),
          '<button class="btn" data-action="modal-close">取消</button>' +
          '<button class="btn primary" data-action="key-save" data-id="0">生成</button>', true);
        break;
      case 'key-edit': {
        const d = await api('/keys');
        const item = (d.items || []).find(x => x.id === id);
        if (!item) { toast('密钥不存在', 'err'); break; }
        openModal('编辑密钥 · ' + item.name, keyForm(item),
          '<button class="btn" data-action="modal-close">取消</button>' +
          '<button class="btn primary" data-action="key-save" data-id="' + id + '">保存</button>', true);
        break;
      }
      case 'key-save': {
        const data = formObject(document.getElementById('modal-form'));
        const r = await api(id ? '/keys/' + id : '/keys',
                            { method: id ? 'PUT' : 'POST', body: JSON.stringify(data) });
        if (id) {
          closeModal(); toast('密钥已更新'); await navigate('keys');
        } else {
          openModal('密钥已生成',
            '<div class="note">完整密钥只显示这一次，请立即复制保存；服务端只保存 SHA-256 摘要。</div>' +
            '<div class="secret-box" id="secret-text">' + esc(r.secret) + '</div>',
            '<button class="btn" data-action="copy-secret">复制</button>' +
            '<button class="btn primary" data-action="modal-close">完成</button>', true);
          await navigate('keys');
        }
        break;
      }
      case 'copy-secret': {
        const text = (document.getElementById('secret-text') || {}).textContent || '';
        try { await navigator.clipboard.writeText(text); toast('已复制到剪贴板'); }
        catch (_) { toast('复制失败，请手动选中复制', 'warn'); }
        break;
      }
      case 'key-del':
        if (!confirm('确定删除这把密钥？使用该密钥的客户端会立即失效。')) break;
        await api('/keys/' + id, { method: 'DELETE' });
        toast('密钥已删除'); await navigate('keys');
        break;
      case 'key-toggle':
        await api('/keys/' + id + '/toggle', { method: 'POST', body: JSON.stringify({ enabled: !enabled }) });
        toast(enabled ? '已停用' : '已启用'); await navigate('keys');
        break;

      /* 日志分页 */
      case 'logs-prev':
        state.logs.offset = Math.max(0, state.logs.offset - state.logs.limit);
        await navigate('logs'); break;
      case 'logs-next':
        state.logs.offset += state.logs.limit;
        await navigate('logs'); break;
      case 'logs-reset':
        state.logs = { limit: 50, offset: 0, model: '', status: '' };
        await navigate('logs'); break;

      /* 设置 */
      case 'settings-save': {
        const data = formObject(document.getElementById('settings-form'));
        let alias;
        try { alias = JSON.parse(data.model_alias || '{}'); }
        catch (_) { toast('模型别名不是合法 JSON', 'err'); return; }
        for (const key of ['checkin_hour', 'checkin_minute', 'cool_seconds', 'max_account_retries',
                           'request_timeout', 'log_retention_days']) {
          data[key] = Number(data[key]);
        }
        data.model_alias = alias;
        await api('/settings', { method: 'PUT', body: JSON.stringify(data) });
        toast('设置已保存，立即生效'); await navigate('settings');
        break;
      }
      case 'password-save': {
        const data = formObject(document.getElementById('password-form'));
        await api('/password', { method: 'POST', body: JSON.stringify(data) });
        toast('密码已修改，请重新登录');
        setTimeout(doLogout, 800);
        break;
      }
      default: break;
    }
  } catch (err) {
    toast(err.message, 'err');
  }
});

/* 点击遮罩空白处关闭模态框 */
modalRoot.addEventListener('click', e => {
  if (e.target.classList.contains('modal-mask')) closeModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});

const RENDERERS = {
  dashboard: renderDashboard, accounts: renderAccounts, keys: renderKeys,
  logs: renderLogs, checkins: renderCheckins, settings: renderSettings,
};

/* ── 启动 ─────────────────────────────────────────── */
(async function boot() {
  updateClock();
  setInterval(updateClock, 30000);
  if (!state.token) { showLogin(); return; }
  try {
    const me = await api('/me');
    state.version = me.version || '-';
    showApp();
    await navigate('dashboard');
  } catch (_) {
    showLogin();
  }
})();
