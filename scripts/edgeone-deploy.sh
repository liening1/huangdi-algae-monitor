#!/bin/bash
# 部署黄棣镇蓝藻卫星监控系统静态站点到 EdgeOne Makers（腾讯云国内镜像）
#
# 前置：
#   1) 腾讯云账号已完成实名认证
#   2) EdgeOne Makers 控制台已创建「direct upload（直接上传）」类型项目，记下项目名称
#   3) 已生成 API Token（Makers / EdgeOne 控制台）
#
# 用法：
#   EDGEONE_TOKEN=xxxx EDGEONE_PROJECT=huangdi-algae-monitor \
#     bash scripts/edgeone-deploy.sh
#
# 说明：
#   - 自动拉取仓库最新 gh-pages（已构建好的站点），导出到临时目录后上传
#   - 使用 edgeone makers deploy（pages 子命令已弃用）
#   - 默认 area=global（国内/全球节点）、env=production
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN="${EDGEONE_TOKEN:?环境变量 EDGEONE_TOKEN 未设置（Makers API Token）}"
PROJECT="${EDGEONE_PROJECT:?环境变量 EDGEONE_PROJECT 未设置（Makers 项目名）}"

# 使用本机托管的 node / npx（绝对路径，避免依赖用户环境）
NODE_DIR="/Users/shiyusheng/.workbuddy/binaries/node/versions/22.22.2/bin"
export PATH="$NODE_DIR:$PATH"

DIST="$(mktemp -d)/dist"

echo "==> [$(date)] 拉取最新 gh-pages"
git -C "$REPO_DIR" fetch origin gh-pages --depth 1

echo "==> 导出站点到 $DIST"
mkdir -p "$DIST"
git -C "$REPO_DIR" archive FETCH_HEAD | tar -x -C "$DIST"

echo "==> 部署到 EdgeOne Makers（项目=$PROJECT, area=global）"
cd "$DIST"
npx --yes edgeone makers deploy . -n "$PROJECT" -t "$TOKEN" -e production -a global --json

echo "==> 完成。控制台查看访问域名后，填入 web/static/app.js 的 CN_MIRROR 并提交推送"
