# -*- coding: utf-8 -*-
"""policy_scope 结构化地区范围层单测（纯函数 + sqlite 回填）。"""
import unittest

from sqlalchemy import create_engine, text

from policy_core import admin_division, policy_scope


def _household(text):
    return admin_division.canonical(text)


class SplitTest(unittest.TestCase):
    def test_multi_value(self):
        self.assertEqual(policy_scope.split_regions('赣州市、吉安市;南昌市/九江市'),
                         ['赣州市', '吉安市', '南昌市', '九江市'])

    def test_blank_and_dup(self):
        self.assertEqual(policy_scope.split_regions(' 莲花县、、莲花县 '), ['莲花县'])
        self.assertEqual(policy_scope.split_regions(''), [])
        self.assertEqual(policy_scope.split_regions(None), [])


class RowsTest(unittest.TestCase):
    def test_national(self):
        rows = policy_scope.rows_for('C-1', '全国')
        self.assertEqual(rows[0]['level'], admin_division.NATION)
        self.assertEqual(rows[0]['region_key'], 'CN')

    def test_province_and_county(self):
        rows = policy_scope.rows_for('S-1', '江西省全省')
        self.assertEqual(rows[0]['level'], admin_division.PROVINCE)
        self.assertEqual(rows[0]['region_key'], 'P:江西省')
        rows = policy_scope.rows_for('X-1', '', details=[(11, '赣州市章贡区')])
        self.assertEqual(rows[0]['level'], admin_division.COUNTY)
        self.assertEqual(rows[0]['detail_id'], '11')
        self.assertIn('章贡区', rows[0]['region_key'])

    def test_zone(self):
        rows = policy_scope.rows_for('X-2', '赣州经济技术开发区')
        self.assertTrue(rows[0]['zone'])
        self.assertTrue(rows[0]['region_key'].startswith('Z:'))
        self.assertEqual(rows[0]['city'], '赣州市')

    def test_unknown_is_kept(self):
        rows = policy_scope.rows_for('X-3', '火星镇')
        self.assertEqual(rows[0]['level'], admin_division.UNKNOWN)
        self.assertTrue(rows[0]['region_key'].startswith('U:'))
        self.assertEqual(rows[0]['confidence'], 'low')

    def test_bare_province_with_default(self):
        rows = policy_scope.rows_for('S-2', '全省')
        self.assertEqual(rows[0]['level'], admin_division.UNKNOWN)
        rows = policy_scope.rows_for('S-2', '全省', default_province='江西省')
        self.assertEqual(rows[0]['level'], admin_division.PROVINCE)
        self.assertEqual(rows[0]['region_key'], 'P:江西省')

    def test_detail_and_policy_deduped(self):
        rows = policy_scope.rows_for('X-4', '莲花县', details=[(1, '莲花县')])
        keys = [(r['detail_id'], r['region_key']) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(rows), 2)


class MatchTest(unittest.TestCase):
    def _scopes(self, regions='', details=()):
        rows = policy_scope.rows_for('P', regions, details)
        return {'policy': [r for r in rows if r['scope_kind'] == 'policy'],
                'detail': [r for r in rows if r['scope_kind'] == 'detail']}

    def test_province_covers_county(self):
        result = policy_scope.match(self._scopes('江西省全省'), _household('江西省赣州市章贡区'))
        self.assertTrue(result['applicable'])

    def test_national_covers_any(self):
        result = policy_scope.match(self._scopes('全国'), _household('湖南省株洲市天元区'))
        self.assertTrue(result['applicable'])

    def test_other_province_disjoint(self):
        result = policy_scope.match(self._scopes('江西省全省'), _household('湖南省株洲市'))
        self.assertFalse(result['applicable'])

    def test_other_county_disjoint(self):
        result = policy_scope.match(self._scopes('南昌县'), _household('南昌市新建区'))
        self.assertFalse(result['applicable'])

    def test_household_too_broad_unknown(self):
        result = policy_scope.match(self._scopes('南昌市新建区'), _household('江西省南昌市'))
        self.assertIsNone(result['applicable'])

    def test_detail_preferred(self):
        scopes = self._scopes('江西省全省', details=[(1, '赣州市章贡区')])
        self.assertTrue(policy_scope.match(scopes, _household('赣州市章贡区'))['applicable'])
        self.assertIsNone(policy_scope.match(scopes, _household('吉安市吉安县'))['applicable'])

    def test_empty_scope(self):
        result = policy_scope.match({'policy': [], 'detail': []}, _household('莲花县'))
        self.assertIsNone(result['applicable'])
        self.assertEqual(result['reason'], 'no_scope')

    def test_zone_host_overlap_kept(self):
        result = policy_scope.match(self._scopes('赣州经济技术开发区'),
                                    _household('江西省赣州市章贡区'))
        self.assertIsNone(result['applicable'])
        self.assertEqual(result['reason'], 'zone_host_overlap_possible')

    def test_zone_same_is_applicable(self):
        result = policy_scope.match(self._scopes('赣州经济技术开发区'),
                                    _household('赣州市赣州经济技术开发区'))
        self.assertTrue(result['applicable'])

    def test_zone_other_city_disjoint(self):
        result = policy_scope.match(self._scopes('赣州经济技术开发区'),
                                    _household('南昌市新建区'))
        self.assertFalse(result['applicable'])

    def test_accepts_text_household(self):
        self.assertTrue(policy_scope.match(self._scopes('江西省全省'), '赣州市章贡区')['applicable'])


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.url = 'sqlite://'
        self.engine = create_engine(self.url)
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE policy (policy_id INTEGER PRIMARY KEY, '
                              'policy_code TEXT, policy_name TEXT, regions TEXT)'))
            conn.execute(text('CREATE TABLE policy_detail (detail_id INTEGER PRIMARY KEY, '
                              'policy_id INTEGER, region TEXT)'))
            conn.execute(text("INSERT INTO policy VALUES (1,'S-1','省级','江西省全省')"))
            conn.execute(text("INSERT INTO policy VALUES (2,'X-1','县级','')"))
            conn.execute(text("INSERT INTO policy VALUES (3,'C-1','全国','全国')"))
            conn.execute(text("INSERT INTO policy_detail VALUES (11,2,'赣州市章贡区')"))
            conn.execute(text("INSERT INTO policy_detail VALUES (12,2,'火星镇')"))
            policy_scope.ensure_schema(conn, url=self.url)

    def test_dry_run_does_not_write(self):
        with self.engine.begin() as conn:
            report = policy_scope.backfill(conn, dry_run=True)
            self.assertEqual(report['policies'], 3)
            self.assertEqual(report['details'], 2)
            self.assertGreaterEqual(report['scope_rows'], 4)
            self.assertEqual(conn.execute(text('SELECT count(*) FROM policy_scope')).scalar(), 0)
            self.assertEqual(report['unresolved'][0]['raw_text'], '火星镇')
            self.assertEqual(report['by_level'][admin_division.NATION], 1)

    def test_apply_and_load(self):
        with self.engine.begin() as conn:
            report = policy_scope.backfill(conn, dry_run=False)
            self.assertGreater(report['scope_rows'], 0)
            scopes = policy_scope.load_scopes(conn, ['S-1', 'X-1', 'C-1'])
            self.assertEqual(len(scopes['S-1']['policy']), 1)
            self.assertEqual(len(scopes['X-1']['detail']), 2)
            self.assertEqual(scopes['C-1']['policy'][0]['region_key'], 'CN')
            self.assertTrue(policy_scope.match(scopes['X-1'], _household('赣州市章贡区'))['applicable'])
            self.assertIsNone(policy_scope.match(scopes['X-1'], _household('火星镇'))['applicable'])
            # 幂等：重复回填不产生重复行
            policy_scope.backfill(conn, dry_run=False)
            total = conn.execute(text('SELECT count(*) FROM policy_scope')).scalar()
            self.assertEqual(total, report['scope_rows'])


if __name__ == '__main__':
    unittest.main()
