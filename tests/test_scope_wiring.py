# -*- coding: utf-8 -*-
"""POLICY_SCOPE 结构化范围过滤的两条链路接线测试（默认关闭，不改变现网行为）。

覆盖：全模型路径 _apply_scope_filter、规则路径 _apply_scope_rows、
开关关闭原样返回、异常退回原集合、事件分类与人话文案。
"""
import unittest
from unittest import mock

import events
from policy_core import full_flow, rule_flow, scope_filter


def _report(kept, dropped, unknown=(), no_scope=()):
    return {
        'version': scope_filter.VERSION,
        'household': {},
        'kept': list(kept),
        'dropped': [dict(code=c, relation='disjoint', reason='x') for c in dropped],
        'unknown': [dict(code=c, relation='unknown', reason='y') for c in unknown],
        'no_scope': list(no_scope),
        'applicable': {},
    }


class FullPathTest(unittest.TestCase):
    def test_disabled_returns_original(self):
        packs = [{'policy_code': 'A'}, {'policy_code': 'B'}]
        coverage = {}
        with mock.patch.dict('os.environ', {'POLICY_SCOPE': '0'}):
            self.assertEqual(full_flow._apply_scope_filter(packs, coverage, '江西省赣州市章贡区'), packs)
        self.assertNotIn('scope_filter', coverage)

    def test_filters_dropped_and_records_summary(self):
        packs = [{'policy_code': 'A'}, {'policy_code': 'B'}, {'policy_code': 'C'}]
        coverage = {}
        report = _report(kept=['A'], dropped=['B'], unknown=['C'])
        with mock.patch.dict('os.environ', {'POLICY_SCOPE': '1'}), \
                mock.patch.object(scope_filter, 'filter_packs', return_value=(packs[:2], dict(report, input=3, output=3))):
            got = full_flow._apply_scope_filter(packs, coverage, '江西省赣州市章贡区')
        self.assertEqual(got, packs[:2])
        self.assertEqual(coverage['scope_filter']['dropped'], 1)
        self.assertEqual(coverage['scope_filter']['unknown'], 1)
        self.assertEqual(coverage['scope_filter']['input'], 3)

    def test_error_degrades_to_original(self):
        packs = [{'policy_code': 'A'}]
        coverage = {}
        with mock.patch.dict('os.environ', {'POLICY_SCOPE': '1'}), \
                mock.patch.object(scope_filter, 'filter_packs', side_effect=RuntimeError('no table')):
            got = full_flow._apply_scope_filter(packs, coverage, '江西省')
        self.assertEqual(got, packs)
        self.assertEqual(coverage['scope_filter']['state'], 'error')

    def test_empty_packs_is_noop(self):
        coverage = {}
        self.assertEqual(full_flow._apply_scope_filter([], coverage, '江西省'), [])


class RulePathTest(unittest.TestCase):
    def test_disabled_returns_original(self):
        rows = [{'code': 'A'}, {'code': 'B'}]
        with mock.patch.dict('os.environ', {'POLICY_SCOPE': '0'}):
            self.assertEqual(rule_flow._apply_scope_rows(rows, {}, '江西省赣州市章贡区'), rows)

    def test_filters_dropped(self):
        rows = [{'code': 'A'}, {'code': 'B'}]
        coverage = {}
        with mock.patch.dict('os.environ', {'POLICY_SCOPE': '1'}), \
                mock.patch.object(scope_filter, 'filter_codes', return_value=_report(kept=['A'], dropped=['B'])):
            got = rule_flow._apply_scope_rows(rows, coverage, '江西省')
        self.assertEqual([r['code'] for r in got], ['A'])
        self.assertEqual(coverage['scope_filter']['output'], 1)

    def test_error_degrades_to_original(self):
        rows = [{'code': 'A'}]
        coverage = {}
        with mock.patch.dict('os.environ', {'POLICY_SCOPE': '1'}), \
                mock.patch.object(scope_filter, 'filter_codes', side_effect=RuntimeError('boom')):
            got = rule_flow._apply_scope_rows(rows, coverage, '江西省')
        self.assertEqual(got, rows)
        self.assertEqual(coverage['scope_filter']['state'], 'error')


class ScopeFilterEventTest(unittest.TestCase):
    def test_full_model_event_classified_user_visible(self):
        kind, phase, exposure, title, status = events.classify(
            'full_model.scope.filter', {'kept': 3, 'dropped': 1})
        self.assertEqual(kind, 'scope.filter')
        self.assertEqual(exposure, 'user')
        self.assertIn('地区范围', title)
        self.assertEqual(status, 'ok')

    def test_top_level_event_also_classified(self):
        kind, phase, exposure, title, _ = events.classify('scope.filter', {'kept': 1})
        self.assertEqual(kind, 'scope.filter')
        self.assertEqual(exposure, 'user')

    def test_narrative_mentions_counts(self):
        fields = {'kept': 3, 'dropped': 1, 'unknown': 2}
        kind, phase, exposure, title, status = events.classify('full_model.scope.filter', fields)
        text = events.narrative('full_model.scope.filter', fields, kind, title, status)
        self.assertIn('保留 3 条', text)
        self.assertIn('排除 1 条', text)
        self.assertIn('待核', text)


if __name__ == '__main__':
    unittest.main()
