"""官方客户端版本自动跟随 —— 不写死在 config.yml，跟着官方发布走。

两个真实来源（2026-11 实测，均为官方公开只读接口）：

  client  GET {cn_billing_base}/v2/update?platform=workbuddy-win32-x64-user
          -> {"version":"5.9.3.42398134","productVersion":"5.9.3.42398134", ...}
          官网下载组件自己调的就是这个接口；国际版用 global_base 的同名接口。
  cli     GET https://registry.npmjs.org/@tencent-ai%2Fcodebuddy-code/latest
          -> {"version":"2.181.0", ...}
          CodeBuddy Code CLI（UA 里的 CLI/ 段）的官方 npm 包，备用源走国内镜像。

UA 主版本段沿用官方客户端历史形态取前三段（5.9.3），完整四段版本记在
X-IDE-Version / 设置页里供核对。这样官方升级后无需改配置、无需重新打包镜像。

降级链：手动钉死 > 上次成功探测值（落盘，重启保留）> 内置默认。
探测失败绝不抛错、绝不阻塞签到：保留旧值，只记录错误。所有读取函数都是
**同步且零网络 IO**，直接返回内存值；网络探测只在后台调度 / 手动刷新时发生。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from .config import CFG
from .upstream import make_async_client

logger = logging.getLogger('buddywong.versions')

CN = 'cn'
GLOBAL = 'global'

# 与 workbuddy-manager / 上游 workbuddy2api 一致的兜底值；只有所有来源都失败且
# 从未成功探测过时才会用到，之后就一直用「上次成功值」，不会倒退回旧版本。
_DEFAULTS = {'client_cn': '5.5.4', 'client_global': '5.5.4', 'cli': '2.137.1'}
_CACHE_FILE = 'wb_versions.json'

# 官网 /v2/update 的 platform 标识（见官网下载组件 download-*.js）；多个候选
# 依次尝试，官方以后改标识也不至于立刻断供。
_CLIENT_PLATFORMS = ('workbuddy-win32-x64-user', 'workbuddy-darwin-arm64')
# CLI 包官方 registry + 国内镜像（容器在国内时 npmjs 可能慢/不通）
_CLI_REGISTRIES = (
    'https://registry.npmjs.org/@tencent-ai%2Fcodebuddy-code/latest',
    'https://registry.npmmirror.com/@tencent-ai/codebuddy-code/latest',
)

# 整串严格匹配：前缀匹配会把 "5.9.3\r\nX-Evil: 1" 这类脏数据静默截成 5.9.3
_VERSION_RE = re.compile(r'^\d+(?:\.\d+){2,3}$')

_lock = threading.RLock()
_probe_lock = threading.Lock()
_state: dict[str, Any] = {
    'client_cn': _DEFAULTS['client_cn'],
    'client_global': _DEFAULTS['client_global'],
    'cli': _DEFAULTS['cli'],
    'full_cn': '',
    'full_global': '',
    'source': 'builtin',       # builtin | cached | official | pinned
    'checked_at': 0.0,         # 上次尝试探测时间（失败也更新，避免每 20s 狂刷）
    'fetched_at': 0.0,         # 上次成功探测时间
    'last_error': '',
    'sources': {},             # 每个来源的本次结果，设置页可读
}


def _clean_version(raw: Any) -> str:
    """严格校验，防止把任意字符串塞进 HTTP 头（头注入 / 脏数据）。"""
    if not isinstance(raw, str):
        return ''
    value = raw.strip()
    return value if len(value) <= 40 and _VERSION_RE.fullmatch(value) else ''


def _short(version: str) -> str:
    """5.9.3.42398134 -> 5.9.3（UA 主版本段保持官方历史三段形态）。"""
    return '.'.join(version.split('.')[:3])


def _cache_path() -> Path:
    return Path(CFG.data_dir) / _CACHE_FILE


def reload() -> None:
    """从 data/wb_versions.json 恢复上次成功探测值（模块导入与测试时调用）。"""
    with _lock:
        for key, value in _DEFAULTS.items():
            _state[key] = value
        _state.update({'full_cn': '', 'full_global': '', 'source': 'builtin',
                       'checked_at': 0.0, 'fetched_at': 0.0,
                       'last_error': '', 'sources': {}})
        try:
            path = _cache_path()
            if not path.exists():
                return
            saved = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                return
            for key in _DEFAULTS:
                value = _clean_version(saved.get(key))
                if value:
                    _state[key] = _short(value) if key.startswith('client') else value
            for key in ('full_cn', 'full_global'):
                value = _clean_version(saved.get(key))
                if value:
                    _state[key] = value
            _state['fetched_at'] = float(saved.get('fetched_at') or 0)
            _state['checked_at'] = float(saved.get('checked_at') or 0)
            _state['source'] = 'cached' if _state['fetched_at'] else 'builtin'
        except Exception as exc:  # noqa: BLE001 — 缓存损坏不该影响启动
            logger.warning('版本缓存读取失败，使用内置默认：%s', exc)


def _persist() -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            payload = {key: _state[key] for key in (*_DEFAULTS, 'full_cn', 'full_global',
                                                    'checked_at', 'fetched_at')}
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    except OSError as exc:
        logger.warning('版本缓存写入失败（不影响运行）：%s', exc)


def _realm_key(realm: str) -> str:
    return 'client_global' if str(realm or '').strip().lower() == GLOBAL else 'client_cn'


def client_version(realm: str = CN) -> str:
    """UA 用的客户端版本（三段）。同步、零 IO。"""
    pinned = _clean_version(CFG.wb_client_version)
    if pinned:
        return pinned
    with _lock:
        return str(_state[_realm_key(realm)] or _DEFAULTS[_realm_key(realm)])


def full_version(realm: str = CN) -> str:
    """X-IDE-Version 用的完整产品版本；没探测到时退回三段主版本。"""
    pinned = _clean_version(CFG.wb_client_version)
    if pinned:
        return pinned
    key = _realm_key(realm)
    with _lock:
        return str(_state['full_global' if key == 'client_global' else 'full_cn']
                   or _state[key] or _DEFAULTS[key])


def cli_version() -> str:
    """UA 的 CLI/ 段。同步、零 IO。"""
    pinned = _clean_version(CFG.wb_cli_version)
    if pinned:
        return pinned
    with _lock:
        return str(_state['cli'] or _DEFAULTS['cli'])


def pinned() -> dict[str, str]:
    """手动钉死的值（空串 = 自动跟随）。"""
    return {'client': _clean_version(CFG.wb_client_version),
            'cli': _clean_version(CFG.wb_cli_version)}


def _client_urls(realm: str) -> list[str]:
    bases = ([CFG.wb_cn_billing_base, CFG.wb_cn_chat_base]
             if realm == CN else [CFG.wb_global_base])
    urls: list[str] = []
    for base in dict.fromkeys(b.rstrip('/') for b in bases if b):
        for platform in _CLIENT_PLATFORMS:
            urls.append(f'{base}/v2/update?platform={platform}')
    return urls


def _cli_urls() -> list[str]:
    configured = getattr(CFG, 'wb_cli_registry_urls', None)
    if isinstance(configured, (list, tuple)) and configured:
        return [str(u) for u in configured if str(u).startswith('https://')]
    return list(_CLI_REGISTRIES)


async def _probe(client: Any, urls: list[str]) -> tuple[str, str, dict]:
    """依次尝试候选源，返回 (完整版本, 命中的 URL, 结果明细)。"""
    detail: dict = {'ok': False, 'url': '', 'error': ''}
    errors: list[str] = []
    for url in urls:
        try:
            resp = await client.get(url)
            status = int(getattr(resp, 'status_code', 0) or 0)
            if status != 200:
                errors.append(f'{url} -> HTTP {status}')
                continue
            data = resp.json()
            if not isinstance(data, dict):
                errors.append(f'{url} -> 响应不是对象')
                continue
            version = _clean_version(data.get('version') or data.get('productVersion'))
            if not version:
                errors.append(f'{url} -> 响应里没有合法 version')
                continue
            detail.update({'ok': True, 'url': url, 'error': ''})
            return version, url, detail
        except Exception as exc:  # noqa: BLE001 — 单源失败继续试下一个
            errors.append(f'{url} -> {type(exc).__name__}: {exc}')
    detail['error'] = '; '.join(errors)[:400]
    return '', '', detail


async def refresh(force: bool = False) -> dict:
    """探测官方发布源并更新内存 + 磁盘缓存。失败保留旧值，永不抛错。

    同一时刻只允许一次探测（scheduler 线程与手动刷新按钮可能并发）。
    """
    ttl = max(1, int(getattr(CFG, 'wb_version_refresh_hours', 6))) * 3600
    now = time.time()
    with _lock:
        if not force and now - float(_state['checked_at']) < ttl:
            return status()
        _state['checked_at'] = now
    if not _probe_lock.acquire(blocking=False):
        with _lock:
            return status()
    try:
        pins = pinned()
        sources: dict[str, dict] = {}
        # 手动钉死的项目不探测，直接标记为 pinned
        for name in ('client_cn', 'client_global'):
            if pins['client']:
                sources[name] = {'ok': True, 'url': '手动钉死', 'error': ''}
        if pins['cli']:
            sources['cli'] = {'ok': True, 'url': '手动钉死', 'error': ''}

        jobs: list[tuple[str, list[str]]] = []
        if not pins['client']:
            jobs.append(('client_cn', _client_urls(CN)))
            jobs.append(('client_global', _client_urls(GLOBAL)))
        if not pins['cli']:
            jobs.append(('cli', _cli_urls()))

        try:
            async with make_async_client(timeout=5) as client:
                for name, urls in jobs:
                    version, _, detail = await _probe(client, urls)
                    detail['version'] = version
                    sources[name] = detail
                    with _lock:
                        if version:
                            _state[name] = _short(version) if name.startswith('client') else version
                            if name == 'client_cn':
                                _state['full_cn'] = version
                            elif name == 'client_global':
                                _state['full_global'] = version
                        else:
                            logger.info('版本源 %s 未取到新版本，保留当前值 %s：%s',
                                        name, _state[name], detail['error'][:160])
        except Exception as exc:  # noqa: BLE001
            sources['_client'] = {'ok': False, 'url': '', 'error': str(exc)[:200]}
            logger.info('版本探测网络异常，保留当前值：%s', exc)

        with _lock:
            _state['sources'] = sources
            ok = [name for name, item in sources.items()
                  if not name.startswith('_') and item.get('ok')]
            failures = [item.get('error') for item in sources.values() if item.get('error')]
            _state['last_error'] = '; '.join(str(e) for e in failures)[:400]
            if ok:
                _state['fetched_at'] = time.time()
                _state['source'] = 'pinned' if pins['client'] and pins['cli'] else 'official'
            elif pins['client'] and pins['cli']:
                _state['source'] = 'pinned'
            elif _state['fetched_at']:
                _state['source'] = 'cached'
            current = (_state['client_cn'], _state['client_global'], _state['cli'])
        _persist()
        if ok:
            logger.info('官方客户端版本已同步：国内 %s / 国际 %s / CLI %s', *current)
        return status()
    finally:
        _probe_lock.release()


def status() -> dict:
    """给 /api/wb/versions 与设置页用的状态快照（不含任何凭据）。"""
    with _lock:
        snap = {key: _state[key] for key in
                ('client_cn', 'client_global', 'cli', 'full_cn', 'full_global',
                 'source', 'checked_at', 'fetched_at', 'last_error')}
        snap['sources'] = dict(_state['sources'])
    pins = pinned()
    hours = max(1, int(getattr(CFG, 'wb_version_refresh_hours', 6)))
    ttl = hours * 3600
    snap.update({
        'pinned': pins,
        'mode': 'pinned' if (pins['client'] or pins['cli']) else 'auto',
        'source_text': {'official': '官方实时', 'cached': '上次同步（缓存）',
                        'builtin': '内置默认', 'pinned': '手动锁定'}.get(snap['source'], '未知'),
        'refresh_hours': hours,
        'next_refresh_at': snap['checked_at'] + ttl if snap['checked_at'] else 0,
        'checked_at_text': _fmt(snap['checked_at']),
        'fetched_at_text': _fmt(snap['fetched_at']),
        'client_update_path': f'/v2/update?platform={_CLIENT_PLATFORMS[0]}',
        'cli_registry': _cli_urls()[0],
        'defaults': dict(_DEFAULTS),
    })
    if pins['client']:
        snap['client_cn'] = snap['client_global'] = pins['client']
        snap['full_cn'] = snap['full_global'] = pins['client']
    if pins['cli']:
        snap['cli'] = pins['cli']
    return snap


def _fmt(ts: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else '从未'


async def refresh_if_stale() -> dict:
    """给后台调度每 tick 调用：TTL 未到直接返回，不产生任何网络请求。"""
    return await refresh()


# 模块导入时先恢复上次成功值；启动后 scheduler 的第一个 tick 会做一次真实同步。
reload()
