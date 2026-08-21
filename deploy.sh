#!/usr/bin/env bash
# ============================================================================
# SGME 一键部署脚本（NAS / 单机）
# ST-12 Docker NAS 一键部署（2026-08-20 实施）
#
# 用法：
#   ./deploy.sh build          # 本地构建镜像
#   ./deploy.sh deploy <host>  # 构建→导出→scp 到 NAS→导入→启动→验证 全流程
#   ./deploy.sh up             # 仅启动（镜像已存在）
#   ./deploy.sh down           # 停止
#   ./deploy.sh logs           # 查看日志
#
# 前置条件：
#   - 本机：docker + docker compose
#   - NAS：docker 可用，ssh 可达
#   - 密钥：docker.env 已就绪（复制 .env.example 填入真实值）
# ============================================================================
set -euo pipefail

IMAGE_TAG="sgme:1.0.0b4-nas-upd2"
TARBALL="sgme-1.0.0b4-nas-upd2.tar"
COMPOSE="docker compose"

log()  { echo -e "\033[1;32m[SGME]\033[0m $*"; }
warn() { echo -e "\033[1;33m[SGME!]\033[0m $*"; }
die()  { echo -e "\033[1;31m[SGME!]\033[0m $*" >&2; exit 1; }

# 密钥文件检查
check_env() {
  [[ -f docker.env ]] || die "docker.env 不存在——先 cp .env.example docker.env 并填入真实密钥"
  grep -qE "^[A-Z_]+=.+" docker.env || die "docker.env 为空或格式不对"
}

build() {
  check_env
  log "构建镜像 $IMAGE_TAG ..."
  $COMPOSE build
  log "构建完成"
}

up() {
  check_env
  log "启动服务（9910 HTTP/WebUI + 9913 MCP）..."
  $COMPOSE up -d
  sleep 5
  verify
}

down() {
  log "停止服务..."
  $COMPOSE down
}

logs() {
  $COMPOSE logs -f --tail 100
}

verify() {
  local host="${1:-127.0.0.1}"
  log "验证健康检查 http://$host:9910/v1/health ..."
  local body
  body="$(curl -sf --max-time 10 "http://$host:9910/v1/health" || true)"
  if [[ "$body" == *'"status":"ok"'* ]]; then
    log "✅ SGME 健康：$body"
  else
    warn "健康检查未通过：${body:-（无响应）}——用 ./deploy.sh logs 排查"
    return 1
  fi
}

# deploy <host>：构建→导出→scp→NAS 导入→启动→验证
deploy() {
  local host="${1:?用法: ./deploy.sh deploy <host>（如 leo@192.168.10.10）}"
  build

  log "导出镜像 $TARBALL ..."
  docker save "$IMAGE_TAG" -o "$TARBALL"

  log "传输到 $host ..."
  ssh "$host" "mkdir -p /tmp/sgme-deploy"
  scp "$TARBALL" docker.env docker-compose.yml "$host:/tmp/sgme-deploy/"

  log "NAS 导入 + 启动 ..."
  ssh "$host" "docker load -i /tmp/sgme-deploy/$TARBALL"
  ssh "$host" "cd /tmp/sgme-deploy && cp -n docker.env docker-compose.yml /vol1/1000/Docker/sgme/ 2>/dev/null || true; sed -i 's|^\(\s*image:\s*\)sgme:.*|\1${IMAGE_TAG}|' /vol1/1000/Docker/sgme/docker-compose.yml; cd /vol1/1000/Docker/sgme && docker compose up -d"
  ssh "$host" "sleep 8; curl -sf --max-time 10 http://127.0.0.1:9910/v1/health || echo NAS健康检查未通过"

  log "✅ 部署完成。验证：curl http://$host:9910/v1/health"
  rm -f "$TARBALL"
}

case "${1:-}" in
  build)  build ;;
  up)     up ;;
  down)   down ;;
  logs)   logs ;;
  deploy) deploy "${2:-}" ;;
  verify) verify "${2:-127.0.0.1}" ;;
  *) echo "用法: $0 {build|up|down|logs|deploy <host>|verify [host]}"; exit 1 ;;
esac
