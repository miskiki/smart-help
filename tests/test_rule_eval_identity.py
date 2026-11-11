# -*- coding: utf-8 -*-
"""户级身份事实 → 成员视角的别名（避免"非脱贫户"家庭出脱贫身份政策卡片）。"""
import unittest

from policy_core import rule_eval


def hh(facts, members=(1,)):
    st = {'facts': facts}
    if members:
        st['members'] = [{'member_no': m} for m in members]
    return {'policy_core': st}


class FactViewIdentityAliasTest(unittest.TestCase):
    def test_household_poverty_exit_false_reaches_every_member(self):
        facts = {'household:poverty_exit': {'state': 'known', 'value': False},
                 '1:age': {'state': 'known', 'value': 62}}
        view = rule_eval.fact_view(hh(facts, members=(1, 2)))
        for no in ('1', '2'):
            self.assertEqual(view[no + ':member_poverty_exit']['state'], 'known')
            self.assertFalse(view[no + ':member_poverty_exit']['value'])

    def test_household_monitoring_known_reaches_member(self):
        facts = {'household:monitoring': {'state': 'known', 'value': True},
                 '1:age': {'state': 'known', 'value': 40}}
        view = rule_eval.fact_view(hh(facts))
        self.assertTrue(view['1:member_monitoring']['value'])

    def test_unknown_household_fact_does_not_invent_member_fact(self):
        facts = {'household:poverty_exit': {'state': 'unknown'},
                 '1:age': {'state': 'known', 'value': 62}}
        view = rule_eval.fact_view(hh(facts))
        self.assertNotIn('1:member_poverty_exit', view)

    def test_member_specific_fact_wins_over_household_alias(self):
        facts = {'household:poverty_exit': {'state': 'known', 'value': False},
                 '1:member_poverty_exit': {'state': 'known', 'value': True}}
        view = rule_eval.fact_view(hh(facts))
        self.assertTrue(view['1:member_poverty_exit']['value'])

    def test_no_members_means_no_aliases(self):
        facts = {'household:poverty_exit': {'state': 'known', 'value': False}}
        view = rule_eval.fact_view(hh(facts, members=()))
        self.assertEqual([k for k in view if 'member_poverty_exit' in k], [])


if __name__ == '__main__':
    unittest.main()
