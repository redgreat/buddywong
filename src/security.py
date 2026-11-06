"""安全工具：管理端口令/会话、API Key、客户端 IP 与白名单。

不引入 passlib / PyJWT / bcrypt 之类的额外依赖，全部用标准库实现：
口令用 PBKDF2-HMAC-SHA256 加盐，会话 token 用 HMAC-SHA256 签名并带过期时间。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from typing import Any, Iterable

from fastapi import Request

from .config import CFG

_PBKDF2_ITERATIONS = 200_000


# ── 管理端口令 ─────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, _PBKDF2_ITERATIONS)
    return f'pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac(
            'sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# ── 管理端会话 token ───────────────────────────────────────────
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def make_token(username: str, ttl: int | None = None) -> str:
    payload = {
        'u': username,
        'iat': int(time.time()),
        'exp': int(time.time()) + (ttl or CFG.session_timeout),
        'nonce': secrets.token_hex(6),
    }
    body = _b64e(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    sig = _b64e(hmac.new(CFG.master_key.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
    return f'{body}.{sig}'


def verify_token(token: str) -> str | None:
    """校验成功返回用户名，失败/过期返回 None。"""
    if not token or '.' not in token:
        return None
    body, sig = token.split('.', 1)
    expected = _b64e(hmac.new(CFG.master_key.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        return str(payload.get('u') or '') or None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def auth_token_from_header(request: Request) -> str:
    """兼容 `Authorization: Bearer xxx` 与直接传裸 token 两种写法。"""
    raw = request.headers.get('authorization', '').strip()
    if raw.lower().startswith('bearer '):
        return raw[7:].strip()
    return raw or request.headers.get('x-admin-token', '').strip()


# ── 对外 API Key ───────────────────────────────────────────────
def generate_api_key() -> str:
    return 'bw-' + secrets.token_urlsafe(32)


def api_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def gateway_key_from_header(request: Request) -> str:
    raw = request.headers.get('authorization', '').strip()
    if raw.lower().startswith('bearer '):
        return raw[7:].strip()
    return raw or request.headers.get('x-api-key', '').strip()


# ── 客户端 IP 与白名单 ─────────────────────────────────────────
def client_ip(request: Request, trust_proxy: bool | None = None) -> str:
    trust = CFG.trust_proxy if trust_proxy is None else trust_proxy
    if trust:
        forwarded = request.headers.get('x-forwarded-for', '').strip()
        if forwarded:
            return forwarded.split(',')[0].strip()
        real = request.headers.get('x-real-ip', '').strip()
        if real:
            return real
    return request.client.host if request.client else ''


def ip_allowed(ip: str, whitelist: Iterable[str] | str | None) -> bool:
    """空名单 = 放行全部；支持精确 IP、CIDR 与 '*'。"""
    if not whitelist:
        return True
    if isinstance(whitelist, str):
        try:
            whitelist = json.loads(whitelist)
        except json.JSONDecodeError:
            whitelist = [part.strip() for part in whitelist.split(',') if part.strip()]
    entries = [str(item).strip() for item in whitelist if str(item).strip()]
    if not entries or '*' in entries:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if '/' in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def model_allowed(model: str, whitelist: Any) -> bool:
    """空名单 = 放行全部。"""
    if not whitelist:
        return True
    if isinstance(whitelist, str):
        try:
            whitelist = json.loads(whitelist)
        except json.JSONDecodeError:
            whitelist = [part.strip() for part in whitelist.split(',') if part.strip()]
    if isinstance(whitelist, dict):
        whitelist = list(whitelist.keys())
    entries = [str(item).strip() for item in whitelist if str(item).strip()]
    return not entries or '*' in entries or model in entries
