#!/usr/bin/env bash
# =============================================================================
# Agent Harness 本地开发一键启动脚本
#   - Docker 仅启动 PostgreSQL / Redis / RabbitMQ 基础设施
#   - 自动生成 backend/.env（含 LLM_API_KEY，缺失时自动填充）
#   - 自动安装缺失依赖（venv / node_modules）
#   - 本机启动后端、前端、obs-worker、session-worker
#
# 用法:
#   ./start.sh         一键启动前后端
#   ./start.sh stop    停止已启动的前后端
#   ./start.sh status  查看运行状态
# =============================================================================

set -uo pipefail

# ============================ 基础配置 ============================
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
LOG_DIR="$ROOT_DIR/logs"
TS="$(date +%Y%m%d-%H%M%S)"
BACKEND_LOG="$LOG_DIR/backend-$TS.log"
FRONTEND_LOG="$LOG_DIR/frontend-$TS.log"
OBS_WORKER_LOG="$LOG_DIR/obs-worker-$TS.log"
SESSION_WORKER_LOG="$LOG_DIR/session-worker-$TS.log"
MAIN_LOG="$LOG_DIR/start-$TS.log"

COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.yml"
BACKEND_PID_FILE="$LOG_DIR/backend.pid"
FRONTEND_PID_FILE="$LOG_DIR/frontend.pid"
OBS_WORKER_PID_FILE="$LOG_DIR/obs-worker.pid"
SESSION_WORKER_PID_FILE="$LOG_DIR/session-worker.pid"
STOP_REQUEST_FILE="$LOG_DIR/stop.requested"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

# ============================ 数据库配置 ============================
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
PG_SUPERUSER="${PG_SUPERUSER:-$(whoami)}"   # 本地 pgsql 超管（brew 默认当前系统用户）
DB_NAME="${DB_NAME:-harness}"
DB_USER="${DB_USER:-harness}"
DB_PASSWORD="${DB_PASSWORD:-harness}"

# ============================ 消息队列 / Redis 配置 ============================
REDIS_PORT="${REDIS_PORT:-6380}"
RABBITMQ_USER="${RABBITMQ_USER:-harness}"
RABBITMQ_PASSWORD="${RABBITMQ_PASSWORD:-harness}"
RABBITMQ_PORT="${RABBITMQ_PORT:-5672}"
RABBITMQ_MANAGEMENT_PORT="${RABBITMQ_MANAGEMENT_PORT:-15672}"

# ============================ 模型配置 ============================
LLM_API_KEY="${LLM_API_KEY:-}"
LLM_MODEL="${LLM_MODEL:-deepseek-chat}"
LLM_BASE_URL="${LLM_BASE_URL:-https://api.deepseek.com}"

# ============================ 颜色 ============================
if [ -t 1 ]; then
  C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_RED="\033[31m"; C_CYAN="\033[36m"; C_RESET="\033[0m"
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""; C_RESET=""
fi

# ============================ 日志 ============================
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$MAIN_LOG"; }
info()  { log "${C_CYAN}[INFO ]${C_RESET} $*"; }
ok()    { log "${C_GREEN}[ OK  ]${C_RESET} $*"; }
warn()  { log "${C_YELLOW}[WARN ]${C_RESET} $*"; }
error() { log "${C_RED}[ERROR]${C_RESET} $*"; }

# 给子进程输出追加时间戳并同时写入日志文件与终端
run_ts() {
  # $1 = 日志文件, 其余 = 命令
  local logfile="$1"; shift
  "$@" 2>&1 | while IFS= read -r line; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$logfile"
  done
}

# ============================ 后端/前端进程 ============================
BACKEND_PID=""
FRONTEND_PID=""
OBS_WORKER_PID=""
SESSION_WORKER_PID=""

is_port_listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
port_pid() { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | tr '\n' ' '; }

# ============================ 1. 前置检查 ============================
check_prereqs() {
  info "===== 前置检查 ====="
  for cmd in psql python3 node npm docker curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      error "缺少命令: $cmd，请先安装后重试"
      exit 1
    fi
  done
  if ! docker info >/dev/null 2>&1; then
    error "Docker daemon 未运行，请先启动 Docker Desktop"
    exit 1
  fi
  ok "工具齐全: psql / python3 / node / npm / docker / curl"
  info "Python: $(python3 --version 2>&1) | Node: $(node --version 2>&1) | npm: $(npm --version 2>&1)"
}

# ============================ 2. Docker 基础设施 ============================
ensure_infrastructure() {
  info "===== 启动开发基础设施（仅 Docker）====="
  # 应用进程必须由本脚本在宿主机启动；只保留数据库、缓存和消息队列容器。
  docker compose -f "$COMPOSE_FILE" stop backend frontend obs-worker session-worker \
    >>"$MAIN_LOG" 2>&1 || true
  if ! docker compose -f "$COMPOSE_FILE" up -d --wait postgres redis rabbitmq \
    >>"$MAIN_LOG" 2>&1; then
    error "Docker 基础设施启动失败，请查看日志: $MAIN_LOG"
    return 1
  fi
  ok "PostgreSQL / Redis / RabbitMQ 已就绪"
  ok "RabbitMQ 管理台: http://127.0.0.1:$RABBITMQ_MANAGEMENT_PORT"
}

# ============================ 3. 数据库（幂等建库） ============================
detect_pg_conn() {
  # 探测可用的 psql 连接参数，结果存入 PG_CONN 数组
  if psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    PG_CONN=(-h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres)
    return 0
  fi
  if psql -U "$PG_SUPERUSER" -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    PG_CONN=(-U "$PG_SUPERUSER" -d postgres)   # socket 连接
    return 0
  fi
  return 1
}

ensure_postgres() {
  info "===== 本地 PostgreSQL 建库检查 ====="
  if ! command -v psql >/dev/null 2>&1; then
    error "未找到 psql，请确认 PostgreSQL 已安装并加入 PATH"
    return 1
  fi

  # Docker 开发库已经由 Compose 初始化；应用账号可用时无需超管建库流程。
  if PGPASSWORD="$DB_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" \
    -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1" >/dev/null 2>&1; then
    ok "应用账号可连接: $DB_USER@$PG_HOST:$PG_PORT/$DB_NAME"
    if PGPASSWORD="$DB_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" \
      -U "$DB_USER" -d "$DB_NAME" -tAc \
      "SELECT 1 FROM pg_extension WHERE extname='vector'" 2>/dev/null | grep -q 1; then
      ok "pgvector 扩展已启用"
      return 0
    fi
  fi

  PG_CONN=()
  if ! detect_pg_conn; then
    error "无法连接本地 PostgreSQL (host=$PG_HOST port=$PG_PORT user=$PG_SUPERUSER)"
    error "请确认服务已启动: brew services start postgresql@15 ，或设置 PG_SUPERUSER 环境变量"
    return 1
  fi
  ok "已连接 PostgreSQL，超管用户: $PG_SUPERUSER"

  # 创建角色（不存在时）
  if [ "$(psql "${PG_CONN[@]}" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'")" = "1" ]; then
    ok "数据库角色已存在: $DB_USER"
  else
    info "创建数据库角色: $DB_USER ..."
    psql "${PG_CONN[@]}" -c "CREATE ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASSWORD'" >>"$MAIN_LOG" 2>&1
    if [ $? -eq 0 ]; then ok "角色创建成功: $DB_USER"; else warn "角色创建失败（请检查权限）"; fi
  fi

  # 创建数据库（不存在时）
  if [ "$(psql "${PG_CONN[@]}" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")" = "1" ]; then
    ok "数据库已存在: $DB_NAME"
  else
    info "创建数据库: $DB_NAME (owner=$DB_USER) ..."
    psql "${PG_CONN[@]}" -c "CREATE DATABASE $DB_NAME OWNER $DB_USER" >>"$MAIN_LOG" 2>&1
    if [ $? -eq 0 ]; then ok "数据库创建成功: $DB_NAME"; else warn "数据库创建失败（请检查权限）"; fi
  fi

  # 验证应用账号可连接目标库
  if PGPASSWORD="$DB_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1" >/dev/null 2>&1; then
    ok "应用账号可连接: $DB_USER@$PG_HOST:$PG_PORT/$DB_NAME"
  else
    warn "应用账号连接验证失败，请检查 pg_hba.conf 认证方式"
  fi

  # RAG：启用 pgvector 扩展（需服务器已安装 pgvector；用超管账号创建）
  info "检查 pgvector 扩展..."
  if PGPASSWORD="$DB_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1 FROM pg_extension WHERE extname='vector'" 2>/dev/null | grep -q 1; then
    ok "pgvector 扩展已启用"
  else
    if psql "${PG_CONN[@]}" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector" >>"$MAIN_LOG" 2>&1; then
      ok "pgvector 扩展已启用"
    else
      warn "pgvector 扩展启用失败（请先安装 pgvector: brew install pgvector 或使用 pgvector/pgvector Docker 镜像）"
    fi
  fi
}

# ============================ 4. backend/.env（本地开发配置） ============================
set_env_value() {
  local env_file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$env_file"; then
    sed -i '' "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '\n%s=%s\n' "$key" "$value" >>"$env_file"
  fi
}

ensure_backend_env() {
  info "===== 检查 backend/.env ====="
  local env_file="$BACKEND_DIR/.env"

  if [ ! -f "$env_file" ]; then
    info "未找到 .env，从 .env.example 生成..."
    cp "$BACKEND_DIR/.env.example" "$env_file"
  fi

  # 幂等填充 LLM_API_KEY
  if [ -n "$LLM_API_KEY" ] && grep -q "^LLM_API_KEY=$LLM_API_KEY$" "$env_file"; then
    ok "LLM_API_KEY 已配置"
  elif [ -z "$LLM_API_KEY" ]; then
    if grep -Eq '^LLM_API_KEY=.+$' "$env_file"; then
      ok "backend/.env 中已有 LLM_API_KEY（保持不变）"
    else
      warn "未提供 LLM_API_KEY，请在 backend/.env 中配置后再调用模型"
    fi
  elif grep -q "^LLM_API_KEY=.*" "$env_file"; then
    warn "检测到已有 LLM_API_KEY（未覆盖），如需更换请手动编辑 $env_file"
  else
    # 变量值含 / 时用 | 作分隔符
    sed -i '' "s|^LLM_API_KEY=.*|LLM_API_KEY=$LLM_API_KEY|" "$env_file"
    ok "已写入 LLM_API_KEY"
  fi

  # 应用在宿主机运行，基础设施通过映射端口访问。
  set_env_value "$env_file" "DATABASE_URL" \
    "postgresql+psycopg://$DB_USER:$DB_PASSWORD@$PG_HOST:$PG_PORT/$DB_NAME"
  set_env_value "$env_file" "REDIS_URL" "redis://127.0.0.1:$REDIS_PORT/0"
  set_env_value "$env_file" "MQ_ENABLED" "true"
  set_env_value "$env_file" "RABBITMQ_URL" \
    "amqp://$RABBITMQ_USER:$RABBITMQ_PASSWORD@127.0.0.1:$RABBITMQ_PORT/"
  set_env_value "$env_file" "RABBITMQ_MANAGEMENT_URL" \
    "http://127.0.0.1:$RABBITMQ_MANAGEMENT_PORT"
  ok "本地 MQ 配置已启用，应用进程将连接宿主机映射端口"

  info "关键配置: LLM_MODEL=$LLM_MODEL | LLM_BASE_URL=$LLM_BASE_URL | DATABASE_URL=$(grep '^DATABASE_URL=' "$env_file" | cut -d= -f2)"
}

# ============================ 5. 依赖检查 ============================
ensure_dependencies() {
  info "===== 依赖检查 ====="
  # 后端
  if [ ! -x "$BACKEND_DIR/.venv/bin/python" ]; then
    warn "未找到 backend/.venv，创建虚拟环境..."
    (cd "$BACKEND_DIR" && python3 -m venv .venv) >>"$MAIN_LOG" 2>&1
  fi
  if ! "$BACKEND_DIR/.venv/bin/python" -c \
    "import aio_pika, fastapi, langgraph, redis, sqlalchemy, uvicorn" >/dev/null 2>&1; then
    info "安装后端依赖 (pip install -r requirements.txt)..."
    (cd "$BACKEND_DIR" && .venv/bin/pip install --quiet -r requirements.txt) 2>&1 | while IFS= read -r l; do echo "[$(date '+%Y-%m-%d %H:%M:%S')] [PIP ] $l" | tee -a "$MAIN_LOG"; done
  else
    ok "后端依赖已就绪"
  fi

  # 前端
  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    info "安装前端依赖 (npm install)..."
    (cd "$FRONTEND_DIR" && npm install) 2>&1 | while IFS= read -r l; do echo "[$(date '+%Y-%m-%d %H:%M:%S')] [NPM ] $l" | tee -a "$MAIN_LOG"; done
  else
    ok "前端依赖已就绪"
  fi
}

# ============================ 6. 本地进程管理 ============================
pid_file_running() {
  local pid_file="$1" pid
  [ -f "$pid_file" ] || return 1
  pid="$(tr -d '[:space:]' <"$pid_file")"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

stop_pid_file() {
  local label="$1" pid_file="$2" pid
  if ! pid_file_running "$pid_file"; then
    rm -f "$pid_file"
    return 0
  fi
  pid="$(tr -d '[:space:]' <"$pid_file")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$pid_file"
      ok "已停止$label (PID $pid)"
      return 0
    fi
    sleep 0.1
  done
  warn "$label未在 2 秒内退出 (PID $pid)"
}

stop_existing() {
  info "===== 停止旧的本地应用进程 ====="
  stop_pid_file "后端" "$BACKEND_PID_FILE"
  stop_pid_file "前端" "$FRONTEND_PID_FILE"
  stop_pid_file "观测 Worker" "$OBS_WORKER_PID_FILE"
  stop_pid_file "会话 Worker" "$SESSION_WORKER_PID_FILE"
  if is_port_listening "$BACKEND_PORT"; then
    warn "端口 $BACKEND_PORT 被占用 (PID: $(port_pid "$BACKEND_PORT"))，正在停止旧后端..."
    kill $(port_pid "$BACKEND_PORT") 2>/dev/null
    sleep 1
  fi
  if is_port_listening "$FRONTEND_PORT"; then
    warn "端口 $FRONTEND_PORT 被占用 (PID: $(port_pid "$FRONTEND_PORT"))，正在停止旧前端..."
    kill $(port_pid "$FRONTEND_PORT") 2>/dev/null
    sleep 1
  fi
  ok "端口已就绪"
}

# ============================ 7. 启动本地后端 ============================
start_backend() {
  info "===== 启动后端 (uvicorn :$BACKEND_PORT) ====="
  info "环境配置: $BACKEND_DIR/.env"
  info "日志: $BACKEND_LOG"
  (
    cd "$BACKEND_DIR"
    exec "$BACKEND_DIR/.venv/bin/python" -m uvicorn app.main:app \
      --env-file "$BACKEND_DIR/.env" \
      --host 0.0.0.0 --port "$BACKEND_PORT" --reload
  ) > >(while IFS= read -r line; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$BACKEND_LOG"
  done) 2>&1 &
  BACKEND_PID=$!
  printf '%s\n' "$BACKEND_PID" >"$BACKEND_PID_FILE"
}

wait_backend_ready() {
  info "等待后端健康检查..."
  for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$BACKEND_PORT/api/v1/health" >/dev/null 2>&1; then
      ok "后端已就绪: http://127.0.0.1:$BACKEND_PORT/api/v1/health"
      return 0
    fi
    sleep 1
  done
  error "后端启动超时，请查看日志: $BACKEND_LOG"
  return 1
}

# ============================ 8. 启动本地 Worker ============================
start_workers() {
  info "===== 启动观测 Worker ====="
  info "日志: $OBS_WORKER_LOG"
  (
    cd "$BACKEND_DIR"
    exec "$BACKEND_DIR/.venv/bin/python" -m app.workers.obs_worker
  ) > >(while IFS= read -r line; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$OBS_WORKER_LOG"
  done) 2>&1 &
  OBS_WORKER_PID=$!
  printf '%s\n' "$OBS_WORKER_PID" >"$OBS_WORKER_PID_FILE"

  info "===== 启动会话 Worker ====="
  info "日志: $SESSION_WORKER_LOG"
  (
    cd "$BACKEND_DIR"
    exec "$BACKEND_DIR/.venv/bin/python" -m app.workers.session_worker
  ) > >(while IFS= read -r line; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$SESSION_WORKER_LOG"
  done) 2>&1 &
  SESSION_WORKER_PID=$!
  printf '%s\n' "$SESSION_WORKER_PID" >"$SESSION_WORKER_PID_FILE"
}

queue_has_consumer() {
  local queue_name="$1"
  curl -fsS -u "$RABBITMQ_USER:$RABBITMQ_PASSWORD" \
    "http://127.0.0.1:$RABBITMQ_MANAGEMENT_PORT/api/queues/%2F/$queue_name" \
    | "$BACKEND_DIR/.venv/bin/python" -c \
      'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("consumers", 0) >= 1 else 1)' \
    >/dev/null 2>&1
}

wait_workers_ready() {
  info "等待 Worker 注册 RabbitMQ 消费者..."
  for _ in $(seq 1 30); do
    if kill -0 "$OBS_WORKER_PID" >/dev/null 2>&1 \
      && kill -0 "$SESSION_WORKER_PID" >/dev/null 2>&1 \
      && queue_has_consumer "obs.spans.q" \
      && queue_has_consumer "session.postprocess.q"; then
      ok "obs-worker / session-worker 已就绪"
      return 0
    fi
    sleep 1
  done
  error "Worker 启动超时，请查看日志: $OBS_WORKER_LOG / $SESSION_WORKER_LOG"
  return 1
}

# ============================ 9. 启动本地前端 ============================
start_frontend() {
  info "===== 启动前端 (vite :$FRONTEND_PORT) ====="
  info "日志: $FRONTEND_LOG"
  (
    cd "$FRONTEND_DIR"
    exec npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT"
  ) > >(while IFS= read -r line; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$FRONTEND_LOG"
  done) 2>&1 &
  FRONTEND_PID=$!
  printf '%s\n' "$FRONTEND_PID" >"$FRONTEND_PID_FILE"
}

wait_frontend_ready() {
  info "等待前端就绪..."
  for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null 2>&1; then
      ok "前端已就绪: http://127.0.0.1:$FRONTEND_PORT/"
      return 0
    fi
    sleep 1
  done
  error "前端启动超时，请查看日志: $FRONTEND_LOG"
  return 1
}

# ============================ 停止 ============================
SKIP_CLEANUP="${SKIP_CLEANUP:-0}"

cleanup() {
  if [ "$SKIP_CLEANUP" = "1" ]; then
    exit 0
  fi
  echo ""
  info "===== 正在停止服务 ====="
  stop_pid_file "后端" "$BACKEND_PID_FILE"
  stop_pid_file "前端" "$FRONTEND_PID_FILE"
  stop_pid_file "观测 Worker" "$OBS_WORKER_PID_FILE"
  stop_pid_file "会话 Worker" "$SESSION_WORKER_PID_FILE"
  # 兜底：清理残留监听进程
  if is_port_listening "$BACKEND_PORT"; then kill $(port_pid "$BACKEND_PORT") 2>/dev/null; fi
  if is_port_listening "$FRONTEND_PORT"; then kill $(port_pid "$FRONTEND_PORT") 2>/dev/null; fi
  rm -f "$STOP_REQUEST_FILE"
  ok "全部服务已停止，日志目录: $LOG_DIR"
  exit 0
}

stop_all() {
  info "===== 停止本地应用进程（保留 Docker 基础设施）====="
  touch "$STOP_REQUEST_FILE"
  stop_pid_file "后端" "$BACKEND_PID_FILE"
  stop_pid_file "前端" "$FRONTEND_PID_FILE"
  stop_pid_file "观测 Worker" "$OBS_WORKER_PID_FILE"
  stop_pid_file "会话 Worker" "$SESSION_WORKER_PID_FILE"
  if is_port_listening "$BACKEND_PORT"; then
    kill $(port_pid "$BACKEND_PORT") 2>/dev/null || true
  fi
  if is_port_listening "$FRONTEND_PORT"; then
    kill $(port_pid "$FRONTEND_PORT") 2>/dev/null || true
  fi
  ok "本地应用进程已停止；PostgreSQL / Redis / RabbitMQ 保持运行"
}

show_status() {
  info "===== 运行状态 ====="
  if is_port_listening "$BACKEND_PORT"; then
    ok "后端  运行中  http://127.0.0.1:$BACKEND_PORT  (PID: $(port_pid "$BACKEND_PORT"))"
  else
    warn "后端  未运行"
  fi
  if is_port_listening "$FRONTEND_PORT"; then
    ok "前端  运行中  http://127.0.0.1:$FRONTEND_PORT  (PID: $(port_pid "$FRONTEND_PORT"))"
  else
    warn "前端  未运行"
  fi
  if pid_file_running "$OBS_WORKER_PID_FILE"; then
    ok "obs-worker      运行中 (PID: $(cat "$OBS_WORKER_PID_FILE"))"
  else
    warn "obs-worker      未运行"
  fi
  if pid_file_running "$SESSION_WORKER_PID_FILE"; then
    ok "session-worker  运行中 (PID: $(cat "$SESSION_WORKER_PID_FILE"))"
  else
    warn "session-worker  未运行"
  fi
  if docker compose -f "$COMPOSE_FILE" ps --status running -q rabbitmq \
    | grep -q .; then
    ok "RabbitMQ       运行中  http://127.0.0.1:$RABBITMQ_MANAGEMENT_PORT"
  else
    warn "RabbitMQ       未运行"
  fi
}

# ============================ 主流程 ============================
trap cleanup INT TERM EXIT

main() {
  rm -f "$STOP_REQUEST_FILE"
  info "================= Agent Harness 本地开发启动 ================="
  info "项目目录: $ROOT_DIR"
  info "日志目录: $LOG_DIR"

  check_prereqs || exit 1
  ensure_infrastructure || exit 1
  ensure_postgres || exit 1
  ensure_backend_env || exit 1
  ensure_dependencies || exit 1
  stop_existing

  start_backend
  wait_backend_ready || exit 1
  start_workers
  wait_workers_ready || exit 1
  start_frontend
  wait_frontend_ready || exit 1

  echo ""
  ok "================= 全部启动完成 ================="
  ok "前端工作台:  ${C_GREEN}http://localhost:$FRONTEND_PORT${C_RESET}"
  ok "后端 API:    ${C_GREEN}http://localhost:$BACKEND_PORT/api/v1/health${C_RESET}"
  ok "API 文档:    ${C_GREEN}http://localhost:$BACKEND_PORT/docs${C_RESET}"
  ok "RabbitMQ:    ${C_GREEN}http://localhost:$RABBITMQ_MANAGEMENT_PORT${C_RESET} (harness / harness)"
  ok "API Key:     sk-dev-key-1 (header: X-API-Key)"
  ok "后端日志: $BACKEND_LOG"
  ok "前端日志: $FRONTEND_LOG"
  ok "观测 Worker 日志: $OBS_WORKER_LOG"
  ok "会话 Worker 日志: $SESSION_WORKER_LOG"
  ok "按 Ctrl+C 停止全部服务"
  echo ""

  # 前台等待，Ctrl+C 触发 cleanup
  while true; do
    if [ -f "$STOP_REQUEST_FILE" ]; then
      info "收到 stop 请求，正在退出本地开发进程"
      exit 0
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null && ! is_port_listening "$BACKEND_PORT"; then
      error "后端进程已退出，请查看日志: $BACKEND_LOG"
      exit 1
    fi
    if ! kill -0 "$OBS_WORKER_PID" 2>/dev/null; then
      error "obs-worker 已退出，请查看日志: $OBS_WORKER_LOG"
      exit 1
    fi
    if ! kill -0 "$SESSION_WORKER_PID" 2>/dev/null; then
      error "session-worker 已退出，请查看日志: $SESSION_WORKER_LOG"
      exit 1
    fi
    sleep 2
  done
}

case "${1:-start}" in
  start)  main ;;
  stop)   SKIP_CLEANUP=1; stop_all ;;
  status) SKIP_CLEANUP=1; show_status ;;
  *)      echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
