#!/bin/bash
# =============================================================================
# SGME 主机侧自动更新代理（ST-34 T-94）
#
# 职责：WebUI「立即更新」确认后，自动完成 SGME 容器更新。
#   1. 轮询意图文件 $SGME_DATA/update/request.json（WebUI 经 POST /v1/admin/update/request 写入）
#   2. 检测到 status=pending → 执行 runbook 16.4 链：
#      git pull → docker build 新镜像 → 备份 compose → 换 tag → compose up -d → 健康验证
#   3. 成功 → status=done + 清空请求；失败 → 回滚旧镜像 + status=failed + 写失败原因
#
# 部署：NAS 主机 cron 每 5 分钟执行（与 nas_watchdog.sh 同模式）。
#   ⚠️ 必须以「src 仓库属主」(LEO) 身份运行，不可用 root——否则 git pull 撞
#      `fatal: detected dubious ownership`，且 root 写入会导致 src 属主漂移。
#   */5 * * * * LEO /vol1/1000/Docker/sgme/scripts/sgme-host-updater.sh >> /vol1/1000/Docker/sgme/logs/updater.log 2>&1
#   前置：data/update 目录须属主为 LEO（容器 root 写 request.json 后需 chown 回去），
#        否则 mark_failed 重写失败状态 / 成功时 rm 会权限拒绝。
#
# 依赖：
#   - NAS 主机：git / docker / docker compose
#   - src 目录 /vol1/1000/Docker/sgme/src（git clone，origin=NAS 本地 bare 仓）
#   - 数据目录 /vol1/1000/Docker/sgme/data（SGME_HOME=/data 挂载点）
#
# 安全设计：
#   - 容器保持无特权（不挂 docker.sock）；更新由主机侧脚本执行
#   - 意图文件校验 target_version 格式（^v?[0-9.]+[a-z0-9]*$），防注入
#   - 锁文件防止并发执行（上一次未完成则跳过本轮）
#   - 失败自动回滚旧镜像 tag（原件不删：旧镜像保留，仅回滚 compose 引用）
# =============================================================================
set -u

# ---- 可配置路径（与部署 runbook 对齐）----
SRC=/vol1/1000/Docker/sgme/src
COMPOSE_DIR=/vol1/1000/Docker/sgme
DATA_DIR=/vol1/1000/Docker/sgme/data
LOG=/vol1/1000/Docker/sgme/logs/updater.log
LOCK=/vol1/1000/Docker/sgme/logs/updater.lock
REQUEST_FILE="$DATA_DIR/update/request.json"
UPSTREAM=origin
BRANCH=main

TS() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(TS) $*" >> "$LOG"; }

# ---- 意图文件读取（JSON 解析：无 jq 依赖的极简提取）----
get_json_field() { # $1=file $2=field
  # 用 grep 提取 "field": "value"（不依赖 jq，NAS 可能未装）
  sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" 2>/dev/null | head -1
}

# ---- 锁：已有锁则跳过（防止并发）----
if [ -f "$LOCK" ]; then
  # 锁超过 30 分钟视为陈旧，允许抢占
  if [ -n "$(find "$LOCK" -mmin +30 2>/dev/null)" ]; then
    log "锁文件陈旧（>30min），清除后继续"
    rm -f "$LOCK"
  else
    exit 0
  fi
fi

# ---- 无请求则退出 ----
if [ ! -f "$REQUEST_FILE" ]; then
  exit 0
fi
TARGET_VERSION=$(get_json_field "$REQUEST_FILE" target_version)
STATUS=$(get_json_field "$REQUEST_FILE" status)
REQUESTED_AT=$(get_json_field "$REQUEST_FILE" requested_at)
if [ "$STATUS" != "pending" ]; then
  exit 0
fi
if [ -z "$TARGET_VERSION" ]; then
  log "意图文件缺 target_version，忽略"
  exit 0
fi
# 版本号格式校验（防注入：仅允许 v?数字.数字.数字[字母数字]）
if ! echo "$TARGET_VERSION" | grep -qE '^v?[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9]*$'; then
  log "target_version 格式非法: $TARGET_VERSION，忽略"
  exit 0
fi

# 已是最新（当前镜像 tag == 目标版本）则直接完成
# ⚠️ 重要副作用（2026-09-03 实证）：版本号是唯一的更新触发器。
#    **同一版本号内的 hotfix 提交不会触发任何重建** —— 镜像 tag 相同即短路退出，
#    git pull / docker build 完全不执行，热修代码不会生效。
#    ⇒ 发布 hotfix 必须 bump patch 版本号（1.1.3 → 1.1.4），否则更新请求会被静默"完成"。
CURRENT_IMAGE=$(docker ps --format '{{.Image}}' --filter name=^/sgme$ 2>/dev/null | head -1)
TARGET_NO_V="${TARGET_VERSION#v}"
if echo "$CURRENT_IMAGE" | grep -qE ":${TARGET_NO_V}(-|:|$)" ; then
  log "当前已是 ${TARGET_VERSION}（$CURRENT_IMAGE），标记完成"
  rm -f "$REQUEST_FILE"
  exit 0
fi

# ============ 开始执行更新 ============
touch "$LOCK"
log "=== 开始自动更新 → ${TARGET_VERSION}（当前镜像 $CURRENT_IMAGE）==="
OLD_IMAGE="$CURRENT_IMAGE"
NEW_TAG="${TARGET_NO_V}-nas-autoupd"
NEW_IMAGE="sgme:${NEW_TAG}"

mark_failed() { # $1=原因
  log "更新失败: $1"
  # 回滚：compose 换回旧镜像 + 重启
  if [ -n "$OLD_IMAGE" ] && [ "$OLD_IMAGE" != "<none>" ]; then
    cp "$COMPOSE_DIR/docker-compose.yml" "$COMPOSE_DIR/docker-compose.yml.bak-rollback-$(date +%Y%m%d%H%M%S)"
    sed -i "s|image:.*sgme:.*|image: $OLD_IMAGE|" "$COMPOSE_DIR/docker-compose.yml"
    (cd "$COMPOSE_DIR" && docker compose up -d) >> "$LOG" 2>&1
    log "已回滚到旧镜像 $OLD_IMAGE"
  fi
  # 写失败状态（保留请求文件，标记 failed + 原因）
  # 先 rm 再写：request.json 可能由容器 root 创建（root-owned 644），
  # 非 root 身份（LEO）无法直接 truncate，rm 后重建即可由当前用户持有。
  rm -f "$REQUEST_FILE"
  cat > "$REQUEST_FILE" <<EOF
{"target_version": "$TARGET_VERSION", "requested_at": "$REQUESTED_AT", "status": "failed", "error": "$1"}
EOF
  rm -f "$LOCK"
  exit 1
}

# 1. git pull（src 落后可能不止本次）
# ⚠️ 必须带命令行级 -c safe.directory（protected config，优先级最高且必生效）：
#    仓库级 / 全局配置在 cron 最小环境下可能读不到，会复现
#    `fatal: detected dubious ownership`（2026-09-03 1.1.4 首次更新失败实证）
# ⚠️ 2026-09-04 复发（新形态，1.1.5 首次更新失败）：报错对象是**裸仓**而非 src。
#    src 已在白名单，但 pull 经本地路径远端拉起 upload-pack 子进程打开裸仓时仍被拒
#    （fatal: detected dubious ownership in repository at '/vol1/1000/git/sgme.git'）。
#    ⇒ 远端为本地路径（绝对路径或 file://）时，必须把裸仓一并加入白名单。
#    -c 通过 GIT_CONFIG_PARAMETERS 传递给子进程，故对 upload-pack 同样生效。
UPSTREAM_URL=$(cd "$SRC" && git remote get-url "$UPSTREAM" 2>/dev/null)
SAFE_ARGS=(-c "safe.directory=$SRC")
case "$UPSTREAM_URL" in
  /*)       SAFE_ARGS+=(-c "safe.directory=$UPSTREAM_URL") ;;
  file://*) SAFE_ARGS+=(-c "safe.directory=${UPSTREAM_URL#file://}") ;;
esac
log "git pull ($SRC) upstream=$UPSTREAM_URL"
PULL_OK=0
for attempt in 1 2; do
  if (cd "$SRC" && git "${SAFE_ARGS[@]}" pull "$UPSTREAM" "$BRANCH" >> "$LOG" 2>&1); then
    PULL_OK=1
    break
  fi
  log "git pull 第 $attempt 次失败，10s 后重试（推送后瞬时属主态多可自愈）"
  sleep 10
done
if [ "$PULL_OK" -ne 1 ]; then
  mark_failed "git pull 失败（已重试 1 次）"
fi

# 2. docker build 新镜像（后台可能 3-5 分钟，重试一次网络抖动）
log "docker build $NEW_IMAGE（$SRC）"
if ! (cd "$SRC" && docker build -t "$NEW_IMAGE" . >> "$LOG" 2>&1); then
  log "首次 build 失败（可能网络抖动），重试一次"
  if ! (cd "$SRC" && docker build -t "$NEW_IMAGE" . >> "$LOG" 2>&1); then
    mark_failed "docker build 失败"
  fi
fi

# 3. 备份 compose + 换 tag
cp "$COMPOSE_DIR/docker-compose.yml" "$COMPOSE_DIR/docker-compose.yml.bak-autoupd-$(date +%Y%m%d%H%M%S)"
if ! (cd "$COMPOSE_DIR" && sed -i "s|image:.*sgme:.*|image: $NEW_IMAGE|" docker-compose.yml); then
  mark_failed "compose 换 tag 失败"
fi
grep 'image:' "$COMPOSE_DIR/docker-compose.yml" >> "$LOG" 2>&1

# 4. compose up -d
log "docker compose up -d"
if ! (cd "$COMPOSE_DIR" && docker compose up -d >> "$LOG" 2>&1); then
  mark_failed "compose up 失败"
fi

# 5. 健康验证（最多等 60s，每 5s 一次）
HEALTHY=0
for i in $(seq 1 12); do
  sleep 5
  ST=$(curl -s -m 5 http://127.0.0.1:9910/v1/health 2>/dev/null | sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  if [ "$ST" = "ok" ]; then
    HEALTHY=1
    break
  fi
done
if [ "$HEALTHY" != "1" ]; then
  mark_failed "健康验证失败（服务未就绪）"
fi

# 5.1 版本一致性校验（防止 tag 与代码不符的假更新）
RUNNING_VERSION=$(curl -s -m 5 http://127.0.0.1:9910/v1/health 2>/dev/null | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
if [ "$RUNNING_VERSION" != "$TARGET_NO_V" ]; then
  mark_failed "版本不一致（运行 $RUNNING_VERSION ≠ 目标 $TARGET_NO_V）"
fi
log "版本确认：$RUNNING_VERSION"

# 6. 成功：标记 done + 清请求
log "=== 更新成功 → ${TARGET_VERSION} ==="
rm -f "$REQUEST_FILE"
rm -f "$LOCK"
exit 0
