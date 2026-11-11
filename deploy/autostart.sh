#!/usr/bin/env bash
# 智能帮扶系统守护进程：开机自启 + 崩溃自愈（无需人工介入）
#
# 用法：
#   bash deploy/autostart.sh --daemon     # 后台常驻（供 cron @reboot 调用）
#   bash deploy/autostart.sh --stop       # 停止守护（并停止服务）
#   bash deploy/autostart.sh --status     # 查看守护状态
#
# 行为：确保同一时刻只有一个守护实例；服务异常退出后自动重启（指数退避，最长 5 分钟）。
set -uo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
LOG="$ROOT/runtime/logs/autostart.log"
PIDFILE="$ROOT/runtime/autostart.pid"
LOCK="$ROOT/runtime/autostart.lock"
mkdir -p "$ROOT/runtime/logs"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

rotate_log() {
    # 防止日志无限增长：超过 50MB 归档一份
    if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 52428800 ]; then
        mv -f "$LOG" "$LOG.1"
        log "日志已轮转"
    fi
}

stop_all() {
    if [ -f "$PIDFILE" ]; then
        local pid; pid=$(cat "$PIDFILE" 2>/dev/null || true)
        if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        fi
        rm -f "$PIDFILE"
    fi
    bash deploy/stop.sh >/dev/null 2>&1 || true
}

case "${1:-}" in
  --stop)
      log "收到停止请求"
      stop_all
      log "守护与服务均已停止"
      exit 0
      ;;
  --status)
      if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
          echo "守护运行中 pid=$(cat "$PIDFILE")"
      else
          echo "守护未运行"
      fi
      exit 0
      ;;
  --daemon)
      exec 9>"$LOCK"
      if ! flock -n 9; then
          log "已有守护实例在运行，本次跳过"
          exit 0
      fi
      # 后台化
      if [ "${2:-}" != "--foreground" ]; then
          setsid nohup bash "$0" --daemon --foreground >>"$LOG" 2>&1 &
          echo "守护已启动（日志 $LOG）"
          exit 0
      fi
      echo $$ > "$PIDFILE"
      rotate_log
      log "守护启动：pid=$$，根目录 $ROOT"
      backoff=10
      while :; do
          if [ -f "$ROOT/runtime/foreground.pid" ]; then
              log "检测到残留服务记录，先清理"
              bash deploy/stop.sh >/dev/null 2>&1 || true
              sleep 3
          fi
          log "启动服务栈"
          bash deploy/start.sh >>"$LOG" 2>&1
          rc=$?
          log "服务栈退出（rc=$rc），${backoff}s 后重启"
          sleep "$backoff"
          # 指数退避，最长 300s；成功运行超过 5 分钟后重置
          if [ "$rc" -eq 0 ]; then backoff=10; else backoff=$(( backoff * 2 )); [ "$backoff" -gt 300 ] && backoff=300; fi
      done
      ;;
  *)
      echo "用法: bash deploy/autostart.sh [--daemon|--stop|--status]"
      exit 1
      ;;
esac
