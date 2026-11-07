"""后台调度线程：每日自动签到、官方客户端版本同步、冷却到期恢复、历史日志清理。

不用 APScheduler / celery —— 一个 daemon 线程每 20 秒 tick 一次就够，
容器里少一个依赖、少一个进程。所有「今天是否已经跑过」的状态存在 settings 表里，
重启容器也不会重复签到。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from . import accounts, db, versions
from .config import CFG

logger = logging.getLogger('buddywong.scheduler')

_TICK_SECONDS = 20
_CLEANUP_INTERVAL = 6 * 3600
_stop = threading.Event()
_thread: threading.Thread | None = None


def _today() -> str:
    return time.strftime('%Y-%m-%d', time.localtime())


def _tick() -> None:
    now = time.time()

    # 1) 冷却到期：恢复成 healthy，下次轮询才会重新选到它
    db.execute("UPDATE accounts SET status = 'healthy', fail_count = 0 "
               "WHERE status = 'cooling' AND cool_until > 0 AND cool_until <= ?", (now,))

    # 2) 每日签到（到点后触发一次，日期记录防重复）
    if db.setting_bool('checkin_enabled', CFG.checkin_enabled):
        hour = max(0, min(23, db.setting_int('checkin_hour', CFG.checkin_hour)))
        minute = max(0, min(59, db.setting_int('checkin_minute', CFG.checkin_minute)))
        local = time.localtime(now)
        if (local.tm_hour, local.tm_min) >= (hour, minute) and db.setting_str('checkin_last_day') != _today():
            db.set_setting('checkin_last_day', _today())
            try:
                results: list[dict[str, Any]] = asyncio.run(accounts.checkin_all('auto'))
                ok = sum(1 for r in results if r.get('ok'))
                logger.info('每日自动签到完成：%d/%d 成功', ok, len(results))
            except Exception as exc:  # noqa: BLE001
                logger.error('每日自动签到异常：%s', exc)

    # 3) 官方客户端版本同步：启动后第一个 tick 就拉一次，之后按 TTL 自动跟随升级。
    #    TTL 未到时这里只做一次时间比较，不创建事件循环、不发网络请求。
    try:
        if now >= versions.status()['next_refresh_at']:
            asyncio.run(versions.refresh_if_stale())
    except Exception as exc:  # noqa: BLE001 — 版本探测失败绝不阻断签到主流程
        logger.warning('官方客户端版本同步失败（沿用上次值）：%s', exc)

    # 4) 日志清理（每 6 小时一次）
    last_cleanup = db.setting_float('logs_cleanup_last_ts', 0)
    if now - last_cleanup >= _CLEANUP_INTERVAL:
        db.set_setting('logs_cleanup_last_ts', now)
        days = db.setting_int('log_retention_days', CFG.log_retention_days)
        try:
            removed = db.cleanup_logs(days)
            if removed:
                logger.info('已清理 %d 条过期日志（保留 %d 天）', removed, days)
        except Exception as exc:  # noqa: BLE001
            logger.error('清理日志失败：%s', exc)


def _loop() -> None:
    logger.info('后台调度线程已启动（tick=%ss）', _TICK_SECONDS)
    while not _stop.wait(_TICK_SECONDS):
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001
            logger.error('调度 tick 异常：%s', exc)
    logger.info('后台调度线程已退出')


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='buddywong-scheduler', daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=3)
