"""C4 灰度：规则路径全流程（抽取复用旧链路 → 确定性求值 → 确定性渲染）。

为什么快：旧链路 84-153s 里 76% 是对 7-9 条政策逐条做 LLM 解释与审计（21 次调用）。
规则路径把"解释政策"移到了离线编译，在线只剩 事实抽取(2次调用) + 毫秒级求值，
总耗时 10 秒量级，且求值覆盖该县全部规则（412 条库），不是 7-9 条候选。

红线：卡片 decision 恒为"已满足部分条件，政策依据待补齐"，绝不输出资格结论；
原文未定义口径（unresolved）如实列出；金额只写"原资料记载"。
"""
import os
import time


def _ev(name, **fields):
    """M0 可观测性：规则快速路径同样输出真实进度事件；事件失败绝不影响判定。"""
    try:
        from chain_trace import event as _event
        _event(name, **fields)
    except Exception:
        pass


def enabled():
    """灰度开关：RULE_ENGINE=off 关闭；默认 on（规则未就绪自动回退旧链路）。"""
    return os.environ.get('RULE_ENGINE', 'on') == 'on'


def _county(region_value):
    try:
        from .geo import normalize_address
        county = (normalize_address(str(region_value or '')) or {}).get('county')
    except Exception:
        county = None
    if not county:
        import re
        matches = re.findall(r'[\u4e00-\u9fa5]{2,3}(?:县|区)', str(region_value or ''))
        county = matches[-1] if matches else ''
    if county and county.endswith(('县', '区')):
        county = county[:-1]
    return county or ''


def _label(fields, field):
    definition = fields.get(field) or {}
    return str(definition.get('label') or field)


def _clause_text(rule):
    """政策条目的条件文字（catalog 的 target/conditions 字段）：可作为“政策条目载明”展示，
    但不是来源文档逐字引文，不能标成“原文”。"""
    if not rule:
        return ''
    target = str(rule.get('detail_target') or rule.get('target') or '').strip()
    cond = str(rule.get('conditions') or rule.get('detail_conditions') or '').strip()
    parts = []
    for text in (target, cond):
        if text and text not in parts:
            parts.append(text)
    return '；'.join(parts)[:80]


_ANCHOR_STOPWORDS = ('补贴', '补助', '救助', '保险', '保障', '津贴', '安置', '援助', '扶持',
                     '服务', '管理', '办法', '通知', '标准', '生活', '最低', '临时', '项目',
                     '工作', '发放', '资格', '认定', '申请', '人员', '对象')
_ANCHOR_GENERIC_CHARS = set(''.join(_ANCHOR_STOPWORDS))
_ANCHOR_MIN = 3


def _anchor_rules(text, rules):
    """用户原话点名政策 → 命中的规则行（policy 级锚定）。

    纯机制零模型调用：政策名与用户文本做最长公共子串匹配（≥3 字且非泛词），
    命中一条即锚定该政策全部规则。qa532 实证：规则库明明有雨露计划规则，
    但原子没说全不出卡，用户点名的政策反而被耕地补贴顶掉——锚定后
    追问优先问点名政策的缺项，且点名政策即使无卡也单独成行告知。
    """
    if not text or len(text) < _ANCHOR_MIN:
        return []
    t = str(text)
    anchor_codes = set()
    for row in rules:
        name = str(row.get('name') or '')
        n = len(name)
        best = ''
        for length in range(n, _ANCHOR_MIN - 1, -1):
            for start in range(0, n - length + 1):
                seg = name[start:start + length]
                if seg not in t or seg in _ANCHOR_STOPWORDS:
                    continue
                if all(ch in _ANCHOR_GENERIC_CHARS for ch in seg):
                    continue  # 纯泛词组合（如"补贴金"）不算点名
                if len(seg) > len(best):
                    best = seg
            if best:
                break  # 已找到该政策最长命中，停止降长
        if best:
            anchor_codes.add(row['policy_code'])
    if not anchor_codes:
        return []
    return [r for r in rules if r['policy_code'] in anchor_codes]


def _anchored_rows(anchored_rules, details, fields, card_keys, household_region='', grades_out=None):
    """点名但未出卡的政策 → 信息行（不出资格结论，只说清缺什么）。"""
    rows = []
    seen = set()
    detail_by_key = {}
    for d in details:
        key = (d['code'], d.get('detail_id'), d.get('member_no'))
        cur = detail_by_key.get(key)
        if cur is None or len(d.get('satisfied') or []) > len(cur.get('satisfied') or []):
            detail_by_key[key] = d
    for rule in anchored_rules:
        key = (rule['policy_code'], rule.get('detail_id'))
        if key in card_keys or key in seen:
            continue
        seen.add(key)
        best = None
        for (code, _did, _mno), d in detail_by_key.items():
            if code == rule['policy_code'] and (best is None or len(d.get('satisfied') or []) > len(best.get('satisfied') or [])):
                best = d
        missing_text = []
        if best:
            for mk in (best.get('missing') or []):
                if isinstance(mk, str) and ':' in mk:
                    entity, name = mk.split(':', 1)
                    who = '家庭' if entity == 'household' else '成员' + entity
                    missing_text.append('%s：%s' % (who, _label(fields, name)))
                else:
                    missing_text.append(str(mk))
        satisfied_text = ['%s=%s（家庭事实已证实）' % (_label(fields, a['field']), a.get('actual'))
                          for a in (best.get('satisfied') or [])[:4]]
        rule_json = rule.get('rule') or {}
        benefit = str(rule_json.get('benefits') or '').strip()
        row = dict(
            code=rule['policy_code'], name=rule.get('name') or '', level=rule.get('level') or '',
            version=rule.get('version') or '', member=(best or {}).get('member') or '家庭',
            member_no=(best or {}).get('member_no') if best is not None else 0,
            detail_id=rule.get('detail_id'), detail_region=rule.get('detail_region') or '',
            row_id='%s:%s' % (rule['policy_code'], (best or {}).get('member_no') if best is not None else 0),
            source='rule_engine', analysis_status='ready_rule',
            decision='您点名核验的政策，因家庭事实尚未说全暂不能判定资格',
            reason='您在本轮消息中点名了该政策，已单独核验；其余条件见下方待补充项',
            relevance_evidence=satisfied_text, satisfied=satisfied_text,
            missing=missing_text or ['等待补充家庭事实后继续核验'],
            missing_keys=[],
            source_gaps=[str(x) for x in (rule.get('unresolved') or [])],
            basis='规则编译自《%s》原文条款%s' % (rule.get('name') or rule['policy_code'],
                                             '；含原文未明确口径，以原文为准' if rule.get('unresolved') else ''),
            benefit=(benefit + '（原资料记载；现行口径及实际可领金额未确认）') if benefit else '本库未收录本分支补贴标准',
            process='本库未收录本分支办理流程', contact='',
            verify=['回答下方追问补充家庭事实，系统会优先核验本政策'],
            proof=dict(value=None, missing=[]),
            condition_progress='已点名单独核验；%s' % ('已有%d项事实得到支持' % len(satisfied_text) if satisfied_text else '家庭事实尚不足以判定'),
            source_hash='', qualification_published=False, reasoning=None,
            recall_sources=['rule_engine:user_request'],
            clause_quote=_clause_text(rule),
            merged_codes=[])
        # 点名政策未出卡：同样给档位（多为 C/D），保证排序与"为什么没有"口径一致。
        try:
            from . import grader
            if grader.enabled():
                card = dict(code=rule['policy_code'], detail_id=rule.get('detail_id'),
                            member=(best or {}).get('member') or '家庭',
                            member_no=(best or {}).get('member_no') or 0,
                            satisfied=(best or {}).get('satisfied') or [],
                            missing=(best or {}).get('missing') or [],
                            conflicts=(best or {}).get('conflicts') or [],
                            unresolved=rule.get('unresolved') or [])
                grade = grader.grade_rule_card(card, rule, household_region,
                                               source_gaps=rule.get('unresolved') or ())
                grade['member'] = row.get('member')
                if grades_out is not None:
                    grades_out[grade['policy'].get('code') or rule['policy_code']] = grade
                row = grader.annotate_row(row, grade, grade.get('region_info'))
        except Exception:
            pass
        rows.append(row)
    return rows


def build_rule_rows(cards, rules, fields, grades=None):
    """规则卡片 → 与 build_rows 兼容的行结构（metadata/表格/Excel 不用改）。

    grades: {(code, member_no): grade} 由 grader 产出；有则附加档位字段
    （tier/tier_label/grade_reason/fit_score/claims），不改卡片固定口径文案。
    """
    from . import grader
    by_key = {}
    for row in rules:
        by_key[(row['policy_code'], row.get('detail_id'))] = row
    rows = []
    for card in cards:
        rule = by_key.get((card['code'], card.get('detail_id'))) or {}
        rule_json = rule.get('rule') or {}
        satisfied_text = []
        seen_atoms = set()
        for atom in card['satisfied']:
            actual = atom.get('actual')
            text = '%s=%s（家庭事实已证实）' % (_label(fields, atom['field']), actual)
            if text in seen_atoms:
                continue
            seen_atoms.add(text)
            satisfied_text.append(text)
        missing_text = []
        for key in card['missing']:
            if isinstance(key, str) and ':' in key:
                entity, name = key.split(':', 1)
                who = '家庭' if entity == 'household' else '成员' + entity
                missing_text.append('%s：%s' % (who, _label(fields, name)))
            else:
                missing_text.append(str(key))
        unresolved = [str(x) for x in (card.get('unresolved') or [])]
        benefit = str(rule_json.get('benefits') or '').strip()
        benefit_text = (benefit + '（原资料记载；现行口径及实际可领金额未确认）') if benefit \
            else '本库未收录本分支补贴标准'
        rows.append(dict(
            code=card['code'], name=card['name'], level=rule.get('level') or '',
            version=rule.get('version') or '', member=card['member'], member_no=card['member_no'],
            detail_id=card.get('detail_id'), detail_region=rule.get('detail_region') or '',
            row_id='%s:%s' % (card['code'], card['member_no']), source='rule_engine',
            analysis_status='ready_rule', decision='已满足部分条件，政策依据待补齐',
            reason='', relevance_evidence=satisfied_text, satisfied=satisfied_text,
            missing=missing_text, missing_keys=list(card['missing']),
            source_gaps=unresolved,
            basis='规则编译自《%s》原文条款%s' % (card['name'] or card['code'],
                                             '；含原文未明确口径，以原文为准' if unresolved else ''),
            benefit=benefit_text, process='本库未收录本分支办理流程', contact='',
            verify=['仅补充下方家庭事实；政策资料缺项由系统继续核对'],
            proof=dict(value=None, missing=list(card['missing'])),
            condition_progress='已有%d项事实得到支持，完整资格未确认' % len(card['satisfied']),
            source_hash='', qualification_published=False, reasoning=None,
            recall_sources=['rule_engine'],
            clause_quote=_clause_text(rule),
            merged_codes=list(card.get('merged_codes') or [])))
        grade = (grades or {}).get((card['code'], card['member_no']))
        if grade:
            rows[-1] = grader.annotate_row(rows[-1], grade, grade.get('region_info'))
    return rows


def build_rule_questions(cards, st, fields, max_questions=3, details=None, anchor_codes=None):
    """缺项 → 追问。schema 与 full_questions 完全一致，多轮回答协议不变。

    问题来源三级（qa532 死胡同实证：无卡时旧逻辑恒 0 问题）：
    0. 用户点名的政策（anchor_codes）：追问优先补齐点名政策的缺项——
       用户明确要核验的政策，其缺失事实信息增益最大；
    1. 卡片缺项（出卡政策的待补条件）——原逻辑；
    2. 无卡或卡缺项不足时，从评估明细取"因家庭事实未知而无法判定"的规则
       （no_satisfied_atom / substantive_condition_missing）的缺失字段，
       按阻塞政策条数排序补足——问阻塞最多政策的事实，信息增益最大。
    冲突类与政策侧缺口（conflict/结构门/完整门）不进追问：追问解决不了它们。
    """
    # 规则路径与全模型路径共用同一个轮数上限来源；规则路径只做确定性排序，
    # 不接模型决策。达到上限后不再生成新问题（pending 保留，用于回收本轮回答）。
    from .full_settings import limits
    max_rounds = limits()['max_rounds']
    if st.get('round', 0) >= max_rounds:
        return []
    anchor_codes = set(anchor_codes or ())
    keys = []
    if anchor_codes and details:
        counts = {}
        for d in details:
            if d.get('code') not in anchor_codes:
                continue
            if d.get('status') not in ('no_satisfied_atom', 'substantive_condition_missing'):
                continue
            for key in set(d.get('missing') or []):
                if isinstance(key, str) and ':' in key:
                    counts[key] = counts.get(key, 0) + 1
        for key, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            if key not in keys:
                keys.append(key)
    for card in cards:
        for key in card['missing']:
            if isinstance(key, str) and ':' in key and key not in keys:
                keys.append(key)
    if details and len(keys) < max_questions:
        counts = {}
        for d in details:
            if d.get('status') not in ('no_satisfied_atom', 'substantive_condition_missing'):
                continue
            for key in set(d.get('missing') or []):
                if isinstance(key, str) and ':' in key:
                    counts[key] = counts.get(key, 0) + 1
        for key, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            if key not in keys:
                keys.append(key)
    questions = []
    for key in keys:
        if key in st.get('asked', {}) or st['facts'].get(key, {}).get('state') == 'known':
            continue
        entity, name = key.split(':', 1)
        definition = fields.get(name)
        if not definition:
            continue
        options = (['是', '否', '不清楚', '暂时无法提供'] if definition['type'] == 'boolean'
                   else list(definition.get('values', [])))
        if options and '不清楚' not in options:
            options += ['不清楚', '暂时无法提供']
        questions.append(dict(
            key='core|' + entity + '|' + name, fact_key=key,
            label=('家庭：' if entity == 'household' else '成员' + entity + '：') + definition['question'],
            reason='用于核实政策明确要求的家庭事实；尚未确认政策资格',
            kind='select' if options else 'text', options=options, value='',
            definition=definition, conflict=False))
        if len(questions) >= max_questions:
            break
    if questions:
        st['round'] = st.get('round', 0) + 1
        for q in questions:
            q.update(round=st['round'], max_rounds=max_rounds)
        st['pending'] = questions
    return questions


def render_rule_mode(rows, questions, coverage):
    """规则路径正文：固定开场与免责句只在会话首轮出现（coverage['round']），之后直接给结论与待补项；
    已答“不清楚/暂时无法提供”的字段不再作为新的补充要求重述，只在正文说明一次。
    P3-B：ANSWER_STYLE=claims（默认）且有卡片时，改用 claim 驱动三段式。"""
    from .result_model import DISCLAIMER, unclear_note, why_not_lines
    from . import claim_answer
    if claim_answer.enabled() and rows:
        intro = []
        if coverage.get('region_status') == 'unknown':
            intro.append('家庭所在地区尚未确认，请补充县区信息后可扩大核验范围。')
        if (coverage.get('rule_engine') or {}).get('readiness') == 'ready_province_only':
            intro.append('该县区在政策库中暂无县级政策，本轮按省级通用政策核验；'
                         '县级政策补充入库后会自动纳入。')
        first = int(coverage.get('round') or 1) <= 1
        if first:
            intro.append('本次按你已确认的家庭事实，逐条核对了本地政策库中该地区适用的政策。')
        anchor = coverage.get('rule_anchor')
        if anchor:
            shown = '、'.join(anchor[:4])
            if len(anchor) > 4:
                shown += '等 %d 项' % len(anchor)
            intro.append('你点名核验：%s——已优先纳入本次核验；补上下面的信息就能给出结论。' % shown)
        return claim_answer.answer(rows, questions, coverage, intro=intro)
    first = int(coverage.get('round') or 1) <= 1
    unclear = coverage.get('unclear_facts') or {}
    parts = []
    if first:
        parts.append('本次按你已确认的家庭事实，逐条核对了本地政策库中该地区适用的政策。')
    anchor = coverage.get('rule_anchor')
    if anchor:
        parts.append('你点名核验：%s——已排在卡片最前；补上下面的信息就能给出结论。' % '、'.join(anchor))
    if parts:
        parts.append('')
    for row in rows:
        parts.append('- **%s**（%s）' % (row['name'] or row['code'], row['member']))
        if row['satisfied']:
            parts.append('  - 已满足：' + '；'.join(row['satisfied']))
        missing = [x for x in (row.get('missing') or []) if x and x not in unclear]
        if missing:
            parts.append('  - 待补充：' + '；'.join(missing))
        if row['source_gaps']:
            parts.append('  - 政策原文未明确的口径：' + '；'.join(row['source_gaps'][:3]))
        parts.append('  - 判定：%s　%s' % (row['decision'], row['benefit']))
    why = why_not_lines(coverage)
    if why:
        parts.extend(['', '\n'.join(why)])
    note = unclear_note(rows, coverage)
    if note:
        parts.extend(['', note])
    parts.append('')
    if questions:
        parts.append('还需要补充下面几项；不确定就直接选“不清楚”，系统不会替您猜。')
        for q in questions:
            parts.append('- %s' % q['label'])
    else:
        parts.append('需要补充的信息已经问完；补充其他家庭情况，可以继续核验更多政策。')
    if first:
        parts.extend(['', DISCLAIMER + '政策原文未写明的条款、现行标准和实际待遇不作推测。'])
    if coverage.get('region_status') == 'unknown':
        parts.insert(0, '家庭所在地区尚未确认，请补充县区信息后可扩大核验范围。')
    if (coverage.get('rule_engine') or {}).get('readiness') == 'ready_province_only':
        parts.insert(0, '该县区在政策库中暂无县级政策，本轮按省级通用政策核验；'
                        '县级政策补充入库后会自动纳入。')
    return '\n'.join(parts)


def _apply_scope_rows(rows, coverage, household_region):
    """POLICY_SCOPE 开启时用 policy_scope 结构化范围过滤规则卡片（默认关闭）。

    与全模型路径同一口径：True 保留、False 丢弃（明确不适用）、None 保留待核、
    无 scope 行保留；异常退回原行集合，绝不因此丢卡。
    """
    if not rows:
        return rows
    try:
        from . import scope_filter
        if not scope_filter.enabled():
            return rows
        report = scope_filter.filter_codes([r.get('code') for r in rows], household_region or '')
        dropped = {item['code'] for item in report['dropped']}
        kept = [r for r in rows if r.get('code') not in dropped]
        report['input'] = len(rows)
        report['output'] = len(kept)
        coverage['scope_filter'] = scope_filter.summary(report)
        _ev('scope.filter', kept=coverage['scope_filter']['kept'],
            dropped=coverage['scope_filter']['dropped'],
            unknown=coverage['scope_filter']['unknown'],
            input=coverage['scope_filter']['input'], output=coverage['scope_filter']['output'])
        return kept
    except Exception as exc:
        coverage['scope_filter'] = dict(state='error', error=type(exc).__name__ + ':' + str(exc)[:120])
        return rows


def try_rule_run(hh, st, fields, coverage, model, session_id, answers=None, text=''):
    """规则模式入口。返回 (body, codes, result)；规则未就绪或出错返回 None（回退旧链路）。"""
    from . import rule_eval
    region = st['facts'].get('household:region', {})
    county = _county(region.get('value'))
    if not county:
        return None
    rules = rule_eval.load_rules(county)
    # 可用性判定：县级规则够 20 条直接 ready；县级政策本来就没有的县区（编译任务
    # 已完成且总数 0），用省级规则出结果，不再每次回退 170-240s 全模型链路。
    from . import rule_readiness
    job = None
    try:
        from . import rule_store
        job = rule_store.job(county)
    except Exception:
        job = None
    readiness = rule_readiness.assess(rules, job)
    _ev('rule.engine.loaded', rules=len(rules), local_rules=readiness['local'],
        readiness=readiness['state'])
    if not readiness['ready']:
        try:
            rule_store.enqueue(county, message='rule_mode 首次请求触发')
        except Exception:
            pass
        coverage['rule_engine'] = dict(state='rules_not_ready', county=county,
                                       local_rules=readiness['local'],
                                       readiness=readiness['state'])
        _ev('rule.not_ready', county=county, local_rules=readiness['local'],
            readiness=readiness['state'])
        return None
    t0 = time.monotonic()
    cards, details = rule_eval.evaluate_family(rules, hh, fields)
    eval_seconds = round(time.monotonic() - t0, 3)
    _ev('rule.evaluate', rules=len(rules), cards=len(cards), seconds=eval_seconds)
    # P2 匹配核心 v2：条件原子三值求值 → A/B/C/D 分档。开关关闭时完全不参与，回退旧排序/文案。
    from . import grader
    match_on = grader.enabled()
    household_region = str((st['facts'].get('household:region') or {}).get('value') or '')
    grades_by_card = {}
    if match_on:
        g0 = time.monotonic()
        rule_by_key = {(r['policy_code'], r.get('detail_id')): r for r in rules}
        for card in cards:
            grade = grader.grade_rule_card(card, rule_by_key.get((card['code'], card.get('detail_id'))),
                                           household_region)
            grade['member'] = card.get('member')
            grades_by_card[(card['code'], card['member_no'])] = grade
        grade_seconds = round(time.monotonic() - g0, 3)
    anchored = _anchor_rules(text, rules)
    anchor_codes = {r['policy_code'] for r in anchored}
    rows = build_rule_rows(cards, rules, fields, grades=grades_by_card)
    card_keys = {(c['code'], c.get('detail_id')) for c in cards}
    anchored_grades = {}
    if anchored:
        rows = _anchored_rows(anchored, details, fields, card_keys, household_region,
                              grades_out=anchored_grades) + rows
        anchor_names = sorted({r.get('name') or r['policy_code'] for r in anchored})
        coverage['rule_anchor'] = anchor_names
    if match_on:
        # 档位优先、点名政策置顶、同档按 fit_score/证据数排序（确定性，无模型参与）。
        rows = grader.sort_rows(rows, pinned=anchor_codes)
        hidden_d = [r for r in rows if r.get('tier') == grader.TIER_D]
        rows = [r for r in rows if r.get('tier') != grader.TIER_D]  # D 档不出卡，原因进 why_not
    elif anchored:
        rows.sort(key=lambda r: 0 if r['code'] in anchor_codes else 1)  # 点名政策置顶，组内保持原序
    from .result_model import dedupe_rows, mark_disclaimer, unclear_fact_map
    rows = dedupe_rows(rows)  # 同名政策多条来源只留一张卡（政策库跨层级重名很多）
    rows = _apply_scope_rows(rows, coverage, household_region)
    _ev('rule.cards', rows=len(rows))
    for _row in rows[:20]:
        _ev('rule.card', policy_code=_row.get('code'), member=_row.get('member') or '',
            count=len(_row.get('satisfied') or []))
    questions = build_rule_questions(cards, st, fields, details=details, anchor_codes=anchor_codes)
    _ev('rule.questions', question_count=len(questions))
    coverage.update(rule_engine=dict(state='used', county=county, rules=len(rules),
                                     cards=len(rows), eval_seconds=eval_seconds,
                                     readiness=readiness['state'],
                                     local_rules=readiness['local'],
                                     province_rules=readiness['province']),
                    scope='规则引擎按地区适用政策逐条核验；不等于逐条资格核验')
    if match_on:
        grades = list(grades_by_card.values()) + list(anchored_grades.values())
        coverage['match_core'] = dict(grader.summary(grades), hidden_d=len(hidden_d))
        # D 档"为什么没有"必须从被隐藏的行取（点名政策也走 anchor 合成行，不在 grades_by_card 里）；
        # 同一政策的多个明细/来源会重复，按 (code, member, reason) 去重。
        _entries, _seen = [], set()
        for r in hidden_d:
            key = (r.get('code'), r.get('member'), r.get('grade_reason') or '')
            if key in _seen:
                continue
            _seen.add(key)
            _entries.append(dict(code=r.get('code'), name=r.get('name'), member=r.get('member'),
                                 tier=r.get('tier'), reason=r.get('grade_reason') or ''))
        coverage['why_not'] = grader.why_not(_entries)[:8]
        _counts = coverage['match_core']['counts']
        # 事件在分档全部完成后发，计数与 coverage['match_core']/页面快照同源（含点名政策合成行）。
        _ev('match.graded', cards=len(rows), a=_counts.get('A'), b=_counts.get('B'),
            c=_counts.get('C'), d=_counts.get('D'), seconds=grade_seconds, version=grader.VERSION)
    coverage['unclear_facts'] = unclear_fact_map(st, fields)
    mark_disclaimer(st, coverage)  # 固定开场与免责句只在会话首轮出现
    body = render_rule_mode(rows, questions, coverage)
    _ev('answer.compose', seconds=round(time.monotonic() - model.started, 2), chars=len(body))
    diagnostics = dict(calls=model.calls, seconds=round(time.monotonic() - model.started, 2),
                       extraction=coverage.get('fact_extraction'), source_analysis=[],
                       question_plan=st.get('question_plan_status'))
    from .result_model import metadata
    result = metadata(hh, rows, questions, coverage, [], diagnostics, dict(status='rule_engine'))
    result['session_id'] = session_id
    try:
        from .shadow import _log
        _log(dict(session_id=session_id, ts=time.strftime('%Y-%m-%dT%H:%M:%S'),
                  county=county, state='rule_mode', cards=len(rows),
                  questions=len(questions), eval_seconds=eval_seconds,
                  total_seconds=diagnostics['seconds'], calls=model.calls))
    except Exception:
        pass
    return body, sorted({r['code'] for r in rows}), result
