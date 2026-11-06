"""OpenAI 兼容反代网关：密钥鉴权 → IP/模型白名单 → 模型别名 → 选号转发 → 计量落库。

对外暴露：
  GET  /v1/models
  POST /v1/chat/completions   （流式 / 非流式）
  POST /v1/embeddings

请求用 `Authorization: Bearer bw-xxx`（或 x-api-key）鉴权；转发时换成所选账号自己的
Token。账号失败会自动冷却并切到下一个，全挂才返回 502，错误体保持 OpenAI 格式。
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import accounts, db
from .config import CFG
from .security import (api_key_hash, client_ip, gateway_key_from_header,
                       ip_allowed, model_allowed)
from .upstream import auth_headers, make_async_client, upstream_url

logger = logging.getLogger('buddywong.gateway')
router = APIRouter(prefix='/v1', tags=['gateway'])

# 这些状态说明「这个账号此刻不可用」，换一个号重试才有意义；
# 400/404 之类是请求本身的问题，换多少账号都一样，直接回给客户端。
_RETRYABLE = {401, 403, 408, 409, 429}


class GatewayError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None,
                 err_type: str = 'invalid_request_error') -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.err_type = err_type

    def body(self) -> dict:
        error: dict[str, Any] = {'message': self.message, 'type': self.err_type}
        if self.code:
            error['code'] = self.code
        return {'error': error}


# ── 鉴权与公共校验 ─────────────────────────────────────────────
def _authenticate(request: Request) -> dict:
    raw_key = gateway_key_from_header(request)
    if not raw_key:
        raise GatewayError(401, '缺少 API Key，请使用 Authorization: Bearer bw-xxx', 'missing_api_key')
    row = db.query_one('SELECT * FROM api_keys WHERE key_hash = ?', (api_key_hash(raw_key),))
    if row is None:
        raise GatewayError(401, '无效的 API Key', 'invalid_api_key')
    if not row['enabled']:
        raise GatewayError(403, '该 API Key 已被停用', 'key_disabled')
    expires = (row['expires_at'] or '').strip()
    if expires and expires < time.strftime('%Y-%m-%d'):
        raise GatewayError(403, '该 API Key 已过期', 'key_expired')
    quota = int(row['quota'] or 0)
    if quota > 0 and int(row['used_count'] or 0) >= quota:
        raise GatewayError(429, '该 API Key 的调用额度已用尽', 'quota_exceeded')
    ip = client_ip(request)
    if not ip_allowed(ip, row['ip_whitelist']):
        raise GatewayError(403, f'来源 IP {ip} 不在白名单内', 'ip_not_allowed')
    db.execute('UPDATE api_keys SET used_count = used_count + 1, last_used_at = ? WHERE id = ?',
               (time.time(), row['id']))
    return row


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GatewayError(400, f'请求体不是合法 JSON：{exc}', 'invalid_json')
    if not isinstance(payload, dict):
        raise GatewayError(400, '请求体必须是 JSON 对象', 'invalid_json')
    return payload


def _prepare(request: Request, payload: dict, key_row: dict) -> tuple[dict, str, str, str]:
    """返回 (改写后的 body, 原始模型, 实际模型, 客户端 IP)。"""
    model = str(payload.get('model') or '').strip()
    if not model:
        raise GatewayError(400, '缺少 model 字段', 'missing_model')
    if not model_allowed(model, key_row['model_whitelist']):
        raise GatewayError(403, f'该 API Key 不允许使用模型 {model}', 'model_not_allowed')
    alias_map = db.setting_dict('model_alias', CFG.model_alias)
    upstream_model = str(alias_map.get(model, model))
    body = dict(payload)
    body['model'] = upstream_model
    return body, model, upstream_model, client_ip(request)


def _aliases() -> dict:
    return db.setting_dict('model_alias', CFG.model_alias)


def _usage(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get('usage')
    return usage if isinstance(usage, dict) else {}


def _log(key_row: dict, model: str, upstream_model: str, ip: str, req_id: str,
         account: dict | None, status: int, ok: bool, latency_ms: int, ttft_ms: int,
         usage: dict, stream: bool, error: str = '') -> None:
    day, hour = db.local_parts()
    db.execute(
        'INSERT INTO request_logs(ts, day, hour, request_id, key_id, key_prefix, account_id, '
        'account_name, model, upstream_model, status_code, ok, latency_ms, ttft_ms, '
        'prompt_tokens, completion_tokens, total_tokens, stream, client_ip, error) '
        'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (time.time(), day, hour, req_id, key_row['id'], key_row['prefix'],
         account['id'] if account else None, account['name'] if account else '',
         model, upstream_model, status, 1 if ok else 0, latency_ms, ttft_ms,
         int(usage.get('prompt_tokens') or 0), int(usage.get('completion_tokens') or 0),
         int(usage.get('total_tokens') or 0), 1 if stream else 0, ip, (error or '')[:500]),
    )


def _retryable(status: int) -> bool:
    return status in _RETRYABLE or status >= 500


def _no_account_error(status: int, message: str, code: str = 'no_available_account') -> GatewayError:
    return GatewayError(status, message, code, 'server_error' if status >= 500 else 'invalid_request_error')


def _empty_pool_error(upstream_model: str) -> GatewayError:
    """区分「压根没这个模型」和「有号但全在冷却」。

    前者换多少账号都没用，应该给 404 让客户端停止重试；后者是暂时的，503 才合适。
    """
    rows = db.query('SELECT models FROM accounts WHERE enabled = 1')
    if rows:
        supported = any(
            model_allowed(upstream_model, accounts.parse_models(row['models']))
            for row in rows
        )
        if not supported:
            return GatewayError(404, f'账号池中没有账号支持模型 {upstream_model}',
                                'model_not_found')
    return _no_account_error(503, '当前没有可用账号（可能全部冷却、过期或已停用）')


# ── 模型列表 ───────────────────────────────────────────────────
@router.get('/models')
async def list_models(request: Request) -> JSONResponse:
    _authenticate(request)
    names: set[str] = set(_aliases().keys())
    for row in db.query('SELECT models FROM accounts WHERE enabled = 1'):
        for model in accounts.parse_models(row['models']):
            if model:
                names.add(str(model))
    data = [{'id': name, 'object': 'model', 'created': int(time.time()), 'owned_by': 'buddywong'}
            for name in sorted(names)]
    return JSONResponse({'object': 'list', 'data': data})


# ── 非流式转发 ─────────────────────────────────────────────────
async def _proxy_non_stream(key_row: dict, path: str, body: dict, model: str,
                            upstream_model: str, ip: str) -> JSONResponse:
    req_id = 'bw-' + secrets.token_hex(8)
    started = time.perf_counter()
    candidates = accounts.select_candidates(upstream_model)
    if not candidates:
        _log(key_row, model, upstream_model, ip, req_id, None, 503, False,
             int((time.perf_counter() - started) * 1000), 0, {}, False, '没有可用账号')
        raise _empty_pool_error(upstream_model)

    last_error = ''
    for account in candidates:
        url = upstream_url(str(account['base_url']), path)
        try:
            async with make_async_client() as client:
                resp = await client.post(url, headers=auth_headers(str(account['api_key'])), json=body)
        except Exception as exc:  # noqa: BLE001
            last_error = f'{type(exc).__name__}: {exc}'
            accounts.mark_failure(account['id'], last_error)
            logger.warning('账号 %s 请求失败：%s', account['name'], last_error)
            continue

        status = resp.status_code
        latency = int((time.perf_counter() - started) * 1000)
        if _retryable(status):
            last_error = f'HTTP {status}: {resp.text[:300]}'
            accounts.mark_failure(account['id'], last_error)
            continue
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        usage = _usage(payload)
        ok = 200 <= status < 300
        if ok:
            accounts.mark_success(account['id'])
        _log(key_row, model, upstream_model, ip, req_id, account, status, ok, latency, 0,
             usage, False, '' if ok else resp.text[:300])
        content = payload if isinstance(payload, dict) else {'error': {
            'message': resp.text[:500] or f'上游返回 HTTP {status}', 'type': 'upstream_error'}}
        return JSONResponse(status_code=status, content=content,
                            headers={'X-Request-Id': req_id})

    latency = int((time.perf_counter() - started) * 1000)
    _log(key_row, model, upstream_model, ip, req_id, None, 502, False, latency, 0, {}, False, last_error)
    raise _no_account_error(502, f'所有候选账号均不可用：{last_error[:300]}', 'upstream_unavailable')


# ── 流式转发 ───────────────────────────────────────────────────
def _inspect_sse(chunk: bytes, state: dict, started: float) -> None:
    """从 SSE 分片里尽量抠出首字延迟与 token 用量（解析不到也不影响转发）。"""
    state['bytes'] += len(chunk)
    if not state['ttft']:
        state['ttft'] = int((time.perf_counter() - started) * 1000)
    text = chunk.decode('utf-8', errors='ignore')
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        raw = line[5:].strip()
        if not raw or raw == '[DONE]':
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get('usage'), dict):
            state['usage'] = obj['usage']


async def _proxy_stream(key_row: dict, path: str, body: dict, model: str,
                        upstream_model: str, ip: str) -> StreamingResponse:
    req_id = 'bw-' + secrets.token_hex(8)
    started = time.perf_counter()
    candidates = accounts.select_candidates(upstream_model)
    if not candidates:
        _log(key_row, model, upstream_model, ip, req_id, None, 503, False,
             int((time.perf_counter() - started) * 1000), 0, {}, True, '没有可用账号')
        raise _empty_pool_error(upstream_model)

    last_error = ''
    for account in candidates:
        client = make_async_client()
        url = upstream_url(str(account['base_url']), path)
        try:
            upstream_req = client.build_request('POST', url,
                                                headers=auth_headers(str(account['api_key'])), json=body)
            resp = await client.send(upstream_req, stream=True)
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            last_error = f'{type(exc).__name__}: {exc}'
            accounts.mark_failure(account['id'], last_error)
            continue

        status = resp.status_code
        if _retryable(status):
            try:
                raw = (await resp.aread()).decode('utf-8', errors='ignore')
            except Exception:  # noqa: BLE001
                raw = ''
            last_error = f'HTTP {status}: {raw[:300]}'
            accounts.mark_failure(account['id'], last_error)
            await resp.aclose()
            await client.aclose()
            continue

        accounts.mark_success(account['id'])
        state: dict[str, Any] = {'ttft': 0, 'usage': {}, 'bytes': 0}
        ok = 200 <= status < 300

        async def stream_body(account=account, resp=resp, client=client, state=state, status=status, ok=ok):
            try:
                async for chunk in resp.aiter_bytes():
                    if ok:
                        _inspect_sse(chunk, state, started)
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                logger.warning('流式转发中断（账号 %s）：%s', account['name'], exc)
            finally:
                await resp.aclose()
                await client.aclose()
                _log(key_row, model, upstream_model, ip, req_id, account, status, ok,
                     int((time.perf_counter() - started) * 1000), state['ttft'],
                     state['usage'], True, '' if ok else last_error)

        return StreamingResponse(
            stream_body(), status_code=status, media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'X-Request-Id': req_id},
        )

    _log(key_row, model, upstream_model, ip, req_id, None, 502, False,
         int((time.perf_counter() - started) * 1000), 0, {}, True, last_error)
    raise _no_account_error(502, f'所有候选账号均不可用：{last_error[:300]}', 'upstream_unavailable')


# ── 入口路由 ───────────────────────────────────────────────────
@router.post('/chat/completions')
async def chat_completions(request: Request):
    key_row = _authenticate(request)
    payload = await _json_body(request)
    body, model, upstream_model, ip = _prepare(request, payload, key_row)
    if body.get('stream'):
        return await _proxy_stream(key_row, '/v1/chat/completions', body, model, upstream_model, ip)
    return await _proxy_non_stream(key_row, '/v1/chat/completions', body, model, upstream_model, ip)


@router.post('/embeddings')
async def embeddings(request: Request):
    key_row = _authenticate(request)
    payload = await _json_body(request)
    body, model, upstream_model, ip = _prepare(request, payload, key_row)
    body.pop('stream', None)
    return await _proxy_non_stream(key_row, '/v1/embeddings', body, model, upstream_model, ip)
