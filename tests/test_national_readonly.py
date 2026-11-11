# -*- coding: utf-8 -*-
"""tests/test_national_readonly.py — coverage_probe 只读覆盖探测单测。

全部用 sqlite 内存库构造 policy_scope 行，不连生产库、不建额外表。
"""
import os
import unittest
from unittest import mock

from sqlalchemy import create_engine, text

from policy_core import admin_division, coverage_probe, policy_scope


def _conn(entries):
    """entries: [(policy_code, regions, details), ...]；返回 (engine, conn)。"""
    engine = create_engine('sqlite:///:memory:')
    conn = engine.connect()
    policy_scope.ensure_schema(conn)
    for policy_code, regions, details in entries:
        policy_scope.replace_policy(
            conn, policy_code, policy_scope.rows_for(policy_code, regions, details or ()))
    conn.commit()
    return engine, conn


class CoverageMatchTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.conn = _conn([('P1', '南昌市新建区', None)])

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def test_result_shape(self):
        cov = coverage_probe.coverage('江西省南昌市新建区', conn=self.conn)
        for key in ('version', 'region', 'level', 'province', 'city', 'county', 'zone',
                    'rows', 'policies', 'covered', 'ancestor_keys', 'reason'):
            self.assertIn(key, cov)
        self.assertEqual(cov['version'], 'coverage-probe-1')
        self.assertEqual(set(cov['region']),
                         {'level', 'province', 'city', 'county', 'zone', 'ambiguous'})

    def test_county_covered(self):
        cov = coverage_probe.coverage('江西省南昌市新建区', conn=self.conn)
        self.assertTrue(cov['covered'])
        self.assertEqual(cov['rows'], 1)
        self.assertEqual(cov['policies'], 1)
        self.assertEqual(cov['level'], 'county')
        self.assertEqual(cov['province'], '江西省')
        self.assertEqual((cov['city'], cov['county']), ('南昌市', '新建区'))
        self.assertFalse(cov['zone'])
        self.assertEqual(cov['reason'], 'covered')
        self.assertEqual(cov['ancestor_keys'],
                         ['CN', 'P:江西省', 'C:江西省/南昌市', 'X:江西省/南昌市/新建区'])

    def test_ancestor_matching_from_wider_policy(self):
        engine, conn = _conn([('P-PROV', '江西省', None), ('P-CITY', '赣州市', None),
                              ('P-NATION', '全国', None)])
        try:
            self.assertTrue(coverage_probe.coverage('江西省赣州市章贡区', conn=conn)['covered'])
            self.assertTrue(coverage_probe.coverage('江西省南昌市新建区', conn=conn)['covered'])
            self.assertTrue(coverage_probe.coverage('湖南省株洲市', conn=conn)['covered'])
        finally:
            conn.close()
            engine.dispose()

    def test_county_policy_does_not_cover_province_or_city(self):
        # 县级政策不能回答"整个江西省/南昌市是否有政策覆盖"
        self.assertFalse(coverage_probe.coverage('江西省', conn=self.conn)['covered'])
        self.assertFalse(coverage_probe.coverage('南昌市', conn=self.conn)['covered'])
        self.assertEqual(coverage_probe.coverage('江西省', conn=self.conn)['reason'],
                         'no_scope_rows')

    def test_detail_rows_are_counted(self):
        engine, conn = _conn([('P-DETAIL', '', [('d1', '南昌市新建区')])])
        try:
            cov = coverage_probe.coverage('南昌市新建区', conn=conn)
            self.assertTrue(cov['covered'])
            self.assertEqual(cov['policies'], 1)
        finally:
            conn.close()
            engine.dispose()


class ZoneCoverageTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.conn = _conn([('P-ZONE', '赣州经济技术开发区', None)])

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def test_zone_policy_does_not_cover_host_county(self):
        cov = coverage_probe.coverage('江西省赣州市章贡区', conn=self.conn)
        self.assertFalse(cov['covered'])
        self.assertEqual(cov['rows'], 0)
        self.assertFalse(any(key.startswith('Z:') for key in cov['ancestor_keys']))

    def test_zone_policy_covers_itself(self):
        cov = coverage_probe.coverage('赣州经济技术开发区', conn=self.conn)
        self.assertTrue(cov['covered'])
        self.assertTrue(cov['zone'])
        self.assertEqual(cov['ancestor_keys'][-1],
                         'Z:江西省/赣州市/赣州经济技术开发区')

    def test_zone_is_not_ancestor(self):
        # 功能区 Z: 行只做精确匹配：任何普通县区的键里都不出现 Z:
        for region in ('江西省赣州市', '江西省赣州市南康区', '江西省南昌市新建区'):
            cov = coverage_probe.coverage(region, conn=self.conn)
            self.assertFalse(any(key.startswith('Z:') for key in cov['ancestor_keys']))


class NationalAndOutsideTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.conn = _conn([('P-NATION', '全国', None)])

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def test_national_covers_every_household(self):
        for region in ('江西省南昌市新建区', '江西省', '湖南省株洲市', '北京市朝阳区'):
            cov = coverage_probe.coverage(region, conn=self.conn)
            self.assertTrue(cov['covered'], region)
            self.assertEqual(cov['ancestor_keys'][0], 'CN')
            self.assertGreaterEqual(cov['policies'], 1)

    def test_outside_province_without_rows(self):
        engine, conn = _conn([('P-JX', '南昌市新建区', None)])
        try:
            cov = coverage_probe.coverage('湖南省株洲市', conn=conn)
            self.assertFalse(cov['covered'])
            self.assertEqual(cov['reason'], 'no_scope_rows')
            self.assertEqual(cov['province'], '湖南省')
            self.assertEqual(cov['city'], '株洲市')
        finally:
            conn.close()
            engine.dispose()


class UnresolvedTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.conn = _conn([('P1', '南昌市新建区', None)])

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def _assert_unresolved(self, text):
        cov = coverage_probe.coverage(text, conn=self.conn)
        self.assertIsNone(cov['covered'])
        self.assertEqual(cov['reason'], 'region_unresolved')
        self.assertEqual(cov['rows'], 0)
        self.assertEqual(cov['ancestor_keys'], [])

    def test_empty_region(self):
        self._assert_unresolved('')
        self._assert_unresolved(None)

    def test_unknown_text(self):
        self._assert_unresolved('火星镇')

    def test_ambiguous_text(self):
        parsed = admin_division.canonical('鼓楼区')
        self.assertTrue(parsed.get('ambiguous'))
        self._assert_unresolved('鼓楼区')


class ReadonlyErrorTest(unittest.TestCase):
    def test_missing_table_returns_none_and_error(self):
        engine = create_engine('sqlite:///:memory:')
        conn = engine.connect()
        try:
            cov = coverage_probe.coverage('南昌市新建区', conn=conn)
            self.assertIsNone(cov['covered'])
            self.assertEqual(cov['reason'], 'query_error')
            self.assertIn('error', cov)
            self.assertTrue(cov['error'])
            # 只读：探测不能顺手建表
            count = conn.execute(text(
                "SELECT count(*) FROM sqlite_master WHERE name='policy_scope'")).scalar()
            self.assertEqual(count, 0)
            # 错误结论不得被表述成"未收录"
            self.assertNotIn('暂未收录', coverage_probe.summary(cov))
            self.assertIn('探测失败', coverage_probe.summary(cov))
        finally:
            conn.close()
            engine.dispose()

    def test_probe_does_not_write_rows(self):
        engine, conn = _conn([('P1', '南昌市新建区', None)])
        try:
            before = conn.execute(text('SELECT count(*) FROM policy_scope')).scalar()
            coverage_probe.coverage('江西省南昌市新建区', conn=conn)
            coverage_probe.coverage('湖南省株洲市', conn=conn)
            after = conn.execute(text('SELECT count(*) FROM policy_scope')).scalar()
            self.assertEqual(before, after)
        finally:
            conn.close()
            engine.dispose()


class SwitchTest(unittest.TestCase):
    def _enabled(self, value):
        with mock.patch.dict(os.environ, {}, clear=False):
            if value is None:
                os.environ.pop('NATIONAL_READONLY', None)
            else:
                os.environ['NATIONAL_READONLY'] = value
            return coverage_probe.enabled()

    def test_default_off(self):
        self.assertFalse(self._enabled(None))
        self.assertFalse(self._enabled('0'))
        self.assertFalse(self._enabled('off'))
        self.assertFalse(self._enabled(''))

    def test_truthy_values(self):
        for value in ('1', 'on', 'true', 'yes', 'ON', 'True'):
            self.assertTrue(self._enabled(value), value)

    def test_coverage_callable_even_when_switch_off(self):
        engine, conn = _conn([('P1', '南昌市新建区', None)])
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('NATIONAL_READONLY', None)
                self.assertFalse(coverage_probe.enabled())
                self.assertTrue(coverage_probe.coverage('南昌市新建区', conn=conn)['covered'])
        finally:
            conn.close()
            engine.dispose()


class HouseholdAndSummaryTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.conn = _conn([('P1', '南昌市新建区', None),
                                        ('P2', '赣州经济技术开发区', None)])

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def test_household_str_and_dict(self):
        cases = [
            '南昌市新建区',
            {'region': '南昌市新建区'},
            {'facts': {'household:region': {'state': 'known', 'value': '南昌市新建区'}}},
            {'policy_core': {'facts': {'household:region': {'value': '南昌市新建区'}}}},
            {'household': {'region': '南昌市新建区'}},
        ]
        for hh in cases:
            cov = coverage_probe.household_coverage(hh, conn=self.conn)
            self.assertTrue(cov['covered'], hh)

    def test_household_missing_region(self):
        for hh in ({}, {'region': ''}, {'region': None}, {'policy_core': {}}):
            cov = coverage_probe.household_coverage(hh, conn=self.conn)
            self.assertIsNone(cov['covered'])
            self.assertEqual(cov['reason'], 'region_unresolved')

    def test_summary_covered(self):
        cov = coverage_probe.coverage('南昌市新建区', conn=self.conn)
        text = coverage_probe.summary(cov)
        self.assertIn('收录了覆盖', text)
        self.assertIn('政策 1 条', text)

    def test_summary_not_covered(self):
        cov = coverage_probe.coverage('湖南省株洲市', conn=self.conn)
        self.assertFalse(cov['covered'])
        text = coverage_probe.summary(cov)
        self.assertIn('暂未收录', text)
        self.assertIn('不代表家庭不符合任何政策', text)

    def test_summary_unresolved(self):
        cov = coverage_probe.coverage('火星镇', conn=self.conn)
        self.assertIn('无法解析', coverage_probe.summary(cov))

    def test_summary_invalid(self):
        self.assertEqual(coverage_probe.summary(None), '地区覆盖探测结果无效。')


if __name__ == '__main__':
    unittest.main()
