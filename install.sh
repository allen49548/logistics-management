#!/usr/bin/env bash
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
# install.sh 位于工具包根目录；包装脚本位于 bin/，两者基准不同。
ROOT="$(cd -P "$(dirname "$SOURCE")" && pwd)"
BIN_DIR="${LOGISTICS_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"

link_tool() {
  local source="$1"
  local name="$2"
  local target="$BIN_DIR/$name"
  if [ -e "$target" ] || [ -L "$target" ]; then
    local current=""
    current="$(readlink "$target" 2>/dev/null || true)"
    if [ "$current" != "$source" ]; then
      echo "[skip] $target 已存在，未覆盖"
      return 0
    fi
  fi
  ln -sfn "$source" "$target"
  echo "[ok] $target -> $source"
}

link_tool "$ROOT/bin/logistics" "logistics"
link_tool "$ROOT/bin/logistics-agent" "logistics-agent"

echo
echo "安装完成。若 $BIN_DIR 不在 PATH，请添加："
echo "  export PATH=\"$BIN_DIR:\$PATH\""
echo "验证："
echo "  logistics doctor"
echo "  logistics-agent list | head"
