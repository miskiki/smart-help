#!/usr/bin/env bash
# 智能帮扶系统一键体检：前置依赖 / 服务 / 数据库 / 规则库 / 守护 / 速度 / 结论
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY=/home/adminroot/miniconda3/envs/smart-help-web/bin/python
[ -f deploy/web.env ] && { set -a; . deploy/web.env; set +a; }
LLM_BIN="${LOCAL_LLM_SERVER:-/home/adminroot/Downloads/llama-src/build/bin/llama-server}"
LLM_MODEL="${LOCAL_LLM_MODEL:-/home/adminroot/Downloads/llama-b11753/models/Qwen3.6-35B-A3B-UD-Q4_K_M_MTP.gguf}"
PROBLEMS=()

chk() { if eval "$2" >/dev/null 2>&1; then printf '  ✓ %s\n' "$1"; else printf '  ✗ %s\n' "$1"; PROBLEMS+=("$1"); fi; }
http() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 --noproxy 127.0.0.1 "$1" 2>/dev/null; }

echo "================ 前置依赖 ================"
chk "推理程序 $LLM_BIN" "[ -x '$LLM_BIN' ]"
chk "作答模型 $(basename "$LLM_MODEL")" "[ -f '$LLM_MODEL' ]"
chk "嵌入模型 Qwen3-Embedding-4B" "[ -f '${LOCAL_EMBED_GGUF:-}' ]"
chk "重排模型 Qwen3-Reranker-4B" "[ -f '${LOCAL_RERANK_GGUF:-}' ]"
chk "Qwen3 词表 tokenizer.json" "[ -f models/qwen3-embedding-4b/tokenizer.json ]"
chk "模型配置 config/model_settings.yaml" "[ -f config/model_settings.yaml ]"

echo "================ 服务 ================"
chk "8081 本地大模型" "[ \"\$(http http://127.0.0.1:8081/health)\" = 200 ]"
chk "8095 嵌入服务（2560维）" "[ \"\$(http http://127.0.0.1:8095/health)\" = 200 ]"
chk "8096 重排兼容层" "[ \"\$(http http://127.0.0.1:8096/health)\" = 200 ]"
chk "8097 重排模型" "[ \"\$(http http://127.0.0.1:8097/health)\" = 200 ]"
chk "5000 Web 应用" "[ \"\$(http http://127.0.0.1:5000/)\" = 200 ]"
chk "规则库 worker" "pgrep -f 'rule_worker.py --watc[h]'"

echo "================ 数据库 / 规则库 ================"
"$PY" - <<'EOF' 2>/dev/null || PROBLEMS+=("金仓数据库查询")
import sys
sys.path.insert(0, 'src')
import config
from sqlalchemy import text
from policy_core import rule_store
import kb_rag
with rule_store.engine().connect() as c:
    llm = c.execute(text("SELECT cfg_value FROM app_config WHERE cfg_key='qa_llm_platform'")).fetchone()
    emb = c.execute(text("SELECT cfg_value FROM app_config WHERE cfg_key='qa_embed_platform'")).fetchone()
    dim = c.execute(text("SELECT cfg_value FROM app_config WHERE cfg_key='vector_index_dim'")).fetchone()
st = rule_store.stats()
by = st.get('by_status', {})
print("  ✓ 金仓连通 | 作答模型=%s | 向量平台=%s | 索引维度=%s | 读取表=%s" % (
    llm[0] if llm else '?', emb[0] if emb else '?', dim[0] if dim else '?', kb_rag._VECTOR_TABLE))
print("  ✓ 规则库 %d 条（完整 %d / 部分 %d / 失败 %d）| 区域 %s" % (
    st.get('total', 0), by.get('complete', 0), by.get('partial', 0), by.get('failed', 0),
    ','.join('%s%d' % (k, v) for k, v in sorted((st.get('by_region') or {}).items()))))
EOF

echo "================ 守护 / 自启 ================"
if [ -f runtime/autostart.pid ] && kill -0 "$(cat runtime/autostart.pid 2>/dev/null)" 2>/dev/null; then
    echo "  ✓ 守护进程运行中（异常自动重启）"
else
    echo "  - 守护进程未运行（手动启动模式）"
fi
if crontab -l 2>/dev/null | grep -q smart-help-autostart; then
    echo "  ✓ 已安装开机自启"
else
    echo "  - 未安装开机自启（执行 bash deploy/install_autostart.sh）"
fi

echo "================ 当前速度（3 次采样）================"
"$PY" - <<'EOF' 2>/dev/null
import json, urllib.request, statistics
Q = '列出农村低保、特困供养、重度残疾人护理补贴的申请条件要点，每项3条，用中文'
def call():
    body = {'model': 'qwen3.6-35b-a3b', 'messages': [{'role': 'user', 'content': Q}],
            'max_tokens': 200, 'temperature': 0}
    req = urllib.request.Request('http://127.0.0.1:8081/v1/chat/completions',
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return float((d.get('timings') or {}).get('predicted_per_second') or 0)
try:
    call()
    v = [call() for _ in range(3)]
    print('  解码 %.1f tok/s（明细 %s）' % (statistics.median(v), [round(x, 1) for x in v]))
except Exception as exc:
    print('  测速失败:', type(exc).__name__)
EOF

echo "================ 结论 ================"
if [ ${#PROBLEMS[@]} -eq 0 ]; then
    echo "  ✅ 系统正常，可直接使用：http://127.0.0.1:5000/ask"
else
    echo "  ⚠️ 发现 ${#PROBLEMS[@]} 项异常："
    for p in "${PROBLEMS[@]}"; do echo "     - $p"; done
    echo "  修复建议：bash deploy/start.sh（或 bash deploy/autostart.sh --daemon）"
fi
