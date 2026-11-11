# -*- coding: utf-8 -*-
import os
import unittest

from sqlalchemy import create_engine

from policy_core import admin_division, policy_scope, scope_filter


def _scope_row(code, region, detail_id='', kind='policy'):
    rows = policy_scope.rows_for(code, region, [(detail_id, region)] if detail_id else ())
    return [r for r in rows if r['scope_kind'] == kind]


class FilterCodesTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.conn = self.engine.connect()
        policy_scope.ensure_schema(self.conn)
        self._add('X-ZB-300', '莲花县', detail_id='d1')
        self._add('S-ZB-090', '江西省全省')
        self._add('X-ZB-248', '新建区', detail_id='d2')
        self._add('X-ZB-999', '', detail_id='d3')  # 没有范围行

    def tearDown(self):
        self.conn.close()

    def _add(self, code, region, detail_id=''):
        rows = policy_scope.rows_for(code, region, [(detail_id, region)] if detail_id else ())
        policy_scope.replace_policy(self.conn, code, rows)
        self.conn.commit()

    def test_local_family_keeps_county_and_province(self):
        r = scope_filter.filter_codes(['X-ZB-300', 'S-ZB-090'], '江西省萍乡市莲花县', conn=self.conn)
        self.assertEqual(sorted(r['kept']), ['S-ZB-090', 'X-ZB-300'])
        self.assertEqual(r['dropped'], [])

    def test_other_province_dropped(self):
        r = scope_filter.filter_codes(['X-ZB-300', 'S-ZB-090', 'X-ZB-248'],
                                      '湖南省株洲市', conn=self.conn)
        self.assertEqual(r['kept'], [])
        self.assertEqual(sorted(i['code'] for i in r['dropped']),
                         ['S-ZB-090', 'X-ZB-248', 'X-ZB-300'])
        self.assertEqual(r['applicable']['X-ZB-300'], False)

    def test_policy_without_scope_is_kept(self):
        r = scope_filter.filter_codes(['X-ZB-999'], '江西省萍乡市莲花县', conn=self.conn)
        self.assertEqual(r['kept'], [])
        self.assertEqual(r['no_scope'], ['X-ZB-999'])

    def test_too_broad_household_is_unknown_not_dropped(self):
        r = scope_filter.filter_codes(['X-ZB-300'], '江西省', conn=self.conn)
        self.assertEqual(r['kept'], [])
        self.assertEqual([i['code'] for i in r['unknown']], ['X-ZB-300'])
        self.assertEqual(r['applicable']['X-ZB-300'], None)

    def test_filter_packs_returns_report(self):
        packs = [{'policy_code': 'X-ZB-300'}, {'policy_code': 'X-ZB-248'}]
        kept, report = scope_filter.filter_packs(packs, '江西省萍乡市莲花县', conn=self.conn)
        self.assertEqual([p['policy_code'] for p in kept], ['X-ZB-300'])
        self.assertEqual((report['input'], report['output']), (2, 1))
        s = scope_filter.summary(report)
        self.assertEqual(s['dropped'], 1)
        self.assertIn('X-ZB-248', s['dropped_codes'])

    def test_empty_input(self):
        r = scope_filter.filter_codes([], '莲花县', conn=self.conn)
        self.assertEqual(r['kept'], [])
        self.assertEqual(scope_filter.summary(scope_filter.filter_packs([], '莲花县', conn=self.conn)[1])['input'], 0)


class SwitchTest(unittest.TestCase):
    def test_default_off(self):
        old = os.environ.pop('POLICY_SCOPE', None)
        try:
            self.assertFalse(scope_filter.enabled())
            os.environ['POLICY_SCOPE'] = '1'
            self.assertTrue(scope_filter.enabled())
            os.environ['POLICY_SCOPE'] = 'off'
            self.assertFalse(scope_filter.enabled())
        finally:
            os.environ.pop('POLICY_SCOPE', None)
            if old is not None:
                os.environ['POLICY_SCOPE'] = old


if __name__ == '__main__':
    unittest.main()
