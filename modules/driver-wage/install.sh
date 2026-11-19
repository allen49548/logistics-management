#!/usr/bin/env bash
# 安装为 WorkBuddy 用户级技能。真实配置 config/local/ 会一并复制（仅本地，不进 git）。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="${HOME}/.workbuddy/skills/driver-wage"

mkdir -p "$DST"
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude '.git/' \
    --exclude 'dist/' \
    --exclude 'out/' \
    --exclude 'state/' \
    --exclude 'examples/out/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.DS_Store' \
    "$SRC/" "$DST/"
else
  cp -R "$SRC/." "$DST/"
fi

echo "已安装到: $DST"
echo "配置目录: $DST/config/local/（真实数据，仅本机）"
echo ""
echo "验证:     python3 \"$DST/scripts/payroll.py\" --profile example demo --month 2026-09"
echo "注意:     在工作区修改 config/local/ 后需重跑 make install 同步到技能副本；"
echo "          也可用 --profile <目录绝对路径> 直接指向工作区配置。"
