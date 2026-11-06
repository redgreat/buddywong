"""buddywong 入口：管理 API + OpenAI 兼容反代网关 + 静态可视化看板。

启动：python -m src.server（容器 CMD 同此）
  GET  /health                 健康检查（compose healthcheck 用）
  /api/*                       管理端 REST（需会话 token）
  /v1/*                        对外反代（需 API Key）
  /                            内置静态看板（无需 Node 构建）
"""
from __future__ import annotations

import logging
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, scheduler
from .api import router as admin_router
from .config import CFG, ROOT, app_version
from .gateway import GatewayError, router as gateway_router
from .security import hash_password

logger = logging.getLogger('buddywong')
_STARTED = time.time()
STATIC_DIR = ROOT / 'src' / 'static'


def _setup_logging() -> None:
    # Windows 本地默认 cp936，日志里出现中文/符号会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8')
            except Exception:  # noqa: BLE001
                pass
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        Path(CFG.log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(CFG.log_dir) / 'buddywong.log', encoding='utf-8'))
    except OSError as exc:
        print(f'[warn] 日志文件不可用：{exc}')
    logging.basicConfig(
        level=logging.DEBUG if CFG.debug else logging.INFO,
        format='%(asctime)s %(levelname)-7s [%(name)s] %(message)s',
        handlers=handlers, force=True,
    )


_setup_logging()


def ensure_admin() -> None:
    """首次启动创建管理员；未指定密码时随机生成并以醒目方式打印一次。"""
    if db.query_one('SELECT id FROM users WHERE username = ?', (CFG.admin_user,)):
        return
    password = CFG.admin_password or secrets.token_urlsafe(14)
    db.execute('INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)',
               (CFG.admin_user, hash_password(password), time.time()))
    if CFG.admin_password:
        logger.info('已按配置创建管理员账号 %s（密码来自配置，不在日志中回显）', CFG.admin_user)
    else:
        logger.warning(
            '\n' + '=' * 62 +
            f'\n  初始管理员账号：{CFG.admin_user}'
            f'\n  初始管理员密码：{password}'
            '\n  请登录后立刻到「设置 → 修改密码」改掉它。'
            '\n  （也可用环境变量 BW_ADMIN_PASSWORD 固定密码）'
            '\n' + '=' * 62)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    ensure_admin()
    scheduler.start()
    logger.info('buddywong %s 已启动：http://%s:%s', app_version(), CFG.host, CFG.port)
    try:
        yield
    finally:
        scheduler.stop()
        db.close()
        logger.info('buddywong 已停止')


app = FastAPI(
    title='buddywong',
    description='自动签到 + 账号池反代 + 可视化控制台',
    version=app_version(),
    lifespan=lifespan,
    docs_url='/docs' if CFG.debug else None,
    redoc_url=None,
    openapi_url='/openapi.json' if CFG.debug else None,
)

app.include_router(admin_router)
app.include_router(gateway_router)


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


@app.get('/health', tags=['system'])
async def health():
    try:
        accounts = db.query_one('SELECT COUNT(*) AS n FROM accounts')
        count = int(accounts['n']) if accounts else 0
    except Exception:  # noqa: BLE001
        count = -1
    return {
        'status': 'ok',
        'version': app_version(),
        'accounts': count,
        'uptime_seconds': int(time.time() - _STARTED),
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
    }


if STATIC_DIR.exists():
    # 必须放在最后：/api、/v1、/health 由上面的路由优先匹配
    app.mount('/', StaticFiles(directory=str(STATIC_DIR), html=True), name='static')
else:
    logger.warning('静态目录不存在：%s，看板将不可用', STATIC_DIR)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level='info', access_log=True)
