# -*- coding: utf-8 -*-
"""P2 分档器单测：A/B/C/D 映射、四类硬拦截、地区维度化、claim、排序与统计。

覆盖 docs/redesign/10-p2-match-core-contract.md §6–§9。不连生产库、不调用模型。
"""
import os
import unittest
from contextlib import contextmanager

from policy_core import conditions as cond
from policy_core import grader


@contextmanager
def match_env(value):
    saved = os.environ.get('MATCH_CORE_V2')
    try:
        if value is None:
            os.environ.pop('MATCH_CORE_V2', None)
        else:
            os.environ['MATCH_CORE_V2'] = value
        yield
    finally:
        if saved is None:
            os.environ.pop('MATCH_CORE_V2', None)
        else:
            os.environ['MATCH_CORE_V2'] = saved


def result(field, state, **kw):
    out = dict(condition_id=kw.pop('condition_id', 'c1'), field=field, state=state,
               evidence_bound=kw.pop('evidence_bound', True), label=kw.pop('label', field))
    out.update(kw)
    return out


REGION_OK = dict(status=grader.REGION_APPLICABLE, verdict=True)
REGION_PARENT = dict(status=grader.REGION_PARENT, verdict=True)
REGION_UNKNOWN = dict(status=grader.REGION_UNKNOWN, verdict=None)
REGION_OTHER = dict(status=grader.REGION_OTHER, verdict=False)


class EnabledTest(unittest.TestCase):
    def test_default_on(self):
        with match_env(None):
            self.assertTrue(grader.enabled())

    def test_switch_off(self):
        with match_env('0'):
            self.assertFalse(grader.enabled())


class RegionStatusTest(unittest.TestCase):
    def test_missing_household_region_is_unknown(self):
        self.assertEqual(grader.region_status({'regions': '莲花县'}, {'region': '莲花县'}, '')['status'],
                         grader.REGION_UNKNOWN)

    def test_same_county_is_applicable(self):
        info = grader.region_status({'regions': '莲花县'}, {'region': '莲花县'}, '江西省莲花县')
        self.assertEqual(info['status'], grader.REGION_APPLICABLE)
        self.assertIs(info['verdict'], True)

    def test_province_policy_is_parent_applicable(self):
        info = grader.region_status({'regions': '江西省'}, {'region': ''}, '江西省莲花县')
        self.assertEqual(info['status'], grader.REGION_PARENT)

    def test_other_county_policy_is_explicit_other(self):
        info = grader.region_status({'regions': '吉安县'}, {'region': '吉安县'}, '江西省莲花县')
        self.assertEqual(info['status'], grader.REGION_OTHER)
        self.assertIs(info['verdict'], False)

    def test_outside_province_household_is_explicit_other(self):
        info = grader.region_status({'regions': '江西省'}, {'region': ''}, '湖南省长沙市')
        self.assertEqual(info['status'], grader.REGION_OTHER)


class GradeConditionsTest(unittest.TestCase):
    def test_all_satisfied_and_bound_is_a(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED), result('disabled', cond.SATISFIED)], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_A)
        self.assertEqual(grade['reason'], 'verified')

    def test_satisfied_with_unknown_is_b(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED), result('region', cond.UNKNOWN)], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_B)
        self.assertEqual(grade['reason'], 'unknown_conditions_remain')

    def test_no_satisfied_only_unknown_is_c(self):
        grade = grader.grade_conditions([result('age', cond.UNKNOWN)], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_C)
        self.assertEqual(grade['reason'], 'need_household_facts')

    def test_violated_is_d(self):
        grade = grader.grade_conditions([result('age', cond.VIOLATED)], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_D)
        self.assertEqual(grade['reason'], 'condition_violated')

    def test_region_explicit_other_is_d_blocker(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED)], REGION_OTHER)
        self.assertEqual(grade['tier'], grader.TIER_D)
        self.assertIn('region_explicit_other', grade['blockers'])

    def test_unknown_region_caps_at_b(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED)], REGION_UNKNOWN)
        self.assertEqual(grade['tier'], grader.TIER_B)
        self.assertEqual(grade['reason'], 'region_not_confirmed')

    def test_binding_gap_caps_at_b(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED, evidence_bound=False)], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_B)
        self.assertEqual(grade['reason'], 'evidence_binding_gap')

    def test_rejected_binding_caps_at_b_and_flags_blocker(self):
        rejected = result('age', cond.UNKNOWN)
        rejected['binding'] = 'rejected'
        grade = grader.grade_conditions([result('disabled', cond.SATISFIED), rejected], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_B)
        self.assertEqual(grade['reason'], 'evidence_binding_rejected')
        self.assertIn('evidence_binding', grade['blockers'])

    def test_rejected_binding_without_verified_support_is_c(self):
        rejected = result('age', cond.UNKNOWN)
        rejected['binding'] = 'rejected'
        grade = grader.grade_conditions([rejected], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_C)

    def test_policy_invalid_is_d(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED)], REGION_OK, policy_valid=False)
        self.assertEqual(grade['tier'], grader.TIER_D)
        self.assertIn('policy_invalid', grade['blockers'])

    def test_safety_wording_is_d(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED)], REGION_OK, safety_ok=False)
        self.assertEqual(grade['tier'], grader.TIER_D)
        self.assertIn('safety_wording', grade['blockers'])

    def test_source_gaps_downgrade_a_to_b(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED)], REGION_OK,
                                        source_gaps=['现行标准未核齐'])
        self.assertEqual(grade['tier'], grader.TIER_B)
        self.assertEqual(grade['reason'], 'policy_source_gaps')

    def test_no_conditions_is_d(self):
        grade = grader.grade_conditions([], REGION_OK)
        self.assertEqual(grade['tier'], grader.TIER_D)
        self.assertEqual(grade['reason'], 'no_conditions')

    def test_missing_keys_collected(self):
        grade = grader.grade_conditions([result('age', cond.UNKNOWN, missing=['1:age'])], REGION_OK)
        self.assertEqual(grade['missing'], ['1:age'])


class ClaimsTest(unittest.TestCase):
    def test_satisfied_claim_carries_evidence_and_verifier(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED, actual=62, value=60,
                                                evidence={'quote': '年满60周岁'})], REGION_OK)
        claim = [c for c in grade['claims'] if c['kind'] == 'satisfied'][0]
        self.assertIn('age', claim['text'])
        self.assertEqual(claim['evidence'][0]['quote'], '年满60周岁')
        self.assertEqual(claim['verified_by'], cond.EVALUATED_BY)

    def test_missing_claim(self):
        grade = grader.grade_conditions([result('age', cond.UNKNOWN, missing=['1:age'])], REGION_OK)
        claim = [c for c in grade['claims'] if c['kind'] == 'missing'][0]
        self.assertIn('待补充', claim['text'])

    def test_possible_claim_for_b(self):
        grade = grader.grade_conditions([result('age', cond.SATISFIED), result('x', cond.UNKNOWN)], REGION_OK)
        kinds = [c['kind'] for c in grade['claims']]
        self.assertIn('possible', kinds)

    def test_rejected_binding_claim_filtered(self):
        rejected = result('age', cond.UNKNOWN)
        rejected['binding'] = 'rejected'
        grade = grader.grade_conditions([result('disabled', cond.SATISFIED), rejected], REGION_OK)
        ids = [c.get('condition_id') for c in grade['claims'] if c['kind'] == 'missing']
        self.assertNotIn('c1', ids)
        self.assertEqual(grade['tier'], grader.TIER_B)


class FitAndSortTest(unittest.TestCase):
    def test_fit_score_prefers_applicable_region_and_satisfied(self):
        base = grader.grade_conditions([result('a', cond.SATISFIED), result('b', cond.UNKNOWN)], REGION_OK)
        weak = grader.grade_conditions([result('a', cond.SATISFIED), result('b', cond.UNKNOWN)], REGION_UNKNOWN)
        self.assertGreater(grader.fit_score(base, REGION_OK), grader.fit_score(weak, REGION_UNKNOWN))

    def test_sort_orders_by_tier_then_score(self):
        rows = [
            dict(code='p1', name='b', tier='B', fit_score=10),
            dict(code='p2', name='a', tier='A', fit_score=5),
            dict(code='p3', name='c', tier='C', fit_score=30),
        ]
        ordered = [row['code'] for row in grader.sort_rows(rows)]
        self.assertEqual(ordered, ['p2', 'p1', 'p3'])

    def test_pinned_policy_stays_on_top(self):
        rows = [
            dict(code='p1', name='a', tier='A', fit_score=30),
            dict(code='p2', name='b', tier='C', fit_score=1),
        ]
        self.assertEqual(grader.sort_rows(rows, pinned={'p2'})[0]['code'], 'p2')

    def test_sort_is_stable_for_equal_keys(self):
        rows = [dict(code='p%d' % i, name='n%d' % i, tier='B', fit_score=5) for i in range(5)]
        ordered = [row['code'] for row in grader.sort_rows(rows)]
        self.assertEqual(ordered, ['p0', 'p1', 'p2', 'p3', 'p4'])


class AnnotateAndSummaryTest(unittest.TestCase):
    def test_annotate_row_keeps_decision_text(self):
        row = dict(code='p1', name='n', decision='已满足部分条件，政策依据待补齐')
        grade = grader.grade_conditions([result('a', cond.SATISFIED)], REGION_OK)
        out = grader.annotate_row(row, grade, REGION_OK)
        self.assertEqual(out['decision'], row['decision'])
        self.assertEqual(out['tier'], grader.TIER_A)
        self.assertIn('claims', out)

    def test_summary_counts(self):
        grades = [grader.grade_conditions([result('a', cond.SATISFIED)], REGION_OK),
                  grader.grade_conditions([result('a', cond.SATISFIED), result('b', cond.UNKNOWN)], REGION_OK),
                  grader.grade_conditions([result('a', cond.VIOLATED)], REGION_OK)]
        out = grader.summary(grades)
        self.assertEqual(out['counts']['A'], 1)
        self.assertEqual(out['counts']['B'], 1)
        self.assertEqual(out['counts']['D'], 1)
        self.assertEqual(out['d_reasons']['condition_violated'], 1)

    def test_why_not_only_d(self):
        entries = [
            dict(code='p1', name='n1', tier='D', reason='condition_violated', member='成员1'),
            dict(code='p2', name='n2', tier='B', reason='unknown', member='成员1'),
        ]
        out = grader.why_not(entries)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['code'], 'p1')
        self.assertEqual(out[0]['reason'], 'condition_violated')


class RuleAdapterTest(unittest.TestCase):
    CARD = dict(code='X-ZB-300', name='困难残疾人生活补贴', member='成员1', member_no='1',
                satisfied=[dict(field='disabled', op='eq', value=True, actual=True, evidence=['E1'])],
                missing=['1:age'], conflicts=[], unresolved=[])

    def test_rule_card_without_quote_is_b(self):
        grade = grader.grade_rule_card(self.CARD, dict(policy_code='X-ZB-300', name='困难残疾人生活补贴',
                                                       region_scope='莲花县', detail_region='莲花县'),
                                       '江西省莲花县')
        self.assertEqual(grade['tier'], grader.TIER_B)
        self.assertEqual(grade['reason'], 'evidence_binding_gap')

    def test_rule_card_conflict_is_d(self):
        card = dict(self.CARD, conflicts=[dict(field='age', value=60, actual=62)])
        grade = grader.grade_rule_card(card, dict(policy_code='X-ZB-300', region_scope='莲花县'),
                                       '江西省莲花县')
        self.assertEqual(grade['tier'], grader.TIER_D)

    def test_rule_card_other_region_is_d(self):
        grade = grader.grade_rule_card(self.CARD, dict(policy_code='X-ZB-300', region_scope='吉安县',
                                                       detail_region='吉安县'), '江西省莲花县')
        self.assertEqual(grade['tier'], grader.TIER_D)


class FullRowAdapterTest(unittest.TestCase):
    ROW = dict(code='S-ZB-006', name='政策A', analysis_status='partial_supported', missing_keys=[],
               source_gaps=[], reasoning=dict(object_matches=[dict(meaning='家庭成员残疾',
                                                                  citation={'quote': '持有残疾人证'})],
                                               unknowns=[], matches=[]))

    PACK = dict(policy=dict(code='S-ZB-006', name='政策A', regions='江西省'),
                detail=dict(region='', target='', conditions=''),
                source=dict(title='政策A', target='残疾人', conditions='持有残疾人证，可申请补贴',
                            parent_target='', parent_conditions='', benefit='', process=''))

    def test_bound_quote_with_no_unknowns_is_a(self):
        grade = grader.grade_full_row(self.ROW, self.PACK, '江西省莲花县')
        self.assertEqual(grade['tier'], grader.TIER_A)

    def test_unbound_quote_downgrades(self):
        row = dict(self.ROW, reasoning=dict(object_matches=[dict(meaning='家庭成员残疾',
                                                                citation={'quote': '一二级残疾人'})],
                                           unknowns=[], matches=[]))
        grade = grader.grade_full_row(row, self.PACK, '江西省莲花县')
        self.assertIn(grade['tier'], ('B', 'C'))

    def test_source_gaps_downgrade_to_b(self):
        grade = grader.grade_full_row(dict(self.ROW, source_gaps=['现行标准未核齐']), self.PACK, '江西省莲花县')
        self.assertEqual(grade['tier'], grader.TIER_B)
        self.assertEqual(grade['reason'], 'policy_source_gaps')

    def test_unknown_region_caps_at_b(self):
        grade = grader.grade_full_row(self.ROW, self.PACK, '')
        self.assertEqual(grade['tier'], grader.TIER_B)


class ClauseTextTest(unittest.TestCase):
    def test_joins_source_fields(self):
        text = grader.clause_text(dict(title='补贴', target='残疾人', conditions='持有残疾人证'))
        for part in ('补贴', '残疾人', '持有残疾人证'):
            self.assertIn(part, text)


if __name__ == '__main__':
    unittest.main()
