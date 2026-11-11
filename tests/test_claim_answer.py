# -*- coding: utf-8 -*-
"""P3-B：claim 驱动三段式回答（结论边界 / 逐条依据 / 待补齐）。
渲染只使用已核验行与已绑定引文；不产生资格结论，不改卡片与档位。"""
import os
import unittest
from unittest import mock

from policy_core import claim_answer


def full_row(code='S-ZB-090', name='困难残疾人生活补贴', quote='持有第二代残疾人证的残疾人', member='成员1'):
    return dict(code=code, name=name, member=member, decision='已满足部分条件，政策依据待补齐',
                satisfied=['年龄 62 岁'], missing=['家庭：低保证明'], claims=[
                    dict(kind='satisfied', field='age', text='年龄达到 60 岁',
                         evidence=[dict(quote=quote, clause_id=code + ':main')])])


def rule_row(code='S-ZB-091', name='重度残疾人护理补贴', member='成员1'):
    return dict(code=code, name=name, member=member, decision='已满足部分条件，政策依据待补齐',
                satisfied=['残疾等级=二级（家庭事实已证实）'], missing=['家庭：低保证明'],
                source_gaps=[], benefit='本库未收录本分支补贴标准', tier='B', claims=[],
                clause_quote='所有农户；持有第二代残疾人证')


def coverage(**kw):
    data = dict(disclaimer=True, match_core=dict(counts=dict(A=0, B=2, C=1, D=0)))
    data.update(kw)
    return data


class SwitchTest(unittest.TestCase):
    def test_default_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ANSWER_STYLE', None)
            self.assertTrue(claim_answer.enabled())

    def test_classic_rollback(self):
        with mock.patch.dict(os.environ, {'ANSWER_STYLE': 'classic'}):
            self.assertFalse(claim_answer.enabled())
        with mock.patch.dict(os.environ, {'ANSWER_STYLE': '0'}):
            self.assertFalse(claim_answer.enabled())

    def test_disabled_answer_is_empty(self):
        with mock.patch.dict(os.environ, {'ANSWER_STYLE': 'classic'}):
            self.assertEqual(claim_answer.answer([full_row()], [], coverage()), '')


class StructureTest(unittest.TestCase):
    def test_three_parts_separated_by_blank_line(self):
        text = claim_answer.answer([full_row()], [], coverage())
        parts = text.split('\n\n')
        self.assertGreaterEqual(len(parts), 3)
        self.assertIn('不符合', '不符合')  # no-op

    def test_boundary_counts_and_no_overclaim(self):
        text = claim_answer.answer([full_row(), rule_row()], [], coverage())
        self.assertIn('2 项与您家庭情况相关的政策', text)
        self.assertIn('2 项已有已核实条件支持', text)
        for word in ('完全符合', '可以领取', '保证符合', '保证能', '已符合资格'):
            self.assertNotIn(word, text)

    def test_verified_quote_rendered(self):
        text = claim_answer.answer([full_row()], [], coverage())
        self.assertIn('持有第二代残疾人证的残疾人', text)
        self.assertIn('原文', text)

    def test_unverified_quote_not_rendered(self):
        broken = full_row(quote='')
        text = claim_answer.answer([broken], [], coverage())
        self.assertNotIn('原文：', text)

    def test_rule_clause_quote_rendered_as_clause_not_verbatim(self):
        text = claim_answer.answer([rule_row()], [], coverage())
        self.assertIn('政策条目载明', text)
        self.assertIn('持有第二代残疾人证', text)
        self.assertNotIn('原文：', text)

    def test_missing_and_questions_in_needs(self):
        q = dict(key='household:income', label='家庭年收入大概多少？', reason='判断收入门槛')
        text = claim_answer.answer([full_row()], [q], coverage())
        self.assertIn('待补充', text)
        self.assertIn('家庭年收入大概多少', text)

    def test_why_not_and_agent_sections_kept(self):
        cov = coverage(why_not=[dict(code='X-ZB-300', name='脱贫人口小额信贷', member='成员1',
                                     reason='condition_violated')],
                       agent=dict(state='ok', findings=[dict(code='S-ZB-300', name='雨露计划',
                                                              member='成员1', reason='证据链未闭合',
                                                              quote='接受职业教育的脱贫家庭子女',
                                                              quote_ok=True)]))
        text = claim_answer.answer([full_row()], [], cov)
        self.assertIn('以下政策本次没有列出卡片', text)
        self.assertIn('核验说明如下', text)

    def test_disclaimer_only_when_flagged(self):
        text = claim_answer.answer([full_row()], [], coverage(disclaimer=False))
        self.assertNotIn('结论以经办机构正式核定为准', text)
        text2 = claim_answer.answer([full_row()], [], coverage(disclaimer=True))
        self.assertIn('结论以经办机构正式核定为准', text2)

    def test_empty_rows_returns_empty(self):
        self.assertEqual(claim_answer.answer([], [], coverage()), '')

    def test_intro_used_for_rule_path(self):
        text = claim_answer.answer([rule_row()], [], coverage(), intro=['本次按你已确认的家庭事实核验。'])
        self.assertTrue(text.startswith('本次按你已确认的家庭事实核验。'))


class IntegrationTest(unittest.TestCase):
    def test_result_model_render_claims_vs_classic(self):
        from policy_core import result_model
        rows = [full_row()]
        with mock.patch.dict(os.environ, {'ANSWER_STYLE': 'classic'}):
            old = result_model.render('固定开场。', rows, [], coverage())
        self.assertIn('固定开场', old)
        self.assertNotIn('本次逐条核对了', old)
        with mock.patch.dict(os.environ, {'ANSWER_STYLE': 'claims'}):
            new = result_model.render('固定开场。', rows, [], coverage())
        self.assertTrue(new.startswith('本次逐条核对了'))
        self.assertNotIn('固定开场', new)

    def test_rule_render_claims_vs_classic(self):
        from policy_core import rule_flow
        rows = [rule_row()]
        cov = coverage(round=1)
        with mock.patch.dict(os.environ, {'ANSWER_STYLE': 'classic'}):
            old = rule_flow.render_rule_mode(rows, [], cov)
        self.assertIn('已满足：', old)
        with mock.patch.dict(os.environ, {'ANSWER_STYLE': 'claims'}):
            new = rule_flow.render_rule_mode(rows, [], cov)
        self.assertTrue(new.startswith('本次按你已确认的家庭事实'))
        self.assertIn('政策条目载明', new)
        self.assertIn('还需要补充与核对的事项', new)

    def test_row_tier_counts_beat_coverage(self):
        rows = [full_row(), dict(rule_row(), tier='C', satisfied=[])]
        text = claim_answer.answer(rows, [], coverage(match_core=dict(counts=dict(A=0, B=9, C=9))))
        self.assertIn('1 项已有已核实条件支持', text)
        self.assertIn('1 项需要补充家庭信息后才能判断', text)


if __name__ == '__main__':
    unittest.main()