#!/usr/bin/env bash
# macOS 本地 bge-reranker-v2-m3 服务管理脚本。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
LOG_DIR="$ROOT_DIR/logs"
VENV_DIR="${RERANK_VENV_DIR:-$BACKEND_DIR/.venv-rerank}"
PYTHON_BIN="$VENV_DIR/bin/python"
PID_FILE="$LOG_DIR/reranker.pid"
LOG_FILE="$LOG_DIR/reranker.log"

RERANK_SERVER_HOST="${RERANK_SERVER_HOST:-127.0.0.1}"
RERANK_SERVER_PORT="${RERANK_SERVER_PORT:-1235}"
RERANK_SERVER_MODEL_ID="${RERANK_SERVER_MODEL_ID:-BAAI/bge-reranker-v2-m3}"
RERANK_SERVER_SERVED_MODEL_NAME="${RERANK_SERVER_SERVED_MODEL_NAME:-text-embedding-bge-reranker-v2-m3}"
RERANK_SERVER_DEVICE="${RERANK_SERVER_DEVICE:-auto}"
RERANK_SERVER_MAX_LENGTH="${RERANK_SERVER_MAX_LENGTH:-1024}"
RERANK_SERVER_BATCH_SIZE="${RERANK_SERVER_BATCH_SIZE:-4}"
RERANK_SERVER_MAX_DOCUMENTS="${RERANK_SERVER_MAX_DOCUMENTS:-128}"

mkdir -p "$LOG_DIR"

is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(tr -d '[:space:]' < "$PID_FILE")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

ensure_runtime() {
  if [ ! -x "$PYTHON_BIN" ]; then
    echo "[reranker] 创建独立虚拟环境: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi
  if ! "$PYTHON_BIN" -c 'import fastapi, torch, transformers, uvicorn' \
    >/dev/null 2>&1; then
    echo "[reranker] 安装模型服务依赖，首次执行需要下载 Python 包"
    "$PYTHON_BIN" -m pip install --upgrade pip
    "$PYTHON_BIN" -m pip install -r "$BACKEND_DIR/requirements-rerank.txt"
  fi
}

export_runtime_env() {
  export RERANK_SERVER_MODEL_ID
  export RERANK_SERVER_SERVED_MODEL_NAME
  export RERANK_SERVER_DEVICE
  export RERANK_SERVER_MAX_LENGTH
  export RERANK_SERVER_BATCH_SIZE
  export RERANK_SERVER_MAX_DOCUMENTS
  export PYTORCH_ENABLE_MPS_FALLBACK="${PYTORCH_ENABLE_MPS_FALLBACK:-1}"
}

start_service() {
  if is_running; then
    echo "[reranker] 已运行，PID $(cat "$PID_FILE")"
    exit 0
  fi
  if lsof -nP -iTCP:"$RERANK_SERVER_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[reranker] 端口 $RERANK_SERVER_PORT 已被占用"
    exit 1
  fi
  ensure_runtime
  export_runtime_env
  echo "[reranker] 启动服务，模型首次运行时会下载权重"
  (
    cd "$BACKEND_DIR"
    nohup "$PYTHON_BIN" -m uvicorn app.rerank_server:app \
      --host "$RERANK_SERVER_HOST" \
      --port "$RERANK_SERVER_PORT" \
      --workers 1 >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
  )
  sleep 1
  if is_running; then
    echo "[reranker] 进程已启动，PID $(cat "$PID_FILE")"
    echo "[reranker] 模型加载状态: tail -f $LOG_FILE"
    echo "[reranker] 健康检查: http://127.0.0.1:$RERANK_SERVER_PORT/health"
  else
    echo "[reranker] 启动失败，查看日志: $LOG_FILE"
    exit 1
  fi
}

stop_service() {
  if ! is_running; then
    rm -f "$PID_FILE"
    echo "[reranker] 未运行"
    exit 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill -TERM "$pid"
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "[reranker] 已停止"
      exit 0
    fi
    sleep 0.25
  done
  echo "[reranker] 进程仍在退出中，PID $pid"
  exit 1
}

show_status() {
  if ! is_running; then
    echo "[reranker] 未运行"
    exit 1
  fi
  echo "[reranker] 运行中，PID $(cat "$PID_FILE")"
  if curl -fsS "http://127.0.0.1:$RERANK_SERVER_PORT/health"; then
    echo
  else
    echo "[reranker] 模型仍在加载或健康检查暂不可用"
  fi
}

run_foreground() {
  ensure_runtime
  export_runtime_env
  cd "$BACKEND_DIR"
  exec "$PYTHON_BIN" -m uvicorn app.rerank_server:app \
    --host "$RERANK_SERVER_HOST" \
    --port "$RERANK_SERVER_PORT" \
    --workers 1
}

case "${1:-start}" in
  start) start_service ;;
  stop) stop_service ;;
  restart)
    if is_running; then stop_service; fi
    start_service
    ;;
  status) show_status ;;
  setup) ensure_runtime ;;
  foreground) run_foreground ;;
  *)
    echo "用法: $0 {start|stop|restart|status|setup|foreground}"
    exit 2
    ;;
esac
