#!/usr/bin/env bash
# LLM 速度探针：一次性给出 bench 上限 + 服务实测 + 硬件健康快照
# 用法: bash deploy/llm_speed_probe.sh
set -u
BASE=/home/adminroot/Downloads/llama-b11753
BIN=/home/adminroot/Downloads/llama-src/build/bin
MODEL=$BASE/models/Qwen3.6-35B-A3B-UD-Q4_K_M_MTP.gguf
PY=/home/adminroot/miniconda3/envs/smart-help-web/bin/python
PORT=8089

echo "================ 1. 硬件健康快照 ================"
nvidia-smi --query-gpu=name,driver_version,pstate,clocks.sm,clocks.max.sm,clocks.mem,power.draw,power.limit,temperature.gpu,utilization.gpu --format=csv,noheader
nvidia-smi -q -d PERFORMANCE 2>/dev/null | grep -A8 "Clocks Event Reasons" | head -9
echo "--- 显存带宽/算力 ---"
$PY - << 'EOF' 2>/dev/null
import torch,time
n=2*1024**3//4
a=torch.empty(n,dtype=torch.float32,device='cuda'); b=torch.empty_like(a)
for _ in range(3): b.copy_(a)
torch.cuda.synchronize(); t=time.time()
for _ in range(20): b.copy_(a)
torch.cuda.synchronize(); dt=time.time()-t
print(f'  显存拷贝带宽: {2*a.numel()*4*20/dt/1e9:.0f} GB/s  (正常 >1100)')
x=torch.randn(8192,8192,device='cuda'); y=torch.randn(8192,8192,device='cuda')
for _ in range(3): x@y
torch.cuda.synchronize(); t=time.time()
for _ in range(10): x@y
torch.cuda.synchronize(); dt=time.time()-t
print(f'  FP32 算力: {2*8192**3*10/dt/1e12:.0f} TFLOPS  (正常 ~64)')
EOF
echo "--- CPU 单核基准 ---"
$PY -c "
import time
t=time.time(); s=0
for i in range(30000000): s+=i
print('  30M 整数循环: %.3f 秒 (正常 ~3.1)'%(time.time()-t))
" 2>/dev/null
echo "--- 空闲时高 CPU 占用进程 ---"
ps -eo pcpu,comm --sort=-pcpu 2>/dev/null | head -6 | tail -5

echo
echo "================ 2. 纯解码上限（llama-bench，无服务）================"
pgrep -x llama-server >/dev/null && { echo "注意：有 llama-server 在跑，bench 结果会偏低"; }
for i in 1 2; do
  $BIN/llama-bench -m "$MODEL" -ngl 99 -p 0 -n 128 -t 8 2>/dev/null \
    | grep tg128 | awk -F'|' '{gsub(/^ +| +$/,"",$9); print "  第'"$i"'次 tg128 = "$9" tok/s"}'
done

echo
echo "================ 3. 服务模式实测（MTP n-max 2）================"
PIDS=$(ss -tlnp 2>/dev/null | grep ":$PORT" | grep -oE "pid=[0-9]+" | cut -d= -f2 | sort -u)
for p in $PIDS; do kill $p 2>/dev/null; done
sleep 6
$BIN/llama-server -m "$MODEL" -ngl 99 -c 8192 -n 8192 --port $PORT --host 127.0.0.1 \
  --jinja --flash-attn on --reasoning off \
  --spec-type draft-mtp --spec-draft-n-max 2 > /tmp/probe_server.log 2>&1 &
for i in $(seq 1 50); do
  sleep 6
  H=$(curl -s --max-time 3 --noproxy 127.0.0.1 http://127.0.0.1:$PORT/health 2>/dev/null)
  echo "$H" | grep -q '"status":"ok"' && break
done
$PY - << 'EOF'
import json,urllib.request,statistics
Q='列出农村低保、特困供养、重度残疾人护理补贴、困难残疾人生活补贴、临时救助这5项政策的申请条件要点，每项写3-4条，用中文'
def call():
    body=dict(model='q',messages=[{'role':'user','content':Q}],max_tokens=500,temperature=0,stream=False)
    req=urllib.request.Request('http://127.0.0.1:8089/v1/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=300) as r: d=json.loads(r.read())
    tm=d.get('timings') or {}
    return round(tm.get('predicted_per_second',0),1)
call()
vals=[call() for _ in range(5)]
print('  服务解码 5 次:', vals, ' 中位数=%.1f' % statistics.median(vals))
EOF
echo "--- MTP 接受率 / 单token耗时 ---"
grep -aE "draft acceptance|eval time" /tmp/probe_server.log | tail -4
PIDS=$(ss -tlnp 2>/dev/null | grep ":$PORT" | grep -oE "pid=[0-9]+" | cut -d= -f2 | sort -u)
for p in $PIDS; do kill $p 2>/dev/null; done
echo "探针结束（测试实例已关闭）"
