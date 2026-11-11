# -*- coding: utf-8 -*-
"""C2 第一步：用现有链路跑 8 个案例，保存每个案例的家庭事实与卡片，作为 A/B 基线。

用法: python baseline_capture.py [--out baseline_hh.json]
输出字段：case_id / county / wall / facts / members / cards / questions
"""
import sys, os, json, time, argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import config, kb_rag
from conversations import selected_config
import policy_core.full_flow as ff
from policy_core.reasoning import _SCOPE_CACHE

COUNTY = {'c1_famA_lianhua': '莲花', 'c2_famA_lianhua': '莲花', 'c3_famA_lianhua': '莲花',
          'c4_famA_lianhua': '莲花', 'c5_famA_mod1级': '莲花', 'c7_famB_lianhua': '莲花',
          'c8_famB_jian': '吉安', 'c6_famB_noregion': None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='baseline_hh.json')
    ap.add_argument('--cases', default='')
    a = ap.parse_args()
    cases = json.load(open(os.path.join(ROOT, 'eval', 'cases.json')))
    if a.cases:
        want = set(a.cases.split(','))
        cases = [c for c in cases if c['case_id'] in want]
    _, cfg = selected_config('本地Qwen3.6-35B / qwen3.6-35b-a3b')

    def llm(*args, **kw):
        kw['cfg'] = cfg
        return kb_rag._llm_answer(*args, **kw)

    out = []
    for case in cases:
        _SCOPE_CACHE.clear()
        hh = {}
        t0 = time.monotonic()
        try:
            body, codes, result = ff.run(hh, case['question'], 'base-' + case['case_id'], None, llm)
            err = None
        except Exception as exc:
            body, codes, result = '', [], {}
            err = type(exc).__name__ + ':' + str(exc)[:160]
        wall = round(time.monotonic() - t0, 1)
        st = hh.get('policy_core') or {}
        facts = st.get('facts') or {}
        cards = []
        for card in (result.get('cards') or []):
            cards.append(dict(code=card.get('code'), name=card.get('name'),
                              member=card.get('member'), member_no=card.get('member_no'),
                              status=card.get('analysis_status') or card.get('status'),
                              satisfied=[str(s)[:60] for s in (card.get('satisfied') or [])],
                              missing=[str(m)[:40] for m in (card.get('missing') or [])]))
        record = dict(case_id=case['case_id'], county=COUNTY.get(case['case_id']), wall=wall, err=err,
                      members=[m.get('member_no') if isinstance(m, dict) else m for m in (st.get('members') or [])],
                      facts={k: dict(state=v.get('state'), value=v.get('value'))
                             for k, v in facts.items() if isinstance(v, dict)},
                      cards=cards, codes=codes,
                      questions=[q.get('fact_key') for q in (result.get('questions') or [])],
                      calls=len((result.get('diagnostics') or {}).get('calls') or [])
                      if isinstance((result.get('diagnostics') or {}).get('calls'), list) else 0)
        out.append(record)
        print('%-20s 墙钟%6.1fs 事实%3d 卡片%d %s%s' % (
            case['case_id'], wall, len(record['facts']), len(cards), codes,
            ('  ERR=' + err) if err else ''), flush=True)
        json.dump(out, open(os.path.join(ROOT, 'eval', a.out), 'w'), ensure_ascii=False, indent=1)
    print('\n已保存:', a.out)


if __name__ == '__main__':
    main()
