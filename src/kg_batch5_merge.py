"""Graph-assisted household recall: one row per policy/member/detail, no writes or LLM calls."""
import math
import re
from collections import defaultdict, deque
# dropped 明细上限（超过只计数，不累积）
DROPPED_DETAIL_LIMIT = 200
# 每个(政策,成员)通过明细的软上限
PASSED_DETAIL_CAP = 10
def estimate_budget(n_rows, evidence_chars=120, batch=4, attempts=3, avg_row_chars=None):
    """
    预算估算改用真实 prompt 长度估计。
    avg_row_chars: 单行实际平均字符数（若为 None，用保守估计）
    """
    if avg_row_chars is None:
        # 保守估计：政策名 + 条件 + 对象 + 地区 + 成员 + 证据
        avg_row_chars = 800 + (evidence_chars or 0)
    batches = math.ceil(n_rows / batch) if n_rows > 0 else 0
    return {
        'rows': n_rows,
        'model_calls_min': batches + 1,
        'model_calls': batches * attempts + 1,
        'prompt_chars': n_rows * avg_row_chars + 1500,
        'estimate_note': 'avg_row_chars 为保守估计，实际可能更低',
    }
def merge_candidates(rule_recs, kg_result, hh, data, row_limit=50,
                     kg_cap=20, detail_cap=None, evidence_chars=120):
    """Verify before selecting. Budgets affect only graph additions; rule rows are never truncated.
    Detail limits apply only to pending graph rows per policy/member.
    Verified applicable details are never discarded by detail or row budgets; a soft cap
    (PASSED_DETAIL_CAP=10) keeps at most the top-10 passed details per policy/member to bound
    model rows. Unknown original conditions remain attached in full: never truncate eligibility
    facts to fit a prompt.
    """
    import kg_forbid_v2 as v2
    for name, value in (('row_limit', row_limit), ('kg_cap', kg_cap),
                        ('detail_cap', detail_cap), ('evidence_chars', evidence_chars)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError('Invalid graph budget: ' + name)
    policies = {p['code']: p for p in data.get('policies', [])}
    members = {str(m['member_no']): m for m in hh.get('members', [])}
    raw, dropped = {}, []
    # dropped 只保留前 N 条明细，其余只计数
    drop_counter = defaultdict(int)
    # 第四阶段：地区预筛计数（只排明确不适用；冲突/未知继续核验）
    prefilter_cnt = {'region_excluded': 0, 'region_not_applicable_saved': 0}
    def record_drop(reason, row, extra=None):
        drop_counter[reason] += 1
        if len(dropped) < DROPPED_DETAIL_LIMIT:
            merged = dict(row)
            merged['drop_reason'] = reason
            if extra:
                merged.update(extra)
            dropped.append(merged)
    def insert(code, member_no, did, source, original=None, paths=None, score=0):
        # 成员号缺失时跳过，避免误合并
        if member_no is None or str(member_no).strip() == '':
            record_drop('missing_member_no', {
                'code': code, 'member_no': None, 'detail_id': did, 'source': source,
            }, {'note': '无法解析成员号，已跳过'})
            return
        key = (str(code), str(member_no), str(did))
        if key in raw:
            row = raw[key]
            row['recall_sources'] = sorted(set(row['recall_sources'] + [source]))
            # 浅拷贝 paths，避免 deepcopy 性能损耗
            row['kg_paths'].extend(list(paths or []))
            return
        p = policies.get(code)
        # 未知政策 code 跳过而非崩溃
        if p is None:
            record_drop('unknown_policy', {
                'code': code, 'member_no': member_no, 'detail_id': did, 'source': source,
            }, {'note': '政策库中不存在该 code，已跳过'})
            return
        det = next((d for d in data.get('by_policy', {}).get(p['pid'], [])
                    if str(d.get('detail_id')) == str(did)), None)
        mb = members.get(str(member_no))
        # 用 dict() 浅拷贝替代 deepcopy
        row = dict(original or {})
        tag = '成员%s' % member_no
        if mb and mb.get('name'):
            tag += '（%s）' % mb['name']
        row.update(
            code=code, pid=p['pid'], name=p['name'], member=tag,
            member_no=member_no, detail_id=did, source=source,
            recall_sources=[source],
            kg_paths=list(paths or []),
            kg_score=score,
        )
        d = det or {}
        for field, pfield in (('level', 'level'), ('version', 'version'), ('cat1', 'cat1'),
                              ('cat2', 'cat2'), ('cat3', 'cat3'), ('attr', 'attr'), ('regions', 'regions')):
            row[field] = p.get(pfield) or row.get(field) or ''
        for field in ('target', 'conditions', 'benefit', 'process'):
            row[field] = d.get(field) or p.get(field) or row.get(field) or ''
        row['detail_region'] = d.get('region') or ''
        row['contact'] = ' '.join(str(x) for x in (d.get('contact') or p.get('contact'), d.get('phone')) if x)
        row['basis'] = '；'.join(str(x) for x in (p.get('doc'), row['target'], row['conditions']) if x)
        row.setdefault('reason', '图谱召回，资格按本成员对应政策明细独立核验')
        row.setdefault('matched', [])
        row.setdefault('missing', [])
        row.setdefault('verify', [])
        row['enjoyed'] = any(str(k) in row['name'] for k in hh.get('enjoyed', []) if k)
        # All evidence belongs to this exact member and this policy's detail.
        if det is None:
            # 政策在用但无明细分支：用政策级字段构造核验单元（条件在 policy 层仍可判定），
            # 不因"无明细"直接放弃核验。
            det = {'detail_id': did, 'pid': p.get('pid'), 'region': p.get('regions'),
                   'conditions': p.get('conditions'), 'target': p.get('target'),
                   'age': p.get('age'), 'lab': p.get('lab'), 'health': p.get('health'),
                   'pov': p.get('pov'), 'benefit': p.get('benefit'), 'process': p.get('process')}
            ev = v2.verify_member_detail(p, det, member_no, hh.get('members', []), hh, hh.get('enjoyed', []))
        else:
            # 第四阶段：地区提前筛选——复用统一地区判定（_region_eval），只排除明确不适用的行；
            # 冲突/未知/家庭地区缺失等情形继续走完整核验，保证结果与原 verify 路径一致。
            hh_region = str((hh or {}).get('region') or '').strip()
            if hh_region:
                rflag, rnote = v2._region_eval(p, det, hh_region)
                if rflag == '不适':
                    prefilter_cnt['region_excluded'] += 1
                    ev = {'verdict': '不适用', 'miss': [], 'checks': {'region': False},
                          'note': '地区核验：%s' % rnote, 'mutex': None, 'evidence_text': '',
                          'reason_codes': ['region_conflict']}
                else:
                    ev = v2.verify_member_detail(p, det, member_no, hh.get('members', []), hh, hh.get('enjoyed', []))
            else:
                ev = v2.verify_member_detail(p, det, member_no, hh.get('members', []), hh, hh.get('enjoyed', []))
        if p.get('status') and p['status'] != '在用':
            ev = dict(ev, verdict='不适用', note='政策已停用')
        # 只对需要修改的字段做浅拷贝
        ev = dict(ev)
        if evidence_chars is not None:
            ev['evidence_text'] = ev.get('evidence_text', '')[:evidence_chars]
        flags = v2.collect_unverified_forbid(data.get('checks', []), hh.get('risk', []))
        hits = [f for f in flags if any(c in row['cat1'] or c in row['name'] for c in f['categories'])]
        row['forbid_v2'] = {
            'mode': 'unverified' if hits else 'none',
            'hit': hits,
            'reason': '来源不清 forbid 仅标注，不删除或降权',
            'evidence': ev,
        }
        raw[key] = row
    for r in rule_recs:
        match = re.match(r'成员(\d+)', str(r.get('member') or ''))
        no = r.get('member_no') if r.get('member_no') is not None else (match.group(1) if match else None)
        insert(r['code'], no, r.get('detail_id'), 'rule', original=r)
    for c in (kg_result or {}).get('candidates', []):
        for m in c.get('members', []):
            insert(c['policy_code'], m.get('member_no'), m.get('detail_id'), 'kg',
                   paths=c.get('kg_paths'), score=c.get('score') or 0)
    # ---------- 分类 ----------
    excluded, rules, graph = [], [], []
    for row in raw.values():
        if row['forbid_v2']['evidence']['verdict'] == '不适用':
            excluded.append(row)
        elif 'rule' in row['recall_sources']:
            rules.append(row)
        else:
            graph.append(row)
    def row_order(r):
        return (0 if r['forbid_v2']['evidence']['verdict'] == '通过' else 1,
                -r['kg_score'], str(r['code']), str(r['member_no']), str(r['detail_id']))
    graph.sort(key=row_order)
    # ---------- detail_cap：仅约束待核实行 ----------
    detail_counts = defaultdict(int)
    groups = defaultdict(list)
    for row in graph:
        key = (row['code'], str(row['member_no']))
        passed = row['forbid_v2']['evidence']['verdict'] == '通过'
        if not passed and detail_cap is not None and detail_counts[key] >= detail_cap:
            record_drop('detail_cap', row)
            continue
        if not passed:
            detail_counts[key] += 1
        groups[row['code']].append(row)
    #政策排序用"最优行"而非"第一行"
    def policy_best_verdict(rows):
        priority = {'通过': 0, '待核实': 1, '不适用': 2}
        return min(priority.get(r['forbid_v2']['evidence']['verdict'], 3) for r in rows)
    def policy_order(code):
        rows = groups[code]
        best_verdict = policy_best_verdict(rows)
        best_score = max(r['kg_score'] for r in rows)
        return (best_verdict, -best_score, str(code))
    ranked = sorted(groups, key=policy_order)
    # ---------- policy_cap：规则政策不占图谱配额 ----------
    rule_codes = {r['code'] for r in rules}
    new = 0
    allowed = []
    for code in ranked:
        if code not in rule_codes:
            if kg_cap is not None and new >= kg_cap:
                for r in groups[code]:
                    record_drop('policy_cap', r)
                continue
            new += 1
        allowed.append(code)
    # ---------- round-robin 挑选 ----------
    queues = []
    for code in allowed:
        by_member = defaultdict(deque)
        for row in groups[code]:
            by_member[str(row['member_no'])].append(row)
        q = deque()
        while any(by_member.values()):
            for no in sorted(by_member):
                if by_member[no]:
                    q.append(by_member[no].popleft())
        queues.append(q)
    # ：通过行优先且不受 row_limit 约束；通过明细受 PASSED_DETAIL_CAP 软约束
    passed_rows = []
    pending_rows = []
    while any(queues):
        for q in queues:
            if not q:
                continue
            row = q.popleft()
            if row['forbid_v2']['evidence']['verdict'] == '通过':
                passed_rows.append(row)
            else:
                pending_rows.append(row)
    # 通过明细按 (政策,成员) 软上限约束
    passed_by_key = defaultdict(list)
    for row in passed_rows:
        passed_by_key[(row['code'], str(row['member_no']))].append(row)
    capped_passed = []
    for key, rows in passed_by_key.items():
        if row_limit is not None and len(rows) > PASSED_DETAIL_CAP:
            rows.sort(key=lambda r: -r['kg_score'])
            for r in rows[PASSED_DETAIL_CAP:]:
                record_drop('passed_detail_cap', r,
                            {'note': '通过明细超过上限，仅保留最优%d条' % PASSED_DETAIL_CAP})
            capped_passed.extend(rows[:PASSED_DETAIL_CAP])
        else:
            capped_passed.extend(rows)
    kept = list(rules) + capped_passed
    # row_limit 作为软上限，通过行无条件保留
    if row_limit is not None:
        remaining = max(0, row_limit - len(kept))
    else:
        remaining = len(pending_rows)
    kept.extend(pending_rows[:remaining])
    for r in pending_rows[remaining:]:
        record_drop('row_limit', r)
    for i, row in enumerate(kept):
        row['row_id'] = 'ROW%d' % i
    # ---------- 统计 ----------
    pending = [r for r in kept if r['forbid_v2']['evidence']['verdict'] == '待核实']
    passed_final = [r for r in kept if r['forbid_v2']['evidence']['verdict'] == '通过']
    # judgment_rows 语义明确化
    judgment_rows = len(passed_final) + len(pending)
    stats = {
        'raw_rows': len(raw),
        'row_count': len(kept),
        'rule_rows': len(rules),
        'kg_rows': len(kept) - len(rules),
        'rule_input_rows': len({(r['code'], str(r.get('member')), str(r.get('detail_id'))) for r in rule_recs}),
        'policy_count': len({r['code'] for r in kept}),
        'passed': len(passed_final),
        'pending_rows': len(pending),
        'excluded_rows': len(excluded),
        'dropped_kg_rows': sum(drop_counter.values()),
        'dropped_detail_capped': min(len(dropped), DROPPED_DETAIL_LIMIT),
        'dropped_by_reason': dict(drop_counter),
        'budget_overflow': row_limit is not None and len(kept) > row_limit,
        'passed_detail_capped': len(passed_rows) - len(capped_passed),
        'region_prefiltered': prefilter_cnt['region_excluded'],
    }
    est = estimate_budget(judgment_rows, evidence_chars or 0)
    stats.update(
        model_judgment_rows=judgment_rows,
        evidence_pending_rows=0,  # 已合并到 pending_rows 概念
        model_calls_min=est['model_calls_min'],
        model_calls=est['model_calls'],
        prompt_chars_estimate=est['prompt_chars'],
    )
    return {
        'rows': kept,
        'excluded': excluded,
        'pending': pending,
        'dropped_kg': dropped,
        'stats': stats,
        # missing_info 直接引用（下游不修改即可）
        'missing_info': list((kg_result or {}).get('missing_info', [])),
    }
