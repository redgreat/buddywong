"""buddywong 回归自测：配置/安全/存储/账号/网关/管理 API 冒烟。

运行：make test  或  python -m unittest discover -s test -t . -v
用临时目录隔离数据库与日志，不碰真实 data/。上游请求全部 mock，不发真实网络请求。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 环境变量必须在 import src.* 之前设置：CFG 在模块加载时就会读它们
_TMP = tempfile.mkdtemp(prefix='buddywong-test-')
os.environ['BW_DATA_DIR'] = _TMP
os.environ['BW_LOG_DIR'] = _TMP
os.environ['BW_ADMIN_PASSWORD'] = 'test-password-123'
os.environ['BW_MASTER_KEY'] = 'test-master-key-for-unittest'
os.environ['BW_CHECKIN_ENABLED'] = '0'

from fastapi.testclient import TestClient  # noqa: E402

from src import accounts, db, gateway, security  # noqa: E402
from src.config import CFG  # noqa: E402
from src.server import app  # noqa: E402


class _FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {
            'id': 'chatcmpl-test',
            'object': 'chat.completion',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'pong'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7},
        }
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers=None, json=None):  # noqa: A002
        return _FakeResponse()

    async def aclose(self):
        return None


class _FakeStreamResponse:
    status_code = 200

    async def aiter_bytes(self):
        yield b'data: {"choices":[{"delta":{"content":"po"}}]}\n\n'
        yield (b'data: {"choices":[{"delta":{"content":"ng"}}],'
               b'"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n')
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        return None


class _FakeStreamClient(_FakeClient):
    def build_request(self, method, url, headers=None, json=None):  # noqa: A002
        return {'method': method, 'url': url}

    async def send(self, request, stream=False):
        return _FakeStreamResponse()


ACCOUNT_PAYLOAD = {
    'name': 'upstream-test',
    'group_name': 'default',
    'base_url': 'https://upstream.example.com/v1',
    'api_key': 'sk-upstream-test',
    'models': 'model-a, model-b',
    'weight': 2,
    'credits': 100,
    'enabled': True,
    'checkin_url': '',
    'checkin_method': 'POST',
    'checkin_headers': '{}',
    'checkin_body': '{}',
}


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._scheduler_patch = mock.patch('src.scheduler.start', lambda: None)
        cls._scheduler_patch.start()
        cls.client = TestClient(app)
        cls.client.__enter__()

        resp = cls.client.post('/api/login', json={
            'username': CFG.admin_user, 'password': 'test-password-123'})
        assert resp.status_code == 200, resp.text
        cls.token = resp.json()['token']
        cls.auth = {'Authorization': 'Bearer ' + cls.token}

        resp = cls.client.post('/api/accounts', json=ACCOUNT_PAYLOAD, headers=cls.auth)
        assert resp.status_code == 200, resp.text
        cls.account_id = resp.json()['item']['id']

        resp = cls.client.post('/api/keys', json={'name': 'smoke-key'}, headers=cls.auth)
        assert resp.status_code == 200, resp.text
        cls.secret = resp.json()['secret']
        cls.gateway_auth = {'Authorization': 'Bearer ' + cls.secret}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls._scheduler_patch.stop()
        db.close()
        shutil.rmtree(_TMP, ignore_errors=True)

    # ── 安全 ──────────────────────────────────────────────
    def test_01_password_hash_roundtrip(self):
        hashed = security.hash_password('correct horse battery')
        self.assertTrue(security.verify_password('correct horse battery', hashed))
        self.assertFalse(security.verify_password('wrong', hashed))
        self.assertFalse(security.verify_password('x', 'not-a-hash'))

    def test_02_token_roundtrip_and_expiry(self):
        token = security.make_token('admin', ttl=60)
        self.assertEqual(security.verify_token(token), 'admin')
        self.assertIsNone(security.verify_token(security.make_token('admin', ttl=-1)))
        self.assertIsNone(security.verify_token(token + 'tampered'))
        self.assertIsNone(security.verify_token(''))

    def test_03_api_key_generation_and_hash(self):
        key = security.generate_api_key()
        self.assertTrue(key.startswith('bw-'))
        self.assertEqual(len(security.api_key_hash(key)), 64)
        self.assertNotEqual(security.api_key_hash(key), security.api_key_hash(key + 'x'))

    def test_04_whitelist_rules(self):
        self.assertTrue(security.ip_allowed('1.2.3.4', []))
        self.assertTrue(security.ip_allowed('10.1.2.3', ['10.0.0.0/8']))
        self.assertFalse(security.ip_allowed('11.1.2.3', ['10.0.0.0/8']))
        self.assertTrue(security.ip_allowed('1.2.3.4', ['1.2.3.4']))
        self.assertTrue(security.ip_allowed('1.2.3.4', '*'))
        self.assertTrue(security.model_allowed('model-a', []))
        self.assertTrue(security.model_allowed('model-a', ['model-a']))
        self.assertFalse(security.model_allowed('model-z', ['model-a']))

    # ── 配置 / 存储 ───────────────────────────────────────
    def test_05_config_and_settings(self):
        self.assertEqual(CFG.port, 3002)
        self.assertEqual(CFG.admin_password, 'test-password-123')
        self.assertIsInstance(db.all_settings(), dict)
        self.assertIsInstance(db.setting_int('cool_seconds', 300), int)
        db.set_setting('unit_test_key', {'a': 1})
        self.assertEqual(db.get_setting('unit_test_key'), {'a': 1})

    # ── 基础端点 ─────────────────────────────────────────
    def test_06_health_and_static_page(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ok')
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('buddywong', resp.text)

    def test_07_login_and_auth_required(self):
        self.assertEqual(self.client.get('/api/me', headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get('/api/me').status_code, 401)
        bad = self.client.post('/api/login', json={'username': 'admin', 'password': 'bad'})
        self.assertEqual(bad.status_code, 401)

    # ── 账号 ─────────────────────────────────────────────
    def test_08_account_crud(self):
        payload = dict(ACCOUNT_PAYLOAD, name='crud-test', base_url='https://crud.example.com/v1')
        resp = self.client.post('/api/accounts', json=payload, headers=self.auth)
        self.assertEqual(resp.status_code, 200, resp.text)
        new_id = resp.json()['item']['id']
        self.assertEqual(resp.json()['item']['api_key_masked'].count('•'), 4)

        resp = self.client.put(f'/api/accounts/{new_id}', json={'weight': 5}, headers=self.auth)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['item']['weight'], 5)
        # 编辑时不传 api_key，明文保持不变
        self.assertEqual(accounts.get_account(new_id)['api_key'], 'sk-upstream-test')

        resp = self.client.get('/api/accounts', headers=self.auth)
        self.assertTrue(any(a['id'] == new_id for a in resp.json()['items']))

        self.assertEqual(self.client.delete(f'/api/accounts/{new_id}', headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get('/api/accounts', headers=self.auth).json()['total'], 1)

    def test_09_account_validation_and_selection(self):
        bad = self.client.post('/api/accounts', json=dict(ACCOUNT_PAYLOAD, base_url='ftp://x'),
                               headers=self.auth)
        self.assertEqual(bad.status_code, 400)
        candidates = accounts.select_candidates('model-a', 3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['id'], self.account_id)
        self.assertEqual(accounts.select_candidates('model-not-supported', 3), [])

    def test_10_checkin_without_url_is_skipped(self):
        result = asyncio.run(accounts.checkin_account(self.account_id, 'manual'))
        self.assertFalse(result['ok'])
        self.assertTrue(result.get('skipped'))

    def test_11_toggle_and_failure_cooling(self):
        accounts.mark_failure(self.account_id, 'mock upstream 500')
        row = accounts.get_account(self.account_id)
        self.assertEqual(row['status'], 'cooling')
        self.assertGreater(row['cool_until'], 0)
        self.assertEqual(accounts.select_candidates('model-a', 3), [])
        accounts.mark_success(self.account_id)
        self.assertEqual(accounts.get_account(self.account_id)['status'], 'healthy')
        self.assertEqual(len(accounts.select_candidates('model-a', 3)), 1)

    # ── 密钥 / 网关 ──────────────────────────────────────
    def test_12_keys_and_models_endpoint(self):
        resp = self.client.get('/api/keys', headers=self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('key_hash', resp.json()['items'][0])

        resp = self.client.get('/v1/models', headers=self.gateway_auth)
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = [m['id'] for m in resp.json()['data']]
        self.assertIn('model-a', ids)

        self.assertEqual(self.client.get('/v1/models').status_code, 401)

    def test_13_gateway_chat_completions_mocked(self):
        with mock.patch('src.gateway.make_async_client', return_value=_FakeClient()):
            resp = self.client.post('/v1/chat/completions', headers=self.gateway_auth, json={
                'model': 'model-a', 'messages': [{'role': 'user', 'content': 'ping'}]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['choices'][0]['message']['content'], 'pong')
        self.assertIn('X-Request-Id', resp.headers)

        logs = self.client.get('/api/logs', headers=self.auth).json()
        self.assertGreaterEqual(logs['total'], 1)
        self.assertEqual(logs['items'][0]['total_tokens'], 7)
        self.assertEqual(logs['items'][0]['account_name'], 'upstream-test')

    def test_14_gateway_error_shapes(self):
        no_key = self.client.post('/v1/chat/completions', json={'model': 'model-a', 'messages': []})
        self.assertEqual(no_key.status_code, 401)
        self.assertIn('error', no_key.json())

        no_model = self.client.post('/v1/chat/completions', headers=self.gateway_auth, json={'messages': []})
        self.assertEqual(no_model.status_code, 400)

        blocked_key = self.client.post('/api/keys', json={
            'name': 'restricted-key', 'model_whitelist': ['model-a']}, headers=self.auth).json()
        restricted = {'Authorization': 'Bearer ' + blocked_key['secret']}
        blocked = self.client.post('/v1/chat/completions', headers=restricted,
                                   json={'model': 'model-z', 'messages': []})
        self.assertEqual(blocked.status_code, 403)

        # 账号池里压根没有这个模型 → 404（而不是让客户端反复重试的 503）
        missing = self.client.post('/v1/chat/completions', headers=self.gateway_auth,
                                   json={'model': 'model-z', 'messages': []})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()['error']['code'], 'model_not_found')

    # ── 日志 / 统计 / 设置 ───────────────────────────────
    def test_15_stats_and_logs_endpoints(self):
        self.assertEqual(self.client.get('/api/stats?days=7', headers=self.auth).status_code, 200)
        overview = self.client.get('/api/overview', headers=self.auth)
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(len(overview.json()['hourly']), 24)
        self.assertEqual(self.client.get('/api/checkin-logs', headers=self.auth).status_code, 200)

    def test_16_settings_update(self):
        resp = self.client.put('/api/settings', headers=self.auth, json={
            'checkin_hour': 9, 'cool_seconds': 60, 'model_alias': {'alias-x': 'model-a'}})
        self.assertEqual(resp.status_code, 200, resp.text)
        items = self.client.get('/api/settings', headers=self.auth).json()['items']
        self.assertEqual(items['checkin_hour'], 9)
        self.assertEqual(items['cool_seconds'], 60)
        self.assertEqual(items['model_alias'], {'alias-x': 'model-a'})
        bad = self.client.put('/api/settings', headers=self.auth, json={'checkin_hour': 99})
        self.assertEqual(bad.status_code, 400)

    def test_17_alias_is_applied_to_upstream_body(self):
        captured = {}

        class _CaptureClient(_FakeClient):
            async def post(self, url, headers=None, json=None):  # noqa: A002
                captured['url'] = url
                captured['model'] = json['model']
                return _FakeResponse()

        with mock.patch('src.gateway.make_async_client', return_value=_CaptureClient()):
            resp = self.client.post('/v1/chat/completions', headers=self.gateway_auth,
                                    json={'model': 'alias-x', 'messages': []})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(captured['model'], 'model-a')
        self.assertTrue(captured['url'].endswith('/v1/chat/completions'))

    def test_18_streaming_chat_completions(self):
        with mock.patch('src.gateway.make_async_client', return_value=_FakeStreamClient()):
            resp = self.client.post('/v1/chat/completions', headers=self.gateway_auth, json={
                'model': 'model-a', 'messages': [{'role': 'user', 'content': 'ping'}],
                'stream': True})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('"content":"po"', resp.text)
        self.assertIn('[DONE]', resp.text)
        self.assertEqual(resp.headers.get('x-request-id', '').startswith('bw-'), True)

        logs = self.client.get('/api/logs?limit=5', headers=self.auth).json()['items']
        streamed = [r for r in logs if r['stream'] and r['total_tokens'] == 3]
        self.assertTrue(streamed, '流式请求的用量应被解析并落库')
        self.assertGreater(streamed[0]['ttft_ms'], -1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
