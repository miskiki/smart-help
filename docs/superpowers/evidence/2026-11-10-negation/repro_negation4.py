# -*- coding: utf-8 -*-
"""② 场景四：首轮建好家庭，第二轮在同一 hh 上说「无残疾」，看追问键。"""
import copy, json, sys
sys.path.insert(0, '/home/adminroot/smart-help/src')
import kb_rag
from policy_core.full_flow import run
from conversations import selected_config
_, cfg = selected_config('本地Qwen3.6-35B / qwen3.6-35b-a3b')
def llm(*a, **kw):
    kw['cfg'] = cfg
    return kb_rag._llm_answer(*a, **kw)

TURN1 = '我家两口人，江西省萍乡市莲花县，农村户口。母亲李某58岁，有低保。我本人30岁。'
TURN2 = '李某无残疾，也没有残疾证。'

hh = {}
print('=' * 70); print('TURN 1:', TURN1, flush=True)
body, codes, r1 = run(hh, TURN1, 'repro4-a', None, llm)
mj = r1.get('match_json') or {}
hh2 = hh  # run() 原地补全内部 hh
print('codes:', list(codes or []))
print('members:', json.dumps(hh2.get('members'), ensure_ascii=False)[:1200])
print('Q1:', [x.get('key') for x in (r1.get('questions') or [])])

print('\n' + '=' * 70); print('TURN 2:', TURN2, flush=True)
body, codes, r2 = run(hh2, TURN2, 'repro4-b', None, llm)
mj2 = r2.get('match_json') or {}
print('codes:', list(codes or []))
print('members:', json.dumps(mj2.get('hh', {}).get('members'), ensure_ascii=False)[:1500])
qs = r2.get('questions') or []
print('Q2 KEYS:', [x.get('key') for x in qs])
for x in qs:
    print('   - %s | %s' % (x.get('key'), (x.get('label') or '')[:70]))
