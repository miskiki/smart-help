# -*- coding: utf-8 -*-
"""P2 条件原子层单测：三值求值、算子、AND/OR/NOT 传播、证据逐字绑定。

覆盖 docs/redesign/10-p2-match-core-contract.md §3–§5。
标准库 unittest；不连生产库、不调用模型。
"""
import unittest

from policy_core import conditions
from policy_core.conditions import SATISFIED, VIOLATED, UNKNOWN, NOT_APPLICABLE

FIELDS = {
    'age': {'type': 'number', 'scope': 'member', 'label': '年龄'},
    'disability_grade': {'type': 'text', 'scope': 'member', 'label': '残疾等级',
                         'values': ['一级', '二级', '三级', '四级']},
    'disabled': {'type': 'boolean', 'scope': 'member', 'label': '是否残疾'},
    'dibao': {'type': 'boolean', 'scope': 'household', 'label': '是否低保'},
    'enrolled': {'type': 'boolean', 'scope': 'member', 'label': '是否在读'},
    'study_stage': {'type': 'text', 'scope': 'member', 'label': '学段',
                    'values': ['学前教育', '小学', '初中', '高中', '中职', '本专科', '义务教育']},
}


def facts(**kw):
    """用 m1__age=62 这类键构造 fact view（m1 表示成员 1）；None 表示未知。"""
    out = {}
    for key, value in kw.items():
        entity, field = key.split('__', 1)
        if entity.startswith('m') and entity[1:].isdigit():
            entity = entity[1:]
        out[entity + ':' + field] = {'state': 'unknown' if value is None else 'known', 'value': value}
    return out


class MakeAtomTest(unittest.TestCase):
    def test_defaults(self):
        atom = conditions.make_atom('age', 'gte', 60, condition_id='c1')
        self.assertEqual(atom['subject'], 'member')
        self.assertEqual(atom['source'], 'rule')
        self.assertNotIn('evaluated_by', atom)  # 只有求值器能写 evaluated_by
        self.assertNotIn('state', atom)

    def test_llm_proposal_without_quote_is_invalid(self):
        atom = conditions.make_atom('disabled', 'eq', True, source='llm_proposal')
        ok, reason = conditions.validate(atom)
        self.assertFalse(ok)
        self.assertEqual(reason, 'missing_evidence_quote')

    def test_unknown_operator_rejected(self):
        atom = conditions.make_atom('age', 'approx', 60)
        ok, reason = conditions.validate(atom)
        self.assertFalse(ok)
        self.assertEqual(reason, 'unsupported_op')

    def test_unknown_source_rejected(self):
        atom = conditions.make_atom('age', 'gte', 60, source='guess')
        ok, reason = conditions.validate(atom)
        self.assertFalse(ok)
        self.assertEqual(reason, 'unsupported_source')


class EvaluateAtomTest(unittest.TestCase):
    def test_number_gte_satisfied(self):
        result = conditions.evaluate_atom(
            conditions.make_atom('age', 'gte', 60),
            facts(m1__age=62), FIELDS, member='1')
        self.assertEqual(result['state'], SATISFIED)
        self.assertEqual(result['actual'], 62)

    def test_number_gte_violated(self):
        result = conditions.evaluate_atom(
            conditions.make_atom('age', 'gte', 60),
            facts(m1__age=35), FIELDS, member='1')
        self.assertEqual(result['state'], VIOLATED)

    def test_missing_fact_is_unknown_not_violated(self):
        result = conditions.evaluate_atom(
            conditions.make_atom('age', 'gte', 60),
            facts(m1__age=None), FIELDS, member='1')
        self.assertEqual(result['state'], UNKNOWN)
        self.assertEqual(result['missing'], ['1:age'])

    def test_member_scope_does_not_leak(self):
        result = conditions.evaluate_atom(
            conditions.make_atom('age', 'gte', 60),
            facts(m1__age=62, m2__age=30), FIELDS, member='2')
        self.assertEqual(result['state'], VIOLATED)

    def test_household_scope(self):
        result = conditions.evaluate_atom(
            conditions.make_atom('dibao', 'eq', True, subject='household'),
            facts(household__dibao=True), FIELDS, member='1')
        self.assertEqual(result['state'], SATISFIED)

    def test_in_operator(self):
        atom = conditions.make_atom('disability_grade', 'in', ['一级', '二级'])
        result = conditions.evaluate_atom(atom, facts(m1__disability_grade='二级'), FIELDS, member='1')
        self.assertEqual(result['state'], SATISFIED)

    def test_nin_operator(self):
        atom = conditions.make_atom('disability_grade', 'nin', ['一级', '二级'])
        result = conditions.evaluate_atom(atom, facts(m1__disability_grade='二级'), FIELDS, member='1')
        self.assertEqual(result['state'], VIOLATED)

    def test_between_operator(self):
        atom = conditions.make_atom('age', 'between', [16, 60])
        self.assertEqual(conditions.evaluate_atom(atom, facts(m1__age=62), FIELDS, member='1')['state'], VIOLATED)
        self.assertEqual(conditions.evaluate_atom(atom, facts(m1__age=30), FIELDS, member='1')['state'], SATISFIED)

    def test_exists_operator(self):
        atom = conditions.make_atom('disability_grade', 'exists')
        self.assertEqual(conditions.evaluate_atom(atom, facts(m1__disability_grade='三级'), FIELDS, member='1')['state'], SATISFIED)
        self.assertEqual(conditions.evaluate_atom(atom, facts(m1__disability_grade=None), FIELDS, member='1')['state'], UNKNOWN)

    def test_contains_operator(self):
        atom = conditions.make_atom('study_stage', 'contains', '中职')
        result = conditions.evaluate_atom(atom, facts(m1__study_stage='中职在读'), FIELDS, member='1')
        self.assertEqual(result['state'], SATISFIED)

    def test_implies_operator(self):
        atom = conditions.make_atom('enrolled', 'implies', True)
        self.assertEqual(conditions.evaluate_atom(atom, facts(m1__enrolled=True), FIELDS, member='1')['state'], SATISFIED)
        self.assertEqual(conditions.evaluate_atom(atom, facts(m1__enrolled=False), FIELDS, member='1')['state'], VIOLATED)

    def test_undefined_field_is_unknown(self):
        atom = conditions.make_atom('unknown_field', 'eq', True)
        result = conditions.evaluate_atom(atom, facts(m1__unknown_field=True), FIELDS, member='1')
        self.assertEqual(result['state'], UNKNOWN)
        self.assertEqual(result['reason'], 'undefined_field')

    def test_invalid_value_is_unknown(self):
        atom = conditions.make_atom('disability_grade', 'eq', '五级')
        result = conditions.evaluate_atom(atom, facts(m1__disability_grade='二级'), FIELDS, member='1')
        self.assertEqual(result['state'], UNKNOWN)


class AggregateTest(unittest.TestCase):
    def made(self, *states):
        return [{'state': s, 'condition_id': 'c%d' % i} for i, s in enumerate(states)]

    def test_and_violated_dominates(self):
        self.assertEqual(conditions.aggregate(self.made(SATISFIED, VIOLATED, UNKNOWN), 'and'), VIOLATED)

    def test_and_all_satisfied(self):
        self.assertEqual(conditions.aggregate(self.made(SATISFIED, SATISFIED), 'and'), SATISFIED)

    def test_and_unknown_without_violation(self):
        self.assertEqual(conditions.aggregate(self.made(SATISFIED, UNKNOWN), 'and'), UNKNOWN)

    def test_or_satisfied_dominates(self):
        self.assertEqual(conditions.aggregate(self.made(VIOLATED, SATISFIED, UNKNOWN), 'or'), SATISFIED)

    def test_or_all_violated(self):
        self.assertEqual(conditions.aggregate(self.made(VIOLATED, VIOLATED), 'or'), VIOLATED)

    def test_not_negates(self):
        self.assertEqual(conditions.aggregate(self.made(SATISFIED), 'not'), VIOLATED)
        self.assertEqual(conditions.aggregate(self.made(VIOLATED), 'not'), SATISFIED)
        self.assertEqual(conditions.aggregate(self.made(UNKNOWN), 'not'), UNKNOWN)
        self.assertEqual(conditions.aggregate(self.made(NOT_APPLICABLE), 'not'), NOT_APPLICABLE)

    def test_empty(self):
        self.assertEqual(conditions.aggregate([], 'and'), NOT_APPLICABLE)


class EvidenceBindingTest(unittest.TestCase):
    CLAUSE = '本县户籍、一级或二级残疾且需要长期照护的残疾人，可申请护理补贴。'

    def test_verbatim_quote_bound(self):
        atom = conditions.make_atom('disability_grade', 'in', ['一级', '二级'],
                                    source='llm_proposal',
                                    evidence={'clause_id': 'C1', 'quote': '一级或二级残疾'})
        bound = conditions.bind_evidence(atom, self.CLAUSE)
        self.assertTrue(bound['evidence_bound'])
        self.assertEqual(bound['binding'], 'ok')

    def test_whitespace_folded_quote_bound(self):
        atom = conditions.make_atom('disability_grade', 'in', ['一级', '二级'],
                                    source='llm_proposal',
                                    evidence={'clause_id': 'C1', 'quote': '一级 或 二级 残疾'})
        self.assertTrue(conditions.bind_evidence(atom, self.CLAUSE)['evidence_bound'])

    def test_paraphrased_quote_rejected(self):
        atom = conditions.make_atom('disability_grade', 'in', ['一级', '二级'],
                                    source='llm_proposal',
                                    evidence={'clause_id': 'C1', 'quote': '一二级残疾'})
        bound = conditions.bind_evidence(atom, self.CLAUSE)
        self.assertFalse(bound['evidence_bound'])
        self.assertEqual(bound['binding'], 'rejected')

    def test_rejected_binding_downgrades_state(self):
        atom = conditions.make_atom('disability_grade', 'in', ['一级', '二级'],
                                    source='llm_proposal',
                                    evidence={'clause_id': 'C1', 'quote': '一级残疾'})
        bound = conditions.bind_evidence(atom, self.CLAUSE)
        facts_view = facts(m1__disability_grade='二级')
        result = conditions.evaluate_atom(bound, facts_view, FIELDS, member='1')
        self.assertEqual(result['state'], UNKNOWN)
        self.assertEqual(result['binding'], 'rejected')

    def test_rule_atom_without_quote_flags_binding_gap(self):
        atom = conditions.make_atom('disabled', 'eq', True, source='rule')
        bound = conditions.bind_evidence(atom, self.CLAUSE)
        self.assertFalse(bound['evidence_bound'])
        self.assertEqual(bound['binding'], 'binding_gap')

    def test_empty_clause_rejects(self):
        atom = conditions.make_atom('disabled', 'eq', True, source='llm_proposal',
                                    evidence={'clause_id': 'C1', 'quote': '残疾'})
        self.assertEqual(conditions.bind_evidence(atom, '')['binding'], 'rejected')


class FlattenExpressionTest(unittest.TestCase):
    def test_flatten_and_tree(self):
        expr = {'all': [
            {'field': 'disabled', 'op': 'eq', 'value': True},
            {'any': [{'field': 'age', 'op': 'gte', 'value': 60},
                     {'field': 'disability_grade', 'op': 'in', 'value': ['一级', '二级']}]},
        ]}
        atoms = conditions.atoms_from_expression(expr, subject='member')
        self.assertEqual([a['field'] for a in atoms], ['disabled', 'age', 'disability_grade'])
        self.assertTrue(all(a['source'] == 'rule' for a in atoms))
        self.assertEqual([a['condition_id'] for a in atoms], ['c1', 'c2', 'c3'])

    def test_flatten_unresolved_yields_no_atom(self):
        self.assertEqual(conditions.atoms_from_expression({'unresolved': '原文未明确'}), [])

    def test_flatten_not_carries_negation(self):
        atoms = conditions.atoms_from_expression({'not': {'field': 'dibao', 'op': 'eq', 'value': True}},
                                                 subject='household')
        self.assertEqual(len(atoms), 1)
        self.assertEqual(atoms[0]['negate'], True)


class EvaluateAllTest(unittest.TestCase):
    def test_evaluate_all_keeps_order_and_states(self):
        atoms = [
            conditions.make_atom('age', 'gte', 60, condition_id='c1'),
            conditions.make_atom('disabled', 'eq', True, condition_id='c2'),
        ]
        results = conditions.evaluate_all(atoms, facts(m1__age=62), FIELDS, member='1')
        self.assertEqual([r['condition_id'] for r in results], ['c1', 'c2'])
        self.assertEqual([r['state'] for r in results], [SATISFIED, UNKNOWN])


if __name__ == '__main__':
    unittest.main()
