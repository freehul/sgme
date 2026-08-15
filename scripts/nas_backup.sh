#!/bin/bash
# SGME 每日备份：data → 机械盘 /vol2，保留 7 份（LEO cron 每天 03:30）
SRC=/vol1/1000/Docker/sgme/data
DST_ROOT=/vol2/1000/sgme-backup
TS=$(date +%Y%m%d)
DST="$DST_ROOT/backup-$TS"
LOG=/vol1/1000/Docker/sgme/logs/backup.log

mkdir -p "$DST"
if rsync -a --delete "$SRC/" "$DST/"; then
  # 保留最近 7 份，删除更旧
  ls -1d "$DST_ROOT"/backup-* 2>/dev/null | sort | head -n -7 | xargs -r rm -rf
  SIZE=$(du -sh "$DST" | cut -f1)
  echo "$(date '+%Y-%m-%d %H:%M:%S') 备份完成 -> $DST ($SIZE)" >> "$LOG"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') 备份失败！rsync 退出码 $?" >> "$LOG"
fi
