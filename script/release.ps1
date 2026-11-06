#!/usr/bin/env pwsh
#
# buddywong 本地发布脚本（Windows / PowerShell 7）
# 打 v* 标签并推送到 origin，触发 .github/workflows/build-deploy.yml
# → GitHub Action 构建 Linux Docker 镜像 → 推送阿里云 ACR。
# 与 git_mcp_server 仓库的 script/release.ps1 为同一套逻辑。

param(
  [Parameter(Position=0)]
  [string]$Version = '',
  [Alias('h')]
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Show-Usage {
  @(
    '用法: .\script\release.ps1 [版本标签]'
    '示例: .\script\release.ps1 v0.0.1'
    ''
    '参数:'
    '  -Version <string>  Git 标签版本 (留空则按最新标签自动 +1)'
    '  -Help              显示帮助'
  ) | ForEach-Object { Write-Host $_ }
}

if ($Help) { Show-Usage; exit 0 }

# 彩色日志函数
function Write-Info($msg)    { Write-Host "➤ $msg" -ForegroundColor Blue }
function Write-Success($msg) { Write-Host "✔ $msg" -ForegroundColor Green }
function Write-Warn($msg)    { Write-Host "⚠ $msg" -ForegroundColor Yellow }
function Write-ErrorMsg($msg){ Write-Host "✖ $msg" -ForegroundColor Red }

function Banner {
  Write-Host ('=' * 46) -ForegroundColor Blue
  Write-Host "buddywong Docker 镜像发布" -ForegroundColor Blue
  Write-Host "版本: $Version" -ForegroundColor Blue
  Write-Host ('=' * 46) -ForegroundColor Blue
}

function Ensure-Git {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-ErrorMsg '未找到 git 命令，请先安装 Git 并确保在 PATH 中。'
    exit 1
  }
}

function Ensure-CleanWorkingTree {
  <#
  .SYNOPSIS
  检查工作区是否干净；如存在改动则要求人工确认后继续打标签与推送。
  #>
  try {
    $changes = git status --porcelain
    if ($changes) {
      Write-Warn "检测到未提交/已暂存/未跟踪的改动：`n$changes"
      $answer = Read-Host "仍要继续打标签并推送吗？输入 YES 继续，其它任意输入中止"
      if ($answer.Trim().ToUpperInvariant() -ne 'YES') {
        Write-ErrorMsg '已取消打标签与推送。'
        exit 1
      }
      Write-Warn '已确认忽略当前改动，将继续打标签与推送。'
    }
  } catch {
    Write-ErrorMsg "检查工作区状态失败: $($_.Exception.Message)"
    exit 1
  }
}

# 获取最新标签（按语义版本排序）
function Get-LatestTag {
  try {
    $tags = git tag --list 'v*' --sort=-version:refname
    if (-not $tags) { return $null }
    return ($tags | Select-Object -First 1).Trim()
  } catch {
    return $null
  }
}

# 将尾数 +1：优先识别 vMAJOR.MINOR.PATCH，否则对末尾数字增量
function Bump-Tail([string]$tag) {
  if ([string]::IsNullOrWhiteSpace($tag)) { return 'v0.0.1' }
  $semver = [regex]::Match($tag, '^v(\d+)\.(\d+)\.(\d+)$')
  if ($semver.Success) {
    $a = [int]$semver.Groups[1].Value
    $b = [int]$semver.Groups[2].Value
    $c = ([int]$semver.Groups[3].Value) + 1
    return "v$($a).$($b).$($c)"
  }
  $general = [regex]::Match($tag, '^(.*?)(\d+)$')
  if ($general.Success) {
    $prefix = $general.Groups[1].Value
    $n = ([int]$general.Groups[2].Value) + 1
    return "$prefix$n"
  }
  return "$tag-1"
}

try {
  Ensure-Git
  Ensure-CleanWorkingTree

  # 若未显式传入版本参数，则依据最新标签自动计算
  $VersionProvided = $PSBoundParameters.ContainsKey('Version') -and -not [string]::IsNullOrWhiteSpace($Version)
  if (-not $VersionProvided) {
    $latest = Get-LatestTag
    if ($latest) {
      Write-Info "检测到当前最新标签: $latest"
      $Version = Bump-Tail $latest
      Write-Info "自动计算版本: $Version"
    } else {
      Write-Warn '未发现任何标签，使用默认 v0.0.1'
      $Version = 'v0.0.1'
    }
  }

  Banner

  Write-Info "创建 Git 标签 $Version"
  $existing = git tag -l $Version | Where-Object { $_ -eq $Version }
  if (-not $existing) {
    $branch = (git branch --show-current).Trim()
    git tag $Version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git tag 创建失败（退出码 $LASTEXITCODE）" }
    Write-Success "标签 $Version 创建成功（分支 $branch）"
  }
  else {
    Write-Warn "标签 $Version 已存在，跳过创建"
  }

  Write-Info "推送标签到远程 origin"
  git push origin $Version | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "git push 失败（退出码 $LASTEXITCODE）" }
  Write-Success "标签 $Version 推送完成"
  Write-Info 'GitHub Action 正在构建镜像并推送阿里云 ACR，完成后在服务器执行: bash script/redeploy.sh'
}
catch {
  Write-ErrorMsg "脚本执行失败: $($_.Exception.Message)"
  exit 1
}
