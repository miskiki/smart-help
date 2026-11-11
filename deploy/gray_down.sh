#!/usr/bin/env bash
# 一键停止灰度实例：只停止 pid 文件指向的灰度 gunicorn，绝不触碰生产 5000。
# 优雅停止：TERM -> 等待 GRAY_STOP_TIMEOUT 秒 -> KILL。
# 幂等：未运行时直接退出 0；不删除任何 pid/日志/数据文件。
# 用法：bash deploy/gray_down.sh
set -uo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="${GRAY_PYTHON:-/home/adminroot/miniconda3/envs/smart-help-web/bin/python}"

[ -f deploy/web.env ] && { set -a; . deploy/web.env; set +a; }
GRAY_ENV="${GRAY_ENV:-$ROOT/deploy/web.gray.env}"
[ -f "$GRAY_ENV" ] && { set -a; . "$GRAY_ENV"; set +a; }

BIND="${GRAY_BIND:-127.0.0.1:5001}"
PIDFILE="${GRAY_PID_FILE:-$ROOT/run/gray.pid}"
STOP_TIMEOUT="${GRAY_STOP_TIMEOUT:-30}"
HOST="${BIND%:*}"
PORT="${BIND##*:}"

die() { echo "灰度停止失败：$*" >&2; exit 1; }
pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
port_listening() {
  "$PY" - "$HOST" "$PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1)
try:
    sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
finally:
    s.close()
PYEOF
}

if [ ! -s "$PIDFILE" ]; then
  echo "灰度实例未运行（pid 文件不存在或为空）：$PIDFILE"
  exit 0
fi
pid="$(cat "$PIDFILE" 2>/dev/null || true)"
case "$pid" in
  ''|*[!0-9]*)
    : > "$PIDFILE"
    echo "灰度实例未运行（pid 文件内容非法，已清理记录）：$PIDFILE"
    exit 0
    ;;
esac
if ! pid_alive "$pid"; then
  : > "$PIDFILE"
  echo "灰度实例未运行（进程 $pid 已不存在，已清理陈旧 pid 记录）"
  exit 0
fi

cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
case "$cmd" in *gunicorn*) : ;; *) die "进程 $pid 不是 gunicorn，拒绝停止" ;; esac
case "$cmd" in *"$BIND"*) : ;; *) die "进程 $pid 未绑定 $BIND，不是本灰度实例，拒绝停止" ;; esac
case "$cmd" in *":5000"*|*foreground-5000.ctl*) die "进程 $pid 疑似生产实例（参数含 5000），拒绝停止" ;; esac

echo "停止灰度实例：pid=$pid bind=$BIND"
kill -TERM "$pid" 2>/dev/null || true
for _ in $(seq 1 $((STOP_TIMEOUT * 2))); do
  pid_alive "$pid" || break
  sleep 0.5
done
if pid_alive "$pid"; then
  echo "  TERM 后 ${STOP_TIMEOUT}s 未退出，发送 KILL"
  kill -KILL "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do pid_alive "$pid" || break; sleep 0.5; done
fi
: > "$PIDFILE"
if pid_alive "$pid"; then
  die "进程 $pid 仍存活，请人工检查"
fi
if port_listening; then
  echo "  ⚠️ 端口 $PORT 仍在监听（可能有其它进程占用，请人工检查）" >&2
  exit 1
fi
echo "✅ 灰度实例已停止（生产 5000 未受影响）"
exit 0
