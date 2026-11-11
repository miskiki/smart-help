#!/usr/bin/env bash
# 灰度实例启动脚本（T25）：在 127.0.0.1:5001 起一个与生产 5000 隔离的只读全国模式实例。
#
# 用法：
#   bash deploy/gray_up.sh
#
# 配置加载顺序（后者覆盖前者）：
#   1) deploy/web.env        生产配置（数据库连接、模型参数等），只读复用
#   2) deploy/web.gray.env   灰度覆盖（只读全国模式开关，由 web.gray.env.example 生成）
#
# 安全约束：
#   - 不触碰生产 gunicorn（127.0.0.1:5000）；bind 到 5000 会被拒绝
#   - 只读守卫：NATIONAL_READONLY=1 且生命周期/导入/自动管理/outbox 消费者全部关闭
#   - 重复执行幂等：本灰度实例已在运行时直接退出 0
#   - 不删除任何 pid/日志/数据文件；pid 记录只做截断清理
#
# 可覆盖环境变量（便于临时端口验证）：
#   GRAY_ENV GRAY_BASE_ENV GRAY_BIND GRAY_PID_FILE GRAY_LOG_FILE
#   GRAY_PYTHON GRAY_START_TIMEOUT GRAY_ALLOW_WRITES
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

die() { echo "灰度启动失败：$*" >&2; exit 1; }
note() { echo "  $*"; }
truthy() { case "${1,,}" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac; }
pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
pid_cmdline() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true; }

# ---- 0. 路径与 Python ----
PY="${GRAY_PYTHON:-/home/adminroot/miniconda3/envs/smart-help-web/bin/python}"
[ -x "$PY" ] || die "Python 不可执行：$PY"
BASE_ENV="${GRAY_BASE_ENV:-$ROOT/deploy/web.env}"
GRAY_ENV="${GRAY_ENV:-$ROOT/deploy/web.gray.env}"
[ -f "$BASE_ENV" ] || die "缺少生产配置文件：$BASE_ENV"
if [ ! -f "$GRAY_ENV" ]; then
  die "缺少灰度配置文件：$GRAY_ENV
  首次使用（灰度配置含真实连接串，不要提交）：
    cp deploy/web.env deploy/web.gray.env
    cat deploy/web.gray.env.example >> deploy/web.gray.env
    chmod 600 deploy/web.gray.env
  或使用 runtime/ 下的副本：GRAY_ENV=... bash deploy/gray_up.sh（见 docs/redesign/23-gray-rollout.md）"
fi

# ---- 1. 载入配置（后者覆盖前者） ----
set -a
. "$BASE_ENV"
. "$GRAY_ENV"
set +a
export PYTHONPATH="$ROOT/src:$ROOT/deploy${PYTHONPATH:+:$PYTHONPATH}"

BIND="${GRAY_BIND:-127.0.0.1:5001}"
PIDFILE="${GRAY_PID_FILE:-$ROOT/run/gray.pid}"
LOGFILE="${GRAY_LOG_FILE:-$ROOT/run/gray.log}"
START_TIMEOUT="${GRAY_START_TIMEOUT:-30}"
case "$BIND" in *:*) : ;; *) die "GRAY_BIND 格式应为 host:port，实际：$BIND" ;; esac
HOST="${BIND%:*}"
PORT="${BIND##*:}"
case "$PORT" in ''|*[!0-9]*) die "GRAY_BIND 端口非法：$BIND" ;; esac
[ "$HOST" = "127.0.0.1" ] || die "灰度实例只允许绑定 127.0.0.1（实际：$BIND）"
if [ "$PORT" = "5000" ]; then
  die "拒绝绑定 5000：生产实例占用该端口，灰度必须使用独立端口（默认 5001）"
fi

# ---- 2. 只读开关守卫（灰度专用：不满足就拒绝启动） ----
if ! truthy "${GRAY_ALLOW_WRITES:-0}"; then
  [ "${SERVICE_SCOPE:-}" = "national" ] || die "SERVICE_SCOPE 必须为 national（当前：${SERVICE_SCOPE:-未设置}）；请确认已加载灰度 env"
  truthy "${REGION_NATIONWIDE:-0}" || die "REGION_NATIONWIDE 必须开启（当前：${REGION_NATIONWIDE:-未设置}）"
  truthy "${NATIONAL_READONLY:-0}" || die "NATIONAL_READONLY 必须为 1（灰度只读模式）"
  truthy "${POLICY_SCOPE:-0}" || die "POLICY_SCOPE 必须开启（当前：${POLICY_SCOPE:-未设置}）"
  for pair in "POLICY_LIFECYCLE=${POLICY_LIFECYCLE:-0}" "POLICY_IMPORT=${POLICY_IMPORT:-0}" "POLICY_AUTOPILOT=${POLICY_AUTOPILOT:-0}" "POLICY_WORKER=${POLICY_WORKER:-0}"; do
    name="${pair%%=*}"
    val="${pair#*=}"
    if truthy "$val"; then die "$name 必须关闭（灰度只读：不写业务表、不消费 outbox）"; fi
  done
fi

# ---- 3. 依赖与目录预检 ----
"$PY" -m gunicorn --version >/dev/null 2>&1 || die "$PY 缺少 gunicorn"
mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOGFILE")"
chmod 700 "$(dirname "$PIDFILE")" 2>/dev/null || true

# ---- 4. 幂等：本灰度实例已在运行则直接退出 ----
is_gray_process() {
  local pid="$1" cmd
  pid_alive "$pid" || return 1
  cmd="$(pid_cmdline "$pid")"
  case "$cmd" in *gunicorn*) : ;; *) return 1 ;; esac
  case "$cmd" in *"$BIND"*) : ;; *) return 1 ;; esac
  return 0
}
port_free() {
  "$PY" - "$HOST" "$PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind((sys.argv[1], int(sys.argv[2])))
finally:
    s.close()
PYEOF
}

if [ -s "$PIDFILE" ]; then
  old_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if pid_alive "$old_pid"; then
    if is_gray_process "$old_pid"; then
      echo "灰度实例已在运行：pid=$old_pid bind=$BIND（幂等退出）"
      exit 0
    fi
    die "pid 文件指向的进程 $old_pid 不是本灰度实例（bind=$BIND）；为防误杀，请人工检查后再启动"
  fi
  note "清理陈旧 pid 记录（进程 $old_pid 已不存在）"
  : > "$PIDFILE"
fi

# ---- 5. 端口占用校验（5000 已由绑定守卫拒绝，这里只校验灰度端口） ----
if ! port_free; then
  if command -v ss >/dev/null 2>&1; then
    echo "端口占用详情：" >&2
    ss -tlnp 2>/dev/null | grep -E "[:.]$PORT\b" >&2 || true
  fi
  die "端口 $PORT 已被占用（灰度需独立端口；生产 5000 不受影响）"
fi

# ---- 6. 启动（与生产相同的 gunicorn 进程模型） ----
echo "启动灰度实例：bind=$BIND pid=$PIDFILE log=$LOGFILE"
echo "只读开关：SERVICE_SCOPE=${SERVICE_SCOPE:-<未设置>} REGION_NATIONWIDE=${REGION_NATIONWIDE:-0} NATIONAL_READONLY=${NATIONAL_READONLY:-0} POLICY_LIFECYCLE=${POLICY_LIFECYCLE:-0} POLICY_WORKER=${POLICY_WORKER:-0}"
cd "$ROOT/src"
nohup "$PY" -m gunicorn \
  --chdir "$ROOT/src" \
  --bind "$BIND" \
  --workers 1 \
  --timeout 600 \
  --graceful-timeout 15 \
  --worker-class gthread \
  --threads 2 \
  --error-logfile - \
  --access-logfile - \
  --capture-output \
  app:app >>"$LOGFILE" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$PIDFILE"
cd "$ROOT"

started=0
for _ in $(seq 1 $((START_TIMEOUT * 2))); do
  if ! pid_alive "$pid"; then break; fi
  if ! port_free; then started=1; break; fi
  sleep 0.5
done

if [ "$started" = "1" ]; then
  echo "灰度实例已启动：pid=$pid"
  echo "  查看状态：bash deploy/gray_status.sh"
  echo "  一键停止：bash deploy/gray_down.sh"
  exit 0
fi

echo "灰度实例启动失败，日志末尾（$LOGFILE）：" >&2
tail -n 40 "$LOGFILE" >&2 2>/dev/null || true
pid_alive "$pid" && kill -TERM "$pid" 2>/dev/null || true
for _ in $(seq 1 20); do pid_alive "$pid" || break; sleep 0.5; done
pid_alive "$pid" && kill -KILL "$pid" 2>/dev/null || true
: > "$PIDFILE"
die "未在 ${START_TIMEOUT}s 内监听 $BIND"
