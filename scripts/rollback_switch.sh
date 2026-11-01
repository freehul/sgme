#!/usr/bin/env bash
# 切库回滚 —— 把 .bak-rebuild-* 备份改名回正式库
#
# 用法：
#   NAS_HOST=LEO@<NAS_IP> STAMP=<切库时的时间戳> bash scripts/rollback_switch.sh [--dry-run]
#
# 语义：停容器 → 备份改名回原名 → 清 WAL/SHM → 起容器。
# 只要切库时用的备份还在，本脚本是安全的（备份不被删除，可反复回滚）。
set -euo pipefail

NAS_HOST="${NAS_HOST:?必须提供 NAS_HOST}"
NAS_SGME_DIR="${NAS_SGME_DIR:?必须提供 NAS_SGME_DIR（NAS 上 sgme 部署目录，见 AI-INSTALL 部署材料）}"
NAS_DATA="$NAS_SGME_DIR/data/data"
CONTAINER="${CONTAINER:-sgme}"
STAMP="${STAMP:?必须提供切库时的时间戳 STAMP（决定备份文件名）}"

BAK_MEM="memory.db.bak-rebuild-$STAMP"
BAK_SES="session.db.bak-rebuild-$STAMP"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

run() {
  if [[ $DRY_RUN -eq 1 ]]; then printf '[dry-run] %s\n' "$*"; else eval "$@"; fi
}

echo "=== 校验备份存在 ==="
run "ssh \"$NAS_HOST\" 'ls -la $NAS_DATA/$BAK_MEM $NAS_DATA/$BAK_SES'"

echo "=== 停容器 ==="
run "ssh \"$NAS_HOST\" 'docker stop $CONTAINER'"

echo "=== 当前库改名留存（不删，便于事后取证据）==="
run "ssh \"$NAS_HOST\" 'cd $NAS_DATA && [ -f memory.db ] && mv memory.db memory.db.switched-\$(date +%Y%m%d-%H%M%S) || true'"
run "ssh \"$NAS_HOST\" 'cd $NAS_DATA && [ -f session.db ] && mv session.db session.db.switched-\$(date +%Y%m%d-%H%M%S) || true'"

echo "=== 备份改名回正式库 ==="
run "ssh \"$NAS_HOST\" 'cd $NAS_DATA && mv $BAK_MEM memory.db && mv $BAK_SES session.db'"

echo "=== 清 WAL/SHM ==="
run "ssh \"$NAS_HOST\" 'cd $NAS_DATA && rm -f memory.db-wal memory.db-shm session.db-wal session.db-shm'"

echo "=== 起容器 ==="
run "ssh \"$NAS_HOST\" 'docker start $CONTAINER'"

if [[ $DRY_RUN -eq 0 ]]; then
  sleep 15
  ssh "$NAS_HOST" "curl -s -m 10 http://127.0.0.1:9910/v1/health" || true
  echo
fi

echo "=== 回滚完成：生产已回到 $STAMP 之前的库 ==="
