"""配置加载：config/config.yml 打底，环境变量覆盖。

优先级：环境变量 > config.yml > 内置默认值。
容器里 CONFIG_PATH=/app/config/config.yml（compose 只读挂载），本地开发自动回落到仓库里的
config/config.yml.example。master_key 未配置时会自动生成并持久化到 data/master_key，
避免每次重建容器后所有已签发的登录态失效。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MASTER_KEY = 'change_this_master_key_in_production'
logger = logging.getLogger('buddywong.config')


def env_str(name: str, default: str = '') -> str:
    value = os.getenv(name)
    return default if value is None or value == '' else value


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, '').strip() or default)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, '').strip() or default)
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, '').strip().lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'on')


def _yaml_section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


class Config:
    def __init__(self) -> None:
        self.config_path = self._config_path()
        raw = self._load_yaml(self.config_path)
        server = _yaml_section(raw, 'server')
        security = _yaml_section(raw, 'security')
        proxy = _yaml_section(raw, 'proxy')
        checkin = _yaml_section(raw, 'checkin')
        workbuddy = _yaml_section(raw, 'workbuddy')

        # ── 服务 ──────────────────────────────────────────────
        self.host: str = env_str('BW_HOST', str(server.get('host', '0.0.0.0')))
        self.port: int = env_int('BW_PORT', int(server.get('port', 3002)))
        self.data_dir: Path = Path(env_str('BW_DATA_DIR', str(server.get('data_dir', ROOT / 'data'))))
        self.log_dir: Path = Path(env_str('BW_LOG_DIR', str(server.get('log_dir', ROOT / 'logs'))))
        self.trust_proxy: bool = env_bool('BW_TRUST_PROXY', bool(server.get('trust_proxy', True)))
        self.debug: bool = env_bool('BW_DEBUG', False)

        # ── 安全 ──────────────────────────────────────────────
        self.admin_user: str = env_str('BW_ADMIN_USER', str(security.get('admin_user', 'admin')))
        self.admin_password: str = env_str('BW_ADMIN_PASSWORD', str(security.get('admin_password', '') or ''))
        self.session_timeout: int = env_int('BW_SESSION_TIMEOUT', int(security.get('session_timeout', 86400)))
        self.master_key: str = self._resolve_master_key(str(security.get('master_key', '') or ''))

        # ── 反代 ──────────────────────────────────────────────
        self.request_timeout: float = env_float('BW_REQUEST_TIMEOUT', float(proxy.get('request_timeout', 120)))
        self.cool_seconds: int = env_int('BW_COOL_SECONDS', int(proxy.get('cool_seconds', 300)))
        self.max_account_retries: int = env_int('BW_MAX_ACCOUNT_RETRIES', int(proxy.get('max_account_retries', 3)))
        self.http_proxy: str = env_str('BW_HTTP_PROXY', str(proxy.get('http_proxy', '') or ''))
        self.model_alias: dict[str, str] = self._model_alias(proxy.get('model_alias'))

        # ── 自动签到 ──────────────────────────────────────────
        self.checkin_enabled: bool = env_bool('BW_CHECKIN_ENABLED', bool(checkin.get('enabled', True)))
        self.checkin_hour: int = env_int('BW_CHECKIN_HOUR', int(checkin.get('hour', 8)))
        self.checkin_minute: int = env_int('BW_CHECKIN_MINUTE', int(checkin.get('minute', 5)))
        self.log_retention_days: int = env_int('BW_LOG_RETENTION_DAYS', int(checkin.get('log_retention_days', 30)))

        # ── WorkBuddy 真实签到协议 ─────────────────────────────
        # 端点与 UA 对齐官方客户端；域名变化时可用环境变量覆盖，不必改代码。
        self.wb_cn_chat_base: str = env_str('BW_WB_CHAT_BASE',
                                            str(workbuddy.get('cn_chat_base', 'https://copilot.tencent.com'))).rstrip('/')
        self.wb_cn_billing_base: str = env_str('BW_WB_BILLING_BASE',
                                               str(workbuddy.get('cn_billing_base', 'https://www.codebuddy.cn'))).rstrip('/')
        self.wb_global_base: str = env_str('BW_WB_GLOBAL_BASE',
                                           str(workbuddy.get('global_base', 'https://www.workbuddy.ai'))).rstrip('/')
        # 客户端版本默认**自动跟随官方发布源**（见 versions.py），这里留空即自动。
        # 只有排查问题需要临时钉死时才填；填了该项就不再自动更新。
        self.wb_client_version: str = env_str('BW_WB_CLIENT_VERSION',
                                              str(workbuddy.get('client_version', '') or '')).strip()
        self.wb_cli_version: str = env_str('BW_WB_CLI_VERSION',
                                           str(workbuddy.get('cli_version', '') or '')).strip()
        self.wb_version_refresh_hours: int = max(1, env_int(
            'BW_WB_VERSION_REFRESH_HOURS', int(workbuddy.get('version_refresh_hours', 6))))
        registries = workbuddy.get('cli_registry_urls')
        self.wb_cli_registry_urls: list[str] = (
            [str(u).strip() for u in registries if str(u).strip().startswith('https://')]
            if isinstance(registries, list) else [])
        self.wb_auto_checkin: bool = env_bool('BW_WB_AUTO_CHECKIN', bool(workbuddy.get('auto_checkin_on_login', True)))
        self.wb_state_ttl: int = max(60, env_int('BW_WB_STATE_TTL', int(workbuddy.get('state_ttl', 900))))

    # ── 内部 ──────────────────────────────────────────────────
    @staticmethod
    def _config_path() -> Path:
        explicit = os.getenv('CONFIG_PATH', '').strip()
        if explicit:
            return Path(explicit)
        preferred = ROOT / 'config' / 'config.yml'
        return preferred if preferred.exists() else ROOT / 'config' / 'config.yml.example'

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        if not path.exists():
            logger.warning('配置文件不存在：%s，使用内置默认值', path)
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.error('读取配置文件失败：%s（%s），使用内置默认值', path, exc)
            return {}

    def _resolve_master_key(self, from_yaml: str) -> str:
        """环境变量 > 配置 > data/master_key（自动生成，跨容器重建保持稳定）。"""
        from_env = os.getenv('BW_MASTER_KEY', '').strip()
        if from_env and from_env != _DEFAULT_MASTER_KEY:
            return from_env
        if from_yaml and from_yaml != _DEFAULT_MASTER_KEY:
            return from_yaml
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key_file = self.data_dir / 'master_key'
        if key_file.exists():
            saved = key_file.read_text(encoding='utf-8').strip()
            if saved:
                return saved
        generated = secrets.token_urlsafe(48)
        key_file.write_text(generated, encoding='utf-8')
        logger.warning('未设置 BW_MASTER_KEY，已生成随机密钥并保存到 %s；请备份该文件', key_file)
        return generated

    @staticmethod
    def _model_alias(from_yaml: Any) -> dict[str, str]:
        raw = os.getenv('BW_MODEL_ALIAS', '').strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                logger.error('BW_MODEL_ALIAS 不是合法 JSON，已忽略')
        if isinstance(from_yaml, dict):
            return {str(k): str(v) for k, v in from_yaml.items()}
        return {}


CFG = Config()


def app_version() -> str:
    """镜像构建时由 APP_VERSION 写入；本地开发回退到源码版本号。"""
    version_file = ROOT / 'src' / 'version.txt'
    if version_file.exists():
        value = version_file.read_text(encoding='utf-8').strip()
        if value:
            return value
    from . import __version__
    return f'v{__version__}'
