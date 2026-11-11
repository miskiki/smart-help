# -*- coding: utf-8 -*-
"""P3 只读工具：观测结构、三值求值、引文逐字回链。"""
import unittest

from policy_core import tools

FIELDS = {
    'age': dict(type='number', label='年龄', scope='member'),
    'disability_level': dict(type='text', label='残疾等级', scope='member',
                             values=['一级', '二级', '三级', '四级']),
    'poverty_exit': dict(type='boolean', label='是否脱贫户', scope='household'),
}


def pack(code='S-ZB-090', name='困难残疾人生活补贴', region='莲花县', **source):
    src = dict(title=name, target='持有第二代残疾人证的残疾人',
               conditions='家庭属于低保或脱贫户；残疾等级为一至四级',
               benefit='每人每月100元', process='向乡镇申请')
    src.update(source)
    return dict(id=code + ':main',
                policy=dict(code=code, name=name, regions=[region]),
                detail=dict(region=region, detail_id='main'), source=src)


def ctx(packs=None, facts=None, decisions=None, region='莲花县'):
    return dict(packs=packs if packs is not None else [pack()],
                hh=dict(facts=facts or {'household:region': dict(state='known', value='莲花县'),
                                        '1:age': dict(state='known', value=62),
                                        '1:disability_level': dict(state='known', value='二级')}),
                decisions=decisions or [], fields=FIELDS, region=region)


class ReadClauseTest(unittest.TestCase):
    def test_read_whole_clause_and_one_field(self):
        c = ctx()
        whole = tools.run('read_clause', {'code': 'S-ZB-090'}, c)
        self.assertTrue(whole['ok'])
        self.assertIn('残疾人证', whole['text'])
        one = tools.run('read_clause', {'code': 'S-ZB-090', 'field': 'benefit'}, c)
        self.assertEqual(one['text'], '每人每月100元')

    def test_unknown_policy_and_bad_field_are_observations(self):
        c = ctx()
        self.assertFalse(tools.run('read_clause', {'code': 'X-999'}, c)['ok'])
        bad = tools.run('read_clause', {'code': 'S-ZB-090', 'field': 'secret'}, c)
        self.assertEqual(bad['error'], 'unsupported_field')

    def test_verbatim_ok_folds_whitespace_only(self):
        source = '家庭属于低保或脱贫户；残疾等级为一至四级'
        self.assertTrue(tools.verbatim_ok('残疾等级为一至 四级', source))
        self.assertFalse(tools.verbatim_ok('残疾等级为二级', source))
        self.assertFalse(tools.verbatim_ok('', source))


class RegionAndFactsTest(unittest.TestCase):
    def test_region_applicable_and_unknown(self):
        c = ctx()
        self.assertEqual(tools.run('region_check', {'code': 'S-ZB-090'}, c)['status'], 'applicable')
        empty = ctx(region='')
        self.assertEqual(tools.run('region_check', {'code': 'S-ZB-090'}, empty)['status'], 'unknown')

    def test_member_facts_known_only(self):
        c = ctx(facts={'1:age': dict(state='known', value=62),
                       '1:disability_level': dict(state='unknown'),
                       'household:poverty_exit': dict(state='known', value=False)})
        obs = tools.run('member_facts', {'member_no': 1}, c)
        self.assertEqual([f['field'] for f in obs['facts']], ['age'])
        hh_obs = tools.run('member_facts', {'member_no': 'household'}, c)
        self.assertEqual(hh_obs['facts'][0]['value'], False)

    def test_condition_three_value(self):
        c = ctx()
        sat = tools.run('evaluate_condition',
                        {'field': 'age', 'op': 'gte', 'value': 60, 'member_no': 1}, c)
        self.assertEqual(sat['state'], 'satisfied')
        viol = tools.run('evaluate_condition',
                         {'field': 'disability_level', 'op': 'in', 'value': ['一级'],
                          'member_no': 1}, c)
        self.assertEqual(viol['state'], 'violated')
        unknown = tools.run('evaluate_condition', {'field': 'poverty_exit', 'value': True}, c)
        self.assertEqual(unknown['state'], 'unknown')
        self.assertEqual(unknown['missing'], ['household:poverty_exit'])

    def test_condition_rejects_undefined_field(self):
        obs = tools.run('evaluate_condition', {'field': 'nope', 'value': True}, ctx())
        self.assertEqual(obs['error'], 'undefined_field')


class SearchAndPriorTest(unittest.TestCase):
    def test_search_policy_returns_codes(self):
        c = ctx(packs=[pack(), pack('X-ZB-300', '脱贫人口小额信贷', target='脱贫户')])
        obs = tools.run('search_policy', {'query': '脱贫'}, c)
        self.assertTrue(obs['hits'])
        self.assertEqual(obs['hits'][0]['code'], 'X-ZB-300')

    def test_prior_judgement_reads_decision_and_verifies_quote(self):
        pack_obj = pack()
        decision = dict(source_id=pack_obj['id'], status='rejected',
                        object_matches=[dict(meaning='残疾人', citation=dict(quote='持有第二代残疾人证'))],
                        unknowns=[dict(fact_key='household:poverty_exit', requirement='低保或脱贫户')])
        c = ctx(packs=[pack_obj], decisions=[decision])
        obs = tools.run('prior_judgement', {'code': 'S-ZB-090'}, c)
        self.assertEqual(obs['status'], 'rejected')
        self.assertTrue(obs['matches'][0]['quote_ok'])
        self.assertEqual(obs['unknowns'][0]['fact_key'], 'household:poverty_exit')

    def test_prior_judgement_marks_unbound_quote(self):
        pack_obj = pack()
        decision = dict(source_id=pack_obj['id'], status='rejected',
                        object_matches=[dict(meaning='编造', citation=dict(quote='残疾等级必须为二级'))])
        c = ctx(packs=[pack_obj], decisions=[decision])
        obs = tools.run('prior_judgement', {'code': 'S-ZB-090'}, c)
        self.assertFalse(obs['matches'][0]['quote_ok'])

    def test_unknown_tool_is_observation(self):
        obs = tools.run('write_policy', {'code': 'S-ZB-090'}, ctx())
        self.assertFalse(obs['ok'])
        self.assertEqual(obs['error'], 'unknown_tool')


if __name__ == '__main__':
    unittest.main()
