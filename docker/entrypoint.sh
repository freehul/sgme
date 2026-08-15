#!/bin/sh
# SGME 容器入口（T-72，2026-08-16）
# 首次启动（空数据卷）把默认 sgme.yaml 物化到 $SGME_HOME/config/（含生产调优：
# l15 prescreen 开启 + fallback: skip_conflict），用户可编辑后重启生效。
# 程序资源（llm.yaml / providers.yaml）始终读镜像内 /app/config/，不受本脚本影响。
set -e

if [ -n "$SGME_HOME" ] && [ ! -f "$SGME_HOME/config/sgme.yaml" ]; then
  mkdir -p "$SGME_HOME/config"
  cp /app/config/sgme.yaml "$SGME_HOME/config/sgme.yaml"
  echo "[sgme-entrypoint] 首次启动：已物化默认 sgme.yaml -> $SGME_HOME/config/sgme.yaml（按需编辑后重启生效）"
fi

exec "$@"
