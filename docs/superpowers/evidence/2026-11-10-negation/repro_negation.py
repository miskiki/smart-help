# -*- coding: utf-8 -*-
"""② 否定语义真实复现：真实全链路，看「无残疾」是否生成 disability_grade 追问。"""
import json, os, sys
ROOT = '/home/adminroot/smart-help'
sys.path.insert(0, os.path.join(ROOT, 'src'))
import kb_rag
import policy_core.full_flow as ff
from conversations import selected_config

LLM_CHOICE = '本地Qwen3.6-35B / qwen3.6-35b-a3b'
TURNS = [
    '请分析我家情况：家里两口人，江西省萍乡市莲花县，农村户口，我母亲58岁，有低保。',
    '我母亲无残疾，也没有残疾证，不用问残疾等级。',
]
_, cfg = selected_config(LLM_CHOICE)
def llm(*a, **kw):
    kw['cfg'] = cfg
    return kb_rag._llm_answer(*a, **kw)

hh = {}
for i, q in enumerate(TURNS):
    print('\n' + '=' * 70); print('TURN %d: %s' % (i + 1, q), flush=True)
    body, codes, result = ff.run(hh, q, 'repro-neg-%d' % i, None, llm)
    print('codes:', list(codes or []))
    mj = result.get('match_json') or {}
    print('members:', json.dumps(mj.get('hh', {}).get('members'), ensure_ascii=False)[:900])
    qs = result.get('questions') or mj.get('questions') or []
    print('QUESTION KEYS:', [x.get('key') for x in qs])
    for x in qs:
        print('   - %s | %s' % (x.get('key'), (x.get('label') or '')[:70]))
    print('PENDING:', [p.get('key') or p.get('fact_key') for p in (mj.get('hh', {}).get('policy_core', {}).get('pending') or [])])
