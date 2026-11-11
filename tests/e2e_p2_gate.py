# -*- coding: utf-8 -*-
"""P2 匹配核心 v2 门禁：9 案例真实链路，校验分档/证据/零误放行。

用法（先 source 生产环境变量）：
    cd <repo> && set -a && . deploy/web.env && set +a
    PYTHONPATH=src /home/adminroot/miniconda3/envs/smart-help-web/bin/python tests/e2e_p2_gate.py

断言（任一失败退出码非 0）：
1. 每案例有 coverage.match_core，version=grade-1，counts 四档齐全；
2. 出卡行的 tier ∈ {A,B,C}，D 档不得出卡（FP 红线：已确认冲突/不适用不发卡）；
3. hidden_d>0 时必须有 why_not 清单，每条有 name 与 reason；
4. A 档卡片必须具备逐字引文证据（claims 里 verified_by=clause_quote 且带 quote）；
5. 已知误放行回归：62 岁家庭不得出"8-15 岁残疾儿童康复救助"（S-ZB-058）；
   c1 家庭已确认"非脱贫户"，不得出脱贫身份政策（X-ZB-300/301）；
6. 无 retrieval_or_analysis_error / 分数异常。
"""
import argparse
import json
import os
import sys
import time

os.environ.setdefault('RULE_SHADOW', '0')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import kb_rag
import policy_core.full_flow as ff
from conversations import selected_config

CASES = os.path.join(ROOT, 'src', 'eval', 'cases.json')
LLM_CHOICE = '本地Qwen3.6-35B / qwen3.6-35b-a3b'
ALLOWED_TIERS = {'A', 'B', 'C'}
# c9 询问"雨露计划"并点名政策：属于真实政策咨询（规则路径会出"点名核验"信息行），
# 不是闲聊，不能按"闲聊=0 卡"断言。闲聊路由由 tests/test_intake.py 覆盖。


def fail(msg, errors):
    errors.append(msg)
    print('  FAIL ' + msg, flush=True)


def check_case(case, errors):
    cid = case['case_id']
    key = cid.split('_')[0]
    t0 = time.monotonic()
    hh = {}
    body, codes, result = ff.run(hh, case['question'], 'p2gate-%s' % cid, None, llm)
    seconds = round(time.monotonic() - t0, 1)
    cov = ((result.get('match_json') or {}).get('coverage') or {})
    cards = result.get('cards') or []
    mc = cov.get('match_core') or {}
    why = cov.get('why_not') or []
    scope = {'case_id': cid, 'seconds': seconds, 'codes': list(codes or []),
             'cards': len(cards), 'match_core': mc, 'why_not': why,
             'rule_engine': (cov.get('rule_engine') or {}).get('state')}
    print('  %-20s %5ss cards=%d codes=%s tiers=%s' % (
        cid, seconds, len(cards), ','.join(codes or []) or '-',
        {r.get('tier') for r in cards}), flush=True)
    if cov.get('retrieval_or_analysis_error'):
        fail('%s 有 retrieval_or_analysis_error' % cid, errors)
        scope['error'] = cov['retrieval_or_analysis_error']
    if cov.get('intake', {}).get('route') == 'chat':
        scope['skipped'] = 'chat'
        if cards:
            fail('%s 闲聊路由不应出卡' % cid, errors)
        return scope
    if not mc or mc.get('state') == 'error':
        fail('%s 缺 match_core（%s）' % (cid, mc), errors)
        return scope
    if mc.get('version') != 'grade-1':
        fail('%s match_core.version=%r' % (cid, mc.get('version')), errors)
    counts = mc.get('counts') or {}
    if set(counts) != {'A', 'B', 'C', 'D'}:
        fail('%s counts 缺档：%s' % (cid, counts), errors)
    # 2. 档位合法性 / D 不出卡
    bad = [r.get('code') for r in cards if r.get('tier') not in ALLOWED_TIERS]
    if bad:
        fail('%s 卡片档位非法或 D 档出卡：%s' % (cid, bad), errors)
    # 3. D 档透明化
    if int(mc.get('hidden_d') or 0) > 0:
        if not why:
            fail('%s hidden_d=%s 却没有 why_not 清单' % (cid, mc.get('hidden_d')), errors)
        for w in why:
            if not (w.get('name') and w.get('reason')):
                fail('%s why_not 条目缺 name/reason：%s' % (cid, w), errors)
    # 4. A 档必须有逐字引文
    for row in cards:
        if row.get('tier') != 'A':
            continue
        quotes = [item.get('quote') for c in (row.get('claims') or []) if c.get('kind') == 'satisfied'
                  for item in (c.get('evidence') or []) if isinstance(item, dict) and item.get('quote')]
        if not quotes:
            fail('%s A 档卡片无逐字引文：%s' % (cid, row.get('code')), errors)
    # 5. 已知误放行回归
    if key in {'c1', 'c2', 'c3', 'c4', 'c5', 'c7'} and 'S-ZB-058' in (codes or []):
        fail('%s 62 岁家庭出了 8-15 岁儿童康复救助' % cid, errors)
    if key == 'c1':
        for code in ('X-ZB-300', 'X-ZB-301'):
            if code in (codes or []):
                fail('%s 非脱贫户家庭出了脱贫身份政策 %s' % (cid, code), errors)
    return scope


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default='')
    ap.add_argument('--out', default=os.path.join(ROOT, 'runtime', 'logs', 'p2_gate.json'))
    a = ap.parse_args()
    _, cfg = selected_config(LLM_CHOICE)
    global llm
    def llm(*args, **kw):
        kw['cfg'] = cfg
        return kb_rag._llm_answer(*args, **kw)

    cases = json.load(open(CASES, encoding='utf8'))
    if a.cases:
        keys = [k.strip() for k in a.cases.split(',') if k.strip()]
        cases = [c for c in cases if any(k in c['case_id'] for k in keys)]
    errors = []
    report = []
    print('P2 门禁开始，%d 案例' % len(cases), flush=True)
    for case in cases:
        try:
            report.append(check_case(case, errors))
        except Exception as exc:
            errors.append('%s 异常 %s: %s' % (case['case_id'], type(exc).__name__, exc))
            print('  FAIL exception %s: %s' % (type(exc).__name__, exc), flush=True)
    payload = {'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
               'cases': report, 'errors': errors}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, 'w', encoding='utf8').write(json.dumps(payload, ensure_ascii=False, indent=2))
    print('\n门禁结果： %s （%d 处失败）→ %s' % ('PASS' if not errors else 'FAIL', len(errors), a.out))
    return 0 if not errors else 1


if __name__ == '__main__':
    sys.exit(main())
