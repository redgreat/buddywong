# buddywong 容器镜像（管理端 + OpenAI 兼容反代网关 + 静态看板）
#
# 设计取舍：
# 1. 单镜像自包含：Python 运行时 + 预编译好的静态前端都烘进镜像，
#    没有 Node 构建阶段，GitHub Action 只做 docker build/push。
# 2. 数据与凭据全部走卷：/app/data（SQLite）、/app/logs（日志）、
#    /app/config（config.yml，只读）。镜像里不放任何真实密钥。
# 3. APP_VERSION 由 CI 通过 build-arg 传入，写入 version.txt 供 /health 与面板显示。

FROM python:3.13-slim

WORKDIR /app

# curl 供容器健康检查使用
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码（静态前端已在 src/static 中，无需构建）
COPY . .

# 构建参数：版本号（放在 COPY 之后，确保 src 目录存在）
ARG APP_VERSION=v0.1.0
RUN echo "${APP_VERSION}" > /app/src/version.txt

RUN mkdir -p /app/data /app/logs

EXPOSE 3002

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3002/health || exit 1

CMD ["python", "-m", "src.server"]
