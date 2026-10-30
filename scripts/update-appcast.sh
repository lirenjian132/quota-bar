#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Windows 的 python3 可能是商店占位 stub(命令存在但一跑就退),
# 所以逐个候选验证"真能执行",不能只看 command -v
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in python python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then
      PYTHON="$cand"
      break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "error: no usable python found (set \$PYTHON to override)" >&2
  exit 1
fi
exec "$PYTHON" "$ROOT/scripts/update_appcast.py" "$@"
