# -*- coding: utf-8 -*-
"""地区追问门（REGION_ASK_GATE）回归：外省/无法确认地区不得静默按江西政策库出卡。

历史缺陷（m03836）：service_scope_check 返回 ask 时，full_flow 只处理 blocked，
ask 被忽略 → 家庭只写“株洲市”（不带省字）仍会继续检索江西政策库并可能出 B 档卡片。
本测试同时锁定：真实 scope 语义、canonical 解析、full_flow 早门（不检索/0 卡/有追问）、
claim 三段式的地区边界句。
"""
import os
import unittest
from unittest import mock

import kg_retrieval
from policy_core import claim_answer, facts, full_flow, region

import events

ASK_REASON = '无法确认家庭所在地区（株洲市）属于江西省，请补充所在市/县（区）全称'


class ScopeCheckTest(unittest.TestCase):
    """service_scope_check 的三态语义（不依赖图谱加载：region_names 显式传入）。"""

    def test_outside_city_without_province_is_ask_not_default_jiangxi(self):
        scope, reason = kg_retrieval.service_scope_check({'region': '株洲市'}, region_names=[])
        self.assertEqual(scope, 'ask')
        self.assertIn('无法确认', reason)

    def test_outside_province_is_blocked(self):
        scope, reason = kg_retrieval.service_scope_check({'region': '湖南省株洲市'}, region_names=[])
        self.assertEqual(scope, 'blocked')
        self.assertIn('仅支持江西省', reason)

    def test_jiangxi_county_is_ok(self):
        scope, _ = kg_retrieval.service_scope_check({'region': '莲花县'}, region_names=['莲花县'])
        self.assertEqual(scope, 'ok')

    def test_empty_region_is_ask(self):
        scope, reason = kg_retrieval.service_scope_check({'region': ''}, region_names=[])
        self.assertEqual(scope, 'ask')
        self.assertIn('缺少家庭所在地区', reason)

    def test_canonical_levels_outside_city_is_unknown(self):
        self.assertEqual(region.canonical_levels('株洲市')['level'], 'unknown')

    def test_canonical_levels_jiangxi_county_is_county(self):
        self.assertEqual(region.canonical_levels('莲花县')['level'], 'county')


class RegionAskGateTest(unittest.TestCase):
    """full_flow 早门：scope=ask 且 canonical 解析不出县区时，不进入规则/检索/模型。"""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            'RULE_ENGINE': 'off', 'RULE_SHADOW': '0', 'REGION_ASK_GATE': '1',
            'ANSWER_STYLE': 'claims',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run_gate(self):
        calls = []

        def fake_ingest(hh, text, fields, model, **kwargs):
            st = facts.state(hh)
            st['facts']['household:region'] = dict(
                state='known', value='株洲市', quote='株洲市', schema_id='region', origin='user')
            hh['region'] = '株洲市'

        def boom(*args, **kwargs):
            calls.append('called')
            raise AssertionError('gate 后不应调用检索/模型链路: %r' % (args[:1],))

        def fake_scope(hh, region_names=None):
            return ('ask', ASK_REASON)

        def fake_questions(*args, **kwargs):
            return []

        llm = boom
        with mock.patch.object(full_flow, '_ingest_with_normalize', fake_ingest), \
             mock.patch.object(kg_retrieval, 'service_scope_check', fake_scope), \
             mock.patch.object(full_flow, 'question_plan', fake_questions), \
             mock.patch.object(full_flow, 'retrieve', boom), \
             mock.patch.object(full_flow, 'load_evidence', boom), \
             mock.patch.object(full_flow, 'reason', boom), \
             mock.patch.object(full_flow, 'build_rows', boom):
            body, matched, result = full_flow.run({}, '我家在株洲市，两口人', 'gate-test', None, llm)
        return body, matched, result, calls

    def test_full_flow_blocks_retrieval_and_asks_region(self):
        body, matched, result, calls = self._run_gate()
        self.assertEqual(calls, [])
        self.assertEqual(matched, [])
        self.assertEqual(result.get('cards') or [], [])
        cov = (result.get('match_json') or {}).get('coverage') or {}
        self.assertTrue(cov.get('region_gate'))
        self.assertEqual(cov.get('region_status'), 'unknown')
        self.assertIn('株洲市', body)
        self.assertIn('市／县', body)
        # 必须给出可回答的地区追问，而不是让用户自己猜
        keys = [q.get('fact_key') for q in (result.get('questions') or []) if isinstance(q, dict)]
        self.assertIn('household:region', keys)

    def test_gate_off_keeps_old_behavior(self):
        with mock.patch.dict(os.environ, {'REGION_ASK_GATE': '0'}):
            self.assertFalse(full_flow.region_ask_gate_enabled())

    def test_gate_default_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('REGION_ASK_GATE', None)
            self.assertTrue(full_flow.region_ask_gate_enabled())


class RegionGateEventTest(unittest.TestCase):
    """地区追问必须作为用户可见事件出现（前端时间线要能看到“为什么这次没核验”）。"""

    def test_region_gate_event_is_user_visible(self):
        kind, phase, exposure, title, status = events.classify('full_model.region.gate', {'reason': '无法确认'})
        self.assertEqual(kind, 'region.gate')
        self.assertEqual(exposure, 'user')
        self.assertEqual(status, 'ok')
        text = events.narrative('full_model.region.gate', {'reason': '无法确认'}, kind, title, status)
        self.assertIn('家庭所在地区', text)
        self.assertIn('江西省', text)


class ClaimRegionBoundaryTest(unittest.TestCase):
    """claim 三段式：地区未确认时“结论边界”必须先说清，不能只给卡片。"""

    ROWS = [dict(name='示例政策', code='X-TEST-001', member='成员1', tier='B',
                 satisfied=['已核实条件'], missing=[], claims=[])]

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'ANSWER_STYLE': 'claims'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_region_unknown_adds_boundary_line(self):
        text = claim_answer.answer(self.ROWS, [], {'region_status': 'unknown'})
        self.assertIn('地区', text)
        self.assertIn('未确认', text)
        self.assertIn('未按属地', text)

    def test_region_known_has_no_region_warning(self):
        text = claim_answer.answer(self.ROWS, [], {'region_status': 'known'})
        self.assertNotIn('未按属地', text)


if __name__ == '__main__':
    unittest.main()