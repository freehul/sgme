#!/bin/bash
# SGME NAS 看门狗（root cron 每 5 分钟）
# 1. docker.sock 缺失 → docker.service 挂了 → 拉起（含 containerd 依赖重试）
# 2. sgme 容器未运行 → 拉起（容器存在则 start，不存在则 compose up）
LOG=/vol1/1000/Docker/sgme/logs/watchdog.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

if [ ! -S /var/run/docker.sock ]; then
  echo "$TS docker.sock 缺失，尝试启动 docker.service" >> "$LOG"
  systemctl start docker.service || {
    echo "$TS docker.service 启动失败，重试 containerd" >> "$LOG"
    systemctl restart containerd
    sleep 5
    systemctl start docker.service
  }
  sleep 15
fi

if [ -S /var/run/docker.sock ]; then
  if ! docker ps --filter name=^/sgme$ --filter status=running -q | grep -q .; then
    if docker ps -a --filter name=^/sgme$ -q | grep -q .; then
      echo "$TS sgme 容器未运行，docker start" >> "$LOG"
      docker start sgme
    else
      echo "$TS sgme 容器不存在，compose up -d" >> "$LOG"
      cd /vol1/1000/Docker/sgme && docker compose up -d
    fi
  fi
fi
