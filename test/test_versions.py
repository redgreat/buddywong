"""官方客户端版本自动跟随的回归自测：不走真实网络，全部用脚本化 HTTP 客户端。

覆盖：官方源解析（4 段 -> UA 3 段）、国内/国际分别取版本、备用源、全源失败
保留旧值、手动钉死跳过探测、磁盘缓存重启恢复、TTL 内不重复请求、UA 集成。
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

_TMP = tempfile.mkdtemp(prefix='buddywong-ver-test-')
os.environ.setdefault('BW_DATA_DIR', _TMP)
os.environ.setdefault('BW_LOG_DIR', _TMP)
os.environ.setdefault('BW_ADMIN_PASSWORD', 'test-password-123')
os.environ.setdefault('BW_MASTER_KEY', 'test-master-key-for-unittest')
os.environ.setdefault('BW_CHECKIN_ENABLED', '0')

from src import versions, workbuddy as wb  # noqa: E402
from src.config import CFG  # noqa: E402

CN_UPDATE = {'version': '5.9.3.42398134', 'productVersion': '5.9.3.42398134'}
GLOBAL_UPDATE = {'version': '5.9.4.42593613', 'productVersion': '5.9.4.42593613'}
CLI_LATEST = {'version': '2.181.0', 'name': '@tencent-ai/codebuddy-code'}


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _ScriptedClient:
    """按调用顺序吐响应；可注入异常 / HTTP 错误来测备用源与降级。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.calls.append(url)
        item = self.script.pop(0) if self.script else {'code': 0, 'data': {}}
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return _Resp(item[0], item[1])
        return _Resp(item)


def _patch(script: list):
    client = _ScriptedClient(script)
    return mock.patch.object(versions, 'make_async_client', return_value=client), client


class VersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix='bw-ver-case-'))
        self.saved = (CFG.data_dir, CFG.log_dir, CFG.wb_client_version,
                      CFG.wb_cli_version, CFG.wb_version_refresh_hours,
                      CFG.wb_cli_registry_urls)
        CFG.data_dir = self.tmp
        CFG.log_dir = self.tmp
        CFG.wb_client_version = ''
        CFG.wb_cli_version = ''
        CFG.wb_version_refresh_hours = 6
        CFG.wb_cli_registry_urls = []
        versions.reload()

    def tearDown(self) -> None:
        (CFG.data_dir, CFG.log_dir, CFG.wb_client_version,
         CFG.wb_cli_version, CFG.wb_version_refresh_hours,
         CFG.wb_cli_registry_urls) = self.saved
        versions.reload()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_version_parsing_is_strict(self):
        self.assertEqual(versions._clean_version('5.9.3.42398134'), '5.9.3.42398134')
        self.assertEqual(versions._short('5.9.3.42398134'), '5.9.3')
        self.assertEqual(versions._clean_version('2.181.0'), '2.181.0')
        self.assertEqual(versions._short('2.181.0'), '2.181.0')
        # 头注入 / 脏数据一律拒绝，不能进 User-Agent
        for bad in ('', None, 123, 'abc', '5.9.3\r\nX-Evil: 1', 'v5.9.3',
                    '5.9.3; rm -rf /', '9' * 60):
            self.assertEqual(versions._clean_version(bad), '', repr(bad))

    def test_02_probe_official_sources_and_persist(self):
        script, client = _patch([CN_UPDATE, GLOBAL_UPDATE, CLI_LATEST])
        with script:
            result = asyncio.run(versions.refresh(force=True))
        self.assertEqual(result['client_cn'], '5.9.3')
        self.assertEqual(result['client_global'], '5.9.4')
        self.assertEqual(result['cli'], '2.181.0')
        self.assertEqual(result['full_cn'], '5.9.3.42398134')
        self.assertEqual(result['source'], 'official')
        self.assertEqual(result['source_text'], '官方实时')
        self.assertEqual(result['mode'], 'auto')
        self.assertEqual(versions.client_version('cn'), '5.9.3')
        self.assertEqual(versions.client_version('global'), '5.9.4')
        self.assertEqual(versions.cli_version(), '2.181.0')
        self.assertTrue(result['fetched_at'] > 0)
        self.assertEqual(len(client.calls), 3)
        self.assertIn('/v2/update?platform=workbuddy-win32-x64-user', client.calls[0])
        self.assertIn('www.workbuddy.ai', client.calls[1])
        self.assertIn('codebuddy-code/latest', client.calls[2])
        # 落盘，重启后不丢
        cache = json.loads((self.tmp / 'wb_versions.json').read_text(encoding='utf-8'))
        self.assertEqual(cache['client_cn'], '5.9.3')
        self.assertEqual(cache['full_cn'], '5.9.3.42398134')

    def test_03_uses_official_values_in_outbound_headers(self):
        with _patch([CN_UPDATE, GLOBAL_UPDATE, CLI_LATEST])[0]:
            asyncio.run(versions.refresh(force=True))
        cn = wb.headers('cn')
        self.assertEqual(cn['User-Agent'], 'WorkBuddy/5.9.3 WorkBuddy/5.9.3 CLI/2.181.0')
        self.assertEqual(cn['X-IDE-Version'], '5.9.3.42398134')
        gl = wb.headers('global')
        self.assertEqual(gl['User-Agent'], 'WorkBuddy/5.9.4 WorkBuddy AI/5.9.4 CLI/2.181.0')
        self.assertEqual(gl['X-IDE-Version'], '5.9.4.42593613')
        billing = wb.billing_headers('cn', {'uid': 'U1', 'access_token': 'AT'})
        self.assertEqual(billing['User-Agent'], 'WorkBuddy/5.9.3')
        self.assertEqual(billing['X-IDE-Version'], '5.9.3.42398134')

    def test_04_fallback_to_secondary_source(self):
        # 国内主源 HTTP 400 -> 同域名第二 platform；CLI 的 npmjs 超时 -> 国内镜像
        script, client = _patch([
            ({'code': 10001, 'msg': 'bad platform'}, 400),
            CN_UPDATE,
            GLOBAL_UPDATE,
            OSError('npmjs timeout'),
            CLI_LATEST,
        ])
        with script:
            result = asyncio.run(versions.refresh(force=True))
        self.assertEqual(result['client_cn'], '5.9.3')
        self.assertEqual(result['cli'], '2.181.0')
        self.assertTrue(any('workbuddy-darwin-arm64' in u for u in client.calls))
        self.assertTrue(any('npmmirror.com' in u for u in client.calls))
        self.assertEqual(result['sources']['client_cn']['url'].split('?')[1],
                         'platform=workbuddy-darwin-arm64')

    def test_05_all_sources_fail_keeps_last_known_good(self):
        with _patch([CN_UPDATE, GLOBAL_UPDATE, CLI_LATEST])[0]:
            asyncio.run(versions.refresh(force=True))
        before = versions.status()
        script, _ = _patch([])   # 默认响应不含 version -> 全源失败
        with script:
            after = asyncio.run(versions.refresh(force=True))
        self.assertEqual(after['client_cn'], '5.9.3', '不能倒退回内置默认')
        self.assertEqual(after['client_global'], '5.9.4')
        self.assertEqual(after['cli'], '2.181.0')
        self.assertEqual(after['source'], 'cached')
        self.assertEqual(after['fetched_at'], before['fetched_at'])
        self.assertTrue(after['last_error'])
        # 签到请求照常能发出去，不因版本探测失败而中断
        self.assertTrue(wb.headers('cn')['User-Agent'].startswith('WorkBuddy/5.9.3'))

    def test_06_never_probed_falls_back_to_builtin(self):
        script, _ = _patch([])
        with script:
            result = asyncio.run(versions.refresh(force=True))
        self.assertEqual(result['client_cn'], versions._DEFAULTS['client_cn'])
        self.assertEqual(result['cli'], versions._DEFAULTS['cli'])
        self.assertEqual(result['source'], 'builtin')
        self.assertEqual(result['fetched_at_text'], '从未')

    def test_07_manual_pin_skips_probing(self):
        CFG.wb_client_version = '5.5.4'
        CFG.wb_cli_version = '2.137.1'
        script, client = _patch([])
        with script:
            result = asyncio.run(versions.refresh(force=True))
        self.assertEqual(client.calls, [], '手动钉死时不应发出任何版本探测请求')
        self.assertEqual(result['mode'], 'pinned')
        self.assertEqual(result['client_cn'], '5.5.4')
        self.assertEqual(result['cli'], '2.137.1')
        self.assertEqual(wb.headers('cn')['User-Agent'],
                         'WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1')
        # 只钉 CLI，客户端仍自动探测
        CFG.wb_client_version = ''
        script2, client2 = _patch([CN_UPDATE, GLOBAL_UPDATE])
        with script2:
            result2 = asyncio.run(versions.refresh(force=True))
        self.assertEqual(result2['client_cn'], '5.9.3')
        self.assertEqual(result2['cli'], '2.137.1')
        self.assertEqual(len(client2.calls), 2)

    def test_08_reload_restores_cached_values(self):
        with _patch([CN_UPDATE, GLOBAL_UPDATE, CLI_LATEST])[0]:
            asyncio.run(versions.refresh(force=True))
        versions.reload()   # 模拟容器重启：模块重新导入
        snap = versions.status()
        self.assertEqual(snap['client_cn'], '5.9.3')
        self.assertEqual(snap['full_cn'], '5.9.3.42398134')
        self.assertEqual(snap['cli'], '2.181.0')
        self.assertEqual(snap['source'], 'cached')

    def test_09_ttl_skips_network_until_due(self):
        script, client = _patch([CN_UPDATE, GLOBAL_UPDATE, CLI_LATEST])
        with script:
            asyncio.run(versions.refresh(force=True))
            first_calls = len(client.calls)
            asyncio.run(versions.refresh())          # TTL 未到
            self.assertEqual(len(client.calls), first_calls)
            CFG.wb_version_refresh_hours = 1
            versions._state['checked_at'] -= 7200    # 人为让 TTL 到期
            asyncio.run(versions.refresh())
            self.assertGreater(len(client.calls), first_calls)

    def test_10_status_shape_for_settings_page(self):
        snap = versions.status()
        for key in ('client_cn', 'client_global', 'cli', 'full_cn', 'full_global',
                    'source', 'source_text', 'mode', 'pinned', 'refresh_hours',
                    'next_refresh_at', 'checked_at_text', 'fetched_at_text',
                    'client_update_path', 'cli_registry', 'last_error', 'sources'):
            self.assertIn(key, snap, key)
        self.assertTrue(snap['cli_registry'].startswith('https://'))
        self.assertTrue(snap['client_update_path'].startswith('/v2/update'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
