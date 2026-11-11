# -*- coding: utf-8 -*-
"""抽取提示词不得只写"别提 disabled=false"这半边。

真链路四轮复现（docs/superpowers/evidence/2026-11-10-negation/）里，
用户第一轮说完"我母亲无残疾"，第二轮之后成员事实里只剩 core_age——
既没有 disability 字符串，也没有 disabled=false。病根是提示词只剩禁止从句：
`health_not_disability_denial` 守卫本来就允许"原文含否定词时输出 false"，
提示词却比守卫更严，把这条合法通道关了。
"""
import unittest

from policy_core import facts, full_facts


class TestFactPromptNegationContract(unittest.TestCase):
    def test_shared_prompt_states_both_halves(self):
        self.assertIn('健康不等于无残疾', facts.FACT_PROMPT)
        self.assertIn('用户明说无残疾时，必须输出 disabled=false', facts.FACT_PROMPT)

    def test_extract_tail_no_longer_forbids_blanket(self):
        self.assertNotIn('健康绝不提disabled=false', full_facts.NEGATION_PROMPT_TAIL)
        self.assertIn('用户明说无残疾时，必须输出 disabled=false',
                      full_facts.NEGATION_PROMPT_TAIL)

    def test_ingest_prompt_carries_the_allowance(self):
        """真正发给模型的那串 system 文本里必须有允许从句。"""
        captured = {}

        def fake_ask(model, purpose, task, payload, system, schema=None):
            captured['system'] = system
            return {'facts': [], 'members': []}

        hh = {}
        original = full_facts.ask
        full_facts.ask = fake_ask
        try:
            full_facts.ingest(hh, '我母亲无残疾。', facts.dictionary(), object())
        finally:
            full_facts.ask = original
        self.assertIn('用户明说无残疾时，必须输出 disabled=false', captured['system'])


class TestValidateFactDisabled(unittest.TestCase):
    def setUp(self):
        self.fields = facts.dictionary()
        # 单成员家庭：跳过 ambiguous_member_quote 的邻居归属判定，只测残疾语义守卫。
        self.members = {'1'}

    def test_explicit_denial_is_accepted(self):
        item = dict(entity='1', field='disabled', value=False, quote='我母亲无残疾')
        full_facts.validate_fact(item, '我母亲无残疾。', self.fields, self.members)

    def test_health_alone_is_rejected(self):
        """健康是健康，不是否认残疾；连"残疾"这个概念词都没有时更不许下 false。"""
        item = dict(entity='1', field='disabled', value=False, quote='我母亲身体健康')
        with self.assertRaises(ValueError):
            full_facts.validate_fact(item, '我母亲身体健康。', self.fields, self.members)

    def test_all_denial_wordings_can_land_false(self):
        """守卫认的四种否定写法，都必须真的能落成 disabled=false。"""
        for quote in ('我母亲没有残疾', '我母亲不是残疾人', '我母亲非残疾', '我母亲无残疾'):
            with self.subTest(quote=quote):
                item = dict(entity='1', field='disabled', value=False, quote=quote)
                full_facts.validate_fact(item, quote + '。', self.fields, self.members)

    def test_disability_stated_without_grade_is_still_true(self):
        item = dict(entity='1', field='disabled', value=True, quote='母亲是残疾人，还没评级')
        full_facts.validate_fact(item, '母亲是残疾人，还没评级。', self.fields, self.members)


if __name__ == '__main__':
    unittest.main()
