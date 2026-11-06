"""上游 HTTP 公共工具：地址拼接、鉴权头、客户端构造（兼容 httpx 0.27/0.28）。"""
from __future__ import annotations

from typing import Any

import httpx

from .config import CFG


def client_kwargs(timeout: float | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        'timeout': timeout or CFG.request_timeout,
        'trust_env': False,          # 不读宿主机 HTTP_PROXY，避免环境变量误污染
        'follow_redirects': True,
    }
    proxy = (CFG.http_proxy or '').strip()
    if proxy:
        kwargs['proxy'] = proxy
    return kwargs


def make_async_client(**overrides: Any) -> httpx.AsyncClient:
    """构造 AsyncClient。老版本 httpx 只认 proxies，这里做一次兼容回退。

    未显式给 timeout 时读「设置」页里的动态值，改完即时生效，不用重启容器。
    """
    from . import db  # 延迟导入：db 初始化需要 config，避免模块环

    kwargs = client_kwargs()
    if 'timeout' not in overrides:
        kwargs['timeout'] = db.setting_float('request_timeout', CFG.request_timeout)
    kwargs.update(overrides)
    try:
        return httpx.AsyncClient(**kwargs)
    except TypeError:
        proxy = kwargs.pop('proxy', None)
        if proxy:
            kwargs['proxies'] = proxy
        return httpx.AsyncClient(**kwargs)


def upstream_url(base_url: str, path: str = '/v1/chat/completions') -> str:
    """base_url 可写站点根，也可写带 /v1 的 OpenAI 兼容根，两种都拼对。"""
    base = (base_url or '').rstrip('/')
    if base.endswith('/v1') and path.startswith('/v1/'):
        path = path[3:]
    return base + path


def auth_headers(api_key: str, extra: dict | None = None, json_body: bool = True) -> dict[str, str]:
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Accept': 'application/json',
    }
    if json_body:
        headers['Content-Type'] = 'application/json'
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers
