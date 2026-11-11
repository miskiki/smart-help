"""C3 shadow 双跑：规则路径与旧链路并行，差异只写日志，不影响用户输出。

设计约束：
  * 零侵入：run_shadow 包在 try/except 里，任何异常都不进主链路
  * 零感知：用户看到的 body/卡片完全来自旧链路，shadow 结果只落 JSONL
  * 自动触发懒编译：请求命中尚未编译的县时登记任务（下次就有规则）
  * 开关：环境变量 RULE_SHADOW=0 关闭（默认开启，求值成本 0.1-0.2s）
"""
import json
import os
import re
import time

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'eval', 'shadow_log.jsonl')


def enabled():
    return os.environ.get('RULE_SHADOW', '1') != '0'


def _county(region_value):
    """完整地址 → 县名（去"县"后缀，与 policy_rule.region_scope 口径一致）。"""
    text_value = str(region_value or '')
    county = None
    try:
        from .geo import normalize_address
        county = (normalize_address(text_value) or {}).get('county')
    except Exception:
        county = None
    if not county:
        matches = re.findall(r'[\u4e00-\u9fa5]{2,3}(?:县|区)', text_value)
        county = matches[-1] if matches else ''
    if county and county.endswith(('县', '区')):
        county = county[:-1]
    return county or ''


def _log(entry):
    # RULE_SHADOW=0 的实验/评测请求一律不落盘：规则模式日志（rule_mode）走的是
    # 这个函数而不是 run_shadow，早先没在这里拦，导致评测记录混进生产巡检日志。
    if not enabled():
        return
    try:
        with open(LOG_PATH, 'a', encoding='utf8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass


def run_shadow(hh, rows, session_id, base_seconds=None):
    """对一次生产请求做规则路径复算并记录差异。永远不抛异常。"""
    if not enabled():
        return None
    t0 = time.monotonic()
    try:
        from .facts import state
        st = state(hh)
        fact = st['facts'].get('household:region') or {}
        county = _county(fact.get('value'))
        if not county:
            return None
        from . import rule_eval
        rules = rule_eval.load_rules(county)
        local = [r for r in rules if (r.get('region_scope') or '') not in ('*', '')]
        if len(local) < 20:
            try:
                from . import rule_store
                rule_store.enqueue(county, message='shadow 首次请求触发')
            except Exception:
                pass
            entry = dict(session_id=session_id, ts=time.strftime('%Y-%m-%dT%H:%M:%S'),
                         county=county, state='rules_not_ready', local_rules=len(local))
            _log(entry)
            return entry
        cards, _details = rule_eval.evaluate_family(rules, hh)
        # 待遇级对比：旧链路出县级代码（X-ZB-300），规则路径出省级代码（S-ZB-087），
        # 同一待遇不同代码，必须按名称匹配（与 ab_compare 口径一致）。
        from difflib import SequenceMatcher
        from .rule_eval import _benefit_key

        def same_benefit(a, b):
            ka, kb = _benefit_key(a), _benefit_key(b)
            if not ka or not kb:
                return False
            return ka in kb or kb in ka or SequenceMatcher(None, ka, kb).ratio() >= 0.82

        base_items = [(str(r.get('member') or '').replace('成员', '').strip(), str(r.get('name') or ''), str(r.get('code'))) for r in rows]
        rule_items = [(str(c['member_no'] or '').replace('成员', '').strip(), str(c.get('name') or ''), str(c['code'])) for c in cards]
        # 覆盖式判定（与 ab_compare 口径一致）：规则路径会把省/县同待遇合并成一张卡，
        # 基线 3 张对规则 2 张是合并效应而非召回丢失。基线卡被任意同待遇规则卡覆盖即算命中。
        matched_pairs, only_base, only_rule = [], [], []
        for bi, brow in enumerate(base_items):
            hit = next((n for n in rule_items if brow[0] == n[0] and same_benefit(brow[1], n[1])), None)
            if hit:
                matched_pairs.append((hit[2], brow[0]))
            else:
                only_base.append((brow[2], brow[0]))
        for rcard in rule_items:
            if not any(brow[0] == rcard[0] and same_benefit(rcard[1], brow[1]) for brow in base_items):
                only_rule.append((rcard[2], rcard[0]))
        entry = dict(
            session_id=session_id, ts=time.strftime('%Y-%m-%dT%H:%M:%S'), county=county,
            state='compared', local_rules=len(local),
            base=sorted((i[2], i[0]) for i in base_items),
            rule=sorted((i[2], i[0]) for i in rule_items),
            matched=sorted(matched_pairs),
            only_base=sorted(only_base), only_rule=sorted(only_rule),
            base_seconds=base_seconds, rule_eval_seconds=round(time.monotonic() - t0, 3))
        _log(entry)
        return entry
    except Exception as exc:
        _log(dict(session_id=session_id, ts=time.strftime('%Y-%m-%dT%H:%M:%S'),
                  state='shadow_error', error=type(exc).__name__ + ':' + str(exc)[:160]))
        return None


def summary(path=LOG_PATH, last=200):
    """最近 N 条 shadow 日志的汇总。"""
    if not os.path.exists(path):
        return dict(state='no_log')
    entries = []
    with open(path, encoding='utf8') as fh:
        for line in fh:
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
    entries = entries[-last:]
    compared = [e for e in entries if e.get('state') == 'compared']
    base_total = sum(len(e.get('base') or []) for e in compared)
    rule_total = sum(len(e.get('rule') or []) for e in compared)
    matched_total = sum(len(e.get('matched') or []) for e in compared)
    only_base_total = sum(len(e.get('only_base') or []) for e in compared)
    only_rule_total = sum(len(e.get('only_rule') or []) for e in compared)
    counties = {}
    for e in compared:
        counties.setdefault(e.get('county'), 0)
        counties[e['county']] += 1
    rule_mode = [e for e in entries if e.get('state') == 'rule_mode']
    errors = [e for e in entries if e.get('state') == 'shadow_error']
    pending = [e.get('county') for e in entries if e.get('state') == 'rules_not_ready']
    return dict(
        entries=len(entries), compared=len(compared), counties=counties,
        recall=round(matched_total / base_total, 3) if base_total else None,
        base_cards=base_total, rule_cards=rule_total, matched=matched_total,
        only_base=only_base_total, only_rule=only_rule_total,
        mean_rule_eval=round(sum(e.get('rule_eval_seconds') or 0 for e in compared) / len(compared), 3) if compared else None,
        mean_base_seconds=round(sum(e.get('base_seconds') or 0 for e in compared) / len(compared), 1) if compared else None,
        errors=len(errors), rules_pending=list(dict.fromkeys(pending)),
        rule_mode_requests=len(rule_mode),
        rule_mode_mean_seconds=round(sum(e.get('total_seconds') or 0 for e in rule_mode) / len(rule_mode), 1) if rule_mode else None)
