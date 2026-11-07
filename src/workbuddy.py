"""WorkBuddy / CodeBuddy 登录与签到协议客户端（对齐 workbuddy-manager 的 services/tencent.py）。

buddywong 的账号有两个来源：
  * custom     —— 手工录入的 OpenAI 兼容端点，签到走通用 HTTP 钩子（accounts.py）
  * workbuddy  —— 页面扫码登录拿到的腾讯账号，签到/积分/刷新走这里的真实协议

协议要点（这些是"能签到"的关键，缺一条就会静默失效）：
  1. 扫码：POST {chat_base}/v2/plugin/auth/state?platform=CLI -> {state, authUrl}
           GET  {chat_base}/v2/plugin/auth/token?state=...   -> accessToken/refreshToken
           GET  {chat_base}/v2/plugin/login/account?state=... -> uid/nickname/enterpriseId
  2. 签到：POST {billing_base}/v2/billing/meter/daily-checkin，带 billing 身份头。
           code=0 成功；code=10001 有两层语义（"今天已签到"是幂等成功，
           "签到失败，请稍后重试"是瞬时限流），必须看 msg 区分，否则会把失败记成成功。
  3. 国际版没有签到体系，企业账号没有个人成长体系，两者直接跳过而不是打 4xx。
  4. 出站请求头要像官方客户端（UA / Origin / X-CodeBuddy-Request / 设备指纹 / X-User-Id）；
     UA 与 X-IDE-Version 的版本号由 versions.py 自动跟随官方发布源，不写死在配置里。

安全边界：accessToken / refreshToken 只在服务端内存 state 缓存与 SQLite 里，
**绝不通过 /api/wb/auth/poll 下发给前端**；前端只拿得到 uid / 昵称 / 状态。
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import threading
import time
from typing import Any

from . import versions
from .config import CFG
from .upstream import make_async_client

logger = logging.getLogger('buddywong.workbuddy')

CN = 'cn'
GLOBAL = 'global'
CN_ORIGIN = 'https://www.codebuddy.cn'
_ALREADY_MARKERS = ('已签到', '已经签到', 'already checked in',
                    'already signed in', 'already checkin')


class WorkBuddyError(Exception):
    """网络层 / 协议层错误，转 502 时带可执行的中文原因。"""


# ── 端点与请求头 ────────────────────────────────────────────────
def normalize_realm(value: Any) -> str:
    return GLOBAL if str(value or '').strip().lower() == GLOBAL else CN


def chat_base(realm: str = CN) -> str:
    return CFG.wb_global_base if normalize_realm(realm) == GLOBAL else CFG.wb_cn_chat_base


def billing_base(realm: str = CN) -> str:
    return CFG.wb_global_base if normalize_realm(realm) == GLOBAL else CFG.wb_cn_billing_base


def origin(realm: str = CN) -> str:
    return CFG.wb_global_base if normalize_realm(realm) == GLOBAL else CN_ORIGIN


def _ua(realm: str = CN) -> str:
    # 版本由 versions.py 自动跟随官方发布源，config.yml 里不需要写死。
    ver = versions.client_version(realm)
    platform = 'WorkBuddy AI' if normalize_realm(realm) == GLOBAL else 'WorkBuddy'
    return f'WorkBuddy/{ver} {platform}/{ver} CLI/{versions.cli_version()}'


def _billing_ua(realm: str = CN) -> str:
    """billing 域官方客户端显式覆写成单段 UA（没有 CLI 段）。"""
    return f'WorkBuddy/{versions.client_version(realm)}'


def _device_headers(uid: str) -> dict[str, str]:
    """每账号一台稳定虚拟设备：由 uid 派生，不随机、不落地。"""
    if not uid:
        return {}
    def derive(purpose: str) -> str:
        return hashlib.sha256(f'buddywong:{purpose}:{uid}'.encode()).hexdigest().upper()
    return {'X-Machine-ID': derive('machine'), 'X-Session-ID': derive('session')}


def headers(realm: str = CN, token: str = '', uid: str = '') -> dict[str, str]:
    h = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Accept-Language': 'en-US' if normalize_realm(realm) == GLOBAL else 'zh-CN',
        'X-Requested-With': 'XMLHttpRequest',
        'X-CodeBuddy-Request': '1',
        'User-Agent': _ua(realm),
        'X-IDE-Version': versions.full_version(realm),
        'Origin': origin(realm),
        'Referer': origin(realm) + '/',
    }
    if token:
        h['Authorization'] = f'Bearer {token}'
    h.update(_device_headers(uid))
    return h


def billing_headers(realm: str, auth: dict) -> dict[str, str]:
    """签到 / 积分 / trial 这类 billing 接口的身份头，不带会被风控判成异常来源。"""
    uid = str(auth.get('uid') or '')
    h = headers(realm, str(auth.get('access_token') or ''), uid)
    h['User-Agent'] = _billing_ua(realm)
    if uid:
        h['X-User-Id'] = uid
    ent = str(auth.get('enterprise_id') or '')
    if ent:
        h['X-Enterprise-Id'] = ent
        h['X-Tenant-Id'] = ent
    domain = str(auth.get('domain') or '')
    if domain:
        h['X-Domain'] = domain
    return h


def auth_from_row(row: dict) -> dict:
    """把 accounts 表的一行转成协议层用的 auth dict。"""
    return {
        'access_token': str(row.get('api_key') or ''),
        'refresh_token': str(row.get('wb_refresh_token') or ''),
        'uid': str(row.get('wb_uid') or ''),
        'enterprise_id': str(row.get('wb_enterprise_id') or ''),
        'domain': str(row.get('wb_domain') or ''),
        'realm': normalize_realm(row.get('realm') or CN),
    }


def chat_headers(row: dict) -> dict[str, str]:
    """反代转发 WorkBuddy 账号时用的头（手工账号走 upstream.auth_headers）。"""
    auth = auth_from_row(row)
    realm = auth['realm']
    h = headers(realm, auth['access_token'], auth['uid'])
    h['Accept'] = 'application/json'
    if auth['uid']:
        h['X-User-Id'] = auth['uid']
    if auth['enterprise_id']:
        h['X-Enterprise-Id'] = auth['enterprise_id']
        h['X-Tenant-Id'] = auth['enterprise_id']
    if auth['domain']:
        h['X-Domain'] = auth['domain']
    return h


def supports_checkin(realm: str, enterprise_id: str = '') -> bool:
    """国际版无签到体系；企业账号没有个人成长体系。"""
    return normalize_realm(realm) != GLOBAL and not str(enterprise_id or '').strip()


def _envelope(resp: Any) -> tuple[int, Any]:
    """腾讯接口统一信封 {code, msg, data}；HTTP 4xx 也可能是业务信封。"""
    try:
        env = resp.json()
    except Exception:  # noqa: BLE001
        return int(getattr(resp, 'status_code', 0) or 0), None
    if isinstance(env, dict) and 'code' in env:
        try:
            return int(env.get('code', -1)), env.get('data')
        except (TypeError, ValueError):
            return -1, env.get('data')
    return int(getattr(resp, 'status_code', 0) or 0), env


def is_already_checkin(code: int, message: str = '') -> bool:
    """code=10001 且文案表示"今日已签到"才算幂等成功；空 msg 保持旧行为。"""
    if code != 10001:
        return False
    msg = (message or '').strip().lower()
    if not msg:
        return True
    return any(m in msg for m in _ALREADY_MARKERS)


def _net_hint(exc: Exception) -> str:
    return (f'连不上 WorkBuddy 接口：{exc}。请检查容器的外网连通性 / DNS，'
            '或在 config.yml 的 proxy.http_proxy（BW_HTTP_PROXY）配置出站代理')


def _client(timeout: float = 20):
    return make_async_client(timeout=timeout)


# ── 扫码登录 ────────────────────────────────────────────────────
# state -> {at, realm, auth_url, ready}；ready 只在服务端，落盘成功后才 drop。
_state_cache: dict[str, dict] = {}
_state_lock = threading.RLock()


def _prune_states() -> None:
    cutoff = time.time() - CFG.wb_state_ttl
    with _state_lock:
        for key in [k for k, v in _state_cache.items() if float(v.get('at') or 0) < cutoff]:
            _state_cache.pop(key, None)


async def start_login(realm: str = CN) -> dict:
    """发起扫码登录，返回 state / authUrl；authUrl 用来生成二维码或直接在浏览器打开。"""
    realm = normalize_realm(realm)
    try:
        async with _client() as client:
            resp = await client.post(
                f'{chat_base(realm)}/v2/plugin/auth/state',
                params={'platform': 'CLI'}, json={}, headers=headers(realm))
    except Exception as exc:  # noqa: BLE001
        raise WorkBuddyError(_net_hint(exc)) from exc
    code, data = _envelope(resp)
    if code != 0 or not isinstance(data, dict) or not data.get('state'):
        raise WorkBuddyError(f'获取授权链接失败 code={code}（腾讯接口未返回 state）')
    state = str(data['state'])
    auth_url = str(data.get('authUrl') or '')
    _prune_states()
    with _state_lock:
        _state_cache[state] = {'at': time.time(), 'realm': realm,
                               'auth_url': auth_url, 'ready': None}
    return {'state': state, 'auth_url': auth_url, 'realm': realm,
            'ttl': CFG.wb_state_ttl, 'supports_checkin': supports_checkin(realm)}


def public_ready(ready: dict) -> dict:
    """下发给前端的 ready：不含任何 token。"""
    return {'status': 'ready', 'uid': ready.get('uid', ''),
            'nickname': ready.get('nickname', ''), 'realm': ready.get('realm', CN),
            'enterprise_id': ready.get('enterprise_id', ''),
            'expires_at': ready.get('expires_at', 0)}


async def poll_login(state: str, realm: str | None = None) -> dict:
    """轮询扫码结果：waiting / ready / expired / invalid / realm_mismatch。"""
    with _state_lock:
        entry = _state_cache.get(state)
        entry = dict(entry) if entry else None
    if entry is None:
        return {'status': 'invalid'}
    if time.time() - float(entry.get('at') or 0) > CFG.wb_state_ttl:
        drop_state(state)
        return {'status': 'expired'}
    registered = entry['realm']
    if realm is not None and normalize_realm(realm) != registered:
        return {'status': 'realm_mismatch', 'expected': registered,
                'got': normalize_realm(realm)}
    if entry.get('ready'):
        return public_ready(entry['ready'])

    try:
        async with _client() as client:
            resp = await client.get(
                f'{chat_base(registered)}/v2/plugin/auth/token',
                params={'state': state}, headers=headers(registered))
            code, data = _envelope(resp)
            if code != 0 or not isinstance(data, dict) or not data.get('accessToken'):
                return {'status': 'waiting'}
            access_token = str(data['accessToken'])
            refresh_token = str(data.get('refreshToken') or '')
            try:
                expires_in = int(data.get('expiresIn') or 3600)
            except (TypeError, ValueError):
                expires_in = 3600
            domain = str(data.get('domain') or '')
            acct_resp = await client.get(
                f'{chat_base(registered)}/v2/plugin/login/account',
                params={'state': state}, headers=headers(registered, access_token))
    except Exception as exc:  # noqa: BLE001
        raise WorkBuddyError(_net_hint(exc)) from exc

    _, acct = _envelope(acct_resp)
    acct = acct if isinstance(acct, dict) else {}
    uid = str(acct.get('uid') or '')
    if not uid:
        return {'status': 'waiting'}

    ready = {
        'uid': uid,
        'nickname': str(acct.get('nickname') or '') or f'WorkBuddy {uid[:8]}',
        'enterprise_id': str(acct.get('enterpriseId') or ''),
        'access_token': access_token,
        'refresh_token': refresh_token,
        'domain': domain,
        'expires_at': int(time.time()) + expires_in,
        'realm': registered,
    }
    # 不在这里 drop：落盘失败时前端还要用同一个 state 重试，重扫二维码没意义。
    with _state_lock:
        if state in _state_cache:
            _state_cache[state]['ready'] = ready
    logger.info('WorkBuddy 授权成功：uid=%s realm=%s', uid, registered)
    return public_ready(ready)


def take_ready(state: str) -> dict | None:
    """取走服务端保管的授权结果（含 token），只给 /api/wb/auth/finish 用。"""
    with _state_lock:
        entry = _state_cache.get(state)
        if not entry:
            return None
        if time.time() - float(entry.get('at') or 0) > CFG.wb_state_ttl:
            _state_cache.pop(state, None)
            return None
        return dict(entry['ready']) if entry.get('ready') else None


def drop_state(state: str) -> None:
    with _state_lock:
        _state_cache.pop(state, None)


def state_age(state: str) -> float | None:
    with _state_lock:
        entry = _state_cache.get(state)
        return time.time() - float(entry['at']) if entry else None


def qr_data_uri(text: str) -> str:
    """把授权链接编码成 PNG data URI（前端 <img> 直接用，无需额外鉴权请求）。"""
    if not text:
        return ''
    try:
        import segno
        buf = io.BytesIO()
        segno.make(text, error='m').save(buf, kind='png', scale=6, border=2,
                                         dark='#0b1626', light='#ffffff')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception as exc:  # noqa: BLE001 — 二维码失败不阻断「复制授权链接」这条路
        logger.warning('二维码生成失败：%s', exc)
        return ''


# ── 签到 / 积分 / 令牌 ──────────────────────────────────────────
async def checkin(auth: dict, realm: str = CN) -> tuple[int, str]:
    """每日签到。返回 (code, message)：
    0 成功 / 10001 今日已签到 / -2 不支持签到（国际版或企业号） /
    -3 上游瞬时拒绝（10001「稍后重试」）/ -1 网络异常 / 其它业务码原样返回。
    """
    realm = normalize_realm(realm)
    if not supports_checkin(realm, auth.get('enterprise_id')):
        return -2, '国际版或企业账号没有个人签到体系，已跳过'
    path = '/billing/meter/daily-checkin' if realm == GLOBAL else '/v2/billing/meter/daily-checkin'
    try:
        async with _client(timeout=30) as client:
            resp = await client.post(f'{billing_base(realm)}{path}', json={},
                                     headers=billing_headers(realm, auth))
        code, _ = _envelope(resp)
        if code == 0:
            return 0, '签到成功'
        if code == 10001:
            msg = ''
            try:
                env = resp.json()
                if isinstance(env, dict):
                    msg = str(env.get('msg') or '')
            except Exception:  # noqa: BLE001
                pass
            if is_already_checkin(code, msg):
                return 10001, '今日已签到'
            return -3, msg or '签到失败，请稍后重试'
        return code, f'签到返回 code={code}'
    except Exception as exc:  # noqa: BLE001
        return -1, f'签到异常：{exc}'


def _resource_accounts(data: Any) -> list | None:
    cur = data
    for _ in range(4):
        if isinstance(cur, dict):
            if isinstance(cur.get('Accounts'), list):
                return cur['Accounts']
            for key in ('Response', 'Data', 'data', 'response'):
                if isinstance(cur.get(key), (dict, list)):
                    cur = cur[key]
                    break
            else:
                return None
        elif isinstance(cur, list):
            return cur
        else:
            return None
    return None


def _package_remain(item: dict) -> float:
    def num(key: str) -> float:
        value = item.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    size = num('CycleCapacitySize')
    if size > 0:
        remain = min(max(num('CycleCapacityRemain'), 0.0), size)
        used = max(size - remain, num('CycleCapacityUsed'))
        return max(0.0, size - used) if size >= used else 0.0
    return num('CapacityRemain')


async def fetch_credits(auth: dict, realm: str = CN) -> tuple[bool, float | None, str]:
    """查实时积分（上游只在定时任务刷新，签到不会自动更新余额）。"""
    realm = normalize_realm(realm)
    if auth.get('enterprise_id'):
        return False, None, '企业账号额度不在个人资源包体系内，暂不查询'
    now = time.time()
    body = {
        'PageNumber': 1, 'PageSize': 100, 'ProductCode': 'p_tcaca', 'Status': [0, 3],
        'PackageEndTimeRangeBegin': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        'PackageEndTimeRangeEnd': time.strftime('%Y-%m-%d %H:%M:%S',
                                                time.localtime(now + 365 * 101 * 86400)),
    }
    suffix = 'get-user-resource'
    paths = ([f'/billing/meter/{suffix}', f'/v2/billing/meter/{suffix}']
             if realm == GLOBAL else [f'/v2/billing/meter/{suffix}'])
    try:
        async with _client(timeout=30) as client:
            resp = None
            for path in paths:
                resp = await client.post(f'{billing_base(realm)}{path}', json=body,
                                         headers=billing_headers(realm, auth))
                if getattr(resp, 'status_code', 0) != 404:
                    break
        if resp is None:
            return False, None, '积分接口没有返回'
        code, data = _envelope(resp)
        if code != 0 or not data:
            return False, None, f'查询失败 code={code}'
        items = _resource_accounts(data)
        if items is None:
            return False, None, '积分响应结构无法识别'
        total = sum(max(0.0, _package_remain(item)) for item in items if isinstance(item, dict))
        return True, (int(total) if float(total).is_integer() else round(total, 2)), '查询成功'
    except Exception as exc:  # noqa: BLE001
        return False, None, f'查询异常：{exc}'


async def refresh_token(auth: dict, realm: str = CN) -> tuple[bool, str, dict]:
    """用 refreshToken 换新 accessToken；返回 (ok, 说明, 新字段)。"""
    realm = normalize_realm(realm)
    refresh = str(auth.get('refresh_token') or '').strip()
    if not refresh:
        return False, '该账号没有 refreshToken，只能重新扫码登录', {}
    uid = str(auth.get('uid') or '')
    h = headers(realm, str(auth.get('access_token') or ''), uid)
    h['X-Refresh-Token'] = refresh
    h['X-Auth-Refresh-Source'] = 'plugin'
    if uid:
        h['X-User-Id'] = uid
    if auth.get('enterprise_id'):
        h['X-Enterprise-Id'] = str(auth['enterprise_id'])
    try:
        async with _client(timeout=30) as client:
            resp = await client.post(f'{chat_base(realm)}/v2/plugin/auth/token/refresh', headers=h)
    except Exception as exc:  # noqa: BLE001
        return False, f'刷新异常：{exc}', {}
    code, data = _envelope(resp)
    if code != 0 or not isinstance(data, dict) or not data.get('accessToken'):
        return False, f'刷新失败 code={code}（可能需要重新扫码）', {}
    try:
        expires_in = int(data.get('expiresIn') or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return True, '刷新成功', {
        'access_token': str(data['accessToken']),
        'refresh_token': str(data.get('refreshToken') or refresh),
        'expires_at': int(time.time()) + expires_in,
        'domain': str(data.get('domain') or auth.get('domain') or ''),
    }


def _collect_model_ids(node: Any, depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 4:
        return out
    if isinstance(node, list):
        for item in node:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                mid = item.get('id') or item.get('modelId') or item.get('model')
                if isinstance(mid, str) and mid.strip():
                    out.append(mid.strip())
                else:
                    out.extend(_collect_model_ids(item, depth + 1))
    elif isinstance(node, dict):
        for key in ('models', 'cli', 'agents', 'data', 'Data', 'Response'):
            if key in node:
                out.extend(_collect_model_ids(node[key], depth + 1))
    return out


async def fetch_model_ids(auth: dict, realm: str = CN) -> list[str]:
    """尽力拉该账号可用模型 ID；拿不到就返回空列表（账号按"不限制模型"处理）。"""
    realm = normalize_realm(realm)
    paths = (['/v2/enterprises/personal/models', '/console/enterprises/personal/models',
              '/v3/config'] if realm == GLOBAL
             else ['/console/enterprises/personal/models', '/v3/config'])
    h = headers(realm, str(auth.get('access_token') or ''), str(auth.get('uid') or ''))
    found: list[str] = []
    try:
        async with _client(timeout=20) as client:
            for path in paths:
                resp = await client.get(f'{chat_base(realm)}{path}', headers=h)
                code, data = _envelope(resp)
                if code == 0 and data:
                    found.extend(_collect_model_ids(data))
    except Exception as exc:  # noqa: BLE001 — 模型目录失败不影响签到主流程
        logger.info('模型目录拉取失败（按不限模型处理）：%s', exc)
    seen: set[str] = set()
    unique: list[str] = []
    for model in found:
        if model and model not in seen:
            seen.add(model)
            unique.append(model)
    return unique[:100]
