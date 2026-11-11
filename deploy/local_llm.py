"""Run the installed GGUF as a loopback-only OpenAI-compatible server."""
import os
from pathlib import Path

def command():
    # 二进制：2026-11-04 用本机 CUDA 13.2 (nvcc) 源码编译，含 Blackwell sm_120 原生 kernel。
    # 旧预编译版 b11753 缺 sm_120 kernel，纯解码 87.7 vs 225.6 tok/s（同模型同参数、机器空闲时实测）。
    # 注意：本机速度随运行状态波动（同一命令 108–213 tok/s 都测到过），
    # 排查/验收请用 deploy/llm_speed_probe.sh 取当时基准，不要用历史数字做承诺。
    binary=Path(os.environ.get('LOCAL_LLM_SERVER','/home/adminroot/Downloads/llama-src/build/bin/llama-server'))
    # 生产主模型：Qwen3.6-35B-A3B MoE（用户指定唯一作答模型，激活参数约 3B）。
    # 仅调试可临时指定其它 GGUF：LOCAL_LLM_MODEL=/path/to.gguf deploy/start.sh
    model=Path(os.environ.get('LOCAL_LLM_MODEL','/home/adminroot/Downloads/llama-b11753/models/Qwen3.6-35B-A3B-UD-Q4_K_M_MTP.gguf'))
    if not binary.is_file() or not model.is_file():
        raise RuntimeError('本地LLM程序或模型文件不存在：binary=%s model=%s' % (binary, model))
    return [str(binary),'--model',str(model),'--alias','qwen3.6-35b-a3b',
            '--host','127.0.0.1','--port','8081','--gpu-layers','all',
            # 上下文：llama-server 的 --ctx-size 是「总池 / 并发槽」，单请求可用 = ctx-size ÷ parallel。
            # 应用真实 prompt 约 5.4–5.8k token + 输出≤4096，故单请求需 ≥16384。
            # 65536 ÷ 4 = 16384/请求（2026-11-04 实测：若写成 16384÷4=4096，所有问答都会 400）。
            '--ctx-size','65536','--parallel','4','--batch-size','1024','--ubatch-size','256',
            '--flash-attn','on','--jinja','--reasoning','off',
            '--chat-template-kwargs','{"enable_thinking":false}','--no-webui','--predict','4096',
            # MTP 投机解码（模型文件自带 MTP 层）。2026-11-04 实测：接受率仅 0.59–0.61、
            # 每步平均接受 2.19 token，收益有限；n-max 2 为实测最优，n-max 6 反而更慢。
            '--spec-type','draft-mtp','--spec-draft-n-max','2']

if __name__=='__main__':
    args=command()
    os.execv(args[0],args)
