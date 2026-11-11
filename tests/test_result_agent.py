# -*- coding: utf-8 -*-
"""P3 渲染层：调查说明与逐字引文，开关关闭时不改变既有版式。"""
import os
import unittest
from unittest import mock

from policy_core import result_model


def row(code='S-ZB-090', name='困难残疾人生活补贴', quote='持有第二代残疾人证的残疾人'):
    return dict(code=code, name=name, member='成员1', decision='已满足部分条件，政策依据待补齐',
                satisfied=['年龄 62 岁'], missing=[], claims=[
                    dict(kind='satisfied', field='age', text='年龄达到 60 岁',
                         evidence=[dict(quote=quote, clause_id=code + ':main')])])


def coverage(agent=True):
    data = dict(unclear_facts={})
    if agent:
        data['agent'] = dict(state='ok', findings=[
            dict(code='S-ZB-090', name='困难残疾人生活补贴', member='成员1',
                 reason='证据链未闭合，未形成卡片', quote='持有第二代残疾人证的残疾人',
                 quote_ok=True, region='applicable')])
    return data


class AgentLinesTest(unittest.TestCase):
    def test_findings_render_with_verified_quote(self):
        lines = result_model.agent_lines(coverage())
        self.assertTrue(lines)
        self.assertIn('核验说明如下', lines[0])
        self.assertIn('持有第二代残疾人证的残疾人', '\n'.join(lines))

    def test_unverified_quote_is_not_rendered(self):
        cov = coverage()
        cov['agent']['findings'][0]['quote_ok'] = False
        lines = result_model.agent_lines(cov)
        self.assertNotIn('持有第二代残疾人证的残疾人', '\n'.join(lines))

    def test_no_agent_data_renders_nothing(self):
        self.assertEqual(result_model.agent_lines({}), [])


class ClaimsTest(unittest.TestCase):
    def test_claims_block_switch_off_by_default(self):
        with mock.patch.dict(os.environ, {'ANSWER_CLAIMS': '0'}):
            self.assertEqual(result_model.claims_block([row()]), [])

    def test_claims_block_quotes_verified_evidence(self):
        with mock.patch.dict(os.environ, {'ANSWER_CLAIMS': '1'}):
            lines = result_model.claims_block([row()])
        self.assertEqual(len(lines), 1)
        self.assertIn('持有第二代残疾人证的残疾人', lines[0])

    def test_claim_without_quote_is_skipped(self):
        with mock.patch.dict(os.environ, {'ANSWER_CLAIMS': '1'}):
            broken = row(quote='')
            self.assertEqual(result_model.claims_block([broken]), [])

    def test_render_includes_agent_section_after_rejected_list(self):
        with mock.patch.dict(os.environ, {'ANSWER_CLAIMS': '0'}):
            text = result_model.render('开场。', [], [], coverage())
        self.assertIn('核验说明如下', text)


if __name__ == '__main__':
    unittest.main()
