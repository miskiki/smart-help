# -*- coding: utf-8 -*-
"""M0 T4 地区软过滤单测：确定性地区语义 + 程序优先裁决 + 证据/检索接入。

覆盖：地区归一/关系判定/政策适用裁决、catalog.applicable_region 程序优先与旧图谱回落、
evidence_loader 软过滤保留/硬丢弃（假 DB 引擎隔离）、retrieval 地区 SQL 只加宽不收缩、
facts 地区等价层。全部标准库 unittest；不连生产库、不改生产数据。
"""
import os
import unittest
from contextlib import contextmanager
from unittest import mock

from policy_core import catalog, evidence_loader, facts, geo, region, retrieval


@contextmanager
def region_env(value):
    """临时设置 REGION_SOFT_FILTER；value=None 表示不设置（走默认值）。"""
    saved = os.environ.get('REGION_SOFT_FILTER')
    try:
        if value is None:
            os.environ.pop('REGION_SOFT_FILTER', None)
        else:
            os.environ['REGION_SOFT_FILTER'] = value
        yield
    finally:
        if saved is None:
            os.environ.pop('REGION_SOFT_FILTER', None)
        else:
            os.environ['REGION_SOFT_FILTER'] = saved


class CanonicalLevelsTest(unittest.TestCase):
    def test_full_address(self):
        got = region.canonical_levels('江西省南昌市新建区')
        self.assertEqual((got['province'], got['city'], got['county']), ('江西省', '南昌市', '新建区'))
        self.assertEqual(got['level'], 'county')
        self.assertFalse(got['nation'])
        self.assertFalse(got['ambiguous'])
        self.assertFalse(got['outside'])

    def test_province_aliases(self):
        for text in ('江西省', '江西', '赣', '赣省'):
            got = region.canonical_levels(text)
            self.assertEqual(got['province'], '江西省', text)
            self.assertEqual(got['level'], 'province', text)

    def test_city_and_county_variants(self):
        cases = {
            '南昌市新建区': ('南昌市', '新建区'),
            '新建区': ('南昌市', '新建区'),
            '江西省新建区': ('南昌市', '新建区'),
            '九江市修水县': ('九江市', '修水县'),
            '莲花县琴亭镇': ('萍乡市', '莲花县'),
            '吉安县': ('吉安市', '吉安县'),
            '南昌县': ('南昌市', '南昌县'),
        }
        for text, (city, county) in cases.items():
            got = region.canonical_levels(text)
            self.assertEqual((got['city'], got['county']), (city, county), text)
            self.assertEqual(got['province'], '江西省', text)

    def test_nation(self):
        got = region.canonical_levels('全国')
        self.assertTrue(got['nation'])
        self.assertEqual(got['level'], 'nation')

    def test_empty_and_unknown(self):
        for text in ('', None, '   '):
            got = region.canonical_levels(text)
            self.assertEqual(got['level'], 'unknown', repr(text))
            self.assertFalse(got['outside'], repr(text))
        got = region.canonical_levels('火星镇')
        self.assertEqual(got['level'], 'unknown')
        self.assertFalse(got['outside'])

    def test_road_name_not_other_province(self):
        # "上海路"包含外省简称，但只是道路名，不能判明确外省。
        got = region.canonical_levels('上海路88号')
        self.assertFalse(got['outside'])
        self.assertEqual(got['level'], 'unknown')

    def test_multiple_counties_ambiguous(self):
        got = region.canonical_levels('新建区、进贤县')
        self.assertTrue(got['ambiguous'])
        self.assertIsNone(got['county'])
        self.assertEqual(got['city'], '南昌市')  # 两县同属南昌市，市级仍确定

    def test_other_province(self):
        for text in ('湖南省株洲市', '户籍在湖南', '我是湖南人'):
            got = region.canonical_levels(text)
            self.assertTrue(got['outside'], text)
            self.assertEqual(got['province'], '湖南省', text)
        self.assertFalse(region.canonical_levels('南昌市上海路')['outside'])

    def test_explicit_other(self):
        for text in ('湖南省株洲市', '户籍在湖南', '外省', '省外'):
            self.assertTrue(region.explicit_other(text), text)
        for text in ('', '南昌市新建区', '火星镇', '省外务工'):
            self.assertFalse(region.explicit_other(text), text)


class RegionRelationTest(unittest.TestCase):
    def relation(self, household, policy_region):
        return region.region_relation(household, policy_region)['relation']

    def test_same_address(self):
        for household, policy_region in (
            ('江西省南昌市新建区', '江西省南昌市新建区'),
            ('南昌市新建区', '新建区'),
            ('江西省新建区', '南昌市新建区'),
        ):
            self.assertEqual(self.relation(household, policy_region), 'same', (household, policy_region))

    def test_policy_covers_household(self):
        # 政策地区是家庭地区的上级：省 vs 县、市 vs 该市县、全国 vs 县。
        cases = (
            ('江西省南昌市新建区', '江西省'),
            ('江西省南昌市新建区', '南昌市'),
            ('江西省南昌市新建区', '全国'),
            ('新建区', '江西省'),
        )
        for household, policy_region in cases:
            got = region.region_relation(household, policy_region)
            self.assertEqual(got['relation'], 'ancestor', (household, policy_region))
            self.assertTrue(region.relation_is_applicable(got), (household, policy_region))

    def test_disjoint_same_city_different_county(self):
        self.assertEqual(self.relation('南昌市新建区', '南昌市南昌县'), 'disjoint')
        self.assertEqual(self.relation('南昌市新建区', '南昌县'), 'disjoint')

    def test_disjoint_same_province_different_city(self):
        self.assertEqual(self.relation('南昌市新建区', '九江市修水县'), 'disjoint')

    def test_outer_province_disjoint(self):
        self.assertEqual(self.relation('湖南省株洲市', '江西省南昌市'), 'disjoint')
        self.assertEqual(self.relation('湖南省株洲市', '全国'), 'ancestor')

    def test_jx_province_vs_variant(self):
        # "江西省" 与 "江西省新建区" 是同一事实的粗细写法，不能判 disjoint。
        self.assertTrue(region.equivalent_regions('江西省', '江西省新建区'))
        self.assertEqual(self.relation('江西省新建区', '江西省'), 'ancestor')
        self.assertEqual(self.relation('江西省', '江西省新建区'), 'descendant')
        self.assertFalse(region.relation_is_applicable(self.relation('江西省', '江西省新建区')))

    def test_unknown_household(self):
        for household in ('', None, '火星镇'):
            got = region.region_relation(household, '江西省南昌市')
            self.assertEqual(got['relation'], 'unknown', repr(household))
            self.assertEqual(got['confidence'], 'low')
            self.assertFalse(region.relation_is_applicable(got))

    def test_ambiguous_county_not_applicable(self):
        # 区县归属有歧义时不裁决（保留候选），绝不因此误放行。
        got = region.region_relation('新建区、进贤县', '南昌县')
        self.assertFalse(region.relation_is_applicable(got))
        self.assertNotEqual(got['relation'], 'same')
        same_city = region.region_relation('新建区、进贤县', '南昌市')
        self.assertTrue(region.relation_is_applicable(same_city))

    def test_relation_is_applicable_helper(self):
        self.assertTrue(region.relation_is_applicable('same'))
        self.assertTrue(region.relation_is_applicable('ancestor'))
        self.assertTrue(region.relation_is_applicable({'relation': 'ancestor'}))
        for other in ('descendant', 'disjoint', 'unknown', None):
            self.assertFalse(region.relation_is_applicable(other), other)


class PolicyVerdictTest(unittest.TestCase):
    def verdict(self, household, regions=None, detail_region=None):
        policy = {'regions': regions} if regions is not None else {}
        detail = {'region': detail_region} if detail_region is not None else {}
        return region.policy_region_verdict(policy, detail, household)

    def test_applicable_cases(self):
        cases = (
            ('南昌市新建区', '江西省', None),
            ('南昌市新建区', '南昌市', None),
            ('南昌市新建区', '全国', None),
            ('南昌市新建区', '江西省、南昌市', None),
            ('南昌市新建区', '江西省', ''),        # 明细地区缺失也按政策级判定
            ('南昌市新建区', None, '南昌市'),      # 没有政策级地区时按明细级判定
        )
        for household, regions, detail_region in cases:
            self.assertIs(self.verdict(household, regions, detail_region), True, (household, regions, detail_region))

    def test_explicit_disjoint_false(self):
        cases = (
            ('南昌市新建区', '南昌县', None),
            ('南昌市新建区', '九江市', None),
            ('湖南省株洲市', '江西省', None),
            ('南昌市新建区', None, '南昌县'),
        )
        for household, regions, detail_region in cases:
            self.assertIs(self.verdict(household, regions, detail_region), False, (household, regions, detail_region))

    def test_unknown_returns_none(self):
        cases = (
            ('', '江西省', None),                  # 家庭地区未知
            ('火星镇', '江西省', None),            # 家庭地区无法解析
            ('南昌市新建区', '火星镇', None),      # 政策地区无法解析
            ('南昌市新建区', '火星镇', '火星镇'),  # 政策+明细都无法解析
            ('南昌市新建区', None, None),          # 双方都没有地区
        )
        for household, regions, detail_region in cases:
            self.assertIsNone(self.verdict(household, regions, detail_region), (household, regions, detail_region))

    def test_policy_detail_conflict_returns_none(self):
        # 政策级"南昌市"适用、明细级"南昌县"不适用 → 不越过程序裁决，交给原图谱判定。
        self.assertIsNone(self.verdict('南昌市新建区', '南昌市', '南昌县'))


class CatalogApplicableRegionTest(unittest.TestCase):
    def instance(self, regions=None, detail_region=None):
        policy = {}
        detail = {}
        if regions is not None:
            policy['regions'] = regions
        if detail_region is not None:
            detail['region'] = detail_region
        return {'policy': policy, 'detail': detail}

    def test_program_verdict_applicable(self):
        with region_env('1'):
            self.assertIs(catalog.applicable_region(self.instance(regions='江西省'), '南昌市新建区', nodes={}), True)
            self.assertIs(catalog.applicable_region(self.instance(regions='南昌市'), '新建区', nodes={}), True)
            self.assertIs(catalog.applicable_region(self.instance(regions='全国'), '新建区', nodes={}), True)

    def test_program_verdict_disjoint_false(self):
        with region_env('1'):
            self.assertIs(catalog.applicable_region(self.instance(regions='南昌县'), '南昌市新建区', nodes={}), False)
            self.assertIs(catalog.applicable_region(self.instance(regions='江西省'), '湖南省株洲市', nodes={}), False)

    def test_empty_region_or_no_region_info_returns_none(self):
        with region_env('1'):
            self.assertIsNone(catalog.applicable_region(self.instance(regions='江西省'), '', nodes={}))
            self.assertIsNone(catalog.applicable_region(self.instance(), '南昌市新建区', nodes={}))

    def _patch_judge(self, statuses):
        calls = []

        def fake_judge(policy, detail, hh_region, nodes, hh_nodes=None, region_idx=None):
            calls.append(hh_region)
            return statuses(len(calls) - 1), 'fake_judge'

        return calls, mock.patch('kg_retrieval.region_judge', fake_judge)

    def test_fallback_to_graph_judge_when_program_unknown(self):
        calls, patcher = self._patch_judge(lambda index: '地区待细化')
        with region_env('1'), patcher:
            got = catalog.applicable_region(self.instance(regions='火星镇', detail_region='火星镇'), '新建区', nodes={})
        self.assertIsNone(got)
        self.assertEqual(len(calls), 1)  # 程序判不出来时才回落到原图谱判定

    def test_graph_status_mapping(self):
        expectations = (('适用', True), ('不适', False), ('冲突', None), ('地区待细化', None))
        for status, expected in expectations:
            calls, patcher = self._patch_judge(lambda index, status=status: status)
            with region_env('1'), patcher:
                got = catalog.applicable_region(self.instance(regions='火星镇', detail_region='火星镇'), '新建区', nodes={})
            self.assertIs(got, expected, status)

    def test_soft_filter_off_strictly_delegates_to_graph(self):
        calls, patcher = self._patch_judge(lambda index: '不适')
        with region_env('0'), patcher:
            # 开关关闭：即使程序层能判"江西省覆盖新建区"，也必须走旧图谱判定。
            got = catalog.applicable_region(self.instance(regions='江西省'), '新建区', nodes={})
        self.assertIs(got, False)
        self.assertEqual(calls, ['新建区'])

    def test_soft_filter_on_skips_graph_when_decided(self):
        calls, patcher = self._patch_judge(lambda index: '不适')
        with region_env('1'), patcher:
            got = catalog.applicable_region(self.instance(regions='江西省'), '新建区', nodes={})
        self.assertIs(got, True)
        self.assertEqual(calls, [])

    def test_detail_region_only(self):
        with region_env('1'):
            got = catalog.applicable_region(self.instance(regions='', detail_region='南昌市'), '新建区', nodes={})
        self.assertIs(got, True)


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return list(self._rows)


class _FakeConn:
    """最小假连接：只按语句内容返回 policy/policy_detail 行。"""

    def __init__(self, policy_rows, detail_rows):
        self._policy_rows = policy_rows
        self._detail_rows = detail_rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if 'FROM policy_detail' in sql:
            return _FakeResult(self._detail_rows)
        if 'FROM policy' in sql:
            return _FakeResult(self._policy_rows)
        return _FakeResult([])


class _FakeEngine:
    def __init__(self, policy_rows, detail_rows):
        self._policy_rows = policy_rows
        self._detail_rows = detail_rows

    def connect(self):
        return _FakeConn(self._policy_rows, self._detail_rows)

    def dispose(self):
        pass


def _policy_row(regions='江西省'):
    return dict(policy_code='P1', policy_id=1, policy_name='测试政策', cur_version='v1',
                status='在用', level='S', regions=regions, category1=None, category2=None,
                category3=None, target=None, conditions=None, benefit=None, process=None,
                doc_ref=None, effect_date=None, expire_date=None)


def _detail_row(region=''):
    return dict(detail_id=11, policy_id=1, region=region, effect_date=None, expire_date=None,
                target=None, conditions=None, benefit=None, process=None)


class EvidenceLoaderRegionTest(unittest.TestCase):
    def load_with_fake_db(self, region_text, policy_regions='江西省', detail_region=''):
        engine = _FakeEngine([_policy_row(policy_regions)], [_detail_row(detail_region)])
        self.events = []
        self.traces = []

        def recorder(name, **fields):
            self.events.append((name, fields))

        def trace_recorder(name, **fields):
            self.traces.append((name, fields))

        import chain_trace
        candidates = [dict(policy_code='P1', policy_version='v1', detail_id=11)]
        with mock.patch('kb_rag._db', lambda: engine), \
                mock.patch.object(evidence_loader, 'load_bundle', lambda: ({}, [])), \
                mock.patch.object(evidence_loader, 'source_record', lambda *a, **k: {}), \
                mock.patch.object(evidence_loader, 'event', recorder), \
                mock.patch.object(chain_trace, 'event', trace_recorder, create=True):
            return evidence_loader.load(candidates, region_text, {})

    def test_soft_filter_default_on(self):
        with region_env(None):
            self.assertTrue(region.soft_filter_enabled())
        with region_env('0'):
            self.assertFalse(region.soft_filter_enabled())
        with region_env('false'):
            self.assertFalse(region.soft_filter_enabled())

    def test_off_region_empty_short_circuits(self):
        with region_env('0'):
            packs, stats = evidence_loader.load([dict(policy_code='P1', policy_version='v1', detail_id=11)], '', {})
        self.assertEqual(packs, [])
        self.assertEqual(stats.get('reason'), 'region_unknown')

    def test_on_region_empty_keeps_candidate_as_unknown(self):
        with region_env('1'):
            packs, stats = self.load_with_fake_db('')
        self.assertEqual(len(packs), 1)
        self.assertIsNone(packs[0]['region_status'])
        self.assertEqual(packs[0]['region_signal'], 'unknown')
        self.assertEqual(stats['region_unknown_kept'], 1)
        self.assertEqual(stats['dropped'].get('region_unconfirmed_kept'), 1)
        names = [name for name, _ in self.traces]
        self.assertIn('region_soft_filter', names)
        self.assertNotIn('region.normalized', names)  # 家庭地区为空不产生归一事件
        fields = dict(self.traces)['region_soft_filter']
        self.assertEqual((fields['kept'], fields['dropped'], fields['unknown']), (1, 0, 1))

    def test_on_explicit_other_still_dropped(self):
        with region_env('1'):
            packs, stats = self.load_with_fake_db('湖南省株洲市', policy_regions='江西省')
        self.assertEqual(packs, [])
        self.assertEqual(stats['dropped'].get('region'), 1)
        self.assertEqual(stats['region_dropped'][0]['code'], 'P1')
        fields = dict(self.traces)['region_soft_filter']
        self.assertEqual((fields['kept'], fields['dropped']), (0, 1))

    def test_on_applicable_kept_with_normalized_event(self):
        with region_env('1'):
            packs, stats = self.load_with_fake_db('南昌市新建区', policy_regions='江西省')
        self.assertEqual(len(packs), 1)
        self.assertIs(packs[0]['region_status'], True)
        self.assertEqual(packs[0]['region_signal'], 'applicable')
        self.assertEqual(stats['region_unknown_kept'], 0)
        normalized = dict(self.traces)['region.normalized']
        self.assertEqual((normalized['province'], normalized['city'], normalized['county']),
                         ('江西省', '南昌市', '新建区'))
        self.assertEqual(normalized['source'], 'household:region')
        # 事件字段只用安全标量，不放用户原话。
        for value in normalized.values():
            self.assertIsInstance(value, (str, int, float, bool))


class _SqlRecorder:
    def __init__(self):
        self.query = None
        self.params = None

    def __call__(self, statement, params=None):
        sql = str(statement)
        if sql.startswith('SELECT p.policy_code'):
            self.query = sql
            self.params = params
        return _FakeResult([])


class _SqlConn:
    def __init__(self, recorder):
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        return self._recorder(statement, params)


class _SqlEngine:
    def __init__(self, recorder):
        self._recorder = recorder

    def connect(self):
        return _SqlConn(self._recorder)

    def dispose(self):
        pass


class RetrievalRegionScopeTest(unittest.TestCase):
    def keyword_query(self, env_value):
        recorder = _SqlRecorder()
        intents = [dict(keywords=['雨露计划'], region='江西省南昌市新建区')]
        with region_env(env_value), mock.patch('kb_rag._db', lambda: _SqlEngine(recorder)):
            retrieval.source_keyword_hits(intents)
        return recorder.query, recorder.params

    def test_soft_on_adds_city_branch_and_keeps_wide_scope(self):
        query, params = self.keyword_query('1')
        # 旧宽召回条件一条不少：省/全国/全省/空地区都要保留。
        self.assertIn('d.region IS NULL', query)
        self.assertIn("d.region IN ('','江西省','全省','全国','国家','省级')", query)
        self.assertIn("d.region LIKE '%全省%'", query)
        self.assertIn("d.region LIKE '%全国%'", query)
        # 新增市级 OR 分支（只加宽、不收缩）。
        self.assertIn('d.region LIKE :city', query)
        self.assertEqual(params.get('city'), '%南昌市%')
        self.assertEqual(params.get('county'), '%新建区%')

    def test_soft_off_keeps_old_sql_scope(self):
        query, params = self.keyword_query('0')
        self.assertNotIn(':city', query)
        self.assertNotIn('city', params)
        self.assertIn('d.region LIKE :county', query)


class FactsRegionEquivalenceTest(unittest.TestCase):
    def test_region_field_accepts_mutual_variants(self):
        # "南昌市新建区" 与 "江西省新建区" 互不包含，通用等价层判不了，地区专用等价层能判。
        self.assertTrue(facts._equivalent_values({}, '南昌市新建区', '江西省新建区', field='region'))
        self.assertFalse(facts._equivalent_values({}, '南昌市新建区', '江西省新建区'))

    def test_record_variant_does_not_conflict(self):
        st = facts.state({})
        facts.record(st, 'household:region', '江西省', quote='原文一')
        facts.record(st, 'household:region', '江西省新建区', quote='原文二')
        fact = st['facts']['household:region']
        self.assertEqual(fact['state'], 'known')
        self.assertEqual(fact['value'], '江西省新建区')  # 保留更具体写法，不改写用户原话
        self.assertEqual([source['quote'] for source in fact['sources']], ['原文一', '原文二'])

    def test_record_mutual_variant_does_not_conflict(self):
        st = facts.state({})
        facts.record(st, 'household:region', '南昌市新建区', quote='原文一')
        facts.record(st, 'household:region', '江西省新建区', quote='原文二')
        fact = st['facts']['household:region']
        self.assertEqual(fact['state'], 'known')
        self.assertIn(fact['value'], ('南昌市新建区', '江西省新建区'))

    def test_record_different_county_still_conflicts(self):
        st = facts.state({})
        facts.record(st, 'household:region', '新建区', quote='原文一')
        facts.record(st, 'household:region', '南昌县', quote='原文二')
        self.assertEqual(st['facts']['household:region']['state'], 'conflict')


if __name__ == '__main__':
    unittest.main()
