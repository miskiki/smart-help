# -*- coding: utf-8 -*-
"""生产环境评测（当前架构：Qwen3-Embedding-4B 2560 维 + Qwen3-Reranker-4B + Qwen3.6-35B）。

它不是"对比两种环境"的工具——项目只维护一套环境。用途：
- 改动规则库/提示词/参数后，回归 8 案例，确认卡片集合与耗时没有退化
- 实验不写生产巡检日志（自动 RULE_SHADOW=0）

用法（在 src 目录）：
    python eval_run.py                      # 全部 8 案例
    python eval_run.py --repeat 2           # 跑 2 轮取稳定值
    python eval_run.py --cases c4,c5        # 指定案例
    python eval_run.py --engine off         # 走兜底链路（不走规则引擎）
"""
import argparse
import json
import os
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
CASES = SRC / 'eval' / 'cases.json'
ROOT = SRC.parent
LLM_CHOICE = '本地Qwen3.6-35B / qwen3.6-35b-a3b'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default='')
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('--engine', choices=['inherit', 'on', 'off'], default='inherit')
    ap.add_argument('--out', default='')
    a = ap.parse_args()

    os.environ['RULE_SHADOW'] = '0'
    if a.engine == 'off':
        os.environ['RULE_ENGINE'] = 'off'
    elif a.engine == 'on':
        os.environ['RULE_ENGINE'] = 'on'

    import model_registry
    import kb_rag
    import policy_core.full_flow as ff
    from conversations import selected_config

    print('环境：平台=%s 维度=%s 表=%s 重排=%s' % (
        model_registry.get_embed_platform(), model_registry.embed_dim(),
        kb_rag._VECTOR_TABLE, model_registry.rerank_cfg().get('rerank_model')), flush=True)

    _, cfg = selected_config(LLM_CHOICE)

    def llm(*args, **kw):
        kw['cfg'] = cfg
        return kb_rag._llm_answer(*args, **kw)

    cases = json.loads(CASES.read_text(encoding='utf8'))
    if a.cases:
        keys = [k.strip() for k in a.cases.split(',') if k.strip()]
        cases = [c for c in cases if any(k in c['case_id'] for k in keys)]

    runs = []
    for rnd in range(a.repeat):
        if a.repeat > 1:
            print('--- 第 %d/%d 轮 ---' % (rnd + 1, a.repeat))
        results = []
        for c in cases:
            t0 = time.monotonic()
            hh = {}
            try:
                body, codes, result = ff.run(hh, c['question'], 'eval%d-%s' % (rnd, c['case_id']), None, llm)
                cov = ((result.get('match_json') or {}).get('coverage') or {})
                rec = {
                    'case_id': c['case_id'], 'wall_seconds': round(time.monotonic() - t0, 1),
                    'cards': len(codes or []), 'codes': list(codes or []),
                    'rule_engine': (cov.get('rule_engine') or {}).get('state'),
                    'fact_extraction': (cov.get('fact_extraction') or {}).get('status'),
                    'error': cov.get('retrieval_or_analysis_error'),
                }
            except Exception as exc:
                rec = {'case_id': c['case_id'], 'error': '%s: %s' % (type(exc).__name__, exc)}
            results.append(rec)
            print('  %-18s %5ss cards=%s codes=%s' % (
                rec['case_id'], rec.get('wall_seconds'), rec.get('cards'),
                ','.join(rec.get('codes') or []) or '-'), flush=True)
        runs.append(results)

    out = a.out or str(SRC / 'eval' / ('run_%s.json' % time.strftime('%Y%m%d_%H%M%S')))
    payload = {
        'environment': {
            'embed_platform': model_registry.get_embed_platform(),
            'embed_dim': model_registry.embed_dim(),
            'vector_table': kb_rag._VECTOR_TABLE,
            'rerank': model_registry.rerank_cfg(),
        },
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'repeat': a.repeat, 'runs': runs,
    }
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf8')

    ok = [r for run in runs for r in run if not r.get('error')]
    print('\n合计: %d 案例 × %d 轮，成功 %d，卡片总数 %d，平均耗时 %.1fs' % (
        len(cases), a.repeat, len(ok), sum(r['cards'] for r in ok),
        sum(r['wall_seconds'] for r in ok) / max(1, len(ok))))
    print('结果已写入', out)


if __name__ == '__main__':
    main()
