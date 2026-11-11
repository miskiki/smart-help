#!/usr/bin/env bash
# 一键启动：本地 LLM(8081) + 向量栈(8095/8096/8097) + Web(5000) + 规则库 worker
# 停止：bash deploy/stop.sh（或本终端 Ctrl+C）
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

# 先载入配置，预检才能用正确的值
set -a
[ -f deploy/web.env ] && . deploy/web.env
set +a
export CHAIN_TRACE_STDOUT=1

fail() { echo "启动预检失败：$*" >&2; exit 1; }

LLM_BIN="${LOCAL_LLM_SERVER:-/home/adminroot/Downloads/llama-src/build/bin/llama-server}"
LLM_MODEL="${LOCAL_LLM_MODEL:-/home/adminroot/Downloads/llama-b11753/models/Qwen3.6-35B-A3B-UD-Q4_K_M_MTP.gguf}"
# ---- 前置依赖预检 ----
[ -x "$LLM_BIN" ] || fail "llama-server 不存在或不可执行：$LLM_BIN
  （旧预编译版 b11753 缺 sm_120 kernel，请用源码编译版）"
[ -f "$LLM_MODEL" ] || fail "作答模型不存在：$LLM_MODEL"
[ -f config/model_settings.yaml ] || fail "缺少模型配置 config/model_settings.yaml"
[ -f src/rule_worker.py ] || fail "缺少规则库 worker：src/rule_worker.py"

[ -f "${LOCAL_EMBED_GGUF:-}" ] || fail "缺少嵌入模型：${LOCAL_EMBED_GGUF:-未配置}"
[ -f "${LOCAL_RERANK_GGUF:-}" ] || fail "缺少重排模型：${LOCAL_RERANK_GGUF:-未配置}"

# ---- 端口占用预检（本脚本不接管未知进程）----
for port in 8081 8095 8096 8097 5000; do
  if command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ":$port "; then
    fail "端口 $port 已被占用。请先停止已有服务（deploy/stop.sh），或手动结束占用进程。"
  fi
done

echo "向量：Qwen3-Embedding-4B（2560 维）+ Qwen3-Reranker-4B"
echo "启动中：8081 本地LLM → 8095 嵌入 → 8097 重排 → 8096 兼容层 → 5000 Web → 规则库 worker"
echo "本地模型：$LLM_MODEL"
echo

exec /home/adminroot/miniconda3/envs/smart-help-web/bin/python -u deploy/foreground.py
