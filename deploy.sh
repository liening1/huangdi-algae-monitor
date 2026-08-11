#!/usr/bin/env bash
# 一键部署：提交点位查询修复 + 飞书预警，推 main，并触发每日重建工作流。
# 用法：  bash deploy.sh
set -e

REPO="/Users/shiyusheng/Documents/黄棣镇蓝藻卫星监控系统"
cd "$REPO"

echo "==> 当前仓库状态"
git status --short

echo "==> 暂存改动文件（仅本次国内镜像相关；docs/ 与 scripts/ 被根 .gitignore 白名单排除，用 -f 强制纳入）"
git add \
  web/static/app.js \
  web/static/index.html \
  web/static/style.css \
  .github/workflows/mirror.yml
git add -f \
  docs/国内镜像部署指南.md \
  scripts/mirror.sh

echo "==> 提交"
git commit -m "feat: 国内镜像支持（前端自动探测切换 + Coding/Gitee 同步指南与脚本）"

echo "==> 推送 main"
git push origin main

echo "==> 尝试触发每日重建工作流（需 gh CLI 已登录；失败不影响推送）"
if command -v gh >/dev/null 2>&1; then
  gh workflow run daily.yml || echo "（gh 触发失败，请到网页手动 Run workflow）"
else
  echo "（未安装 gh，请到网页手动 Run workflow）"
fi

echo ""
echo "部署已推送。请等待 Actions 完成（约几分钟）："
echo "  https://github.com/liening1/huangdi-algae-monitor/actions"
echo "或手动触发：Actions -> 每日重建静态站 -> Run workflow"
echo ""
echo "==> 飞书密钥（仅需设置一次，二选一）："
echo "  命令行: gh secret set FEISHU_WEBHOOK --body \"https://open.feishu.cn/open-apis/bot/v2/hook/5c719342-213d-492e-a423-ba582ae887de\""
echo "  网页:   仓库 Settings -> Secrets and variables -> Actions -> New repository secret"
echo "          Name=FEISHU_WEBHOOK  Value=https://open.feishu.cn/open-apis/bot/v2/hook/5c719342-213d-492e-a423-ba582ae887de"
echo ""
echo "==> 国内镜像（可选，激活后每日自动同步）："
echo "  1) 在 app.js 顶部把 CN_MIRROR 改成你的 Coding/Gitee Pages 地址；"
echo "  2) 仓库 Settings -> Secrets 添加 CODING_TOKEN / CODING_REPO（或 GITEE_TOKEN / GITEE_REPO）；"
echo "  3) 详见 docs/国内镜像部署指南.md；mirror.yml 配置密钥后自动运行。"
