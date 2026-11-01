#!/usr/bin/env bash
# 啟動 DDPAI 工具服務。
#
#   ./scripts/run.sh real      真機模式：自動探測 193.168.0.1 等候選 IP（預設）
#   ./scripts/run.sh demo      示範模式：啟動模擬相機 + 儀表板，不需要實機
#   ./scripts/run.sh stop      停止所有服務
#   ./scripts/run.sh status    看目前狀態
#
# 兩個模式都用 8766 埠，互斥。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"
RUN_DIR="$ROOT/.run"
PORT="${DDPAI_PORT:-8766}"
FAKE_PORT="${DDPAI_FAKE_PORT:-8088}"

if [[ ! -x "$PY" ]]; then
  echo "找不到 .venv，先執行："
  echo "  uv venv --python 3.11 .venv"
  echo "  uv pip install --python .venv/bin/python -e '.[dev,web]'"
  exit 1
fi

mkdir -p "$RUN_DIR"

port_owner() { ss -ltnp 2>/dev/null | awk -v p=":$1 " '$4 ~ p {print $NF}' | head -1; }

stop_all() {
  for name in web fake; do
    pidfile="$RUN_DIR/$name.pid"
    if [[ -f "$pidfile" ]]; then
      pid="$(cat "$pidfile")"
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        echo "已停止 $name (pid $pid)"
      fi
      rm -f "$pidfile"
    fi
  done
  # 保險：清掉殘留的同類進程（用方括號避免 pkill 比對到自己）
  pkill -f "[u]vicorn ddpai_dashcam.web.app" 2>/dev/null || true
  pkill -f "[f]ake_camera_server.py" 2>/dev/null || true
}

status_all() {
  echo "埠 $PORT： $(port_owner "$PORT" || echo 空閒)"
  echo "埠 $FAKE_PORT： $(port_owner "$FAKE_PORT" || echo 空閒)"
  for name in web fake; do
    pidfile="$RUN_DIR/$name.pid"
    [[ -f "$pidfile" ]] && echo "$name pid: $(cat "$pidfile")" || echo "$name pid: 無"
  done
  echo
  curl -s -m 5 "http://127.0.0.1:$PORT/api/health" || echo "（儀表板未回應）"
  echo
}

case "${1:-real}" in
  real)
    stop_all
    echo "啟動儀表板（真機模式，自動探測相機）…"
    setsid nohup "$PY" -m uvicorn ddpai_dashcam.web.app:app \
      --host 0.0.0.0 --port "$PORT" \
      > "$RUN_DIR/web.log" 2>&1 < /dev/null &
    echo $! > "$RUN_DIR/web.pid"
    sleep 4
    echo "  儀表板： http://127.0.0.1:$PORT"
    echo "  日誌：   $RUN_DIR/web.log"
    echo
    echo "現在先跑一次診斷，看相機在不在："
    DDPAI_HOST="${DDPAI_HOST:-}" "$ROOT/.venv/bin/ddpai" doctor 2>&1 | tail -14 || true
    ;;
  demo)
    stop_all
    echo "啟動模擬相機（mode=${DDPAI_FAKE_MODE:-session}，埠 $FAKE_PORT）…"
    setsid nohup "$PY" scripts/fake_camera_server.py \
      --port "$FAKE_PORT" --mode "${DDPAI_FAKE_MODE:-session}" \
      > "$RUN_DIR/fake.log" 2>&1 < /dev/null &
    echo $! > "$RUN_DIR/fake.pid"

    for _ in $(seq 1 20); do
      curl -sf -m 1 "http://127.0.0.1:$FAKE_PORT/vcam/cmd.cgi?cmd=API_GetBaseInfo" >/dev/null 2>&1 && break
      sleep 0.3
    done

    echo "啟動儀表板（示範模式，指向模擬相機）…"
    DDPAI_HOST="127.0.0.1:$FAKE_PORT" setsid nohup "$PY" -m uvicorn ddpai_dashcam.web.app:app \
      --host 0.0.0.0 --port "$PORT" \
      > "$RUN_DIR/web.log" 2>&1 < /dev/null &
    echo $! > "$RUN_DIR/web.pid"

    for _ in $(seq 1 40); do
      curl -sf -m 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
      sleep 0.3
    done

    echo "  儀表板： http://127.0.0.1:$PORT"
    echo "  模擬相機：127.0.0.1:$FAKE_PORT"
    echo
    curl -s -m 5 "http://127.0.0.1:$PORT/api/health" || true
    echo
    ;;
  stop)   stop_all ;;
  status) status_all ;;
  *)
    echo "用法：$0 {real|demo|stop|status}" >&2
    exit 2
    ;;
esac
