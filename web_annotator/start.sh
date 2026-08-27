#!/usr/bin/env bash

# 优先使用 PYTHON 环境变量；否则向上查找 .venv，回退系统 python。
set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python}"
for d in "$PWD" "$(dirname "$PWD")"; do
    [ -x "$d/.venv/bin/python" ] && PYTHON="$d/.venv/bin/python" && break
done
exec "$PYTHON" -m uvicorn server:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8765}" --reload
