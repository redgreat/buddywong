# buddywong

**WorkBuddy 自动签到获取 AI Token 平台 · 页面扫码登录 + 账号池管理 + OpenAI 兼容反代 + 可视化控制台**

参考 [workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) 的功能划分与腾讯签到协议，重新实现的一套**自包含、可独立运行**的轻量项目。单容器、单进程、一个 SQLite 文件就能跑，不依赖任何外部上游网关。

主线是**签到**：登录后台后，在「账号管理 → 扫码登录 WorkBuddy」页面直接授权账号，服务端拿到凭据后立刻签到、同步模型、查实时积分，之后每天自动签到。手工录入的 OpenAI 兼容账号仍可走通用 HTTP 签到钩子。

> **发布形态只有一种**：Linux 服务器上的 Docker 镜像。没有 Windows 安装器、没有一键安装脚本、没有 GitHub Release 更新器。
> 发布链路整套沿用 `git_mcp_server` 仓库：本地 `release.ps1` 打标签 → GitHub Action 打包 → 推送阿里云 ACR → 服务器 `redeploy.sh` 自动更新。

---

## 功能一览

### WorkBuddy 真实签到（主功能，页面直接登录）

- **页面扫码登录**：后台「账号管理 → 扫码登录 WorkBuddy」选国内版 / 国际版 → 服务端生成二维码（也可打开授权页用手机号 + 验证码）→ 微信 / QQ 扫码授权 → 自动落库，全程不用手抄 Token
- 对齐官方客户端协议：`/v2/plugin/auth/state` 发码、`/v2/plugin/auth/token` 换 `accessToken`、`/v2/plugin/login/account` 取 uid/昵称；出站 UA、Origin、`X-CodeBuddy-Request`、设备指纹、`X-User-Id` 全部按官方形态构造
- **真实每日签到**：`POST /v2/billing/meter/daily-checkin`。`code=0` 成功；`code=10001` 有两层语义 —— 「今天已签到」算幂等成功，「签到失败，请稍后重试」记为可重试失败（-3），不会把失败误记成成功
- 签到成功回查实时积分（`get-user-resource`，按套餐 `CycleCapacityRemain` 口径），页面余额即时更新
- `accessToken` 临近过期自动用 `refreshToken` 换新；也可点「刷新授权」手动刷新
- 国际版没有签到体系、企业账号没有个人成长体系 → 直接跳过，不发无效请求
- 登录后立即签到一次，之后每天到点自动签到；同一 uid 重新扫码 = 刷新凭据，不产生重复账号
- 扫码账号自动同步官方模型目录；反代转发时走 `/v2/chat/completions` + 官方身份头
- **客户端版本自动跟随官方发布**：UA 里的客户端版本与 CLI 版本不用写进配置，后台自动从官网更新接口和官方 npm 包取最新版本（`config.yml` 里没有这两项），官方升级后无需改配置、无需重新打包镜像

### 账号管理

- 账号 CRUD、分组、启停、权重（1–20）、积分/余额、到期日
- 手工录入的账号仍支持**通用 HTTP 签到**：可配签到地址 / 方法 / 请求头 / 请求体，每日到点自动执行
- 签到成功时从响应里自动提取 `credits / balance / points` 等字段回写余额
- 手动签到、批量签到、连通性探测（手工账号拉 `/v1/models`，WorkBuddy 账号拉官方模型目录）、状态分档
- 签到流水全量留痕，区分自动 / 手动 / 登录后 / 跳过四种触发方式

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
              定时线程：每日签到 / 官方版本同步 / 冷却恢复 / 日志清理
                                    │
              httpx ──> 手工账号：OpenAI 兼容端点（/v1）
                    └──> WorkBuddy 账号：腾讯官方端点（登录 /v2/plugin/*、
                                          签到与积分 /v2/billing/meter/*、对话 /v2/chat/*）
```

技术栈：**Python 3.13 + FastAPI + uvicorn + httpx + PyYAML + segno + SQLite**，前端为内置静态资源。运行时依赖只有 5 个 Python 包，二维码由纯 Python 的 segno 生成，不需要 Pillow / CDN / Node。

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

> 容器端口默认只绑 `127.0.0.1:3002`。管理端持有全部账号凭据，**不要直接暴露公网**。

### 登录后台（两层登录别搞混）

**第一层：管理员登录**（进后台，账密）：

浏览器打开 `http://<服务器>:3002`（只绑了 127.0.0.1 时走下面的 Nginx 反代），默认账号 `admin`，密码二选一：

```bash
# ① 没设 BW_ADMIN_PASSWORD：首启随机生成，只在容器日志里打印一次
docker compose logs buddywong | grep -A6 '初始管理员'

# ② 想用固定密码：在 docker-compose.yml 的 environment 里加
#    - BW_ADMIN_PASSWORD=你的强密码
#    然后 docker compose up -d（仅首次建库生效；已建库请到「设置 → 修改管理密码」改）
```

登录进去第一件事：到「设置 → 修改管理密码」改掉初始密码。会话 token 存浏览器 localStorage，退出登录即失效。

**第二层：WorkBuddy 账号登录**（在页面里扫码授权，拿到的是腾讯账号的 Token）：

后台 →「账号管理」→「扫码登录 WorkBuddy」→ 选版本 →「生成二维码」→ 微信/QQ 扫码或点授权链接用手机号登录 → 页面自动轮询，授权成功即纳管 + 同步模型 + 立即签到一次。这一步**不需要**登录服务器、不需要手工粘贴 Token，全部在页面完成。

> 如果二维码一直「等待扫码」：确认容器能访问 `copilot.tencent.com` / `www.codebuddy.cn`（必要时配 `BW_HTTP_PROXY`）；授权页支持手机号 + 短信验证码，慢一点没关系，二维码有效期 15 分钟。

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
| `BW_WB_CHAT_BASE` | `https://copilot.tencent.com` | 国内版登录 / 对话 / 模型目录基址 |
| `BW_WB_BILLING_BASE` | `https://www.codebuddy.cn` | 国内版签到 / 积分基址 |
| `BW_WB_GLOBAL_BASE` | `https://www.workbuddy.ai` | 国际版基址（无签到体系） |
| `BW_WB_VERSION_REFRESH_HOURS` | `6` | 官方客户端版本自动同步间隔（小时） |
| `BW_WB_CLIENT_VERSION` / `BW_WB_CLI_VERSION` | **留空（自动）** | 仅排查问题时用来临时钉死版本，填了该项就不再自动更新 |
| `BW_WB_AUTO_CHECKIN` | `1` | 扫码成功后是否立刻签到 |
| `BW_WB_STATE_TTL` | `900` | 二维码 / state 有效期（秒） |

WorkBuddy 端点一般不用改；域名调整时用上面这些环境变量覆盖，不必改代码。对应 `config.yml` 的 `workbuddy:` 段（见 `config/config.yml.example`）。扫码登录、签到、积分查询、版本同步都走 `BW_HTTP_PROXY` 出站代理，服务器访问不了腾讯域名时先配它。

### 官方客户端版本自动跟随（无需配置、无需重新发版）

签到请求要带得像官方客户端，UA 里有两个版本段：`WorkBuddy/<客户端版本>` 和 `CLI/<CLI 版本>`。
**这两项没有写死在 `config.yml` 里**，由 `src/versions.py` 自动从官方发布源取：

| 版本 | 官方来源 | 取值 |
|---|---|---|
| 国内版客户端 | `GET {cn_billing_base}/v2/update?platform=workbuddy-win32-x64-user`（官网下载页自己调的接口） | `version` 前三段，如 `5.9.3.42398134` → `5.9.3` |
| 国际版客户端 | 同上，走 `global_base` | 如 `5.9.4.42593613` → `5.9.4` |
| CLI | `GET https://registry.npmjs.org/@tencent-ai%2Fcodebuddy-code/latest`（备用：npmmirror 国内镜像） | `version`，如 `2.181.0` |

- 完整四段版本另放在 `X-IDE-Version` 头里；「设置 → 官方客户端版本」能看到当前生效值、来源、上次同步时间和各探测源结果，还能点「立即同步」。
- 启动后第一个调度 tick（约 20 秒内）同步一次，之后每 `BW_WB_VERSION_REFRESH_HOURS` 小时自动跟随。CDN / npm 不通时自动试备用域名。
- **降级链**：手动钉死 > 上次成功值（写入 `data/wb_versions.json`，容器重建不丢）> 内置默认（`5.5.4` / `2.137.1`）。探测失败只记日志、沿用旧值，**绝不阻断签到**，也不会倒退回旧版本。
- 版本号经过严格格式校验，脏数据不会进 HTTP 头。真遇到官方改版需要锁版本时，用 `BW_WB_CLIENT_VERSION=5.9.3` / `BW_WB_CLI_VERSION=2.181.0` 临时钉死即可，不用改代码。

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
`/api/login`、`/api/overview`、`/api/stats`、`/api/accounts`、`/api/keys`、`/api/logs`、`/api/checkin-logs`、`/api/settings`、`/api/password`、`/api/wb/versions`。

页面扫码登录对应的三个接口（都要求管理员会话）：

| 接口 | 作用 |
|---|---|
| `POST /api/wb/auth/start` | `{"realm":"cn"}` → `state` / `auth_url` / `qr`（PNG data URI）/ 有效期 |
| `GET /api/wb/auth/poll?state=...&realm=cn` | 每 2 秒轮询 → `waiting` / `ready` / `expired` / `invalid` / `realm_mismatch` |
| `POST /api/wb/auth/finish` | `{"state":"...","auto_checkin":true}` → 落库、同步模型、立即签到 |
| `POST /api/wb/accounts/{id}/refresh` | 刷新 accessToken 并回查实时积分 |
| `GET /api/wb/versions` | 当前生效的官方客户端版本、来源、上次/下次同步时间 |
| `POST /api/wb/versions/refresh` | 立即向官方发布源重新同步一次版本 |

> `poll` 的响应**不含任何 Token**；`accessToken` / `refreshToken` 只在服务端内存 state 缓存和 SQLite 里，落库成功后 state 立即销毁。

### 签到结果口径

| `code` | 含义 | 记入流水 |
|---|---|---|
| `0` | 签到成功 | 成功 |
| `10001` + 「今天已签到」 | 幂等成功 | 成功 |
| `10001` + 「签到失败，请稍后重试」 | 瞬时限流，可重试 | 失败（`-3`） |
| `-2` | 国际版 / 企业账号无签到体系 | 跳过，不发请求 |
| `-1` | 网络异常 | 失败 |
| 其它业务码 | 原样返回 | 失败 |

---

## 数据与备份

| 路径（宿主机） | 内容 | 是否必须备份 |
|---|---|---|
| `./data/buddywong.db` | 账号、密钥摘要、调用日志、签到流水、设置 | **是** |
| `./data/master_key` | 会话签名密钥（自动生成时才有） | **是** |
| `./data/wb_versions.json` | 上次成功探测到的官方客户端版本（丢失会回退内置默认） | 否 |
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
│   ├── accounts.py                      # 账号 CRUD / 选号 / 两种签到 / 探测
│   ├── workbuddy.py                     # 腾讯扫码登录 / 签到 / 积分 / 刷新 / 二维码
│   ├── versions.py                      # 官方客户端版本自动跟随（官网 /v2/update + npm）
│   ├── scheduler.py                     # 后台调度线程
│   ├── upstream.py                      # httpx 客户端工具
│   ├── config.py / db.py / security.py  # 配置 / SQLite（含旧库迁移）/ 认证
│   └── static/                          # index.html + app.css + app.js
├── test/test_smoke.py                   # 基础回归自测
├── test/test_workbuddy.py              # 扫码登录 / 签到协议回归自测
├── test/test_versions.py              # 官方版本自动跟随 / 降级 / 钉死回归自测
└── Makefile
```

---

## 安全边界

- 只用于管理**本人及自愿授权**的账号；禁止批量注册、账号交易、接码、盗号、绕过平台风控或商业转售。
- 管理端持有全部账号凭据：端口只绑回环，公网一律走 HTTPS 反代 + 访问控制，首启随机密码登录后立即修改。
- 对外 API Key 只存 SHA-256 摘要，明文仅签发时显示一次；账号 Token 不通过管理 API 回显。
- 扫码登录的 `accessToken` / `refreshToken` 只在服务端内存 state 缓存（15 分钟 TTL）与 SQLite 里，`/api/wb/auth/poll` 不下发任何凭据；管理端全部接口要求会话鉴权。
- 旧库升级由 `db._migrate()` 自动补列，不删数据；升级前仍建议备份 `data/buddywong.db`。
- 只纳管本人及自愿授权的账号，不要用于批量注册、账号交易或绕过平台风控。
- `/docs` 与 `/openapi.json` 默认关闭。

## 许可

MIT，见 [LICENSE](LICENSE)。
