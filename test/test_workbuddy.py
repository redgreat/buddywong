"""WorkBuddy 真实登录 / 签到协议与「页面扫码登录」管理 API 回归自测。

全部网络请求 mock，不发真实请求；凭据不外泄、10001 两层语义、国际版跳过、
旧库迁移后的新列、扫码落库与自动签到都在这里覆盖。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 与 test_smoke 一样：环境变量要在 import src.* 之前设置。
# 独立测试本文件时也要能跑，所以这里重新指定一份自己的临时目录。
_TMP = tempfile.mkdtemp(prefix='buddywong-wb-test-')
os.environ.setdefault('BW_DATA_DIR', _TMP)
os.environ.setdefault('BW_LOG_DIR', _TMP)
os.environ.setdefault('BW_ADMIN_PASSWORD', 'test-password-123')
os.environ.setdefault('BW_MASTER_KEY', 'test-master-key-for-unittest')
os.environ.setdefault('BW_CHECKIN_ENABLED', '0')

from fastapi.testclient import TestClient  # noqa: E402

from src import accounts, db, versions, workbuddy as wb  # noqa: E402
from src.config import CFG  # noqa: E402
from src.server import app, ensure_admin  # noqa: E402

AUTH = {'uid': 'UID1', 'access_token': 'AT1', 'refresh_token': 'RT1',
        'domain': 'cn', 'enterprise_id': '', 'realm': 'cn'}


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


class _ScriptedClient:
    """按调用顺序吐出预设响应，并记录每次请求的 URL / headers。"""

    def __init__(self, script: list[dict | Exception]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def _next(self, url: str, kwargs: dict) -> _Resp:
        self.calls.append((url, kwargs))
        if not self.script:
            return _Resp({'code': 0, 'data': {}})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    async def post(self, url, **kwargs):
        return self._next(url, kwargs)

    async def get(self, url, **kwargs):
        return self._next(url, kwargs)


def _patch(script: list[dict | Exception]):
    client = _ScriptedClient(script)
    return mock.patch.object(wb, 'make_async_client', return_value=client), client


class ProtocolTest(unittest.TestCase):
    """纯协议层：扫码、10001 语义、积分、刷新、二维码。"""

    def setUp(self) -> None:
        wb._state_cache.clear()
        CFG.wb_state_ttl = 900

    def test_01_start_login_and_qr(self):
        script, client = _patch([{'code': 0, 'data': {'state': 'S1',
                                                      'authUrl': 'https://auth.example/qr?state=S1'}}])
        with script:
            out = asyncio.run(wb.start_login('cn'))
        self.assertEqual(out['state'], 'S1')
        self.assertTrue(out['auth_url'].startswith('https://auth.example/'))
        self.assertTrue(out['supports_checkin'])
        self.assertIn('/v2/plugin/auth/state', client.calls[0][0])
        self.assertEqual(client.calls[0][1]['params'], {'platform': 'CLI'})
        qr = wb.qr_data_uri(out['auth_url'])
        self.assertTrue(qr.startswith('data:image/png;base64,'))
        self.assertEqual(wb.qr_data_uri(''), '')

    def test_02_poll_login_ready_never_leaks_token(self):
        script, _ = _patch([
            {'code': 0, 'data': {'state': 'S2', 'authUrl': 'https://auth.example/S2'}},
            {'code': 0, 'data': {'accessToken': 'AT-SECRET', 'refreshToken': 'RT-SECRET',
                                 'expiresIn': 3600, 'domain': 'cn'}},
            {'code': 0, 'data': {'uid': 'UID2', 'nickname': '存伟的号'}},
        ])
        with script:
            asyncio.run(wb.start_login('cn'))
            public = asyncio.run(wb.poll_login('S2'))
        self.assertEqual(public['status'], 'ready')
        self.assertEqual(public['nickname'], '存伟的号')
        dumped = json.dumps(public, ensure_ascii=False)
        self.assertNotIn('AT-SECRET', dumped)
        self.assertNotIn('RT-SECRET', dumped)
        self.assertNotIn('access_token', dumped)
        ready = wb.take_ready('S2')
        self.assertEqual(ready['access_token'], 'AT-SECRET')
        self.assertEqual(ready['refresh_token'], 'RT-SECRET')
        # 落盘失败时仍能用同一个 state 重试，不要求用户重扫
        self.assertEqual(asyncio.run(wb.poll_login('S2'))['status'], 'ready')
        wb.drop_state('S2')
        self.assertEqual(asyncio.run(wb.poll_login('S2'))['status'], 'invalid')

    def test_03_poll_waiting_and_realm_mismatch(self):
        script, _ = _patch([
            {'code': 0, 'data': {'state': 'S3', 'authUrl': 'https://auth.example/S3'}},
            {'code': 0, 'data': {}},
        ])
        with script:
            asyncio.run(wb.start_login('cn'))
            self.assertEqual(asyncio.run(wb.poll_login('S3'))['status'], 'waiting')
            self.assertEqual(asyncio.run(wb.poll_login('S3', 'global'))['status'],
                             'realm_mismatch')

    def test_04_checkin_code_semantics(self):
        cases = [
            ({'code': 0, 'msg': 'ok'}, 0, '签到成功'),
            ({'code': 10001, 'msg': '今天已签到'}, 10001, '今日已签到'),
            ({'code': 10001, 'msg': 'Already checked in today'}, 10001, '今日已签到'),
            ({'code': 10001, 'msg': '签到失败，请稍后重试'}, -3, '签到失败，请稍后重试'),
            ({'code': 10001}, 10001, '今日已签到'),
            ({'code': 429, 'msg': 'limited'}, 429, '签到返回 code=429'),
        ]
        for payload, code, message in cases:
            script, _ = _patch([payload])
            with script:
                got = asyncio.run(wb.checkin(AUTH))
            self.assertEqual(got, (code, message), payload)

        # 国际版 / 企业账号：直接跳过，不打接口
        script, client = _patch([])
        with script:
            self.assertEqual(asyncio.run(wb.checkin(AUTH, 'global')),
                             (-2, '国际版或企业账号没有个人签到体系，已跳过'))
            ent = dict(AUTH, enterprise_id='ENT1')
            self.assertEqual(asyncio.run(wb.checkin(ent, 'cn'))[0], -2)
        self.assertEqual(client.calls, [])

        # 网络异常 -> -1，而不是抛到 500
        script, _ = _patch([OSError('network down')])
        with script:
            self.assertEqual(asyncio.run(wb.checkin(AUTH))[0], -1)

        # 请求形态：/v2/billing/meter/daily-checkin + 官方身份头
        script, client = _patch([{'code': 0, 'data': {}}])
        with script:
            asyncio.run(wb.checkin(AUTH))
        url, kwargs = client.calls[0]
        self.assertTrue(url.endswith('/v2/billing/meter/daily-checkin'))
        h = kwargs['headers']
        self.assertEqual(h['Authorization'], 'Bearer AT1')
        self.assertEqual(h['X-User-Id'], 'UID1')
        self.assertEqual(h['X-CodeBuddy-Request'], '1')
        self.assertEqual(h['User-Agent'], 'WorkBuddy/' + versions.client_version('cn'))
        self.assertEqual(h['X-IDE-Version'], versions.full_version('cn'))

    def test_05_fetch_credits_and_refresh_token(self):
        script, _ = _patch([{'code': 0, 'data': {'Response': {'Accounts': [
            {'CycleCapacitySize': 1000, 'CycleCapacityRemain': 420, 'CycleCapacityUsed': 580},
            {'CapacityRemain': 80},
        ]}}}])
        with script:
            ok, credits, message = asyncio.run(wb.fetch_credits(AUTH))
        self.assertTrue(ok)
        self.assertEqual(credits, 500)
        self.assertEqual(message, '查询成功')

        script, client = _patch([{'code': 0, 'data': {'accessToken': 'AT-NEW',
                                                        'refreshToken': 'RT-NEW',
                                                        'expiresIn': 7200, 'domain': 'cn'}}])
        with script:
            ok, message, fields = asyncio.run(wb.refresh_token(AUTH))
        self.assertTrue(ok)
        self.assertEqual(fields['access_token'], 'AT-NEW')
        self.assertEqual(fields['refresh_token'], 'RT-NEW')
        self.assertEqual(client.calls[0][1]['headers']['X-Refresh-Token'], 'RT1')
        self.assertEqual(client.calls[0][1]['headers']['X-Auth-Refresh-Source'], 'plugin')

        script, _ = _patch([])
        with script:
            self.assertFalse(asyncio.run(wb.refresh_token(dict(AUTH, refresh_token='')))[0])

    def test_06_fetch_model_ids_is_best_effort(self):
        script, _ = _patch([{'code': 0, 'data': {'models': [{'id': 'wb-model-a'}]}},
                            {'code': 0, 'data': {'models': ['wb-model-b']}}])
        with script:
            ids = asyncio.run(wb.fetch_model_ids(AUTH))
        self.assertEqual(ids, ['wb-model-a', 'wb-model-b'])
        script, _ = _patch([OSError('offline')])
        with script:
            self.assertEqual(asyncio.run(wb.fetch_model_ids(AUTH)), [])


class WorkBuddyApiTest(unittest.TestCase):
    """页面扫码登录整条链路：start -> poll -> finish（自动签到）+ 鉴权与去重。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 独立临时库：test_smoke 结束时已关闭并删掉它自己的库
        CFG.data_dir = Path(_TMP)
        CFG.log_dir = Path(_TMP)
        db.close()
        cls._scheduler_patch = mock.patch('src.scheduler.start', lambda: None)
        cls._scheduler_patch.start()
        cls.client = TestClient(app)
        cls.client.__enter__()
        ensure_admin()
        resp = cls.client.post('/api/login', json={
            'username': CFG.admin_user, 'password': 'test-password-123'})
        assert resp.status_code == 200, resp.text
        cls.auth = {'Authorization': 'Bearer ' + resp.json()['token']}
        wb._state_cache.clear()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls._scheduler_patch.stop()
        db.close()
        shutil.rmtree(_TMP, ignore_errors=True)

    def _flow_script(self) -> list[dict]:
        return [
            {'code': 0, 'data': {'state': 'FLOW1', 'authUrl': 'https://auth.example/flow1'}},
            {'code': 0, 'data': {'accessToken': 'AT-FLOW', 'refreshToken': 'RT-FLOW',
                                 'expiresIn': 3600, 'domain': 'cn'}},
            {'code': 0, 'data': {'uid': 'UID-FLOW', 'nickname': '扫码测试号'}},
            {'code': 0, 'msg': 'ok', 'data': {}},                      # daily-checkin
            {'code': 0, 'data': {'Response': {'Accounts': [           # get-user-resource
                {'CycleCapacitySize': 1000, 'CycleCapacityRemain': 300}]}}},
        ]

    def test_07_all_wb_endpoints_require_admin_session(self):
        self.assertEqual(self.client.post('/api/wb/auth/start', json={}).status_code, 401)
        self.assertEqual(self.client.get('/api/wb/auth/poll?state=x').status_code, 401)
        self.assertEqual(self.client.post('/api/wb/auth/finish', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/wb/accounts/1/refresh').status_code, 401)

    def test_08_full_qr_login_and_auto_checkin(self):
        script, client = _patch(self._flow_script())
        with script, mock.patch.object(wb, 'fetch_model_ids',
                                      new=mock.AsyncMock(return_value=['wb-model-a'])):
            started = self.client.post('/api/wb/auth/start', json={'realm': 'cn'},
                                       headers=self.auth)
            self.assertEqual(started.status_code, 200, started.text)
            data = started.json()
            self.assertEqual(data['state'], 'FLOW1')
            self.assertTrue(data['qr'].startswith('data:image/png;base64,'))

            polled = self.client.get('/api/wb/auth/poll?state=FLOW1&realm=cn', headers=self.auth)
            self.assertEqual(polled.status_code, 200, polled.text)
            self.assertEqual(polled.json()['status'], 'ready')
            self.assertNotIn('AT-FLOW', polled.text)
            self.assertNotIn('RT-FLOW', polled.text)

            finished = self.client.post('/api/wb/auth/finish', headers=self.auth,
                                        json={'state': 'FLOW1', 'auto_checkin': True})
            self.assertEqual(finished.status_code, 200, finished.text)
            body = finished.json()
            item = body['item']
            self.assertEqual(item['source'], 'workbuddy')
            self.assertEqual(item['wb_uid'], 'UID-FLOW')
            self.assertEqual(item['name'], '扫码测试号')
            self.assertEqual(item['proxy_path'], '/v2')
            self.assertEqual(item['models'], ['wb-model-a'])
            self.assertTrue(item['checkin_supported'])
            self.assertTrue(item['wb_refresh_configured'])
            self.assertNotIn('AT-FLOW', finished.text)
            self.assertNotIn('RT-FLOW', finished.text)

            self.assertTrue(body['checkin']['ok'])
            self.assertEqual(body['checkin']['status_code'], 0)
            self.assertEqual(body['checkin']['credits'], 300)
            self.assertEqual(accounts.get_account(item['id'])['api_key'], 'AT-FLOW')
            self.assertEqual(accounts.get_account(item['id'])['credits'], 300)

            # 签到流水留痕（trigger=login）
            logs = self.client.get('/api/checkin-logs', headers=self.auth).json()['items']
            self.assertTrue(any(r['account_id'] == item['id'] and r['trigger'] == 'login'
                                for r in logs))
            # 请求确实打到了腾讯端点
            urls = [c[0] for c in client.calls]
            self.assertTrue(any('/v2/plugin/auth/state' in u for u in urls))
            self.assertTrue(any('/v2/billing/meter/daily-checkin' in u for u in urls))
            self.assertTrue(any('/v2/billing/meter/get-user-resource' in u for u in urls))

        # state 已消费，重复提交不产生第二个账号
        again = self.client.post('/api/wb/auth/finish', headers=self.auth,
                                 json={'state': 'FLOW1'})
        self.assertEqual(again.status_code, 409)
        uid_rows = db.query("SELECT id FROM accounts WHERE wb_uid = 'UID-FLOW'")
        self.assertEqual(len(uid_rows), 1)

    def test_09_relogin_same_uid_updates_in_place(self):
        before = db.query("SELECT COUNT(*) AS n FROM accounts WHERE wb_uid = 'UID-FLOW'")[0]['n']
        self.assertEqual(before, 1)
        item = accounts.upsert_workbuddy({
            'uid': 'UID-FLOW', 'nickname': '扫码测试号', 'realm': 'cn',
            'access_token': 'AT-REFRESHED', 'refresh_token': 'RT-NEW',
            'expires_at': 9999999999, 'domain': 'cn', 'enterprise_id': '',
        })
        after = db.query("SELECT COUNT(*) AS n FROM accounts WHERE wb_uid = 'UID-FLOW'")[0]['n']
        self.assertEqual(after, 1, '同一 uid 重新扫码应更新凭据，不能产生重复账号')
        self.assertEqual(accounts.get_account(item['id'])['api_key'], 'AT-REFRESHED')
        self.assertNotIn('AT-REFRESHED', json.dumps(item, ensure_ascii=False))

    def test_10_global_and_enterprise_are_skipped(self):
        item = accounts.upsert_workbuddy({
            'uid': 'UID-GLOBAL', 'nickname': '国际版号', 'realm': 'global',
            'access_token': 'AT-G', 'refresh_token': '', 'expires_at': 0,
            'domain': '', 'enterprise_id': '',
        })
        self.assertFalse(item['checkin_supported'])
        script, client = _patch([])
        with script:
            result = asyncio.run(accounts.checkin_account(item['id'], 'manual'))
        self.assertTrue(result['skipped'])
        self.assertEqual(client.calls, [], '国际版不该向签到接口发请求')
        logs = db.query("SELECT trigger FROM checkin_logs WHERE account_id = ?", (item['id'],))
        self.assertTrue(logs and logs[-1]['trigger'] == 'skip')

    def test_11_workbuddy_account_proxy_uses_v2_and_official_headers(self):
        rows = db.query("SELECT * FROM accounts WHERE wb_uid = 'UID-FLOW'")
        account = rows[0]
        url = __import__('src.gateway', fromlist=['account_url']).account_url(
            account, '/v1/chat/completions')
        self.assertTrue(url.endswith('/v2/chat/completions'), url)
        headers = __import__('src.gateway', fromlist=['account_headers']).account_headers(account)
        self.assertEqual(headers['Authorization'], 'Bearer AT-REFRESHED')
        self.assertEqual(headers['X-User-Id'], 'UID-FLOW')
        self.assertEqual(headers['X-CodeBuddy-Request'], '1')
        # 手工账号仍走 /v1 + 纯 Bearer
        manual = {'base_url': 'https://up.example.com/v1', 'api_key': 'sk-x',
                  'source': 'custom', 'proxy_path': '/v1'}
        self.assertTrue(__import__('src.gateway', fromlist=['account_url']).account_url(
            manual, '/v1/chat/completions').endswith('/v1/chat/completions'))

    def test_12_settings_exposes_workbuddy_info(self):
        resp = self.client.get('/api/settings', headers=self.auth)
        self.assertEqual(resp.status_code, 200)
        info = resp.json()['workbuddy']
        self.assertIn('cn_chat_base', info)
        self.assertTrue(info['auto_checkin_on_login'])
        self.assertEqual(info['versions']['mode'], 'auto')
        self.assertEqual(info['client_version'], versions.client_version('cn'))

    def test_13_versions_endpoints_require_admin_and_force_refresh(self):
        self.assertEqual(self.client.get('/api/wb/versions').status_code, 401)
        self.assertEqual(self.client.post('/api/wb/versions/refresh').status_code, 401)
        snap = self.client.get('/api/wb/versions', headers=self.auth)
        self.assertEqual(snap.status_code, 200)
        self.assertIn('refresh_hours', snap.json())

        scripted = _ScriptedClient([
            {'version': '5.9.3.42398134'},
            {'version': '5.9.4.42593613'},
            {'version': '2.181.0'},
        ])
        with mock.patch.object(versions, 'make_async_client', return_value=scripted):
            refreshed = self.client.post('/api/wb/versions/refresh', headers=self.auth)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertTrue(refreshed.json()['ok'])
        self.assertEqual(refreshed.json()['client_cn'], '5.9.3')
        self.assertEqual(refreshed.json()['cli'], '2.181.0')
        # 刷新后新版本立刻用于出站请求头
        self.assertEqual(wb.headers('cn')['User-Agent'],
                         'WorkBuddy/5.9.3 WorkBuddy/5.9.3 CLI/2.181.0')
        versions.reload()


if __name__ == '__main__':
    unittest.main(verbosity=2)
