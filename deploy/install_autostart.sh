#!/usr/bin/env bash
# 安装/卸载「开机自启」（用户级 cron @reboot，无需 root，无需登录）
#
#   bash deploy/install_autostart.sh            # 安装并立即启动守护
#   bash deploy/install_autostart.sh --remove   # 卸载（停止守护与服务）
#   bash deploy/install_autostart.sh --status   # 查看安装状态
set -uo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LINE="@reboot sleep 25; cd $ROOT && bash deploy/autostart.sh --daemon >> runtime/logs/autostart.log 2>&1"
MARK="# smart-help-autostart"

case "${1:-}" in
  --status)
      if crontab -l 2>/dev/null | grep -q "smart-help-autostart"; then
          echo "已安装开机自启："
          crontab -l 2>/dev/null | grep -A1 "smart-help-autostart"
      else
          echo "未安装开机自启"
      fi
      bash deploy/autostart.sh --status
      exit 0
      ;;
  --remove)
      crontab -l 2>/dev/null | grep -v "smart-help-autostart" | grep -v "deploy/autostart.sh --daemon" | crontab - 2>/dev/null || true
      bash deploy/autostart.sh --stop
      echo "已卸载开机自启"
      exit 0
      ;;
esac

# 安装（幂等）
{ crontab -l 2>/dev/null | grep -v "smart-help-autostart" | grep -v "deploy/autostart.sh --daemon" ; printf '%s\n%s\n' "$MARK" "$LINE"; } | crontab -
echo "✓ 已安装开机自启（用户 cron @reboot）"
echo "  条目: $LINE"
bash deploy/autostart.sh --daemon
sleep 5
bash deploy/autostart.sh --status
