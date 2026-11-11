"""链路分层计时探针：阶段耗时 + 每次LLM调用的 token/耗时/server timings。

用法: python profile_pipeline.py c1_famA_lianhua c8_famB_jian
"""
import os, sys, json, time, functools, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kb_rag
from conversations import selected_config
import policy_core.full_flow as ff
import policy_core.full_advice as fa

stage_log = []
records = []


def timed(name):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            t0 = time.monotonic()
            try:
                return fn(*a, **kw)
            finally:
                stage_log.append((name, round(time.monotonic() - t0, 2)))
        return wrapper
    return deco


_BUDGETS=[]
_OrigBudget=ff.Budget
class _RecBudget(_OrigBudget):
    def __init__(self,*a,**kw):
        super().__init__(*a,**kw); _BUDGETS.append(self)
ff.Budget=_RecBudget


def instrument():
    ff.ingest = timed('1_fact_extract')(ff.ingest)
    ff.retrieval_plan = timed('2_retrieval_plan')(ff.retrieval_plan)
    ff.retrieve = timed('3_vector_retrieve')(ff.retrieve)
    ff.load_evidence = timed('4_load_evidence')(ff.load_evidence)
    ff.prioritize = timed('5_rerank_prioritize')(ff.prioritize)
    ff.reason = timed('6_reason_cascade')(ff.reason)
    ff.build_rows = timed('7_build_rows')(ff.build_rows)
    ff.question_plan = timed('8_question_plan')(ff.question_plan)
    fa.generate = timed('9_advice_generate')(fa.generate)
    ff.render = timed('10_render')(ff.render)


def main():
    instrument()
    cases = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'eval', 'cases.json')))
    want = sys.argv[1:] or ['c1_famA_lianhua']
    choice, cfg = selected_config(os.environ.get('PROFILE_PLATFORM','本地Qwen3.6-35B / qwen3.6-35b-a3b'))

    def llm(*a, **kw):
        kw['cfg'] = cfg
        return kb_rag._llm_answer(*a, **kw)

    for cid in want:
        case = next(c for c in cases if c['case_id'] == cid)
        stage_log.clear()
        t0 = time.monotonic()
        body, codes, result = ff.run({}, case['question'], 'profile-' + cid, None, llm)
        wall = round(time.monotonic() - t0, 2)
        print('\n' + '=' * 78)
        print('案例: %s  墙钟=%.1fs  卡片=%d %s' % (cid, wall, len(codes), codes))
        print('-' * 78)
        agg = collections.OrderedDict()
        for name, sec in stage_log:
            agg[name] = agg.get(name, 0) + sec
        total_stage = sum(agg.values())
        for name, sec in agg.items():
            print('  %-22s %7.2fs  %5.1f%%  %s' % (name, sec, 100 * sec / wall, '#' * int(40 * sec / wall)))
        print('  阶段合计 %.2fs / 墙钟 %.2fs (差值为进程与调度开销)' % (total_stage, wall))

        calls = _BUDGETS[-1].calls if _BUDGETS else []
        diag = result.get('diagnostics') or {}
        cov = (result.get('match_json') or {}).get('coverage') or {}
        sched = (cov.get('reasoning_audit') or {}).get('scheduler') or {}
        if sched.get('groups'):
            print('-' * 78)
            print('reason() 并发调度（每组的起止，毫秒相对本轮开始）:')
            for g in sched['groups']:
                st, ed = g.get('started_ms') or 0, g.get('finished_ms') or 0
                print('  group%-2s %6.0f -> %6.0f  (%5.1fs)  来源=%s' % (
                    g.get('index'), st, ed, (ed - st) / 1000.0, g.get('source_ids')))
            print('  已提交组数=%s 最大并发=4' % sched.get('submitted_groups'))
            print('  batches 状态: %s' % [(b.get('status'), len(b.get('attempted_ids') or [])) for b in (cov.get('reasoning_audit') or {}).get('batches', [])])
        print('-' * 78)
        print('LLM 调用明细: 共 %d 次' % len(calls))
        print('  %-34s %4s %9s %9s %9s %8s' % ('purpose', '次数', '输出tok', '输入tok', '耗时s', 'tok/s'))
        byp = collections.OrderedDict()
        for c in calls:
            if not isinstance(c, dict):
                continue
            p = c.get('purpose') or '?'
            d = byp.setdefault(p, dict(n=0, out=0, inp=0, sec=0.0, rate=[]))
            d['n'] += 1
            d['out'] += c.get('completion_tokens') or 0
            d['inp'] += c.get('prompt_tokens') or 0
            d['sec'] += c.get('seconds') or 0
            st = c.get('server_timings') or {}
            if st.get('predicted_per_second'):
                d['rate'].append(st['predicted_per_second'])
        tot_out = tot_in = tot_sec = 0
        for p, d in byp.items():
            rate = round(sum(d['rate']) / len(d['rate']), 1) if d['rate'] else 0
            print('  %-34s %4d %9d %9d %9.1f %8.1f' % (p, d['n'], d['out'], d['inp'], d['sec'], rate))
            tot_out += d['out']; tot_in += d['inp']; tot_sec += d['sec']
        print('  %-34s %4d %9d %9d %9.1f' % ('合计', sum(d['n'] for d in byp.values()), tot_out, tot_in, tot_sec))
        if tot_out and tot_sec:
            print('  → 输出 %.0f tok / LLM累计耗时 %.1fs = 有效 %.1f tok/s（串行等效）' % (tot_out, tot_sec, tot_out / tot_sec))
        print('  → 输入/输出比 %.1f:1' % (tot_in / max(1, tot_out)))
        json.dump(dict(case=cid, wall=wall, stages=stage_log, calls=calls, codes=codes),
                  open('/tmp/profile_%s.json' % cid, 'w'), ensure_ascii=False, indent=1)
        print('  明细已存 /tmp/profile_%s.json' % cid)


if __name__ == '__main__':
    main()
