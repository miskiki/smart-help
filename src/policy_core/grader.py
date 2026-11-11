# -*- coding: utf-8 -*-
"""P2 候选分档器：A/B/C/D + 四类硬拦截 + 排序 + claim。

契约见 docs/redesign/10-p2-match-core-contract.md §6–§9。

设计要点：
- 只有四类硬拦截能把候选判死（evidence_binding / safety_wording / policy_invalid /
  region_explicit_other）；其余旧门（domain / completeness / structural / audience /
  name / 成员类目）的结果降级为 A 档必要条件与扣分项，不再一票否决。
- 档位只由本模块写：调用方（规则路径 / 全模型路径）只提供"已求值原子结果 + 地区状态"。
- 排序稳定：A>B>C，再按地区贴合与已证条件数、再按证据数；用户点名政策（anchor）置顶。
"""
import os

from . import conditions as cond
from . import region

VERSION = 'grade-1'
TIER_A, TIER_B, TIER_C, TIER_D = 'A', 'B', 'C', 'D'
TIERS = (TIER_A, TIER_B, TIER_C, TIER_D)
TIER_ORDER = {TIER_A: 0, TIER_B: 1, TIER_C: 2, TIER_D: 3}
TIER_LABEL = {
    TIER_A: '已核验（政策依据待补齐）',
    TIER_B: '可能符合（非资格结论，需核实）',
    TIER_C: '需补充家庭信息',
    TIER_D: '不适用',
}
INTERCEPTS = ('evidence_binding', 'safety_wording', 'policy_invalid', 'region_explicit_other')

REGION_APPLICABLE = 'applicable'
REGION_PARENT = 'parent_applicable'
REGION_UNKNOWN = 'unknown'
REGION_REFERENCE = 'reference'
REGION_OTHER = 'explicit_other'
REGION_RANK = {REGION_APPLICABLE: 3, REGION_PARENT: 2, REGION_REFERENCE: 1, REGION_UNKNOWN: 1, REGION_OTHER: 0}


def enabled():
    """MATCH_CORE_V2：默认开启；=0 完全回退旧行为（不排序、不写分档）。"""
    return os.environ.get('MATCH_CORE_V2', '1').strip() != '0'


def region_status(policy, detail, household_region):
    """政策地区 vs 家庭地区 → (status, verdict, note)。

    status: applicable / parent_applicable / unknown / reference / explicit_other
    verdict: True(适用) / False(明确不适用) / None(无法确定)
    """
    text = str(household_region or '').strip()
    if not text:
        return dict(status=REGION_UNKNOWN, verdict=None, note='household_region_missing')
    if region.explicit_other(text):
        return dict(status=REGION_OTHER, verdict=False, note='household_explicit_other')
    verdict = region.policy_region_verdict(policy, detail, text)
    if verdict is True:
        scopes = region._policy_scopes((policy or {}).get('regions'))
        detail_region = str((detail or {}).get('region') or '').strip()
        relation = region.region_relation(text, detail_region if detail_region else (scopes[0] if scopes else ''))
        same = relation.get('relation') == 'same'
        return dict(status=REGION_APPLICABLE if same else REGION_PARENT, verdict=True,
                    note=relation.get('note') or '')
    if verdict is False:
        return dict(status=REGION_OTHER, verdict=False, note='policy_region_disjoint')
    return dict(status=REGION_UNKNOWN, verdict=None, note='region_unresolved')


def _collect(results):
    satisfied = [r for r in results or [] if r.get('state') == cond.SATISFIED]
    violated = [r for r in results or [] if r.get('state') == cond.VIOLATED]
    unknown = [r for r in results or [] if r.get('state') == cond.UNKNOWN]
    return satisfied, violated, unknown


def grade_conditions(results, region_info=None, safety_ok=True, policy_valid=True,
                     require_evidence=True, source_gaps=()):
    """核心档位判定。results 是 conditions.evaluate_all 的输出（已求值）。"""
    satisfied, violated, unknown = _collect(results)
    region_info = region_info or dict(status=REGION_UNKNOWN, verdict=None)
    status = region_info.get('status') or REGION_UNKNOWN
    blockers, reason = [], ''
    binding_rejected = any(r.get('binding') == 'rejected' for r in satisfied + unknown)

    if not policy_valid:
        blockers.append('policy_invalid')
    if not safety_ok:
        blockers.append('safety_wording')
    if status == REGION_OTHER:
        blockers.append('region_explicit_other')
    if binding_rejected:
        blockers.append('evidence_binding')

    if [b for b in blockers if b in ('policy_invalid', 'safety_wording', 'region_explicit_other')]:
        # 政策无效 / 口径越界 / 属地明确不符：判死（不出卡，进"为什么没有"）。
        tier, reason = TIER_D, blockers[0]
    elif binding_rejected:
        # 证据绑定失败封顶：已满足的都不算数了，但没有证据不是"不适用"。
        tier, reason = (TIER_B if satisfied else TIER_C), 'evidence_binding_rejected'
    elif violated:
        tier, reason = TIER_D, 'condition_violated'
    elif not satisfied and not unknown:
        tier, reason = TIER_D, 'no_conditions'
    else:
        bound = all(r.get('evidence_bound', True) for r in satisfied) if require_evidence else True
        if satisfied and not unknown and bound and status in (REGION_APPLICABLE, REGION_PARENT):
            tier, reason = TIER_A, 'verified'
        elif satisfied and bound:
            tier, reason = TIER_B, 'unknown_conditions_remain' if unknown else 'region_not_confirmed'
        elif satisfied:
            tier, reason = TIER_B, 'evidence_binding_gap'
        else:
            tier, reason = TIER_C, 'need_household_facts'

        if source_gaps and tier == TIER_A:
            # 政策来源有缺口：不能称"已核验"，但也不是"家庭缺信息"，降到 B 并保留口径。
            tier, reason = TIER_B, 'policy_source_gaps'
    if tier != TIER_A and status == REGION_UNKNOWN:
        reason = reason or 'region_unknown'
    grade = dict(
        tier=tier, reason=reason, blockers=blockers,
        satisfied=len(satisfied), violated=len(violated), unknown=len(unknown),
        missing=cond.missing_facts(results),
        region=status, version=VERSION,
        claims=claims_for(satisfied, unknown, violated, tier, results),
    )
    return grade


def claims_for(satisfied, unknown, violated, tier, results):
    """从已求值原子派生 claim（卡片与回答的唯一事实来源）。"""
    claims = []
    for result in satisfied:
        claims.append(dict(kind='satisfied', condition_id=result.get('condition_id'),
                           field=result.get('field'), text=_claim_text(result, 'satisfied'),
                           actual=result.get('actual'), expected=result.get('value'),
                           evidence=_evidence_of(result), verified_by=cond.EVALUATED_BY,
                           evidence_bound=bool(result.get('evidence_bound', False))))
    for result in unknown:
        if result.get('binding') == 'rejected':
            continue
        claims.append(dict(kind='missing', condition_id=result.get('condition_id'),
                           field=result.get('field'), text=_claim_text(result, 'missing'),
                           missing=result.get('missing') or [], evidence=_evidence_of(result),
                           verified_by=cond.EVALUATED_BY))
    if tier == TIER_D and violated:
        for result in violated:
            claims.append(dict(kind='not_applicable', condition_id=result.get('condition_id'),
                               field=result.get('field'), text=_claim_text(result, 'violated'),
                               actual=result.get('actual'), expected=result.get('value'),
                               verified_by=cond.EVALUATED_BY))
    if tier == TIER_B and satisfied:
        claims.append(dict(kind='possible', text='可能符合（非资格结论），仍有 %d 项条件需核实' % len(unknown),
                           verified_by=cond.EVALUATED_BY))
    return claims


def _claim_text(result, kind):
    field = str(result.get('field') or '')
    label = _field_label(result)
    actual = result.get('actual')
    expected = result.get('value')
    if kind == 'satisfied':
        return '%s：%s（要求 %s）' % (label, _short(actual), _short(expected))
    if kind == 'missing':
        return '%s：待补充' % label
    if kind == 'violated':
        return '%s：%s（要求 %s，不满足）' % (label, _short(actual), _short(expected))
    return field


def _field_label(result):
    return str(result.get('label') or result.get('field') or '')


def _short(value):
    if isinstance(value, (list, tuple)):
        return '/'.join(str(v) for v in value)[:40]
    if value is None:
        return '未提供'
    text = str(value)
    return text[:40]


def _evidence_of(result):
    evidence = result.get('evidence')
    if isinstance(evidence, dict) and evidence.get('quote'):
        return [dict(quote=evidence.get('quote'), clause_id=evidence.get('clause_id'))]
    if isinstance(evidence, list):
        return evidence
    return []


def fit_score(grade, region_info=None, extra=0):
    """排序分：地区贴合 + 已证条件 + 证据完整度，确定性、可解释。"""
    status = (region_info or {}).get('status') or grade.get('region') or REGION_UNKNOWN
    score = REGION_RANK.get(status, 1) * 10
    score += int(grade.get('satisfied') or 0) * 4
    score -= int(grade.get('unknown') or 0) * 1
    score += sum(1 for claim in grade.get('claims') or [] if claim.get('evidence')) * 2
    score += int(extra or 0)
    return score


def sort_rows(rows, pinned=None):
    """按 A>B>C、fit_score、证据数排序；pinned 为用户点名政策 code 集合（置顶）。"""
    pinned = set(pinned or ())

    def key(row):
        tier = row.get('tier') or TIER_C
        code = row.get('code')
        return (0 if code in pinned else 1, TIER_ORDER.get(tier, 3),
                -int(row.get('fit_score') or 0),
                -len(row.get('evidence') or []),
                str(row.get('name') or ''))

    return sorted(list(rows or []), key=key)


def annotate_row(row, grade, region_info=None):
    """把档位写回行（附加字段，不改卡片固定口径文案）。"""
    out = dict(row)
    out['tier'] = grade.get('tier')
    out['tier_label'] = TIER_LABEL.get(grade.get('tier') or '', '')
    out['grade_reason'] = grade.get('reason')
    out['fit_score'] = fit_score(grade, region_info)
    out['claims'] = grade.get('claims') or []
    out['grade'] = dict(tier=grade.get('tier'), reason=grade.get('reason'),
                        notes=grade.get('notes'), blockers=grade.get('blockers') or [],
                        missing_keys=grade.get('missing') or [])
    return out




def clause_text(source):
    """来源原文文本（用于逐字引文绑定）：title/target/conditions/parent_*/benefit/process 拼接。"""
    if isinstance(source, str):
        return source
    if not isinstance(source, dict):
        return ''
    keys = ('title', 'target', 'conditions', 'parent_target', 'parent_conditions',
            'benefit', 'process', 'name', 'detail_target', 'detail_conditions')
    return ' '.join(str(source.get(k) or '') for k in keys)


def _first_quote(evidence):
    if isinstance(evidence, str) and evidence.strip():
        return evidence.strip()
    if isinstance(evidence, (list, tuple)):
        for item in evidence:
            if isinstance(item, dict) and str(item.get('quote') or '').strip():
                return str(item['quote']).strip()
            if isinstance(item, str) and len(item.strip()) >= 4 and ' ' in item.strip():
                return item.strip()
    if isinstance(evidence, dict):
        return str(evidence.get('quote') or '').strip()
    return ''


def grade_rule_card(card, row=None, household_region='', policy_valid=True, source_gaps=()):
    """规则路径适配：card 来自 rule_eval.evaluate_family（已求值），这里只做档位判定。

    规则原子的 evidence 多为离线编译的 clause id（E1/E2），本轮没有逐字引文，
    因此 binding=binding_gap → 最高 B 档（"已满足部分条件，政策依据待补齐"正是 B 的语义）。
    """
    row = dict(row or {})
    clause = clause_text(row)
    results = []
    index = 0
    for item in card.get('satisfied') or []:
        index += 1
        quote = _first_quote(item.get('evidence'))
        atom = cond.make_atom(item.get('field'), item.get('op') or 'eq', item.get('value'),
                              source='rule', condition_id='c%d' % index,
                              evidence={'quote': quote} if quote else None)
        bound = cond.bind_evidence(atom, clause)
        results.append(dict(bound, state=cond.SATISFIED, actual=item.get('actual'),
                            label=str(item.get('field') or ''),
                            evidence=quote_or_ids(item.get('evidence'))))
    for item in card.get('conflicts') or []:
        index += 1
        results.append(dict(cond.make_atom(item.get('field'), 'eq', item.get('value'),
                                           source='rule', condition_id='c%d' % index),
                            state=cond.VIOLATED, actual=item.get('actual'),
                            label=str(item.get('field') or '')))
    for key in card.get('missing') or []:
        index += 1
        results.append(dict(cond.make_atom(str(key).split(':')[-1], 'eq', True, source='rule',
                                           condition_id='c%d' % index),
                            state=cond.UNKNOWN, missing=[str(key)],
                            label=str(key).split(':')[-1]))
    detail_region = str(row.get('detail_region') or row.get('region_scope') or '')
    info = region_status({'regions': detail_region}, {'region': detail_region}, household_region)
    grade = grade_conditions(results, info, policy_valid=policy_valid,
                             source_gaps=source_gaps)
    grade['region_info'] = info
    grade['policy'] = dict(code=row.get('policy_code') or card.get('code'), name=row.get('name') or card.get('name'))
    return grade


def quote_or_ids(evidence):
    """证据列表：引文用 {quote}，只有 clause id 时原样保留（A 档判定会因此降 B）。"""
    if isinstance(evidence, (list, tuple)):
        out = []
        for item in evidence:
            if isinstance(item, dict) and item.get('quote'):
                out.append(dict(quote=item['quote'], clause_id=item.get('clause_id')))
            elif isinstance(item, str):
                out.append(dict(clause_id=item))
        return out
    if isinstance(evidence, dict) and evidence.get('quote'):
        return [dict(quote=evidence['quote'], clause_id=evidence.get('clause_id'))]
    return []


def grade_full_row(row, pack=None, household_region='', safety_ok=True):
    """全模型路径适配：row 来自 reasoning.build_rows（已带 object_matches 引文）。

    对象匹配的引文必须逐字回链 pack['source'] 原文；引文对不上的不能算"已满足"（降 unknown），
    候选最高 B；来源缺口（source_gaps）只降档不改卡片口径。
    """
    row = dict(row or {})
    pack = pack or {}
    decision = row.get('reasoning') or {}
    clause = clause_text(pack.get('source') or row)
    results = []
    index = 0
    quotes = []
    for item in list(decision.get('object_matches') or []) + list(decision.get('matches') or []):
        quote = str(((item or {}).get('citation') or {}).get('quote') or '').strip()
        index += 1
        atom = cond.make_atom('policy_object', 'contains', quote, subject='policy',
                              source='llm_proposal', condition_id='c%d' % index,
                              evidence={'quote': quote} if quote else None,
                              reason=str((item or {}).get('meaning') or ''))
        bound = cond.bind_evidence(atom, clause)
        if not bound.get('evidence_bound'):
            bound['state'] = cond.UNKNOWN
        else:
            bound['state'] = cond.SATISFIED
            quotes.append(dict(quote=quote))
        bound['label'] = str((item or {}).get('meaning') or '原文支持的条件')
        results.append(bound)
    for question in decision.get('unknowns') or []:
        key = str(question.get('fact_key') or '')
        index += 1
        results.append(dict(cond.make_atom(key.split(':')[-1] or 'unknown', 'eq', True,
                                          source='structured', condition_id='c%d' % index),
                            state=cond.UNKNOWN, missing=[key] if key else [],
                            label=str(question.get('requirement') or key)))
    info = region_status(pack.get('policy'), pack.get('detail'), household_region)
    grade = grade_conditions(results, info, safety_ok=safety_ok,
                             source_gaps=row.get('source_gaps') or ())
    grade['region_info'] = info
    grade['policy'] = dict(code=row.get('code'), name=row.get('name'))
    grade['quotes'] = quotes
    return grade


def summary(grades):
    """覆盖度统计（进 coverage['match_core']）。"""
    counts = {tier: 0 for tier in TIERS}
    reasons = {}
    for grade in grades or []:
        counts[grade.get('tier') or TIER_D] = counts.get(grade.get('tier') or TIER_D, 0) + 1
        if grade.get('tier') == TIER_D:
            reason = grade.get('reason') or 'unknown'
            reasons[reason] = reasons.get(reason, 0) + 1
    return dict(counts=counts, d_reasons=reasons, version=VERSION)


def why_not(entries):
    """D 档的"为什么没有"清单：{code, name, member, tier, reason, region}。"""
    out = []
    for entry in entries or []:
        if (entry.get('tier') or TIER_D) != TIER_D:
            continue
        out.append(dict(code=entry.get('code'), name=entry.get('name'),
                        member=entry.get('member'), reason=entry.get('reason') or '',
                        region=entry.get('region'), notes=entry.get('notes') or ''))
    return out
