# -*- coding: utf-8 -*-
"""P3 调查 agent：有界、只读、引文回链、失败隔离。"""
import copy
import json
import os
import unittest
from unittest import mock

from policy_core import agent

FIELDS = {'age': dict(type='number', label='年龄', scope='member'),
          'poverty_exit': dict(type='boolean', label='是否脱贫户', scope='household')}


def pack(code='S-ZB-090', name='困难残疾人生活补贴'):
    return dict(id=code + ':main', policy=dict(code=code, name=name, regions=['莲花县']),
                detail=dict(region='莲花县', detail_id='main'),
                source=dict(title=name, target='持有第二代残疾人证的残疾人',
                            conditions='家庭属于低保或脱贫户', benefit='每人每月100元'))


def decision(source_id, member='成员1', status='rejected', error='core_audience_unconfirmed'):
    return dict(source_id=source_id, member=member, status=status,
                primary_review=dict(error=error), object_matches=[])


def inputs():
    hh = dict(facts={'household:region': dict(state='known', value='莲花县'),
                     '1:age': dict(state='known', value=62)})
    p = pack()
    return hh, [p], [decision(p['id'])], [], FIELDS


class FakeModel:
    def __init__(self, plans, remaining=60):
        self.plans = list(plans)
        self._remaining = remaining
        self.calls = []

    @property
    def remaining(self):
        return self._remaining

    def __call__(self, prompt, payload=None, *, system=None, purpose=None, temperature=0, **kw):
        self.calls.append(purpose)
        plan = self.plans[min(len(self.calls) - 1, len(self.plans) - 1)]
        if isinstance(plan, Exception):
            raise plan
        return json.dumps(plan, ensure_ascii=False)


class EnvelopeTest(unittest.TestCase):
    def test_no_focus_is_skipped_and_calls_nothing(self):
        with mock.patch.dict(os.environ, {'AGENT_LOOP': '1'}):
            hh, packs, decisions, audit, fields = inputs()
            model = FakeModel([{'done': True}])
            out = agent.investigate(hh, packs, [], [], fields, {}, model)
        self.assertEqual(out['state'], 'skipped')
        self.assertEqual(model.calls, [])

    def test_switch_off_disables_loop(self):
        with mock.patch.dict(os.environ, {'AGENT_LOOP': '0'}):
            out = agent.investigate(*inputs(), {}, FakeModel([{'done': True}]))
        self.assertEqual(out['state'], 'disabled')

    def test_low_budget_skips_without_model_call(self):
        model = FakeModel([{'done': True}], remaining=5)
        out = agent.investigate(*inputs(), {}, model)
        self.assertEqual(out['state'], 'budget_skipped')
        self.assertEqual(model.calls, [])

    def test_model_error_is_isolated(self):
        model = FakeModel([RuntimeError('boom')])
        out = agent.investigate(*inputs(), {}, model)
        self.assertEqual(out['state'], 'model_error')
        self.assertEqual(out['calls'], 0)


class LoopTest(unittest.TestCase):
    def test_actions_are_executed_and_findings_assembled(self):
        plans = [dict(thought='先读原文再求值', actions=[
            dict(tool='read_clause', args=dict(code='S-ZB-090')),
            dict(tool='evaluate_condition', args=dict(code='S-ZB-090', field='age', op='gte', value=60, member_no=1))],
            findings=[dict(code='S-ZB-090', note='条件已求值，缺低保状态')], done=True)]
        with mock.patch.dict(os.environ, {'AGENT_LOOP': '1'}):
            out = agent.investigate(*inputs(), {}, FakeModel(plans))
        self.assertEqual(out['state'], 'ok')
        self.assertEqual(out['calls'], 1)
        self.assertEqual(set(out['tools']), {'read_clause', 'evaluate_condition'})
        finding = out['findings'][0]
        self.assertEqual(finding['code'], 'S-ZB-090')
        self.assertIn(finding['quote'], pack()['source']['target'])
        self.assertTrue(finding['quote_ok'])
        self.assertIn('条件已求值', finding['reason'])

    def test_overclaim_note_is_not_quoted_as_reason(self):
        plans = [dict(thought='核对', actions=[dict(tool='region_check', args=dict(code='S-ZB-090'))],
                      findings=[dict(code='S-ZB-090', note='该户符合资格，可以领取')], done=True)]
        out = agent.investigate(*inputs(), {}, FakeModel(plans))
        self.assertNotIn('符合资格', out['findings'][0]['reason'])
        self.assertNotIn('可以领取', out['findings'][0]['reason'])

    def test_loop_is_bounded_by_steps_and_calls(self):
        plans = [dict(thought='继续核对', actions=[dict(tool='member_facts', args=dict(member_no=1)),
                                                 dict(tool='member_facts', args=dict(member_no=1))],
                      done=False)] * 6
        with mock.patch.dict(os.environ, {'AGENT_MAX_STEPS': '2', 'AGENT_MAX_CALLS': '2'}):
            out = agent.investigate(*inputs(), {}, FakeModel(plans))
        self.assertLessEqual(out['calls'], 2)
        self.assertLessEqual(out['steps'], 2)
        self.assertEqual(len(out['tools']), 1)  # 同名工具去重

    def test_agent_never_mutates_rows_or_decisions(self):
        hh, packs, decisions, audit, fields = inputs()
        snap = (copy.deepcopy(hh), copy.deepcopy(packs), copy.deepcopy(decisions))
        plans = [dict(thought='核对', actions=[dict(tool='read_clause', args=dict(code='S-ZB-090'))],
                      done=True)]
        out = agent.investigate(hh, packs, decisions, audit, fields, {}, FakeModel(plans))
        self.assertEqual(hh, snap[0])
        self.assertEqual(packs, snap[1])
        self.assertEqual(decisions, snap[2])
        self.assertTrue(out['findings'])  # 只产出说明，不产出卡片

    def test_region_other_is_reported_as_mechanical_reason(self):
        hh, packs, decisions, audit, fields = inputs()
        hh['facts']['household:region'] = dict(state='known', value='湖南省长沙市')
        out = agent.investigate(hh, packs, decisions, audit, fields, {}, FakeModel([{'done': True}]))
        self.assertIn('其他地区', out['findings'][0]['reason'])
        self.assertEqual(out['findings'][0]['region'], 'explicit_other')


if __name__ == '__main__':
    unittest.main()
