# -*- coding: utf-8 -*-
"""只读全国模式（NATIONAL_READONLY）接线测试。

语义：开启后，家庭所在地区在政策库中没有任何收录条目时，如实说明"暂无收录"，
不生成政策卡片、不作资格判断；探测失败/无法解析时放行，绝不误判"未收录"。
"""
import os
import unittest
from unittest import mock

import events
from policy_core import full_flow

OUT_PROVINCE = '湖南省株洲市'


def _env(readonly='1'):
    return mock.patch.dict(os.environ, {
        'RULE_ENGINE': 'off', 'RULE_SHADOW': '0', 'REGION_ASK_GATE': '1',
        'ANSWER_STYLE': 'claims', 'NATIONAL_READONLY': readonly,
        'SERVICE_SCOPE': 'national', 'REGION_NATIONWIDE': '1',
    })


def _fake_ingest(hh, text, fields, model, **kwargs):
    from policy_core import facts
    st = facts.state(hh)
    st['facts']['household:region'] = dict(
        state='known', value=OUT_PROVINCE, quote=OUT_PROVINCE,
        schema_id='region', origin='user')
    hh['region'] = OUT_PROVINCE


class _Boom(Exception):
    pass


class ReadonlyShortCircuitTest(unittest.TestCase):
    """收录为 0 时短路：不检索、不调模型、0 卡 0 问。"""

    def setUp(self):
        self.env = _env('1')
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, cov):
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise _Boom('短路后不应进入检索/模型链路')

        with mock.patch.object(full_flow, '_ingest_with_normalize', _fake_ingest), \
             mock.patch.object(full_flow, '_region_coverage', lambda value: cov), \
             mock.patch.object(full_flow, 'retrieve', boom), \
             mock.patch.object(full_flow, 'load_evidence', boom), \
             mock.patch.object(full_flow, 'reason', boom), \
             mock.patch.object(full_flow, 'build_rows', boom):
            body, matched, result = full_flow.run({}, '我家在湖南省株洲市，两口人', 'ro-test', None, boom)
        return body, matched, result, calls

    def test_no_coverage_returns_text_without_cards(self):
        cov = dict(version='coverage-probe-1', level='city', province='湖南省', city='株洲市',
                   county=None, rows=0, policies=0, covered=False, reason='no_scope_rows')
        body, matched, result, calls = self._run(cov)
        self.assertEqual(calls, [])
        self.assertEqual(matched, set())
        self.assertEqual(result.get('cards'), [])
        self.assertEqual(result.get('table_rows'), [])
        self.assertEqual(result['match_json']['judge_status'], 'full_model_evidence_bound')
        self.assertIn('还没有收录', body)
        self.assertIn('只读', body)
        self.assertIn('不作资格判断', body)
        self.assertIn('株洲市', body)
        self.assertNotIn('符合', body.replace('不符合', ''))
        coverage = result['match_json']['coverage']
        self.assertTrue(coverage.get('readonly_national'))
        self.assertTrue(coverage.get('no_policy_data'))
        self.assertIn('只读全国模式', coverage.get('scope'))
        self.assertIn('株洲市', str(coverage.get('region_coverage')))
        self.assertEqual(result['match_json']['advice_audit']['status'], 'readonly_no_data')

    def test_events_emitted(self):
        cov = dict(level='city', rows=0, policies=0, covered=False)
        seen = []

        def fake_event(name, **data):
            seen.append((name, data))

        with mock.patch.object(full_flow, 'event', fake_event):
            body, matched, result, calls = self._run(cov)
        names = [item[0] for item in seen]
        self.assertIn('region.coverage', names)
        self.assertIn('result', names)
        gap = dict(seen[names.index('region.coverage')][1])
        self.assertEqual(gap.get('status'), 'none')
        self.assertEqual(gap.get('policies'), 0)
        self.assertIn('株洲市', gap.get('region'))
        self.assertEqual(result['match_json']['advice_audit']['status'], 'readonly_no_data')

    def test_covered_true_does_not_short_circuit(self):
        cov = dict(level='city', rows=0, policies=3, covered=True)
        body, matched, result, calls = self._run(cov)
        self.assertTrue(calls)  # 已进入检索链路
        self.assertEqual(matched, [])
        self.assertEqual(result['match_json']['coverage'].get('no_policy_data'), None)

    def test_covered_none_does_not_short_circuit(self):
        cov = dict(level='city', rows=None, policies=None, covered=None, reason='query_error')
        body, matched, result, calls = self._run(cov)
        self.assertTrue(calls)
        self.assertNotIn('还没有收录', body)
        self.assertEqual(result['match_json']['coverage'].get('no_policy_data'), None)

    def test_region_ask_wins_over_coverage(self):
        """地区解析不明时先追问补充地区，不做收录探测。"""
        seen = []

        def fake_cov(value):
            seen.append(value)
            return dict(covered=False, rows=0, policies=0, level='unknown')

        def fake_ingest(hh, text, fields, model, **kwargs):
            from policy_core import facts
            st = facts.state(hh)
            st['facts']['household:region'] = dict(
                state='known', value='火星镇', quote='火星镇',
                schema_id='region', origin='user')
            hh['region'] = '火星镇'

        def boom(*args, **kwargs):
            raise _Boom('不应进入检索')

        with mock.patch.object(full_flow, '_ingest_with_normalize', fake_ingest), \
             mock.patch.object(full_flow, '_region_coverage', fake_cov), \
             mock.patch.object(full_flow, 'retrieve', boom), \
             mock.patch.object(full_flow, 'load_evidence', boom), \
             mock.patch.object(full_flow, 'reason', boom), \
             mock.patch.object(full_flow, 'build_rows', boom):
            body, matched, result = full_flow.run({}, '我家住火星镇', 'ro-ask', None, boom)
        self.assertEqual(seen, [])
        self.assertEqual(matched, [])
        self.assertIn('火星镇', body)
        self.assertTrue(result['match_json']['coverage'].get('region_gate'))


class SwitchTest(unittest.TestCase):
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NATIONAL_READONLY', None)
            self.assertFalse(full_flow.national_readonly_enabled())

    def test_truthy_values(self):
        for raw in ('1', 'on', 'true', 'yes', ' TRUE '):
            with mock.patch.dict(os.environ, {'NATIONAL_READONLY': raw}):
                self.assertTrue(full_flow.national_readonly_enabled(), raw)

    def test_falsy_values(self):
        for raw in ('0', 'off', 'false', 'no', ''):
            with mock.patch.dict(os.environ, {'NATIONAL_READONLY': raw}):
                self.assertFalse(full_flow.national_readonly_enabled(), raw)

    def test_disabled_never_probes(self):
        seen = []

        def fake_cov(value):
            seen.append(value)
            return dict(covered=False, rows=0, policies=0)

        def boom(*args, **kwargs):
            raise _Boom('关闭时不应进入检索')

        with _env('0'), \
             mock.patch.object(full_flow, '_ingest_with_normalize', _fake_ingest), \
             mock.patch.object(full_flow, '_region_coverage', fake_cov), \
             mock.patch.object(full_flow, 'retrieve', boom), \
             mock.patch.object(full_flow, 'load_evidence', boom), \
             mock.patch.object(full_flow, 'reason', boom), \
             mock.patch.object(full_flow, 'build_rows', boom):
            body, matched, result = full_flow.run({}, '我家在湖南省株洲市', 'ro-off', None, boom)
        self.assertEqual(seen, [])
        self.assertNotIn('还没有收录', body)
        self.assertIsNone(result['match_json']['coverage'].get('no_policy_data'))


class AdviceTextTest(unittest.TestCase):
    def test_county_level_hint(self):
        text = full_flow._readonly_no_data_advice('湖南省株洲市荷塘区', dict(level='county', rows=0))
        self.assertIn('还没有收录', text)
        self.assertIn('不作资格判断', text)
        self.assertIn('县区名称有误', text)

    def test_city_level_hint(self):
        text = full_flow._readonly_no_data_advice('湖南省株洲市', dict(level='city', rows=0, policies=0))
        self.assertIn('请补充所在市／县（区）全称', text)
        self.assertIn('0条政策明细', text)

    def test_never_says_ineligible(self):
        text = full_flow._readonly_no_data_advice('火星镇', dict(level='unknown', rows=0))
        for bad in ('不符合条件', '不具备资格', '不能申请'):
            self.assertNotIn(bad, text)


class CoverageEventTest(unittest.TestCase):
    def test_classify(self):
        kind, phase, exposure, title, status = events.classify(
            'full_model.region.coverage', {'policies': 0})
        self.assertEqual(kind, 'region.coverage')
        self.assertEqual(exposure, 'user')
        self.assertIn('收录', title)

    def test_top_level_classify(self):
        kind, _, exposure, _, _ = events.classify('region.coverage', {})
        self.assertEqual(kind, 'region.coverage')

    def test_narrative_none(self):
        text = events.narrative('region.coverage', {}, 'region.coverage', '地区政策收录检查', 'ok')
        self.assertIn('核对', text)

    def test_narrative_zero(self):
        text = events.narrative('region.coverage', {'policies': 0}, 'region.coverage', 'x', 'ok')
        self.assertIn('暂无', text)

    def test_narrative_count(self):
        text = events.narrative('region.coverage', {'policies': 7}, 'region.coverage', 'x', 'ok')
        self.assertIn('7 条', text)


if __name__ == '__main__':
    unittest.main()