# -*- coding: utf-8 -*-
"""② 场景三：完全模拟生产入口（每轮 full_resolve 载入会话态），说「无残疾」看是否仍追等级。"""
import json, sys
sys.path.insert(0, '/home/adminroot/smart-help/src')
import kb_rag
from policy_core.full_flow import run
from policy_core.facts import resolve as full_resolve
from conversations import selected_config
_, cfg = selected_config('本地Qwen3.6-35B / qwen3.6-35b-a3b')
def llm(*a, **kw):
    kw['cfg'] = cfg
    return kb_rag._llm_answer(*a, **kw)

from app import app
SID = 'repro-neg3-%d' % __import__('time').time()
TURNS = [
    '我家两口人，江西省萍乡市莲花县，农村户口，母亲58岁，有低保。',
    '我母亲无残疾。',
]
with app.app_context():
  for i, q in enumerate(TURNS):
      print('\n' + '=' * 70); print('TURN %d: %s' % (i + 1, q), flush=True)
      hh = full_resolve(q, SID, None)
      body, codes, result = run(hh, q, SID, None, llm)
      mj = result.get('match_json') or {}
      print('codes:', list(codes or []))
      print('members:', json.dumps(mj.get('hh', {}).get('members'), ensure_ascii=False)[:1200])
      qs = result.get('questions') or mj.get('questions') or []
      print('QUESTION KEYS:', [x.get('key') for x in qs])
      for x in qs:
          print('   - %s | %s' % (x.get('key'), (x.get('label') or '')[:70]))
