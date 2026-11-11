# -*- coding: utf-8 -*-
"""地区纵向深度单测：直辖市、同名区歧义、省直辖县级市、功能区、双向关系与 scope 键。

全部为纯函数/内存 sqlite 断言；coverage_probe 用例一律注入自建 sqlite 连接，
不会触碰生产库（也不建表写库以外的任何副作用）。
"""
import unittest

from sqlalchemy import create_engine, text

from policy_core import admin_division, coverage_probe, policy_scope


def _scopes(regions='', details=()):
    rows = policy_scope.rows_for('P', regions, details)
    return {'policy': [r for r in rows if r['scope_kind'] == 'policy'],
            'detail': [r for r in rows if r['scope_kind'] == 'detail']}


class MunicipalityCanonicalTest(unittest.TestCase):
    def test_municipality_county_city_empty(self):
        got = admin_division.canonical('北京市东城区')
        self.assertEqual(got['level'], admin_division.COUNTY)
        self.assertEqual(got['province'], '北京市')
        # 直辖市不编造地级市：city 保持空，由 province+county 表达
        self.assertIsNone(got['city'])
        self.assertEqual(got['county'], '东城区')
        self.assertEqual(got['ambiguous'], [])

    def test_municipality_zone_style_county(self):
        got = admin_division.canonical('上海市浦东新区')
        self.assertEqual(got['level'], admin_division.COUNTY)
        self.assertEqual(got['province'], '上海市')
        self.assertIsNone(got['city'])
        self.assertEqual(got['county'], '浦东新区')
        # “新区”按功能区后缀识别，但仍是县区级行政区
        self.assertTrue(got['zone'])

    def test_municipality_province_level(self):
        got = admin_division.canonical('北京市')
        self.assertEqual(got['level'], admin_division.PROVINCE)
        self.assertEqual(got['province'], '北京市')
        self.assertIsNone(got['city'])
        self.assertIsNone(got['county'])

    def test_municipality_household_resolves_without_province(self):
        got = admin_division.canonical('东城区')
        self.assertEqual(got['level'], admin_division.COUNTY)
        self.assertEqual(got['province'], '北京市')
        self.assertEqual(got['county'], '东城区')


class AmbiguousCountyTest(unittest.TestCase):
    def test_ambiguous_county_level_unknown(self):
        got = admin_division.canonical('朝阳区')
        # 北京/长春都有朝阳区：不猜归属，保持 unknown
        self.assertEqual(got['level'], admin_division.UNKNOWN)
        self.assertEqual(got['ambiguous'], ['朝阳区'])
        self.assertIsNone(got['province'])

    def test_ambiguous_policy_relation_unknown(self):
        got = admin_division.relation('朝阳区', '北京市朝阳区')
        self.assertEqual(got['relation'], 'unknown')
        self.assertIsNone(got['applicable'])

    def test_ambiguous_scope_key_is_u_prefix(self):
        rows = policy_scope.rows_for('X-1', '朝阳区')
        self.assertEqual(rows[0]['level'], admin_division.UNKNOWN)
        self.assertEqual(rows[0]['region_key'], 'U:朝阳区')
        self.assertEqual(rows[0]['confidence'], 'low')


class ProvinceDirectCountyTest(unittest.TestCase):
    def test_henan_jiyuan(self):
        got = admin_division.canonical('河南省济源市')
        self.assertEqual(got['level'], admin_division.COUNTY)
        self.assertEqual(got['province'], '河南省')
        self.assertIsNone(got['city'])
        self.assertEqual(got['county'], '济源市')

    def test_hubei_xiantao_and_qianjiang(self):
        for name in ('湖北省仙桃市', '湖北省潜江市', '湖北省天门市'):
            got = admin_division.canonical(name)
            self.assertEqual(got['level'], admin_division.COUNTY, name)
            self.assertEqual(got['province'], '湖北省', name)
            self.assertIsNone(got['city'], name)

    def test_hainan_direct_county(self):
        # 海南文昌/五指山是省直辖县级市
        for name in ('海南省文昌市', '海南省五指山市'):
            got = admin_division.canonical(name)
            self.assertEqual(got['level'], admin_division.COUNTY, name)
            self.assertEqual(got['province'], '海南省', name)
            self.assertIsNone(got['city'], name)

    def test_danzhou_is_prefecture_city_in_rmb_data(self):
        # 儋州市在民政区划里是地级市（不是省直辖县级市），按数据如实解析
        got = admin_division.canonical('海南省儋州市')
        self.assertEqual(got['level'], admin_division.CITY)
        self.assertEqual(got['province'], '海南省')
        self.assertEqual(got['city'], '儋州市')

    def test_bare_jiyuan_resolves(self):
        got = admin_division.canonical('济源市')
        self.assertEqual(got['level'], admin_division.COUNTY)
        self.assertEqual(got['province'], '河南省')
        self.assertIsNone(got['city'])


class FunctionalZoneTest(unittest.TestCase):
    def test_known_zone_maps_to_city(self):
        got = admin_division.canonical('赣州经济技术开发区')
        self.assertEqual(got['level'], admin_division.COUNTY)
        self.assertTrue(got['zone'])
        self.assertEqual(got['province'], '江西省')
        self.assertEqual(got['city'], '赣州市')
        self.assertEqual(got['county'], '赣州经济技术开发区')

    def test_zone_suffix_guesses_unique_city(self):
        got = admin_division.canonical('苏州工业园区')
        self.assertTrue(got['zone'])
        self.assertEqual(got['province'], '江苏省')
        self.assertEqual(got['city'], '苏州市')
        got = admin_division.canonical('郑州经济技术开发区')
        self.assertEqual(got['city'], '郑州市')

    def test_street_address_not_zone(self):
        got = admin_division.canonical('上海路88号')
        self.assertEqual(got['level'], admin_division.UNKNOWN)
        self.assertFalse(got['zone'])

    def test_zone_relation(self):
        got = admin_division.relation('赣州经济技术开发区', '赣州市')
        self.assertEqual(got['relation'], 'descendant')
        self.assertIsNone(got['applicable'])
        got = admin_division.relation('赣州市', '赣州经济技术开发区')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])


class DepthRelationTest(unittest.TestCase):
    def test_municipality_ancestor(self):
        got = admin_division.relation('北京市', '北京市东城区')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])

    def test_municipality_descendant(self):
        # 家庭只到直辖市、政策到区：政策更具体，只能参考不能判适用（FP=0）
        got = admin_division.relation('北京市东城区', '北京市')
        self.assertEqual(got['relation'], 'descendant')
        self.assertIsNone(got['applicable'])

    def test_province_direct_county_ancestor(self):
        got = admin_division.relation('河南省', '河南省济源市')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])

    def test_province_direct_county_descendant(self):
        got = admin_division.relation('河南省济源市', '河南省')
        self.assertEqual(got['relation'], 'descendant')
        self.assertIsNone(got['applicable'])

    def test_municipality_disjoint(self):
        got = admin_division.relation('北京市', '上海市')
        self.assertEqual(got['relation'], 'disjoint')
        self.assertIsNone(got['applicable'])

    def test_same_city_counties_disjoint(self):
        got = admin_division.relation('东城区', '西城区')
        self.assertEqual(got['relation'], 'disjoint')

    def test_municipality_ancestor_without_province_prefix(self):
        got = admin_division.relation('北京市', '东城区')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])

    def test_unknown_place_stays_unknown(self):
        got = admin_division.relation('某某镇', '北京市')
        self.assertEqual(got['relation'], 'unknown')
        self.assertIsNone(got['applicable'])


class RegionKeyTest(unittest.TestCase):
    def test_municipality_county_key(self):
        rows = policy_scope.rows_for('X-1', '北京市东城区')
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['level'], admin_division.COUNTY)
        self.assertEqual(row['province'], '北京市')
        self.assertIsNone(row['city'])
        # 直辖市县区键：省/市/县 三段，缺地级市用空段，不出现字符串 'None'
        self.assertEqual(row['region_key'], 'X:北京市//东城区')
        self.assertNotIn('None', row['region_key'])

    def test_direct_county_key(self):
        rows = policy_scope.rows_for('X-2', '河南省济源市')
        self.assertEqual(rows[0]['region_key'], 'X:河南省//济源市')
        self.assertNotIn('None', rows[0]['region_key'])

    def test_municipality_province_key(self):
        rows = policy_scope.rows_for('S-1', '北京市')
        self.assertEqual(rows[0]['level'], admin_division.PROVINCE)
        self.assertEqual(rows[0]['region_key'], 'P:北京市')

    def test_municipality_zone_detail_key(self):
        rows = policy_scope.rows_for('X-3', '', details=[('11', '上海市浦东新区')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['scope_kind'], 'detail')
        self.assertEqual(rows[0]['region_key'], 'Z:上海市//浦东新区')
        self.assertNotIn('None', rows[0]['region_key'])

    def test_match_municipality_county_three_states(self):
        scopes = _scopes('北京市东城区')
        self.assertTrue(policy_scope.match(scopes, '东城区')['applicable'])
        self.assertFalse(policy_scope.match(scopes, '西城区')['applicable'])
        # 家庭只到直辖市 → 无法判断是否在东城区，保留 None
        self.assertIsNone(policy_scope.match(scopes, '北京市')['applicable'])

    def test_match_direct_county_three_states(self):
        scopes = _scopes('河南省济源市')
        self.assertTrue(policy_scope.match(scopes, '济源市')['applicable'])
        self.assertFalse(policy_scope.match(scopes, '仙桃市')['applicable'])
        self.assertIsNone(policy_scope.match(scopes, '河南省')['applicable'])

    def test_match_municipality_province_policy(self):
        scopes = _scopes('北京市')
        self.assertTrue(policy_scope.match(scopes, '北京市东城区')['applicable'])


class CoverageProbeTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        with self.engine.connect() as conn:
            policy_scope.ensure_schema(conn)
            conn.commit()

    def test_no_scope_rows_is_false_not_error(self):
        with self.engine.connect() as conn:
            cov = coverage_probe.coverage('北京市东城区', conn)
            self.assertEqual(cov['level'], admin_division.COUNTY)
            self.assertEqual(cov['province'], '北京市')
            self.assertEqual(cov['county'], '东城区')
            self.assertFalse(cov['covered'])
            self.assertEqual(cov['reason'], 'no_scope_rows')
            self.assertEqual(cov['rows'], 0)
            self.assertEqual(cov['policies'], 0)
            # 直辖市县区的祖先键包含省/县两级，缺地级市段也不出现 'None'
            self.assertIn('P:北京市', cov['ancestor_keys'])
            self.assertIn('X:北京市//东城区', cov['ancestor_keys'])
            for key in cov['ancestor_keys']:
                self.assertIsInstance(key, str)
                self.assertNotIn('None', key)

    def test_probe_does_not_write_or_create_rows(self):
        with self.engine.connect() as conn:
            coverage_probe.coverage('北京市东城区', conn)
            self.assertEqual(
                conn.execute(text('SELECT count(*) FROM policy_scope')).scalar(), 0)

    def test_covered_true_when_scope_row_matches(self):
        with self.engine.connect() as conn:
            rows = policy_scope.rows_for('P-1', '北京市东城区')
            policy_scope.replace_policy(conn, 'P-1', rows)
            conn.commit()
            cov = coverage_probe.coverage('北京市东城区', conn)
            self.assertTrue(cov['covered'])
            self.assertEqual(cov['reason'], 'covered')
            self.assertGreaterEqual(cov['rows'], 1)

    def test_direct_county_probe(self):
        with self.engine.connect() as conn:
            cov = coverage_probe.coverage('河南省济源市', conn)
            self.assertEqual(cov['level'], admin_division.COUNTY)
            self.assertEqual(cov['province'], '河南省')
            self.assertEqual(cov['county'], '济源市')
            self.assertFalse(cov['covered'])
            self.assertIn('X:河南省//济源市', cov['ancestor_keys'])

    def test_zone_probe_uses_z_key(self):
        with self.engine.connect() as conn:
            cov = coverage_probe.coverage('上海市浦东新区', conn)
            self.assertTrue(cov['zone'])
            self.assertFalse(cov['covered'])
            self.assertIn('Z:上海市//浦东新区', cov['ancestor_keys'])
            self.assertNotIn('X:上海市//浦东新区', cov['ancestor_keys'])

    def test_missing_table_reports_query_error(self):
        engine = create_engine('sqlite://')
        with engine.connect() as conn:
            cov = coverage_probe.coverage('北京市东城区', conn)
            self.assertIsNone(cov['covered'])
            self.assertEqual(cov['reason'], 'query_error')
            self.assertTrue(cov.get('error'))
            # 探测不得顺手建表
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM sqlite_master WHERE name='policy_scope'")).scalar(), 0)


if __name__ == '__main__':
    unittest.main()
