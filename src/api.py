"""管理端 REST API：管理员登录、WorkBuddy 页面扫码登录、仪表盘、账号、密钥、日志、统计、设置。

所有 /api/*（除 /api/login 外）都要求管理端会话 token：
  Authorization: Bearer <token>
错误统一返回 FastAPI 的 {"detail": "..."}。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from . import accounts, db, versions, workbuddy as wb
from .config import CFG, app_version
from .security import (auth_token_from_header, generate_api_key, api_key_hash,
                       hash_password, verify_password, verify_token, make_token)

logger = logging.getLogger('buddywong.api')
router = APIRouter(prefix='/api', tags=['admin'])


# ── 认证依赖 ───────────────────────────────────────────────────
def current_user(request: Request) -> str:
    username = verify_token(auth_token_from_header(request))
    if not username:
        raise HTTPException(status_code=401, detail='登录已失效，请重新登录')
    row = db.query_one('SELECT username FROM users WHERE username = ?', (username,))
    if row is None:
        raise HTTPException(status_code=401, detail='用户不存在，请重新登录')
    return username


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f'请求体不是合法 JSON：{exc}')
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail='请求体必须是 JSON 对象')
    return data


def _day_start(days_ago: int = 0) -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) - days_ago * 86400


# ── 登录 ───────────────────────────────────────────────────────
@router.post('/login')
async def login(request: Request):
    data = await _body(request)
    username = str(data.get('username') or '').strip()
    password = str(data.get('password') or '')
    row = db.query_one('SELECT * FROM users WHERE username = ?', (username,))
    if row is None or not verify_password(password, row['password_hash']):
        raise HTTPException(status_code=401, detail='用户名或密码错误')
    return {'token': make_token(username), 'username': username, 'version': app_version()}


@router.get('/me')
async def me(user: str = Depends(current_user)):
    return {'username': user, 'version': app_version()}


@router.post('/password')
async def change_password(request: Request, user: str = Depends(current_user)):
    data = await _body(request)
    old = str(data.get('old_password') or '')
    new = str(data.get('new_password') or '')
    if len(new) < 8:
        raise HTTPException(status_code=400, detail='新密码至少 8 位')
    row = db.query_one('SELECT * FROM users WHERE username = ?', (user,))
    if row is None or not verify_password(old, row['password_hash']):
        raise HTTPException(status_code=400, detail='原密码不正确')
    db.execute('UPDATE users SET password_hash = ? WHERE username = ?', (hash_password(new), user))
    return {'ok': True, 'message': '密码已修改，请重新登录'}


# ── 仪表盘 / 统计 ──────────────────────────────────────────────
@router.get('/overview')
async def overview(user: str = Depends(current_user)):
    account_rows = db.query('SELECT * FROM accounts')
    status_count: dict[str, int] = {}
    for row in account_rows:
        status = accounts.effective_status(row)
        status_count[status] = status_count.get(status, 0) + 1
    key_rows = db.query('SELECT COUNT(*) AS total, SUM(enabled) AS enabled FROM api_keys')
    today = db.query_one(
        'SELECT COUNT(*) AS total, SUM(ok) AS ok, AVG(latency_ms) AS avg_latency, '
        'SUM(total_tokens) AS tokens FROM request_logs WHERE ts >= ?', (_day_start(),)) or {}
    since = time.time() - 86400
    hourly = db.query(
        "SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') AS bucket, "
        'COUNT(*) AS total, SUM(ok) AS ok FROM request_logs WHERE ts >= ? '
        'GROUP BY bucket ORDER BY bucket', (since,))
    now = time.localtime()
    series = []
    for back in range(23, -1, -1):
        stamp = time.localtime(time.time() - back * 3600)
        label = time.strftime('%Y-%m-%d %H', stamp)
        hit = next((h for h in hourly if h['bucket'] == label), None)
        series.append({'label': time.strftime('%H:00', stamp),
                       'total': int(hit['total']) if hit else 0,
                       'ok': int(hit['ok'] or 0) if hit else 0})
    recent = db.query('SELECT * FROM request_logs ORDER BY ts DESC LIMIT 8')
    total_calls = int(today.get('total') or 0)
    ok_calls = int(today.get('ok') or 0)
    return {
        'version': app_version(),
        'server_time': time.strftime('%Y-%m-%d %H:%M:%S', now),
        'accounts': {
            'total': len(account_rows),
            'enabled': sum(1 for r in account_rows if r['enabled']),
            'by_status': status_count,
            'healthy': status_count.get('healthy', 0) + status_count.get('unknown', 0),
        },
        'keys': {'total': int(key_rows[0]['total'] or 0) if key_rows else 0,
                 'enabled': int(key_rows[0]['enabled'] or 0) if key_rows else 0},
        'today': {
            'calls': total_calls,
            'ok': ok_calls,
            'failed': total_calls - ok_calls,
            'success_rate': round(ok_calls * 100 / total_calls, 1) if total_calls else 100.0,
            'avg_latency_ms': round(float(today.get('avg_latency') or 0)),
            'tokens': int(today.get('tokens') or 0),
        },
        'hourly': series,
        'recent_logs': recent,
    }


@router.get('/stats')
async def stats(days: int = 7, user: str = Depends(current_user)):
    days = max(1, min(90, days))
    rows = db.query(
        'SELECT day, COUNT(*) AS total, SUM(ok) AS ok, SUM(total_tokens) AS tokens, '
        'AVG(latency_ms) AS avg_latency FROM request_logs WHERE ts >= ? GROUP BY day ORDER BY day',
        (_day_start(days - 1),))
    by_day = {r['day']: r for r in rows}
    series = []
    for back in range(days - 1, -1, -1):
        label = time.strftime('%Y-%m-%d', time.localtime(time.time() - back * 86400))
        hit = by_day.get(label)
        series.append({'day': label, 'total': int(hit['total']) if hit else 0,
                       'ok': int(hit['ok'] or 0) if hit else 0,
                       'tokens': int(hit['tokens'] or 0) if hit else 0,
                       'avg_latency_ms': round(float(hit['avg_latency'] or 0)) if hit else 0})
    return {'items': series}


# ── 账号 ───────────────────────────────────────────────────────
@router.get('/accounts')
async def list_accounts(user: str = Depends(current_user)):
    items = accounts.list_accounts()
    return {'items': items, 'total': len(items)}


@router.post('/accounts')
async def create_account(request: Request, user: str = Depends(current_user)):
    try:
        return {'ok': True, 'item': accounts.create_account(await _body(request))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put('/accounts/{account_id}')
async def update_account(account_id: int, request: Request, user: str = Depends(current_user)):
    try:
        item = accounts.update_account(account_id, await _body(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail='账号不存在')
    return {'ok': True, 'item': item}


@router.delete('/accounts/{account_id}')
async def delete_account(account_id: int, user: str = Depends(current_user)):
    if not accounts.delete_account(account_id):
        raise HTTPException(status_code=404, detail='账号不存在')
    return {'ok': True}


@router.post('/accounts/{account_id}/toggle')
async def toggle_account(account_id: int, request: Request, user: str = Depends(current_user)):
    data = await _body(request)
    item = accounts.set_enabled(account_id, bool(data.get('enabled', True)))
    if item is None:
        raise HTTPException(status_code=404, detail='账号不存在')
    return {'ok': True, 'item': item}


@router.post('/accounts/{account_id}/probe')
async def probe_account(account_id: int, user: str = Depends(current_user)):
    result = await accounts.probe_account(account_id)
    return {'ok': result['ok'], **result}


@router.post('/accounts/{account_id}/checkin')
async def checkin_account(account_id: int, user: str = Depends(current_user)):
    return await accounts.checkin_account(account_id, 'manual')


@router.post('/accounts/checkin-all')
async def checkin_all(user: str = Depends(current_user)):
    results = await accounts.checkin_all('manual')
    return {'ok': True, 'total': len(results),
            'succeeded': sum(1 for r in results if r.get('ok')), 'items': results}


# ── WorkBuddy 页面扫码登录 ──────────────────────────────────────
# 三个接口全部要求管理端会话；accessToken / refreshToken 只在服务端内存与 SQLite 里，
# poll 返回给前端的只有 uid / 昵称 / 状态，不会把账号凭据下发到浏览器。
@router.post('/wb/auth/start')
async def wb_auth_start(request: Request, user: str = Depends(current_user)):
    """发起扫码登录，返回 state、授权链接和 PNG 二维码 data URI。"""
    data = await _body(request)
    realm = wb.normalize_realm(data.get('realm'))
    try:
        out = await wb.start_login(realm)
    except wb.WorkBuddyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    out['qr'] = wb.qr_data_uri(out['auth_url'])
    out['hint'] = ('国内版：微信/QQ 扫码或手机号+验证码登录，成功后自动签到'
                   if realm == 'cn' else '国际版没有签到体系，扫码只用于纳管账号')
    return out


@router.get('/wb/auth/poll')
async def wb_auth_poll(state: str = '', realm: str = '', user: str = Depends(current_user)):
    """前端每 2 秒轮询一次：waiting / ready / expired / invalid / realm_mismatch。"""
    if not state:
        return {'status': 'invalid'}
    try:
        return await wb.poll_login(state, realm or None)
    except wb.WorkBuddyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post('/wb/auth/finish')
async def wb_auth_finish(request: Request, user: str = Depends(current_user)):
    """授权成功后落库（同一 uid 重新扫码 = 刷新凭据），并可选立刻签到一次。"""
    data = await _body(request)
    state = str(data.get('state') or '').strip()
    ready = wb.take_ready(state)
    if ready is None:
        raise HTTPException(status_code=409, detail='授权尚未完成或已过期，请重新扫码')
    try:
        item = accounts.upsert_workbuddy(ready)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 模型目录是锦上添花：拿不到就保持「不限模型」，绝不因为目录失败让登录失败。
    try:
        row = accounts.get_account(int(item['id']))
        models = await wb.fetch_model_ids(wb.auth_from_row(row), str(row['realm']))
        if models:
            accounts.set_models(int(item['id']), models)
            item['models'] = models
    except Exception as exc:  # noqa: BLE001
        logger.info('登录后同步模型目录失败（按不限模型处理）：%s', exc)

    checkin = None
    auto = (bool(data.get('auto_checkin', True)) and CFG.wb_auto_checkin
            and bool(item['checkin_supported']))
    if auto:
        checkin = await accounts.checkin_account(int(item['id']), 'login')
    wb.drop_state(state)
    return {'ok': True, 'item': item, 'checkin': checkin,
            'message': f"已登录并纳管 {item['name']}"}


@router.post('/wb/accounts/{account_id}/refresh')
async def wb_account_refresh(account_id: int, user: str = Depends(current_user)):
    """令牌失效时手动刷新授权，并顺带回查实时积分。"""
    row = accounts.get_account(account_id)
    if row is None or str(row.get('source')) != 'workbuddy':
        raise HTTPException(status_code=404, detail='WorkBuddy 账号不存在')
    return await accounts.refresh_wb_account(account_id)


# ── 官方客户端版本（自动跟随，无需写进 config.yml） ─────────────
@router.get('/wb/versions')
async def wb_versions(user: str = Depends(current_user)):
    """当前生效版本、来源、上次/下次同步时间与各探测源结果。"""
    return versions.status()


@router.post('/wb/versions/refresh')
async def wb_versions_refresh(user: str = Depends(current_user)):
    """设置页「立即同步」按钮：强制重新向官方发布源取一次版本。"""
    result = await versions.refresh(force=True)
    ok = any(item.get('ok') for item in result['sources'].values())
    return {'ok': ok, **result}


# ── API Key ────────────────────────────────────────────────────
def _json_list(value: Any) -> str:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else [p.strip() for p in text.split(',') if p.strip()]
        except json.JSONDecodeError:
            items = [p.strip() for p in text.replace('\n', ',').split(',') if p.strip()]
    else:
        items = []
    return json.dumps([str(i).strip() for i in items if str(i).strip()], ensure_ascii=False)


def _key_values(data: dict, existing: dict | None = None) -> dict:
    base = existing or {}
    name = str(data.get('name', base.get('name', '')) or '').strip()
    if not name:
        raise ValueError('密钥名称不能为空')
    try:
        quota = max(0, int(data.get('quota', base.get('quota', 0)) or 0))
    except (TypeError, ValueError):
        raise ValueError('额度必须是非负整数（0 表示不限）')
    expires = str(data.get('expires_at', base.get('expires_at', '')) or '').strip()
    if expires:
        try:
            time.strptime(expires, '%Y-%m-%d')
        except ValueError:
            raise ValueError('到期日格式应为 YYYY-MM-DD')
    return {
        'name': name,
        'enabled': 1 if data.get('enabled', base.get('enabled', 1)) in (1, True, '1', 'true', 'on', 'yes') else 0,
        'quota': quota,
        'ip_whitelist': _json_list(data.get('ip_whitelist', base.get('ip_whitelist'))),
        'model_whitelist': _json_list(data.get('model_whitelist', base.get('model_whitelist'))),
        'expires_at': expires,
    }


def _public_key(row: dict) -> dict:
    item = dict(row)
    item.pop('key_hash', None)
    item['ip_whitelist'] = json.loads(item.get('ip_whitelist') or '[]')
    item['model_whitelist'] = json.loads(item.get('model_whitelist') or '[]')
    return item


@router.get('/keys')
async def list_keys(user: str = Depends(current_user)):
    items = [_public_key(r) for r in db.query('SELECT * FROM api_keys ORDER BY id DESC')]
    return {'items': items, 'total': len(items)}


@router.post('/keys')
async def create_key(request: Request, user: str = Depends(current_user)):
    try:
        values = _key_values(await _body(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    raw_key = generate_api_key()
    values.update({
        'key_hash': api_key_hash(raw_key),
        'prefix': raw_key[:11] + '...',
        'created_at': time.time(),
    })
    columns = ', '.join(values)
    placeholders = ', '.join('?' for _ in values)
    key_id = db.execute(f'INSERT INTO api_keys ({columns}) VALUES ({placeholders})', tuple(values.values()))
    row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    # 明文只在创建这一刻返回一次，之后库里只有 sha256
    return {'ok': True, 'item': _public_key(row), 'secret': raw_key}


@router.put('/keys/{key_id}')
async def update_key(key_id: int, request: Request, user: str = Depends(current_user)):
    existing = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    if existing is None:
        raise HTTPException(status_code=404, detail='密钥不存在')
    try:
        values = _key_values(await _body(request), existing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    assignments = ', '.join(f'{key} = ?' for key in values)
    db.execute(f'UPDATE api_keys SET {assignments} WHERE id = ?', tuple(values.values()) + (key_id,))
    return {'ok': True, 'item': _public_key(db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,)))}


@router.delete('/keys/{key_id}')
async def delete_key(key_id: int, user: str = Depends(current_user)):
    if db.query_one('SELECT id FROM api_keys WHERE id = ?', (key_id,)) is None:
        raise HTTPException(status_code=404, detail='密钥不存在')
    db.execute('DELETE FROM api_keys WHERE id = ?', (key_id,))
    return {'ok': True}


@router.post('/keys/{key_id}/toggle')
async def toggle_key(key_id: int, request: Request, user: str = Depends(current_user)):
    data = await _body(request)
    db.execute('UPDATE api_keys SET enabled = ? WHERE id = ?',
               (1 if data.get('enabled', True) else 0, key_id))
    row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    if row is None:
        raise HTTPException(status_code=404, detail='密钥不存在')
    return {'ok': True, 'item': _public_key(row)}


# ── 日志 ───────────────────────────────────────────────────────
@router.get('/logs')
async def list_logs(limit: int = 50, offset: int = 0, model: str = '',
                    status: str = '', key_id: int = 0, user: str = Depends(current_user)):
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    where, params = [], []
    if model:
        where.append('model LIKE ?')
        params.append(f'%{model}%')
    if status == 'ok':
        where.append('ok = 1')
    elif status == 'fail':
        where.append('ok = 0')
    if key_id:
        where.append('key_id = ?')
        params.append(key_id)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    total = db.query_one(f'SELECT COUNT(*) AS n FROM request_logs{clause}', tuple(params))
    items = db.query(f'SELECT * FROM request_logs{clause} ORDER BY ts DESC LIMIT ? OFFSET ?',
                     tuple(params) + (limit, offset))
    return {'items': items, 'total': int(total['n']) if total else 0, 'limit': limit, 'offset': offset}


@router.get('/checkin-logs')
async def list_checkin_logs(limit: int = 100, account_id: int = 0,
                            user: str = Depends(current_user)):
    items = accounts.list_checkin_logs(limit, account_id or None)
    return {'items': items, 'total': len(items)}


# ── 动态设置 ───────────────────────────────────────────────────
_SETTING_RULES: dict[str, tuple[str, int, int]] = {
    # key: (类型, 最小值, 最大值)
    'checkin_hour': ('int', 0, 23),
    'checkin_minute': ('int', 0, 59),
    'cool_seconds': ('int', 0, 86400),
    'max_account_retries': ('int', 1, 10),
    'request_timeout': ('int', 5, 600),
    'log_retention_days': ('int', 0, 3650),
}


@router.get('/settings')
async def get_settings(user: str = Depends(current_user)):
    return {'items': db.all_settings(), 'config_file': str(CFG.config_path),
            'http_proxy': CFG.http_proxy or '', 'session_timeout': CFG.session_timeout,
            'workbuddy': {
                'cn_chat_base': CFG.wb_cn_chat_base,
                'cn_billing_base': CFG.wb_cn_billing_base,
                'global_base': CFG.wb_global_base,
                'client_version': versions.client_version('cn'),
                'versions': versions.status(),
                'auto_checkin_on_login': CFG.wb_auto_checkin,
                'state_ttl': CFG.wb_state_ttl,
            }}


@router.put('/settings')
async def update_settings(request: Request, user: str = Depends(current_user)):
    data = await _body(request)
    updated: dict[str, Any] = {}
    if 'checkin_enabled' in data:
        db.set_setting('checkin_enabled', bool(data['checkin_enabled']))
        updated['checkin_enabled'] = bool(data['checkin_enabled'])
    if 'model_alias' in data:
        alias = data['model_alias']
        if isinstance(alias, str):
            try:
                alias = json.loads(alias)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail='模型别名必须是合法的 JSON 对象')
        if not isinstance(alias, dict):
            raise HTTPException(status_code=400, detail='模型别名必须是 JSON 对象')
        clean = {str(k).strip(): str(v).strip() for k, v in alias.items() if str(k).strip() and str(v).strip()}
        db.set_setting('model_alias', clean)
        updated['model_alias'] = clean
    for key, (kind, low, high) in _SETTING_RULES.items():
        if key not in data:
            continue
        try:
            value = int(data[key])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f'{key} 必须是整数')
        if not low <= value <= high:
            raise HTTPException(status_code=400, detail=f'{key} 取值范围 {low}-{high}')
        db.set_setting(key, value)
        updated[key] = value
    return {'ok': True, 'items': db.all_settings(), 'updated': updated}
