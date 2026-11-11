# -*- coding: utf-8 -*-
"""C2：离线 A/B——用编译规则 + 确定性求值重算 8 个案例，与基线卡片逐条比对。

用法: python ab_compare.py [--baseline baseline_hh.json]

产出：
  * 每案例：基线卡片 / 规则卡片 / 交集 / 仅基线 / 仅规则
  * 汇总：卡片集合一致率、规则路径新增卡片、丢失卡片、误放行候选
  * 规则路径墙钟（纯求值，不含模型）
"""
import sys, os, json, time, argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import config  # noqa: F401  确保环境与业务一致
from policy_core import rule_eval, compiler
from policy_core.rule_eval import _benefit_key

# 已知误放行（体检阶段确认过的历史误判，用于检查规则路径是否也踩）
KNOWN_FP = {('X-ZB-001', 'c7_famB_lianhua'), ('S-ZB-034', 'c8_famB_jian')}


def key_of(card):
    return (card['code'], str(card.get('member_no') if card.get('member_no') is not None else card.get('member')))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--baseline', default='baseline_hh.json')
    ap.add_argument('--detail', action='store_true')
    a = ap.parse_args()
    baseline = json.load(open(os.path.join(ROOT, 'eval', a.baseline)))
    print('已编译规则统计:', compiler.stats())

    rows = []
    for case in baseline:
        county = case['county']
        if not county:
            print('%-20s 跳过（无地区）' % case['case_id'])
            continue
        hh = dict(policy_core=dict(facts=case['facts'], members=[dict(member_no=m) for m in (case['members'] or ['1'])]))
        t0 = time.monotonic()
        rules = rule_eval.load_rules(county)
        cards, details = rule_eval.evaluate_family(rules, hh)
        elapsed = time.monotonic() - t0

        # 按"待遇"而非政策代码比对：省级与县级同待遇会被合并成一张卡，
        # 直接比代码会把合并误报成召回丢失。
        from difflib import SequenceMatcher
        def same_benefit(a, b):
            ka, kb = _benefit_key(a), _benefit_key(b)
            if not ka or not kb:
                return False
            return ka in kb or kb in ka or SequenceMatcher(None, ka, kb).ratio() >= 0.82

        base = {key_of(c): c for c in case['cards']}
        new = {key_of(c): c for c in cards}
        inter = {(k) for k in base if any(same_benefit(base[k].get('name'), v.get('name')) for v in new.values())}
        only_base = set(base) - inter
        covered = set()
        for k in new:
            if any(same_benefit(new[k].get('name'), base[b].get('name')) for b in base):
                covered.add(k)
        only_new = set(new) - covered
        inter = {k for k in base if k not in only_base}
        fp_hit = [k for k in new if (k[0], case['case_id']) in KNOWN_FP]

        rows.append(dict(case=case['case_id'], county=county, rules=len(rules),
                         base_n=len(base), new_n=len(new), inter=len(inter),
                         only_base=sorted(only_base), only_new=sorted(only_new),
                         fp_hit=fp_hit, seconds=round(elapsed, 2), wall_base=case['wall'],
                         new_cards=[dict(key='%s/%s'%k, **{kk:vv for kk,vv in v.items() if kk!='satisfied'}) for k,v in new.items()], details=details))

        print('\n' + '=' * 80)
        print('%s [%s] 规则%d条  基线卡片%d → 规则卡片%d  墙钟 %.2fs（基线 %.1fs）' % (
            case['case_id'], county, len(rules), len(base), len(new), elapsed, case['wall']))
        print('  交集 %d: %s' % (len(inter), sorted(inter)))
        if only_base:
            print('  仅基线有（规则路径丢失）: %s' % sorted(only_base))
        if only_new:
            print('  仅规则有（规则路径新增）: %s' % sorted(only_new))
        if fp_hit:
            print('  ⚠ 规则路径命中了历史误放行: %s' % fp_hit)
        if a.detail:
            for card in sorted(cards, key=lambda c: c['code']):
                print('    %-10s %-6s %-20s 已满足%d 缺项%d %s%s' % (
                    card['code'], card['member'], (card['name'] or '')[:20],
                    len(card['satisfied']), len(card['missing']),
                    ('合并:' + ','.join(card.get('merged_codes') or [])) if card.get('merged_codes') else '',
                    (' 冲突:' + str(card['conflicts'])) if card['conflicts'] else ''))

    print('\n' + '=' * 80)
    tot_base = sum(r['base_n'] for r in rows)
    tot_new = sum(r['new_n'] for r in rows)
    tot_inter = sum(r['inter'] for r in rows)
    union = tot_base + tot_new - tot_inter
    print('汇总（%d 个有地区案例）' % len(rows))
    print('  基线卡片 %d，规则卡片 %d，交集 %d' % (tot_base, tot_new, tot_inter))
    print('  卡片集合 Jaccard 一致率: %.1f%%' % (100 * tot_inter / max(1, union)))
    print('  规则路径命中历史误放行: %d' % sum(len(r['fp_hit']) for r in rows))
    print('  规则路径平均耗时: %.2fs（纯求值，不含事实抽取）' % (
        sum(r['seconds'] for r in rows) / max(1, len(rows))))
    print('  基线平均耗时: %.1fs' % (sum(r['wall_base'] for r in rows) / max(1, len(rows))))
    json.dump([{k: v for k, v in r.items() if k != 'details'} for r in rows],
              open(os.path.join(ROOT, 'eval', 'ab_result.json'), 'w'), ensure_ascii=False, indent=1, default=str)
    print('\n明细已存 eval/ab_result.json')


if __name__ == '__main__':
    main()
