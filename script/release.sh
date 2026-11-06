#!/bin/bash
#
# buddywong 发布脚本（Linux / macOS，等价于 script/release.ps1）
# 打 v* 标签并推送到 origin，触发 GitHub Action 构建镜像并推送阿里云 ACR。

set -euo pipefail

VERSION=""
HELP=false

show_usage() {
  cat <<'EOF'
用法: ./script/release.sh [版本标签]
示例: ./script/release.sh v0.0.1

参数:
  版本标签          Git 标签版本 (留空则按最新标签自动 +1)
  -h, --help       显示帮助
EOF
}

# 解析参数
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      HELP=true
      shift
      ;;
    *)
      VERSION="$1"
      shift
      ;;
  esac
done

if $HELP; then
  show_usage
  exit 0
fi

# 彩色日志函数
info()    { echo -e "\033[34m➤ $*\033[0m"; }
success() { echo -e "\033[32m✔ $*\033[0m"; }
warn()    { echo -e "\033[33m⚠ $*\033[0m"; }
error()   { echo -e "\033[31m✖ $*\033[0m"; }

banner() {
  echo -e "\033[34m=============================================="
  echo "buddywong Docker 镜像发布"
  echo "版本: $VERSION"
  echo -e "==============================================\033[0m"
}

ensure_git() {
  if ! command -v git &>/dev/null; then
    error '未找到 git 命令，请先安装 Git 并确保在 PATH 中。'
    exit 1
  fi
}

ensure_clean_working_tree() {
  local changes
  changes=$(git status --porcelain 2>/dev/null) || {
    error "检查工作区状态失败"
    exit 1
  }
  if [[ -n "$changes" ]]; then
    warn "检测到未提交/已暂存/未跟踪的改动："
    echo "$changes"
    read -rp "仍要继续打标签并推送吗？输入 YES 继续，其它任意输入中止: " answer
    if [[ "${answer^^}" != "YES" ]]; then
      error '已取消打标签与推送。'
      exit 1
    fi
    warn '已确认忽略当前改动，将继续打标签与推送。'
  fi
}

get_latest_tag() {
  git tag --list 'v*' --sort=-version:refname 2>/dev/null | head -n1 | tr -d '[:space:]'
}

bump_tail() {
  local tag="$1"
  if [[ -z "$tag" ]]; then
    echo "v0.0.1"
    return
  fi
  if [[ "$tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    local a="${BASH_REMATCH[1]}"
    local b="${BASH_REMATCH[2]}"
    local c=$(( ${BASH_REMATCH[3]} + 1 ))
    echo "v${a}.${b}.${c}"
    return
  fi
  if [[ "$tag" =~ ^(.*?)([0-9]+)$ ]]; then
    local prefix="${BASH_REMATCH[1]}"
    local n=$(( ${BASH_REMATCH[2]} + 1 ))
    echo "${prefix}${n}"
    return
  fi
  echo "${tag}-1"
}

main() {
  ensure_git
  ensure_clean_working_tree

  if [[ -z "$VERSION" ]]; then
    latest=$(get_latest_tag)
    if [[ -n "$latest" ]]; then
      info "检测到当前最新标签: $latest"
      VERSION=$(bump_tail "$latest")
      info "自动计算版本: $VERSION"
    else
      warn '未发现任何标签，使用默认 v0.0.1'
      VERSION="v0.0.1"
    fi
  fi

  banner

  info "创建 Git 标签 $VERSION"
  if ! git tag -l "$VERSION" | grep -qx "$VERSION"; then
    branch=$(git branch --show-current | tr -d '[:space:]')
    git tag "$VERSION"
    success "标签 $VERSION 创建成功（分支 $branch）"
  else
    warn "标签 $VERSION 已存在，跳过创建"
  fi

  info "推送标签到远程 origin"
  git push origin "$VERSION"
  success "标签 $VERSION 推送完成"
  info 'GitHub Action 正在构建镜像并推送阿里云 ACR，完成后在服务器执行: bash script/redeploy.sh'
}

main
