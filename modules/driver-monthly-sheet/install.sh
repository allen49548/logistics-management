#!/usr/bin/env bash
# 安装为 WorkBuddy 用户级技能（默认仓库就在技能目录，此脚本用于新机器部署）。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="${HOME}/.workbuddy/skills/driver-monthly-sheet"

if [ "$SRC" = "$DST" ]; then
  echo "已在技能目录: $DST（无需复制）"
else
  mkdir -p "$DST"
  rsync -a --exclude '.git/' --exclude 'state/' --exclude '__pycache__/' \
        --exclude '*.pyc' --exclude '.DS_Store' "$SRC/" "$DST/" 2>/dev/null \
    || cp -R "$SRC/." "$DST/"
  echo "已安装到: $DST"
fi

echo ""
echo "验证: python3 \"$DST/scripts/driver_sheet.py\" selftest"
echo "配置: \"$DST/config/local/config.json\"（init 生成，真实数据仅本机）"
