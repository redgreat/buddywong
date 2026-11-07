"""账号管理：CRUD、加权轮询选号、连通性探测、手动/自动签到、状态与冷却。

两类账号：
  * custom    —— 手工录入的 OpenAI 兼容端点（base_url + api_key）+ 通用 HTTP 签到钩子
  * workbuddy —— 页面扫码登录拿到的腾讯账号，凭据自动维护，走 workbuddy.py 的真实协议签到

buddywong 不依赖任何外部网关：反代时直接从这里的账号池里挑一个转发请求。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import db, workbuddy as wb
from .config import CFG
from .security import model_allowed
from .upstream import auth_headers, make_async_client, upstream_url

logger = logging.getLogger('buddywong.accounts')

_rr_lock = threading.RLock()
_rr_cursor = 0

_ACCOUNT_FIELDS = (
    'id', 'name', 'group_name', 'base_url', 'api_key', 'models', 'enabled', 'weight',
    'status', 'credits', 'expires_at', 'checkin_url', 'checkin_method', 'checkin_headers',
    'checkin_body', 'last_checkin_at', 'last_probe_at', 'last_error', 'fail_count',
    'cool_until', 'success_count', 'fail_total', 'created_at', 'updated_at',
    'source', 'realm', 'wb_uid', 'wb_refresh_token', 'wb_domain', 'wb_enterprise_id',
    'wb_nickname', 'wb_expires_at', 'proxy_path',
)


# ── 工具 ───────────────────────────────────────────────────────
def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return [part.strip() for part in text.replace('\n', ',').split(',') if part.strip()]
    return []


# 对外公开的解析别名（网关列模型 / 判断账号是否支持某模型时复用）
parse_models = _as_list


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _mask(secret: str) -> str:
    text = secret or ''
    return (text[:4] + '••••' + text[-4:]) if len(text) > 10 else ('••••' if text else '')


def effective_status(row: dict, now: float | None = None) -> str:
    now = now if now is not None else time.time()
    if not row.get('enabled'):
        return 'disabled'
    expires = (row.get('expires_at') or '').strip()
    if expires and expires < time.strftime('%Y-%m-%d', time.localtime(now)):
        return 'expired'
    if float(row.get('cool_until') or 0) > now:
        return 'cooling'
    if row.get('status') == 'error':
        return 'error'
    if not row.get('last_probe_at'):
        return 'unknown'
    return str(row.get('status') or 'unknown')


def public(row: dict) -> dict:
    """对外输出：绝不返回明文 api_key / refreshToken（管理端也不需要回显）。"""
    item = {key: row.get(key) for key in _ACCOUNT_FIELDS}
    item['api_key_masked'] = _mask(str(item.pop('api_key') or ''))
    item['wb_refresh_configured'] = bool(item.pop('wb_refresh_token', ''))
    item['models'] = _as_list(item['models'])
    item['checkin_headers'] = _as_dict(item['checkin_headers'])
    item['checkin_body'] = _as_dict(item['checkin_body'])
    item['checkin_supported'] = (item.get('source') != 'workbuddy' or
                                 wb.supports_checkin(item.get('realm'), item.get('wb_enterprise_id')))
    item['effective_status'] = effective_status(row)
    item['cool_remaining'] = max(0, int(float(item.get('cool_until') or 0) - time.time()))
    return item


def _today_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


# ── CRUD ───────────────────────────────────────────────────────
def list_accounts() -> list[dict]:
    rows = db.query('SELECT * FROM accounts ORDER BY group_name, id')
    stats = {
        r['account_id']: r
        for r in db.query(
            'SELECT account_id, COUNT(*) AS total, SUM(ok) AS ok_count, '
            'SUM(total_tokens) AS tokens FROM request_logs WHERE ts >= ? GROUP BY account_id',
            (_today_start(),))
    }
    result = []
    for row in rows:
        item = public(row)
        today = stats.get(row['id'], {})
        item['today_calls'] = int(today.get('total') or 0)
        item['today_ok'] = int(today.get('ok_count') or 0)
        item['today_tokens'] = int(today.get('tokens') or 0)
        result.append(item)
    return result


def get_account(account_id: int) -> dict | None:
    return db.query_one('SELECT * FROM accounts WHERE id = ?', (account_id,))


def normalize_payload(data: dict, existing: dict | None = None) -> dict:
    """校验并规范化前端提交的字段，非法输入直接抛 ValueError。"""
    base = dict(existing or {})
    name = str(data.get('name', base.get('name', '')) or '').strip()
    if not name:
        raise ValueError('账号名称不能为空')
    base_url = str(data.get('base_url', base.get('base_url', '')) or '').strip().rstrip('/')
    parsed = urlparse(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('上游地址必须是 http:// 或 https:// 开头的完整地址')

    api_key = str(data.get('api_key', '') or '').strip()
    if not api_key:
        api_key = str(base.get('api_key', '') or '').strip()
    if not api_key:
        raise ValueError('API Key / Token 不能为空')

    method = str(data.get('checkin_method', base.get('checkin_method', 'POST')) or 'POST').upper()
    if method not in ('GET', 'POST', 'PUT', 'PATCH'):
        raise ValueError('签到方法只支持 GET / POST / PUT / PATCH')

    try:
        weight = max(1, min(20, int(data.get('weight', base.get('weight', 1)) or 1)))
    except (TypeError, ValueError):
        raise ValueError('权重必须是 1-20 的整数')
    try:
        credits = float(data.get('credits', base.get('credits', 0)) or 0)
    except (TypeError, ValueError):
        raise ValueError('积分/余额必须是数字')

    expires = str(data.get('expires_at', base.get('expires_at', '')) or '').strip()
    if expires:
        try:
            time.strptime(expires, '%Y-%m-%d')
        except ValueError:
            raise ValueError('到期日格式应为 YYYY-MM-DD')

    return {
        'name': name,
        'group_name': str(data.get('group_name', base.get('group_name', 'default')) or 'default').strip(),
        'base_url': base_url,
        'api_key': api_key,
        'models': json.dumps([str(m).strip() for m in _as_list(data.get('models', base.get('models'))) if str(m).strip()], ensure_ascii=False),
        'enabled': 1 if data.get('enabled', base.get('enabled', 1)) in (1, True, '1', 'true', 'on', 'yes') else 0,
        'weight': weight,
        'credits': credits,
        'expires_at': expires,
        'checkin_url': str(data.get('checkin_url', base.get('checkin_url', '')) or '').strip(),
        'checkin_method': method,
        'checkin_headers': json.dumps(_as_dict(data.get('checkin_headers', base.get('checkin_headers'))), ensure_ascii=False),
        'checkin_body': json.dumps(_as_dict(data.get('checkin_body', base.get('checkin_body'))), ensure_ascii=False),
    }


def create_account(data: dict) -> dict:
    values = normalize_payload(data)
    now = time.time()
    columns = list(values) + ['status', 'created_at', 'updated_at']
    placeholders = ', '.join('?' for _ in columns)
    new_id = db.execute(
        f'INSERT INTO accounts ({", ".join(columns)}) VALUES ({placeholders})',
        tuple(values.values()) + ('unknown', now, now),
    )
    return public(get_account(new_id))


def update_account(account_id: int, data: dict) -> dict | None:
    existing = get_account(account_id)
    if existing is None:
        return None
    values = normalize_payload(data, existing)
    values['updated_at'] = time.time()
    assignments = ', '.join(f'{key} = ?' for key in values)
    db.execute(f'UPDATE accounts SET {assignments} WHERE id = ?', tuple(values.values()) + (account_id,))
    return public(get_account(account_id))


def delete_account(account_id: int) -> bool:
    if get_account(account_id) is None:
        return False
    db.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
    return get_account(account_id) is None


def set_enabled(account_id: int, enabled: bool) -> dict | None:
    db.execute('UPDATE accounts SET enabled = ?, status = ?, cool_until = 0, updated_at = ? WHERE id = ?',
               (1 if enabled else 0, 'unknown' if enabled else 'disabled', time.time(), account_id))
    row = get_account(account_id)
    return public(row) if row else None


# ── WorkBuddy 扫码登录账号 ───────────────────────────────────────
def upsert_workbuddy(ready: dict) -> dict:
    """把扫码授权结果落库；同一 uid 再次扫码 = 刷新凭据，不产生重复账号。"""
    uid = str(ready.get('uid') or '').strip()
    token = str(ready.get('access_token') or '').strip()
    if not uid or not token:
        raise ValueError('授权结果缺少 uid / accessToken，请重新扫码')
    realm = wb.normalize_realm(ready.get('realm'))
    existing = db.query_one("SELECT * FROM accounts WHERE source = 'workbuddy' AND wb_uid = ?", (uid,))
    now = time.time()
    name = str(ready.get('nickname') or '').strip() or f'WorkBuddy {uid[:8]}'
    values = {
        'name': name,
        'group_name': str(existing['group_name']) if existing else 'workbuddy',
        'base_url': wb.chat_base(realm),
        'api_key': token,
        'enabled': 1,
        'credits': float(existing['credits']) if existing else 0.0,
        'source': 'workbuddy',
        'realm': realm,
        'wb_uid': uid,
        'wb_refresh_token': str(ready.get('refresh_token') or ''),
        'wb_domain': str(ready.get('domain') or ''),
        'wb_enterprise_id': str(ready.get('enterprise_id') or ''),
        'wb_nickname': name,
        'wb_expires_at': float(ready.get('expires_at') or 0),
        'proxy_path': '/v2',          # WorkBuddy 对话端点在 /v2/chat/completions
        'status': 'healthy',
        'last_error': '',
        'fail_count': 0,
        'cool_until': 0,
        'updated_at': now,
    }
    if existing:
        assignments = ', '.join(f'{key} = ?' for key in values)
        db.execute(f'UPDATE accounts SET {assignments} WHERE id = ?',
                   tuple(values.values()) + (existing['id'],))
        account_id = int(existing['id'])
        logger.info('WorkBuddy 账号重新授权：%s（uid=%s）', name, uid)
    else:
        values.update({'models': '[]', 'weight': 1, 'expires_at': '',
                       'checkin_url': '', 'checkin_method': 'POST',
                       'checkin_headers': '{}', 'checkin_body': '{}', 'created_at': now})
        columns = ', '.join(values)
        placeholders = ', '.join('?' for _ in values)
        account_id = db.execute(f'INSERT INTO accounts ({columns}) VALUES ({placeholders})',
                                tuple(values.values()))
        logger.info('WorkBuddy 账号已纳管：%s（uid=%s, realm=%s）', name, uid, realm)
    return public(get_account(account_id))


def set_models(account_id: int, models: list[str]) -> dict | None:
    db.execute('UPDATE accounts SET models = ?, updated_at = ? WHERE id = ?',
               (json.dumps([str(m) for m in models if str(m).strip()], ensure_ascii=False),
                time.time(), account_id))
    row = get_account(account_id)
    return public(row) if row else None


def _apply_refresh(account_id: int, fields: dict) -> None:
    db.execute('UPDATE accounts SET api_key = ?, wb_refresh_token = ?, wb_expires_at = ?, '
               'wb_domain = ?, status = ?, last_error = "", updated_at = ? WHERE id = ?',
               (str(fields.get('access_token') or ''), str(fields.get('refresh_token') or ''),
                float(fields.get('expires_at') or 0), str(fields.get('domain') or ''),
                'healthy', time.time(), account_id))


async def refresh_wb_account(account_id: int) -> dict:
    """刷新 accessToken 并顺带查实时积分；页面「刷新授权」按钮走这里。"""
    row = get_account(account_id)
    if row is None or str(row.get('source')) != 'workbuddy':
        return {'ok': False, 'message': '不是 WorkBuddy 账号'}
    auth = wb.auth_from_row(row)
    ok, message, fields = await wb.refresh_token(auth, auth['realm'])
    if ok:
        _apply_refresh(account_id, fields)
        auth.update(fields)
    fresh = get_account(account_id)
    cok, credits, cmsg = await wb.fetch_credits(wb.auth_from_row(fresh), str(fresh['realm']))
    if cok and credits is not None:
        db.execute('UPDATE accounts SET credits = ?, updated_at = ? WHERE id = ?',
                   (credits, time.time(), account_id))
    return {'ok': ok, 'message': message, 'credits': credits if cok else None,
            'credits_message': cmsg, 'item': public(get_account(account_id))}


# ── 轮询选号 ───────────────────────────────────────────────────
def select_candidates(model: str = '', limit: int | None = None) -> list[dict]:
    """按权重轮转返回可用账号（有序，第一个优先，失败后依次换下一个）。"""
    global _rr_cursor
    limit = limit or db.setting_int('max_account_retries', CFG.max_account_retries)
    now = time.time()
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    rows = db.query(
        'SELECT * FROM accounts WHERE enabled = 1 AND cool_until < ? '
        "AND (expires_at = '' OR expires_at >= ?) ORDER BY id",
        (now, today),
    )
    eligible = [r for r in rows if not model or model_allowed(model, _as_list(r['models']))]
    if not eligible:
        return []
    sequence: list[int] = []
    for row in eligible:
        sequence.extend([row['id']] * max(1, min(10, int(row['weight'] or 1))))
    with _rr_lock:
        start = _rr_cursor % len(sequence)
        _rr_cursor = (_rr_cursor + 1) % len(sequence)
    ordered: list[int] = []
    for offset in range(len(sequence)):
        account_id = sequence[(start + offset) % len(sequence)]
        if account_id not in ordered:
            ordered.append(account_id)
    by_id = {r['id']: r for r in eligible}
    return [by_id[i] for i in ordered[:max(1, limit)]]


def mark_success(account_id: int) -> None:
    db.execute(
        'UPDATE accounts SET status = ?, fail_count = 0, cool_until = 0, last_error = "", '
        'success_count = success_count + 1, updated_at = ? WHERE id = ?',
        ('healthy', time.time(), account_id),
    )


def mark_failure(account_id: int, error: str = '', cool: bool = True) -> None:
    seconds = db.setting_int('cool_seconds', CFG.cool_seconds)
    db.execute(
        'UPDATE accounts SET status = ?, fail_count = fail_count + 1, '
        'fail_total = fail_total + 1, last_error = ?, cool_until = ?, updated_at = ? WHERE id = ?',
        ('cooling' if cool else 'error', (error or '')[:500],
         time.time() + seconds if cool else 0, time.time(), account_id),
    )


# ── 探测与签到 ─────────────────────────────────────────────────
async def probe_account(account_id: int) -> dict:
    row = get_account(account_id)
    if row is None:
        return {'ok': False, 'message': '账号不存在'}
    if str(row.get('source')) == 'workbuddy':
        # 腾讯账号没有 /v1/models：探测 = 拉官方模型目录，顺手刷新可用模型
        try:
            models = await wb.fetch_model_ids(wb.auth_from_row(row), str(row['realm']))
        except Exception as exc:  # noqa: BLE001
            models = []
            logger.warning('WorkBuddy 模型目录探测失败：%s', exc)
        result = {'ok': bool(models), 'status_code': 200 if models else 0, 'models': models,
                  'message': f'连通正常，同步到 {len(models)} 个模型' if models else '未取到模型目录，请检查令牌或网络'}
        if models:
            set_models(account_id, models)
        db.execute('UPDATE accounts SET last_probe_at = ?, status = ?, last_error = ?, updated_at = ? WHERE id = ?',
                   (time.time(), 'healthy' if models else 'error',
                    '' if models else result['message'], time.time(), account_id))
        return result
    url = upstream_url(str(row['base_url']), '/v1/models')
    result: dict[str, Any] = {'ok': False, 'status_code': 0, 'models': [], 'message': ''}
    try:
        async with make_async_client(timeout=20) as client:
            resp = await client.get(url, headers=auth_headers(str(row['api_key']), json_body=False))
        result['status_code'] = resp.status_code
        result['ok'] = 200 <= resp.status_code < 300
        if result['ok']:
            try:
                payload = resp.json()
                result['models'] = [str(item.get('id')) for item in payload.get('data', []) if item.get('id')]
            except ValueError:
                pass
            result['message'] = f'连通正常，发现 {len(result["models"])} 个模型'
        else:
            result['message'] = f'上游返回 {resp.status_code}: {resp.text[:200]}'
    except Exception as exc:  # noqa: BLE001
        result['message'] = f'探测失败：{exc}'
    db.execute('UPDATE accounts SET last_probe_at = ?, status = ?, last_error = ?, updated_at = ? WHERE id = ?',
               (time.time(), 'healthy' if result['ok'] else 'error',
                '' if result['ok'] else result['message'][:500], time.time(), account_id))
    return result


def _extract_credits(payload: Any) -> float | None:
    """从签到响应里尽力提取积分/余额；不同平台字段名不一样，做常见几种兼容。"""
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for key in ('data', 'result', 'body'):
        if isinstance(payload.get(key), dict):
            candidates.append(payload[key])
    for obj in candidates:
        for key in ('credits', 'balance', 'points', 'token', 'tokens', 'quota', 'remain'):
            value = obj.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value.strip())
                except ValueError:
                    continue
    return None


async def _checkin_workbuddy(row: dict, trigger: str) -> dict:
    """真实 WorkBuddy 签到：令牌保鲜 -> daily-checkin -> 查实时积分 -> 落库。"""
    account_id = int(row['id'])
    auth = wb.auth_from_row(row)
    realm = auth['realm']
    now = time.time()
    result: dict[str, Any] = {'ok': False, 'skipped': False, 'status_code': 0,
                              'message': '', 'credits': None,
                              'account_id': account_id, 'account_name': str(row['name'])}
    log_trigger = trigger

    if not wb.supports_checkin(realm, auth.get('enterprise_id')):
        result['skipped'] = True
        result['status_code'] = -2
        result['message'] = '国际版 / 企业账号没有个人签到体系，已跳过'
        log_trigger = 'skip'
        db.execute('UPDATE accounts SET updated_at = ? WHERE id = ?', (now, account_id))
    else:
        # 签到用的是 accessToken；临近过期先用 refreshToken 换新的，否则签到必失败。
        expires = float(row.get('wb_expires_at') or 0)
        if expires and expires - now < 600 and auth['refresh_token']:
            refreshed, message, fields = await wb.refresh_token(auth, realm)
            if refreshed:
                _apply_refresh(account_id, fields)
                auth.update(fields)
            else:
                logger.warning('账号 %s 签到前刷新令牌失败：%s（仍尝试签到）',
                               row['name'], message)

        code, message = await wb.checkin(auth, realm)
        result['status_code'] = int(code)
        result['ok'] = code in (0, 10001)
        result['message'] = message + ('（可稍后重试）' if code == -3 else '')
        if result['ok']:
            credits_ok, credits, credits_msg = await wb.fetch_credits(auth, realm)
            if credits_ok and credits is not None:
                result['credits'] = credits
            else:
                logger.info('账号 %s 积分回查未成功：%s', row['name'], credits_msg)

        if result['ok']:
            if result['credits'] is None:
                db.execute('UPDATE accounts SET last_checkin_at = ?, status = ?, last_error = "", '
                           'fail_count = 0, updated_at = ? WHERE id = ?',
                           (now, 'healthy', now, account_id))
            else:
                db.execute('UPDATE accounts SET last_checkin_at = ?, credits = ?, status = ?, '
                           'last_error = "", fail_count = 0, updated_at = ? WHERE id = ?',
                           (now, result['credits'], 'healthy', now, account_id))
        else:
            db.execute('UPDATE accounts SET last_checkin_at = ?, status = ?, last_error = ?, '
                       'updated_at = ? WHERE id = ?',
                       (now, 'error', result['message'][:500], now, account_id))

    db.execute(
        'INSERT INTO checkin_logs(ts, account_id, account_name, ok, status_code, message, trigger) '
        'VALUES(?, ?, ?, ?, ?, ?, ?)',
        (now, account_id, row['name'], 1 if result['ok'] else 0,
         result['status_code'], result['message'][:500], log_trigger),
    )
    return result


async def checkin_account(account_id: int, trigger: str = 'manual') -> dict:
    """执行一次签到；WorkBuddy 账号走真实协议，手工账号走通用 HTTP 钩子。"""
    row = get_account(account_id)
    if row is None:
        return {'ok': False, 'message': '账号不存在'}
    if str(row.get('source')) == 'workbuddy':
        return await _checkin_workbuddy(row, trigger)
    url = str(row['checkin_url'] or '').strip()
    if not url:
        return {'ok': False, 'skipped': True, 'message': '未配置签到地址'}
    extra_headers = _as_dict(row['checkin_headers'])
    headers = auth_headers(str(row['api_key']), extra_headers)
    body = _as_dict(row['checkin_body'])
    method = str(row['checkin_method'] or 'POST').upper()
    result: dict[str, Any] = {'ok': False, 'status_code': 0, 'message': '', 'credits': None}
    try:
        async with make_async_client(timeout=30) as client:
            kwargs: dict[str, Any] = {'headers': headers}
            if method != 'GET' and body:
                kwargs['json'] = body
            resp = await client.request(method, url, **kwargs)
        result['status_code'] = resp.status_code
        result['ok'] = 200 <= resp.status_code < 300
        text = resp.text[:300]
        try:
            payload = resp.json()
            result['credits'] = _extract_credits(payload)
            if isinstance(payload, dict):
                text = str(payload.get('message') or payload.get('msg') or text)
        except ValueError:
            pass
        result['message'] = (('签到成功' if result['ok'] else '签到失败') + f'（HTTP {resp.status_code}）：{text}')[:500]
    except Exception as exc:  # noqa: BLE001
        result['message'] = f'签到异常：{exc}'[:500]

    now = time.time()
    if result['ok']:
        if result['credits'] is None:
            db.execute('UPDATE accounts SET last_checkin_at = ?, status = ?, last_error = "", '
                       'fail_count = 0, updated_at = ? WHERE id = ?',
                       (now, 'healthy', now, account_id))
        else:
            db.execute('UPDATE accounts SET last_checkin_at = ?, credits = ?, status = ?, last_error = "", '
                       'fail_count = 0, updated_at = ? WHERE id = ?',
                       (now, result['credits'], 'healthy', now, account_id))
    else:
        db.execute('UPDATE accounts SET last_checkin_at = ?, last_error = ?, updated_at = ? WHERE id = ?',
                   (now, result['message'][:500], now, account_id))
    db.execute(
        'INSERT INTO checkin_logs(ts, account_id, account_name, ok, status_code, message, trigger) '
        'VALUES(?, ?, ?, ?, ?, ?, ?)',
        (now, account_id, row['name'], 1 if result['ok'] else 0,
         result['status_code'], result['message'], trigger),
    )
    result['account_id'] = account_id
    result['account_name'] = row['name']
    return result


async def checkin_all(trigger: str = 'auto') -> list[dict]:
    rows = db.query(
        "SELECT id FROM accounts WHERE enabled = 1 AND (checkin_url <> '' "
        "OR (source = 'workbuddy' AND realm <> 'global' AND wb_enterprise_id = '')) ORDER BY id")
    results = []
    for row in rows:
        results.append(await checkin_account(row['id'], trigger))
        await asyncio.sleep(0.3)   # 轻微错峰，别一次性把平台打爆
    return results


def list_checkin_logs(limit: int = 100, account_id: int | None = None) -> list[dict]:
    if account_id:
        return db.query('SELECT * FROM checkin_logs WHERE account_id = ? ORDER BY ts DESC LIMIT ?',
                        (account_id, min(500, max(1, limit))))
    return db.query('SELECT * FROM checkin_logs ORDER BY ts DESC LIMIT ?', (min(500, max(1, limit)),))
