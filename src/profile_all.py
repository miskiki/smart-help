# -*- coding: utf-8 -*-
"""全案例剖面：覆盖漏斗 + 阶段耗时 + 调用/token + 失败批次。只读，不改业务。"""
import sys, os, json, time, collections
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import config, kb_rag
from conversations import selected_config
import policy_core.full_flow as ff
from policy_core.reasoning import _SCOPE_CACHE

cases = json.load(open(os.path.join(ROOT, 'eval', 'cases.json')))
choice, cfg = selected_config(os.environ.get('PROFILE_PLATFORM', '本地Qwen3.6-35B / qwen3.6-35b-a3b'))

def llm(*a, **kw):
    kw['cfg'] = cfg
    return kb_rag._llm_answer(*a, **kw)

_BUDGETS = []
_OrigBudget = ff.Budget
class _Rec(_OrigBudget):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw); _BUDGETS.append(self)
ff.Budget = _Rec

info = {}
_ol = ff.load_evidence
def pl(c, r, cf):
    packs, stats = _ol(c, r, cf)
    info['eligible'] = [p['policy']['code'] for p in packs]
    info['cand'] = len(c); info['drop'] = stats.get('dropped')
    return packs, stats
ff.load_evidence = pl

out = []
for c in cases:
    info.clear(); _SCOPE_CACHE.clear()
    t0 = time.monotonic()
    try:
        body, codes, result = ff.run({}, c['question'], 'pa-' + c['case_id'], None, llm)
        err = None
    except Exception as exc:
        codes = []; result = {}; err = type(exc).__name__ + ':' + str(exc)[:120]
    wall = round(time.monotonic() - t0, 1)
    cov = (result.get('match_json') or {}).get('coverage') or {}
    ra = cov.get('reasoning_audit') or {}
    batches = ra.get('batches') or []
    calls = _BUDGETS[-1].calls if _BUDGETS else []
    tok = sum(x.get('completion_tokens') or 0 for x in calls if isinstance(x, dict))
    tin = sum(x.get('prompt_tokens') or 0 for x in calls if isinstance(x, dict))
    rec = dict(case=c['case_id'], wall=wall, codes=codes, err=err,
               cand=info.get('cand'), eligible=len(info.get('eligible') or []),
               analyzed=len(cov.get('analyzed_policy_codes') or []),
               unanalyzed=sorted(set(info.get('eligible') or []) - set(cov.get('analyzed_policy_codes') or [])),
               pending=cov.get('interpretation_pending'), failed_src=cov.get('interpretation_failed'),
               cards=len(codes), questions=len(result.get('questions') or []),
               calls=len(calls), out_tok=tok, in_tok=tin,
               batches=[(b.get('status'), len(b.get('attempted_ids') or [])) for b in batches],
               groups=len((ra.get('scheduler') or {}).get('groups') or []),
               drop=info.get('drop'), reviewed=cov.get('reviewed_sources'), cache=cov.get('cache_hits'))
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False), flush=True)

json.dump(out, open(os.path.join(ROOT, 'eval', 'profile_all.json'), 'w'), ensure_ascii=False, indent=1)
print('\n=== 汇总 ===')
for r in out:
    print('%-20s 墙钟%6.1fs 候选%4s 适用%3s 分析%2s 出卡%d 追问%d 调用%3d 输出tok%5d 失败源%s' % (
        r['case'], r['wall'], r['cand'], r['eligible'], r['analyzed'], r['cards'], r['questions'],
        r['calls'], r['out_tok'], r['failed_src']))
