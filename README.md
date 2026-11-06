# buddywong

**自动签到获取 AI Token 平台 · 账号池管理 + OpenAI 兼容反代 + 可视化控制台**

参考 [workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) 的功能划分（账号管理 / 反代网关 / 可视化设置），重新实现的一套**自包含、可独立运行**的轻量项目。单容器、单进程、一个 SQLite 文件就能跑，不依赖任何外部上游网关。

> **发布形态只有一种**：Linux 服务器上的 Docker 镜像。没有 Windows 安装器、没有一键安装脚本、没有 GitHub Release 更新器。
> 发布链路整套沿用 `git_mcp_server` 仓库：本地 `release.ps1` 打标签 → GitHub Action 打包 → 推送阿里云 ACR → 服务器 `redeploy.sh` 自动更新。

---

## 功能一览

### 账号管理

- 账号 CRUD、分组、启停、权重（1–20）、积分/余额、到期日
- **自动签到**：每个账号可配签到地址 / 方法 / 请求头 / 请求体，每日到点自动执行（时刻可配）
- 签到成功时从响应里自动提取 `credits / balance / points` 等字段回写余额
- 手动签到、批量签到、连通性探测（拉 `/v1/models`）、状态分档（正常 / 未探测 / 冷却 / 异常 / 过期 / 停用）
- 签到流水全量留痕，可查每次的 HTTP 状态与返回说明

### 反代网关（对外 `/v1`）

- OpenAI 兼容：`GET /v1/models`、`POST /v1/chat/completions`（流式 + 非流式）、`POST /v1/embeddings`
- **多密钥分发**：每把密钥独立的额度、有效期、IP 白名单（支持 CIDR）、模型白名单；数据库只存 SHA-256 摘要
- 账号**加权轮询**选号，失败自动冷却并切换下一个账号，全挂才返回 502（OpenAI 格式错误体）
- 模型别名映射（下游名字 → 上游实际模型），迁移无感
- 全量审计：密钥、IP、账号、模型、状态码、耗时、首字延迟、Token 用量逐笔落库

### 可视化

- 深色单页看板，原生 HTML/CSS/JS，**零 Node 构建**、零 CDN 依赖，离线可用
- 仪表盘：KPI 卡 + 近 24 小时调用柱状图 + 账号状态分布 + 最近调用
- 账号 / 密钥 / 调用日志 / 签到流水 / 系统设置五个页面，模态框增删改，操作即时反馈
- 调用日志支持模型 / 状态筛选与分页

---

## 架构

```
浏览器 ──> Nginx/Caddy(HTTPS) ──> buddywong 容器 :3002
                                    │
                    ┌───────────────┼────────────────┐
                    │               │                │
              静态看板 SPA     /api/* 管理端     /v1/* 反代网关
              (src/static)    JWT-like token      API Key 鉴权
                    │               │                │
                    └───────────────┼────────────────┘
                                    │
                          SQLite (/app/data/buddywong.db)
                                    │
              定时线程：每日签到 / 冷却恢复 / 日志清理
                                    │
              httpx ──> 各账号的上游 OpenAI 兼容端点
```

技术栈：**Python 3.13 + FastAPI + uvicorn + httpx + PyYAML + SQLite**，前端为内置静态资源。运行时依赖只有 4 个 Python 包。

---

## Linux 服务器部署（唯一发布形态）

前置：Linux 服务器 + Docker + Docker Compose v2，且能拉取阿里云 ACR 镜像（私有仓库需先 `docker login`）。

### 首次部署

```bash
# 1. 固定部署目录（redeploy.sh 里写死了这个路径）
mkdir -p /root/buddywong && cd /root/buddywong
git clone https://github.com/redgreat/buddywong.git .

# 2. 配置（至少改 master_key），并给脚本加执行权限
cp config/config.yml.example config/config.yml
chmod +x script/*.sh
vi config/config.yml

# 3. 拉镜像并启动
docker compose pull
docker compose up -d

# 4. 拿初始管理员密码（未设 BW_ADMIN_PASSWORD 时随机生成，只打印一次）
docker compose logs buddywong | grep -A6 '初始管理员'

# 5. 健康检查
curl http://127.0.0.1:3002/health
```

浏览器打开 `http://<服务器>:3002`（若只绑了 127.0.0.1，请走下面的反向代理）。登录后第一件事：到「设置 → 修改管理密码」改掉初始密码。

> 容器端口默认只绑 `127.0.0.1:3002`。管理端持有全部账号凭据，**不要直接暴露公网**。

### Nginx 反向代理示例

```nginx
server {
    listen 443 ssl;
    server_name bw.example.com;

    ssl_certificate     /etc/nginx/cert/fullchain.pem;
    ssl_certificate_key /etc/nginx/cert/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:3002;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering    off;          # 流式响应必须关缓冲
        proxy_read_timeout 300s;
    }
}
```

`proxy_buffering off` 是流式对话能逐字输出的关键；`X-Forwarded-For` 让 IP 白名单拿到真实客户端 IP。

---

## 发布链路

```
本地 Windows                 GitHub                     阿里云 ACR            服务器
─────────────                ───────                    ──────────            ──────
pwsh script/release.ps1
  → 自动递增 v0.0.x
  → git tag + git push  ──>  build-deploy.yml
                              → docker build (linux/amd64)
                              → push :vX.Y.Z + :latest  ──>  镜像就绪
                                                                │
                              bash script/redeploy.sh  <──────────┘
                                stop → rm → 清旧镜像 → 清日志
                                → compose pull → up -d → 状态检查
```

### 一次性配置

在 GitHub 仓库 `Settings → Secrets and variables → Actions` 添加：

| Secret | 说明 |
|---|---|
| `ALIYUN_USERNAME` | 阿里云 ACR 登录用户名 |
| `ALIYUN_PASSWORD` | 阿里云 ACR 固定密码 / 访问令牌 |
| `ALIYUN_NAMESPACE` | ACR 命名空间（本仓库为 `redgreat`） |
| `DOCKER_IMAGE_NAME` | 镜像名（本仓库为 `buddywong`） |

> `docker-compose.yml` 里的镜像地址是硬编码的 `registry.cn-hangzhou.aliyuncs.com/redgreat/buddywong:latest`。
> 如果你的 namespace / 镜像名不同，**同时改 compose 和这里的 Secrets**，两边必须一致。

服务器首次拉取私有镜像前登录一次：

```bash
docker login --username=<阿里云账号> registry.cn-hangzhou.aliyuncs.com
```

### 日常发版

```bash
# 本地（Windows PowerShell 7）
pwsh -NoProfile -ExecutionPolicy Bypass -File .\script\release.ps1          # 自动 +1
pwsh -NoProfile -ExecutionPolicy Bypass -File .\script\release.ps1 v0.1.0   # 指定版本
# 或 make release

# 等 GitHub Action 跑完（Actions 页可看进度），然后在服务器：
cd /root/buddywong && bash script/redeploy.sh
```

`release.ps1` 会检查工作区是否干净、按最新 `v*` 标签自动递增版本、创建并推送标签；标签推送即触发 CI。脚本带 UTF-8 BOM，PowerShell 7 与 5.1 都能直接执行（推荐用 7）。Linux 本地可用等价的 `script/release.sh`。

### 让服务器「自动更新」

`redeploy.sh` 本身是一次完整的无人值守更新流程，挂到 crontab 即可定时更新（每次会重新 pull `:latest`）：

```bash
crontab -e
# 每 10 分钟检查一次（镜像没变时 docker compose pull 很快，容器会被重建）
*/10 * * * * bash /root/buddywong/script/redeploy.sh >> /root/buddywong/logs/redeploy.log 2>&1
```

> 注意：这个脚本每次都会重建容器。若希望只在有新镜像时才重启，把 crontab 换成
> 「先 `docker compose pull`，比较镜像 ID，有变化再执行 `redeploy.sh`」的包装逻辑。

---

## 配置

配置文件 `config/config.yml`（从 `.example` 复制），以只读方式挂载进容器。**所有项都能用环境变量覆盖**，优先级：环境变量 > config.yml > 内置默认值。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `CONFIG_PATH` | `/app/config/config.yml` | 配置文件路径 |
| `BW_HOST` / `BW_PORT` | `0.0.0.0` / `3002` | 监听地址与端口 |
| `BW_DATA_DIR` / `BW_LOG_DIR` | `/app/data` / `/app/logs` | 数据库与日志目录 |
| `BW_MASTER_KEY` | 自动生成并持久化 | 会话签名密钥；未配置时写入 `data/master_key`，**请备份** |
| `BW_ADMIN_USER` / `BW_ADMIN_PASSWORD` | `admin` / 随机 | 管理员账号与首启密码 |
| `BW_SESSION_TIMEOUT` | `86400` | 会话有效期（秒） |
| `BW_TRUST_PROXY` | `1` | 是否信任 `X-Forwarded-For` |
| `BW_REQUEST_TIMEOUT` | `120` | 上游请求超时（秒） |
| `BW_COOL_SECONDS` | `300` | 账号失败冷却时长（秒） |
| `BW_MAX_ACCOUNT_RETRIES` | `3` | 单次请求最多换几个账号 |
| `BW_MODEL_ALIAS` | `{}` | 模型别名，JSON 字符串 |
| `BW_HTTP_PROXY` | 空 | 出站代理（http/https/socks5） |
| `BW_CHECKIN_ENABLED` / `BW_CHECKIN_HOUR` / `BW_CHECKIN_MINUTE` | `1` / `8` / `5` | 每日自动签到时刻（容器 TZ=Asia/Shanghai） |
| `BW_LOG_RETENTION_DAYS` | `30` | 日志保留天数，0 = 永久 |

`checkin_enabled`、`checkin_hour/minute`、`cool_seconds`、`max_account_retries`、`request_timeout`、`model_alias`、`log_retention_days` 还能在「设置」页热改，写入数据库后立即生效，不用重启容器。

---

## 接入使用

签发密钥：面板「密钥分发 → 签发新密钥」，明文只显示一次，请立即复制。

```bash
# 查看可用模型
curl http://127.0.0.1:3002/v1/models -H "Authorization: Bearer bw-xxxx"

# 非流式对话
curl http://127.0.0.1:3002/v1/chat/completions \
  -H "Authorization: Bearer bw-xxxx" -H "Content-Type: application/json" \
  -d '{"model":"你的模型名","messages":[{"role":"user","content":"你好"}],"stream":false}'

# 流式对话加 "stream": true 即可
```

Python（OpenAI SDK 直连）：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:3002/v1", api_key="bw-xxxx")
print(client.chat.completions.create(
    model="你的模型名", messages=[{"role": "user", "content": "hi"}]
).choices[0].message.content)
```

管理端 API 统一在 `/api/*`，登录后用 `Authorization: Bearer <会话token>`；主要端点：
`/api/login`、`/api/overview`、`/api/stats`、`/api/accounts`、`/api/keys`、`/api/logs`、`/api/checkin-logs`、`/api/settings`、`/api/password`。

---

## 数据与备份

| 路径（宿主机） | 内容 | 是否必须备份 |
|---|---|---|
| `./data/buddywong.db` | 账号、密钥摘要、调用日志、签到流水、设置 | **是** |
| `./data/master_key` | 会话签名密钥（自动生成时才有） | **是** |
| `./config/config.yml` | 启动配置 | **是** |
| `./logs/` | 运行日志 | 否 |

compose 里三者都是 bind mount，重建容器不会丢。`redeploy.sh` 只清理 `./logs/*.log*` 和旧镜像，**不动 data/**。

---

## 本地开发

```bash
pip install -r requirements.txt
python -m src.server          # http://127.0.0.1:3002
make test                     # 回归自测（unittest，含接口冒烟）
```

设置 `BW_DEBUG=1` 可打开 `/docs`。本地不设 `BW_DATA_DIR` 时，数据写到仓库内的 `data/`、`logs/`（已 gitignore）。

---

## 目录结构

```
buddywong/
├── .github/workflows/build-deploy.yml   # tag → Docker 镜像 → 阿里云 ACR
├── Dockerfile                           # python:3.13-slim，零 Node 构建
├── docker-compose.yml                   # 生产部署（ACR 镜像 + 卷 + healthcheck）
├── config/config.yml.example            # 配置模板
├── script/
│   ├── release.ps1                      # 本地打 tag（Windows）
│   ├── release.sh                       # 本地打 tag（Linux）
│   └── redeploy.sh                      # 服务器自动更新
├── src/
│   ├── server.py                        # FastAPI 入口 /health / 静态托管
│   ├── api.py                           # 管理端 REST
│   ├── gateway.py                       # OpenAI 兼容反代
│   ├── accounts.py                      # 账号 CRUD / 选号 / 签到 / 探测
│   ├── scheduler.py                     # 后台调度线程
│   ├── upstream.py                      # httpx 客户端工具
│   ├── config.py / db.py / security.py  # 配置 / SQLite / 认证
│   └── static/                          # index.html + app.css + app.js
├── test/test_smoke.py               # 回归自测
└── Makefile
```

---

## 安全边界

- 只用于管理**本人及自愿授权**的账号；禁止批量注册、账号交易、接码、盗号、绕过平台风控或商业转售。
- 管理端持有全部账号凭据：端口只绑回环，公网一律走 HTTPS 反代 + 访问控制，首启随机密码登录后立即修改。
- 对外 API Key 只存 SHA-256 摘要，明文仅签发时显示一次；账号 Token 不通过管理 API 回显。
- `/docs` 与 `/openapi.json` 默认关闭。

## 许可

MIT，见 [LICENSE](LICENSE)。
