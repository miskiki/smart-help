"""Member-name labels and age-gated education questions.

Covers the four user-visible clarification bugs:
  1. member questions were all "成员N：" with no name, so users could not tell
     which person to answer for -> the discoverer is now asked with a name.
  2. the same field was sometimes asked more than once -> questions are keyed by
     the member they are about, so a member is asked once per round.
  3. every member was asked everything -> education-era questions are only sent
     to members whose age makes study plausible.
  4. elderly members were asked about school -> age gate drops those questions.
"""
import os
import unittest

from policy_core import member_labels
from policy_core.member_labels import (
    MEMBER_LABEL_NAME, AGE_GATED_QUESTION, cn2int, extract_member_names,
    member_display, education_question_allowed, member_label_name_enabled,
    age_gated_questions_enabled,
)


class _Env(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        for name in (MEMBER_LABEL_NAME, AGE_GATED_QUESTION):
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def set_switches(self, names='1', age='1'):
        os.environ[MEMBER_LABEL_NAME] = names
        os.environ[AGE_GATED_QUESTION] = age


class NumberTest(_Env):
    def test_cn2int_shapes(self):
        self.assertEqual(cn2int('3'), 3)
        self.assertEqual(cn2int('十'), 10)
        self.assertEqual(cn2int('一'), 1)
        self.assertIsNone(cn2int('十 十'))


class ExtractTest(_Env):
    def test_parentheses(self):
        self.assertEqual(extract_member_names('成员1（钱七）年龄60岁'), {1: '钱七'})

    def test_colon(self):
        self.assertEqual(extract_member_names('成员1:张三 年龄45岁'), {1: '张三'})

    def test_spaced_before_field(self):
        self.assertEqual(extract_member_names('成员1 张三 年龄45岁'), {1: '张三'})

    def test_skips_field_not_name(self):
        # "年龄" is not a person; the name must be absent rather than "年龄".
        self.assertEqual(extract_member_names('成员1年龄60岁'), {})

    def test_two_members(self):
        names = extract_member_names('成员1（张三）、成员2（李四）都在上学')
        self.assertEqual(names, {1: '张三', 2: '李四'})

    def test_household_prefix(self):
        self.assertEqual(extract_member_names('家庭成员1 王五 热心'), {1: '王五'})

    def test_none_without_text(self):
        self.assertEqual(extract_member_names(None), {})


class DisplayTest(_Env):
    def test_name_shown_by_default(self):
        self.assertEqual(member_display('3', '钱七'), '成员3（钱七）')

    def test_no_name_is_number_only(self):
        self.assertEqual(member_display('3', None), '成员3')

    def test_switch_off_hides_name(self):
        self.set_switches(names='0', age='0')
        self.assertEqual(member_display('3', '钱七'), '成员3')

    def test_non_chinese_name_ignored(self):
        self.assertEqual(member_display('3', 'Abc'), '成员3')

    def test_household_present(self):
        found = extract_member_names('八旬老人 钱七 家庭情况：成员1（钱七）')
        self.assertEqual(found, {1: '钱七'})


class AgeGateTest(_Env):
    ST = {'facts': {'1:age': {'state': 'known', 'value': 60},
                    '1:enrolled': {'state': 'unknown'}}}

    def test_switch_default_on(self):
        self.assertTrue(member_label_name_enabled())
        self.assertTrue(age_gated_questions_enabled())

    def test_switch_cleared_allows_everything(self):
        self.set_switches(names='1', age='0')
        self.assertTrue(education_question_allowed(self.ST, '1'))

    def test_elderly_member_blocked(self):
        self.assertFalse(education_question_allowed(self.ST, '1'))

    def test_unknown_age_blocked_until_known(self):
        st = {'facts': {'1:age': {'state': 'unknown'}}}
        self.assertFalse(education_question_allowed(st, '1'))

    def test_plausible_age_allowed(self):
        st = {'facts': {'1:age': {'state': 'known', 'value': 17}}}
        self.assertTrue(education_question_allowed(st, '1'))

    def test_young_child_asked_age_before_school(self):
        st = {'facts': {'1:age': {'state': 'known', 'value': 2}}}
        self.assertFalse(education_question_allowed(st, '1'))

    def test_degree_age_allowed(self):
        st = {'facts': {'2:age': {'state': 'known', 'value': 40}}}
        self.assertTrue(education_question_allowed(st, '2'))


class TestKinshipAndNameDiscovery(unittest.TestCase):
    """缺陷 B：用户说"我母亲张兰"，系统只学会"成员N"，名字永远发现不了。

    _MEMBER_HEAD 只认字面量"成员1"，于是 member_display 的
    "成员2（张兰）"渲染能力一直空转，追问只能显示"成员2："。
    """

    def test_kinship_with_name(self):
        found = member_labels.extract_kinship_roles('我母亲张兰58岁，我本人30岁')
        self.assertEqual(found.get('母亲'), '张兰')
        self.assertEqual(found.get('本人'), '')

    def test_plain_name_next_to_kinship(self):
        found = member_labels.extract_kinship_roles('张兰是我母亲，60岁')
        self.assertEqual(found.get('母亲'), '张兰')

    def test_unknown_kinship_is_absent(self):
        self.assertEqual(member_labels.extract_kinship_roles('家里三口人'), {})

    def test_field_word_is_not_a_name(self):
        """"母亲无残疾"里的"无残疾"是字段词，不是人名。"""
        self.assertEqual(member_labels.extract_kinship_roles('我母亲无残疾'), {'母亲': ''})

    def test_member_head_still_works(self):
        self.assertEqual(member_labels.extract_member_names('成员1（张三）'), {1: '张三'})

    def test_kinship_binds_only_when_one_member_is_left(self):
        text = '我母亲张兰58岁'
        self.assertEqual(member_labels.extract_member_names(text), {})
        # 两个成员都没编号：谁是母亲无法判定，不猜。
        self.assertEqual(member_labels.extract_member_names(text, known_members=['1', '2']), {})
        # 另一个成员已被"成员1（张三）"指名，剩下那个只能是母亲。
        self.assertEqual(
            member_labels.extract_member_names('成员1（张三），我母亲张兰58岁', known_members=['1', '2']),
            {1: '张三', '2': '张兰'})

    def test_member_display_renders_name(self):
        os.environ[member_labels.MEMBER_LABEL_NAME] = '1'
        try:
            self.assertEqual(member_labels.member_display('2', '张兰'), '成员2（张兰）')
        finally:
            os.environ.pop(member_labels.MEMBER_LABEL_NAME, None)


if __name__ == '__main__':
    unittest.main()

class TestNamedOwnerBinding(unittest.TestCase):
    """原话点名的人，事实必须能落到那个人身上（真链路第二轮就是这么丢的）。"""

    TEXT = '我家两口人，母亲张兰58岁。我本人30岁。'

    def test_kinship_name_is_discovered_from_colloquial_speech(self):
        from policy_core.member_labels import extract_kinship_roles
        roles = extract_kinship_roles(self.TEXT)
        self.assertEqual(roles.get('母亲'), '张兰')

    def test_named_quote_overrides_model_member_number(self):
        from policy_core import full_facts
        fields = {'disabled': {'scope': 'member', 'type': 'boolean',
                               'label': '残疾人', 'meaning': '残疾', 'question': '是否残疾'}}
        text = '张兰无残疾。'
        # 模型把点名的人算成成员1，但姓名映射说张兰是成员2 → 以点名为准，不整条拒
        got = full_facts.validate_fact(
            {'entity': '1', 'field': 'disabled', 'value': False, 'quote': '张兰无残疾'},
            text, fields, {'1', '2'}, {'2': '张兰'})
        self.assertEqual(got['entity'], '2')
        self.assertIs(got['value'], False)

    def test_unnamed_multi_member_quote_still_rejected(self):
        from policy_core import full_facts
        fields = {'disabled': {'scope': 'member', 'type': 'boolean',
                               'label': '残疾人', 'meaning': '残疾', 'question': '是否残疾'}}
        with self.assertRaises(ValueError):
            full_facts.validate_fact(
                {'entity': '1', 'field': 'disabled', 'value': False, 'quote': '无残疾'},
                '无残疾。', fields, {'1', '2'}, {})


if __name__ == '__main__':
    unittest.main()
