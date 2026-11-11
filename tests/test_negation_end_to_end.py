# -*- coding: utf-8 -*-
"""离线见证：显式否认残疾能一路走成事实，纯"健康"仍然不许。

真链路四轮复现（docs/superpowers/evidence/2026-11-10-negation/）的缺陷 A：
第一轮说了"我母亲无残疾"，第二轮之后成员事实里只剩 core_age —— 否定被抽取
提示词压掉了。这里用 stub 模型（不连 LLM、不连库）把"抽取 → 校验 → 落库"
这一段走完，锁住修复的两半：
  1. 显式否认 → facts 里出现布尔 False；
  2. 只提健康 → 不许凭空产出 False 事实。
"""
import unittest

from policy_core import facts, full_facts


class _StubModel(object):
    """按 purpose 回不同形状：抽取消 → facts，审计 → accepted_indices。"""

    def __init__(self, extracted):
        self.extracted = extracted
        self.prompts = {}

    def __call__(self, model, purpose, task, payload, system, schema=None):
        self.prompts[purpose] = system
        if purpose == 'core_full_fact_audit':
            return {'accepted_indices': list(range(len(self.extracted.get('facts') or []))),
                    'issues': [], 'omissions': []}
        return self.extracted


class TestDenialReachesFacts(unittest.TestCase):
    def _run(self, text, extracted, members=('2',)):
        stub = _StubModel(extracted)
        original = full_facts.ask
        full_facts.ask = stub
        try:
            hh = {}
            facts.state(hh)
            hh['policy_core']['members'] = list(members)
            full_facts.ingest(hh, text, facts.dictionary(), object())
        finally:
            full_facts.ask = original
        return hh['policy_core']['facts'], stub

    def test_explicit_denial_lands_as_false(self):
        """用户明说无残疾 ⇒ 成员事实里真的出现 disabled=False。"""
        stored, stub = self._run(
            '我母亲无残疾。',
            {'facts': [['2', 'disabled', False, 0]], 'members': ['2']})
        self.assertIn('2:disabled', stored)
        self.assertIs(stored['2:disabled']['value'], False)
        # 发给模型的提示词里必须有允许从句，否则模型还是不敢写 false。
        self.assertIn('用户明说无残疾时，必须输出 disabled=false',
                      stub.prompts['core_full_fact_extract'])

    def test_health_only_never_becomes_false_fact(self):
        """只说身体健康，模型就算写 false 也会被守卫拦掉（拒它的是证据规则，不是提示词）。"""
        with self.assertRaises(ValueError) as ctx:
            self._run(
                '我母亲身体很健康。',
                {'facts': [['2', 'disabled', False, 0]], 'members': ['2']})
        message = str(ctx.exception)
        self.assertTrue(
            'concept_not_explicit' in message
            or 'health_not_disability_denial' in message
            or '未通过事实证据校验' in message,
            message)

    def test_positive_disability_still_recorded(self):
        """反向守卫：真的说了是残疾人，不能被否定逻辑吞掉。"""
        stored, _ = self._run(
            '我母亲是残疾人，还没评级。',
            {'facts': [['2', 'disabled', True, 0]], 'members': ['2']})
        self.assertIn('2:disabled', stored)
        self.assertIs(stored['2:disabled']['value'], True)


if __name__ == '__main__':
    unittest.main()

class TestNamedDenialOwner(unittest.TestCase):
    """真链路第二轮：用户说「张兰无残疾」，模型把张兰算成成员1，事实整条被丢。

    修法有两半：姓名要从**用户原话**（source_text）落档到成员，归属以点名为准。
    """

    def _ingest(self, hh, text, extracted, source_text=None, members=('1', '2')):
        stub = _StubModel(extracted)
        original = full_facts.ask
        full_facts.ask = stub
        try:
            facts.state(hh)
            hh['policy_core']['members'] = list(members)
            full_facts.ingest(hh, text, facts.dictionary(), object(), source_text=source_text)
        finally:
            full_facts.ask = original
        return hh

    def test_name_is_bound_from_user_wording_not_normalized_text(self):
        """规范化把「母亲张兰58岁」改成「成员2，58岁」，姓名只留在原话里。"""
        hh = self._ingest(
            {}, '成员2今年58岁。', {'facts': [['2', 'age', 58, 0]], 'members': ['1', '2']},
            source_text='我家两口人，母亲张兰58岁。我本人30岁。')
        self.assertEqual(hh['policy_core'].get('member_names', {}).get('2'), '张兰')

    def test_named_denial_overrides_model_member_number(self):
        """模型说成员1，原话却点名张兰（成员2）⇒ 以点名为准落 2:disabled=false。"""
        hh = self._ingest(
            {}, '成员2今年58岁。', {'facts': [['2', 'age', 58, 0]], 'members': ['1', '2']},
            source_text='我家两口人，母亲张兰58岁。我本人30岁。')
        hh = self._ingest(
            hh, '张兰无残疾。', {'facts': [['1', 'disabled', False, 0]], 'members': ['1', '2']})
        self.assertIn('2:disabled', hh['policy_core']['facts'])
        self.assertIs(hh['policy_core']['facts']['2:disabled']['value'], False)


if __name__ == '__main__':
    unittest.main()
