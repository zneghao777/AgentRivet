#!/usr/bin/env bash
# =============================================================================
# 本机生产部署脚本(基于 Docker Compose)
#   start   首次部署或更新:创建 .env.prod -> 拉取最新镜像 -> 启动
#   update  手动更新:拉取 GHCR 最新镜像并重建(Watchtower 也会自动做这件事)
#   status  查看运行状态
#   logs    跟踪后端日志
#   stop    停止全部服务
#   down    停止并删除容器(数据卷保留)
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"
ENV_FILE="$ROOT_DIR/deploy/.env.prod"

C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_RED="\033[31m"; C_RESET="\033[0m"
info() { echo -e "${C_GREEN}[INFO ]${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}[WARN ]${C_RESET} $*"; }
error() { echo -e "${C_RED}[ERROR]${C_RESET} $*"; }

ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$ROOT_DIR/deploy/.env.prod.example" "$ENV_FILE"
    warn "已从模板生成 deploy/.env.prod"
    warn "请先编辑该文件,填写 GHCR_OWNER、POSTGRES_PASSWORD、LLM_API_KEY 等配置后再运行!"
    exit 1
  fi
}

# --env-file: 让 compose 里的 ${VAR} 插值能读到 deploy/.env.prod
compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

case "${1:-start}" in
  start)
    ensure_env
    info "登录 GHCR(用于拉取镜像,按提示输入 GitHub 用户名和 Personal Access Token)..."
    docker login ghcr.io
    info "拉取最新镜像..."
    compose pull
    info "启动全部服务..."
    compose up -d
    info "部署完成。前端: http://localhost  后端: http://localhost:8000"
    ;;
  update)
    ensure_env
    info "拉取最新镜像..."
    compose pull backend frontend
    info "重建服务..."
    compose up -d backend frontend
    info "清理旧镜像..."
    docker image prune -f
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f --tail=100 backend
    ;;
  stop)
    compose stop
    ;;
  down)
    compose down
    info "已停止并移除容器(数据卷 pgdata 保留)"
    ;;
  *)
    echo "用法: $0 [start|update|status|logs|stop|down]"
    exit 1
    ;;
esac
