#!/usr/bin/env bash
# 灰度实例只读状态检查：进程/端口/HTTP 探测/关键开关。
# 用法：bash deploy/gray_status.sh
# 退出码：0=健康；1=异常或未运行。只读探测，不修改任何文件/数据库。
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="${GRAY_PYTHON:-/home/adminroot/miniconda3/envs/smart-help-web/bin/python}"

[ -f deploy/web.env ] && { set -a; . deploy/web.env; set +a; }
GRAY_ENV="${GRAY_ENV:-$ROOT/deploy/web.gray.env}"
[ -f "$GRAY_ENV" ] && { set -a; . "$GRAY_ENV"; set +a; }

BIND="${GRAY_BIND:-127.0.0.1:5001}"
PIDFILE="${GRAY_PID_FILE:-$ROOT/run/gray.pid}"
LOGFILE="${GRAY_LOG_FILE:-$ROOT/run/gray.log}"
HOST="${BIND%:*}"
PORT="${BIND##*:}"
PROBLEMS=()

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 --noproxy 127.0.0.1 "$1" 2>/dev/null || true; }
http_body() { curl -s --max-time 5 --noproxy 127.0.0.1 "$1" 2>/dev/null || true; }
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

echo "================ 灰度实例进程 ================"
pid=""
if [ -s "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  echo "  pid 文件：$PIDFILE（pid=${pid:-<空>}）"
elif [ -f "$PIDFILE" ]; then
  echo "  pid 文件为空：$PIDFILE"
else
  echo "  pid 文件不存在：$PIDFILE"
fi

running=0
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  running=1
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  echo "  ✓ 进程存活：pid=$pid"
  case "$cmd" in
    *gunicorn*) echo "  ✓ 进程类型：gunicorn" ;;
    *) echo "  ✗ 进程 $pid 不是 gunicorn"; PROBLEMS+=("pid 指向进程不是灰度 gunicorn") ;;
  esac
  case "$cmd" in
    *"$BIND"*) echo "  ✓ 绑定参数匹配：$BIND" ;;
    *) echo "  ✗ 绑定参数不匹配（期望 $BIND）"; PROBLEMS+=("进程 bind 参数不匹配") ;;
  esac
  case "$cmd" in
    *":5000"*|*foreground-5000.ctl*) echo "  ✗ 检测到生产绑定参数（5000），拒绝视为灰度实例"; PROBLEMS+=("进程参数疑似生产实例") ;;
  esac
else
  if [ -n "$pid" ]; then
    echo "  ✗ pid=$pid 已不存在（陈旧记录，可执行 gray_down.sh 清理）"
    PROBLEMS+=("pid 文件陈旧")
  else
    echo "  - 灰度实例未运行"
  fi
  PROBLEMS+=("灰度实例未运行")
fi

echo "================ 端口 / HTTP 探测 ================"
if port_listening; then
  echo "  ✓ 端口 $BIND 正在监听"
else
  echo "  ✗ 端口 $BIND 未监听"
  PROBLEMS+=("端口 $PORT 未监听")
fi
root_code="$(http_code "http://$BIND/")"
if [ "$root_code" = "200" ]; then
  echo "  ✓ http://$BIND/ -> 200"
else
  echo "  ✗ http://$BIND/ -> ${root_code:-000}（期望 200）"
  PROBLEMS+=("首页 HTTP ${root_code:-000}")
fi
stats_code="$(http_code "http://$BIND/api/stats")"
case "$stats_code" in
  200) echo "  ✓ /api/stats -> 200：$(http_body "http://$BIND/api/stats" | head -c 300)" ;;
  401|403) echo "  ✓ /api/stats -> $stats_code（需登录，接口可达）" ;;
  *)
    echo "  - /api/stats -> ${stats_code:-000}"
    if [ "$root_code" = "200" ]; then PROBLEMS+=("/api/stats HTTP $stats_code"); fi
    ;;
esac
if [ "$running" = "1" ]; then
  prod_code="$(http_code "http://127.0.0.1:5000/")"
  echo "  - 生产 5000 首页探测：${prod_code:-000}（只读，不触碰生产进程）"
fi

echo "================ 关键开关 ================"
if [ -f "$GRAY_ENV" ]; then
  echo "  ✓ 灰度 env 已加载：$GRAY_ENV"
else
  echo "  ✗ 灰度 env 不存在：$GRAY_ENV（请先生成）"
  PROBLEMS+=("缺少灰度 env")
fi
check_eq() {
  local name="$1" expect="$2" actual="$3"
  if [ "$actual" = "$expect" ]; then
    echo "  ✓ $name=$actual"
  else
    echo "  ✗ $name=${actual:-<未设置>}（期望 $expect）"
    PROBLEMS+=("$name 非预期")
  fi
}
check_eq SERVICE_SCOPE national "${SERVICE_SCOPE:-}"
check_eq REGION_NATIONWIDE 1 "${REGION_NATIONWIDE:-}"
check_eq NATIONAL_READONLY 1 "${NATIONAL_READONLY:-}"
check_eq POLICY_SCOPE 1 "${POLICY_SCOPE:-}"
check_eq POLICY_LIFECYCLE 0 "${POLICY_LIFECYCLE:-}"
check_eq POLICY_IMPORT 0 "${POLICY_IMPORT:-}"
check_eq POLICY_AUTOPILOT 0 "${POLICY_AUTOPILOT:-}"
check_eq POLICY_WORKER 0 "${POLICY_WORKER:-}"
echo "  - 其它相关开关（仅展示）：POLICY_STATUS_FILTER=${POLICY_STATUS_FILTER:-<未设置>} REGION_SOFT_FILTER=${REGION_SOFT_FILTER:-<未设置>} CONFLICT_SCOPE_PROGRAM=${CONFLICT_SCOPE_PROGRAM:-<未设置>}"

echo "================ 日志 ================"
if [ -f "$LOGFILE" ]; then
  echo "  - $LOGFILE（$(stat -c '%s' "$LOGFILE" 2>/dev/null || echo '?') bytes，最近修改 $(stat -c '%y' "$LOGFILE" 2>/dev/null | cut -d. -f1)）"
  echo "    （只显示文件信息，不回显日志正文，避免连接串/口令泄露）"
else
  echo "  - 日志尚未生成：$LOGFILE"
fi

echo "================ 结论 ================"
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  echo "  ✅ 灰度实例正常：http://$BIND/"
  exit 0
fi
echo "  ⚠️ 发现 ${#PROBLEMS[@]} 项异常："
for p in "${PROBLEMS[@]}"; do echo "     - $p"; done
echo "  启动/修复：bash deploy/gray_up.sh；停止：bash deploy/gray_down.sh"
exit 1
