#!/usr/bin/env bash
# SGME 2.0 重建库切库脚本 —— **需用户明确批准后执行**
#
# 用法（环境变量必填，避免把真实地址写进仓库）：
#   NAS_HOST=LEO@<NAS_IP> \
#   LOCAL_HOME=/d/Projects/SGME/rebuild-20261031/home \
#   bash scripts/switch_to_rebuild_db.sh [--dry-run]
#
# 语义（对应 docs/plans/切库方案-SGME2.0重建上线-20261101.md 第五节）：
#   阶段1 冻结提炼 + WAL checkpoint + 双库改名备份
#   阶段2 上传重建库（memory.db / session.db 成对，保证溯源一致）
#   阶段3 起容器 + 健康校验
# 回滚见同目录 rollback_switch.sh。
#
# 红线：原件（raw/sessions）不动；备份只改名不删除；脚本中途失败即停（set -e）。
set -euo pipefail

NAS_HOST="${NAS_HOST:?必须提供 NAS_HOST，如 LEO@<NAS_IP>}"
LOCAL_HOME="${LOCAL_HOME:?必须提供 LOCAL_HOME（重建库 home 目录）}"
NAS_SGME_DIR="${NAS_SGME_DIR:?必须提供 NAS_SGME_DIR（NAS 上 sgme 部署目录，见 AI-INSTALL 部署材料）}"
NAS_DATA="$NAS_SGME_DIR/data/data"
CONTAINER="${CONTAINER:-sgme}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

STAMP="${STAMP:-$(date +%Y%m%d-%H%M%S)}"
BAK_MEM="memory.db.bak-rebuild-$STAMP"
BAK_SES="session.db.bak-rebuild-$STAMP"

say() { printf '\n=== %s ===\n' "$*"; }
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
  else
    eval "$@"
  fi
}

# ---------- 0. 前置校验 ----------
say "0. 前置校验"
[[ -f "$LOCAL_HOME/data/memory.db" ]] || { echo "本地重建库不存在：$LOCAL_HOME/data/memory.db"; exit 1; }
[[ -f "$LOCAL_HOME/data/session.db" ]] || { echo "本地 session.db 不存在：$LOCAL_HOME/data/session.db"; exit 1; }

python_bin="$(command -v python3 || command -v python || true)"
if [[ -n "$python_bin" ]]; then
  echo "重建库记忆数：$( "$python_bin" -c "
import sqlite3,sys
c=sqlite3.connect('file:$LOCAL_HOME/data/memory.db?mode=ro',uri=True)
print(c.execute('select count(*) from memories').fetchone()[0])" )"
fi

run "ssh \"$NAS_HOST\" 'docker inspect $CONTAINER --format \"{{.State.Status}}\"'"

# ---------- 1. 冻结 + checkpoint + 备份 ----------
say "1. 冻结提炼 + WAL checkpoint"
# 先停容器（最彻底的冻结：不做半开状态下的 checkpoint 竞态）
run "ssh \"$NAS_HOST\" 'docker stop $CONTAINER'"

say "1b. 备份改名（不删除，回滚锚点）"
run "ssh \"$NAS_HOST\" 'cd $NAS_DATA && cp -a memory.db $BAK_MEM && cp -a session.db $BAK_SES && ls -la $BAK_MEM $BAK_SES'"

say "1c. 备份完整性核对（sha256 + 行数）"
run "ssh \"$NAS_HOST\" 'cd $NAS_DATA && sha256sum $BAK_MEM $BAK_SES'"

# ---------- 2. 上传重建库 ----------
say "2. 上传重建库（成对换：memory.db + session.db）"
run "scp \"$LOCAL_HOME/data/memory.db\" \"$NAS_HOST:$NAS_DATA/memory.db\""
run "scp \"$LOCAL_HOME/data/session.db\" \"$NAS_HOST:$NAS_DATA/session.db\""

say "2b. 清掉旧 WAL/SHM（换库后旧 WAL 必须失效，否则会与新库对不上）"
run "ssh \"$NAS_HOST\" 'cd $NAS_DATA && rm -f memory.db-wal memory.db-shm session.db-wal session.db-shm'"

# ---------- 3. 起容器 + 校验 ----------
say "3. 启动容器"
run "ssh \"$NAS_HOST\" 'docker start $CONTAINER'"

say "3b. 健康校验（等待 20s 让服务就绪）"
if [[ $DRY_RUN -eq 0 ]]; then
  sleep 20
  ssh "$NAS_HOST" "curl -s -m 10 http://127.0.0.1:9910/v1/health" || true
  echo
  ssh "$NAS_HOST" "docker exec $CONTAINER python -c \"
import sqlite3
m=sqlite3.connect('/data/data/memory.db')
print('memories:', m.execute('select count(*) from memories').fetchone()[0])
print('vectors :', m.execute('select count(*) from memory_vectors').fetchone()[0])
s=sqlite3.connect('/data/data/session.db')
print('raw_files:', dict(s.execute(\\\"select status,count(*) from raw_files group by status\\\").fetchall()))
\"" || true
else
  echo "[dry-run] 跳过健康校验"
fi

cat <<EOF

=== 切库完成（本地库 → NAS 生产）===

备份（可回滚）：
  $NAS_DATA/$BAK_MEM
  $NAS_DATA/$BAK_SES

回滚：bash scripts/rollback_switch.sh（同 STAMP 传 STAMP=$STAMP）

切库后必做的三件事：
  1. 迁移人工纠错清单：python scripts/migrate_rejected_manual.py --apply
  2. 补拉窗口期原件（NAS raw/sessions 与重建库的差集）并提炼
  3. 确认 FTS 索引已建：memories_fts 行数应 ≈ 记忆数（首次搜索/启动时自建）
EOF
