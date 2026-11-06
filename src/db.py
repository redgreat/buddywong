"""SQLite 存储层：账号、密钥、调用审计、签到流水、动态设置。

单连接 + RLock + WAL：本项目是单容器小流量控制台，这个组合足够且最省心。
时间统一用 time.time() 的浮点秒，另存本地时区的 day/hour 冗余列 ——
SQLite 的 date(ts,'unixepoch') 按 UTC 算，直接用会把 UTC+8 的凌晨算到前一天。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .config import CFG

logger = logging.getLogger('buddywong.db')
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    group_name       TEXT NOT NULL DEFAULT 'default',
    base_url         TEXT NOT NULL,
    api_key          TEXT NOT NULL,
    models           TEXT NOT NULL DEFAULT '[]',
    enabled          INTEGER NOT NULL DEFAULT 1,
    weight           INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'unknown',
    credits          REAL NOT NULL DEFAULT 0,
    expires_at       TEXT NOT NULL DEFAULT '',
    checkin_url      TEXT NOT NULL DEFAULT '',
    checkin_method   TEXT NOT NULL DEFAULT 'POST',
    checkin_headers  TEXT NOT NULL DEFAULT '{}',
    checkin_body     TEXT NOT NULL DEFAULT '{}',
    last_checkin_at  REAL,
    last_probe_at    REAL,
    last_error       TEXT NOT NULL DEFAULT '',
    fail_count       INTEGER NOT NULL DEFAULT 0,
    cool_until       REAL NOT NULL DEFAULT 0,
    success_count    INTEGER NOT NULL DEFAULT 0,
    fail_total       INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    key_hash        TEXT NOT NULL UNIQUE,
    prefix          TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    quota           INTEGER NOT NULL DEFAULT 0,
    used_count      INTEGER NOT NULL DEFAULT 0,
    ip_whitelist    TEXT NOT NULL DEFAULT '[]',
    model_whitelist TEXT NOT NULL DEFAULT '[]',
    expires_at      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    last_used_at    REAL
);

CREATE TABLE IF NOT EXISTS request_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    day               TEXT NOT NULL,
    hour              INTEGER NOT NULL,
    request_id        TEXT NOT NULL,
    key_id            INTEGER,
    key_prefix        TEXT NOT NULL DEFAULT '',
    account_id        INTEGER,
    account_name      TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    upstream_model    TEXT NOT NULL DEFAULT '',
    status_code       INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    ttft_ms           INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    stream            INTEGER NOT NULL DEFAULT 0,
    client_ip         TEXT NOT NULL DEFAULT '',
    error             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts        ON request_logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_day       ON request_logs(day);
CREATE INDEX IF NOT EXISTS idx_logs_key       ON request_logs(key_id, ts);
CREATE INDEX IF NOT EXISTS idx_logs_account   ON request_logs(account_id, ts);

CREATE TABLE IF NOT EXISTS checkin_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    account_id   INTEGER NOT NULL,
    account_name TEXT NOT NULL DEFAULT '',
    ok           INTEGER NOT NULL DEFAULT 0,
    status_code  INTEGER NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT '',
    trigger      TEXT NOT NULL DEFAULT 'auto'
);
CREATE INDEX IF NOT EXISTS idx_checkin_ts ON checkin_logs(ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 可在「设置」页热改的项，启动时用 config.yml / 环境变量的值播种（不覆盖已有值）
_SEED: dict[str, Any] = {
    'checkin_enabled': CFG.checkin_enabled,
    'checkin_hour': CFG.checkin_hour,
    'checkin_minute': CFG.checkin_minute,
    'cool_seconds': CFG.cool_seconds,
    'max_account_retries': CFG.max_account_retries,
    'request_timeout': CFG.request_timeout,
    'model_alias': CFG.model_alias,
    'log_retention_days': CFG.log_retention_days,
}


def db_path() -> Path:
    return Path(CFG.data_dir) / 'buddywong.db'


def connect() -> sqlite3.Connection:
    """建库、建表、播种设置；可重复调用。"""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        Path(CFG.data_dir).mkdir(parents=True, exist_ok=True)
        Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path()), check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA)
        _conn = conn
        for key, value in _SEED.items():
            conn.execute('INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)',
                         (key, json.dumps(value, ensure_ascii=False)))
        logger.info('数据库已就绪：%s', db_path())
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        cur = connect().execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _lock:
        cur = connect().execute(sql, tuple(params))
        return int(cur.lastrowid or 0)


def execute_many(sql: str, rows: list[tuple]) -> None:
    with _lock:
        connect().executemany(sql, rows)


# ── 动态设置 ───────────────────────────────────────────────────
def get_setting(key: str, default: Any = None) -> Any:
    row = query_one('SELECT value FROM settings WHERE key = ?', (key,))
    if row is None:
        return default
    try:
        return json.loads(row['value'])
    except (TypeError, ValueError, json.JSONDecodeError):
        return row['value']


def set_setting(key: str, value: Any) -> None:
    execute('INSERT INTO settings(key, value) VALUES(?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, json.dumps(value, ensure_ascii=False)))


def all_settings() -> dict[str, Any]:
    return {row['key']: get_setting(row['key']) for row in query('SELECT key FROM settings')}


def setting_str(key: str, default: str = '') -> str:
    value = get_setting(key, default)
    return '' if value is None else str(value)


def setting_bool(key: str, default: bool = False) -> bool:
    value = get_setting(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def setting_int(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def setting_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def setting_dict(key: str, default: dict | None = None) -> dict:
    value = get_setting(key, default or {})
    return value if isinstance(value, dict) else {}


# ── 时间口径 ───────────────────────────────────────────────────
def local_parts(ts: float | None = None) -> tuple[str, int]:
    """返回 (本地日期 YYYY-MM-DD, 本地小时 0-23)，写入日志时统一调用。"""
    t = time.time() if ts is None else float(ts)
    lt = time.localtime(t)
    return time.strftime('%Y-%m-%d', lt), lt.tm_hour


def cleanup_logs(retention_days: int) -> int:
    """按天数清理调用日志与签到流水；retention_days <= 0 表示不清理。"""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    with _lock:
        cur = connect().execute('DELETE FROM request_logs WHERE ts < ?', (cutoff,))
        n1 = cur.rowcount or 0
        cur = connect().execute('DELETE FROM checkin_logs WHERE ts < ?', (cutoff,))
        n2 = cur.rowcount or 0
    return n1 + n2
