#!/bin/bash
#
# buddywong 服务器重新部署脚本（Linux）
# 与 git_mcp_server 仓库的 script/redeploy.sh 为同一套流程：
#   停旧容器 → 删旧容器 → 清本地旧镜像 → 清历史日志 → pull 最新镜像 → 后台启动 → 查状态
#
# 部署目录固定 /root/buddywong（compose 文件、config/、data/、logs/ 都在这里）。
# 可手动执行，也可挂 crontab 定时执行实现「自动更新」，例如每 10 分钟：
#   */10 * * * * /root/buddywong/script/redeploy.sh >> /root/buddywong/logs/redeploy.log 2>&1

# 进入指定的绝对路径目录
cd /root/buddywong || { echo "进入目录失败，中止执行"; exit 1; }

echo "=========================================="
echo "    开始重新部署 buddywong 容器环境"
echo "=========================================="

echo "➤ 1. 停止并移除旧容器: buddywong ..."
docker compose stop buddywong
docker compose rm -f buddywong

echo "➤ 2. 删除本地所有的 buddywong 镜像记录..."
# 注意：原模板里 `awk '{print $2}'` 取的是 TAG 而不是镜像 ID，
# 这里改用 --format 明确取 {{.ID}}，避免把 tag 字符串喂给 docker rmi。
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
  | grep 'buddywong' \
  | awk '{print $2}' \
  | sort -u \
  | xargs -r docker rmi -f

echo "➤ 3. 清理所有旧的日志数据..."
# 只删日志文件，不删目录（compose 的 bind mount 依赖目录存在）
if [ -d ./logs ]; then
  find ./logs -type f -name '*.log*' -delete 2>/dev/null || true
fi
echo "日志清理完成。"

echo "➤ 4. 重新拉取最新镜像并后台启动..."
if docker compose pull buddywong; then
  docker compose up -d buddywong
else
  echo "⚠ 拉取最新镜像失败，先用本地旧镜像恢复服务，避免服务长时间中断"
  docker compose up -d buddywong
  echo "容器已用旧镜像恢复；请检查 ACR 网络 / 凭据后重试 redeploy.sh"
  exit 1
fi

echo "等待 2 秒检查容器状态..."
sleep 2

echo "➤ 5. 当前 buddywong 容器运行状态:"
docker ps -a --filter "name=buddywong"

echo "=========================================="
echo "    容器已更新并重启完成！"
echo "=========================================="
