#!/bin/sh
set -eu

DATA_DIR="${DATA_DIR:-/data}"
INPUT_DIR="${INPUT_DIR:-/input}"
OUTPUT_DIR="${OUTPUT_DIR:-/output}"
PRELOAD_DIR="${ARGOS_PRELOAD_DIR:-/opt/argos-preload}"

mkdir -p "$DATA_DIR"/tmp "$DATA_DIR"/models "$DATA_DIR"/home "$OUTPUT_DIR"

if [ ! -d "$INPUT_DIR" ]; then
  echo "[WARN] 输入目录 $INPUT_DIR 不存在；请检查 compose 的卷映射" >&2
fi
if [ ! -w "$OUTPUT_DIR" ]; then
  echo "[ERROR] 输出目录 $OUTPUT_DIR 不可写；请检查权限" >&2
  exit 1
fi

mkdir -p "$PRELOAD_DIR" 2>/dev/null || true

echo "[INFO] LinguaFlow 启动：输入=$INPUT_DIR 输出=$OUTPUT_DIR 数据=$DATA_DIR"
exec python -m app
