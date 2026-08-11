#!/usr/bin/env bash
# ============================================================
# 国内镜像同步脚本
# 把本仓库 gh-pages 分支（已构建好的完整静态站点）强推到
# Coding Pages / Gitee Pages 等国内可直连的静态托管。
#
# 用法示例：
#   CODING_TOKEN=xxxx CODING_REPO=e.coding.net/<team>/huangdi/huangdi.git \
#     bash scripts/mirror.sh
#   GITEE_TOKEN=xxxx GITEE_REPO=gitee.com/<user>/huangdi-algae-monitor.git \
#     bash scripts/mirror.sh
#
# 环境变量：
#   CODING_TOKEN / CODING_REPO   推送到 Coding（部署分支 coding-pages）
#   GITEE_TOKEN  / GITEE_REPO    推送到 Gitee（部署分支即推送分支，默认 master）
# 二者可同时配置，会依次推送。
# ============================================================
set -euo pipefail

REPO_URL="https://github.com/liening1/huangdi-algae-monitor.git"
WORK=$(mktemp -d)
BRANCH="${DEPLOY_BRANCH:-coding-pages}"   # Coding Pages 默认部署分支；Gitee 可改用 master
echo "==> 工作目录: $WORK"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "==> 克隆 gh-pages（已构建站点，含 outputs 数据）..."
git clone --depth 1 --branch gh-pages "$REPO_URL" "$WORK/site"
cd "$WORK/site"

# 重新初始化为干净静态根（去掉 github 的 .git，避免误推历史）
rm -rf .git
git init -q
git config user.name "mirror-bot"
git config user.email "mirror@local"
git add -A
if git diff --cached --quiet; then
  echo "==> 无变更，跳过提交"
else
  git commit -q -m "mirror: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

push_to() {
  local token="$1" repo="$2" label="$3"
  [ -z "$token" ] && { echo "==> 未配置 ${label} 令牌，跳过"; return; }
  [ -z "$repo" ] && { echo "==> 未配置 ${label} 仓库，跳过"; return; }
  echo "==> 推送到 ${label}: $repo (分支 $BRANCH)"
  # 形如 e.coding.net/team/proj/repo.git 或 gitee.com/user/repo.git
  local auth="${token}@${repo#https://}"
  git push -f "https://${auth}" "HEAD:${BRANCH}"
  echo "==> ${label} 推送完成"
}

push_to "${CODING_TOKEN:-}" "${CODING_REPO:-}" "Coding"
# Gitee 通常用 master 作为 Pages 分支
DEPLOY_BRANCH="${GITEE_BRANCH:-master}" push_to "${GITEE_TOKEN:-}" "${GITEE_REPO:-}" "Gitee"

echo "==> 镜像同步结束"
