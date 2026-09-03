#!/bin/sh
# SGME 容器入口（T-72，2026-08-16；T-142 资源内迁适配）
# 首次启动（空数据卷）把默认 llm.yaml / providers.yaml / sgme.yaml 物化到
# $SGME_HOME/config/（sgme.yaml 含生产调优：l15 prescreen 开启 + fallback: skip_conflict），
# 用户可编辑后重启生效。程序资源现位于镜像内 /app/sgme/resources/config/。
set -e

# T-142：程序资源已内迁至 /app/sgme/resources/config/（随包分发）。
# 首次启动（空数据卷）把 llm.yaml/providers.yaml/sgme.yaml 物化到 $SGME_HOME/config/，
# 已存在则保留用户改动（不覆盖）。
for f in llm.yaml providers.yaml sgme.yaml; do
  if [ -n "$SGME_HOME" ] && [ ! -f "$SGME_HOME/config/$f" ]; then
    mkdir -p "$SGME_HOME/config"
    cp "/app/sgme/resources/config/$f" "$SGME_HOME/config/$f"
    echo "[sgme-entrypoint] 首次启动：已物化默认 $f -> $SGME_HOME/config/$f（按需编辑后重启生效）"
  fi
done

# 首次启动物化内置角色（T-55 缺陷修复，2026-08-18）：
# ROLES_DIR = $SGME_HOME/roles（运行时读用户目录），镜像内置角色在 /app/roles。
# 空卷首次启动时若不复制，角色管理页将无内置模板（B63 容器化迁移曾踩坑）。
if [ -n "$SGME_HOME" ] && [ ! -d "$SGME_HOME/roles" ]; then
  if [ -d /app/roles ] && ls /app/roles/*.json >/dev/null 2>&1; then
    mkdir -p "$SGME_HOME/roles"
    cp /app/roles/*.json "$SGME_HOME/roles/"
    echo "[sgme-entrypoint] 首次启动：已物化内置角色 -> $SGME_HOME/roles/（butler/companion/friend/mentor）"
  fi
fi

exec "$@"
