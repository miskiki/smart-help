# -*- coding: utf-8 -*-
"""地区归一：政策库里的功能区写法（开发区/新区/管委会/风景名胜区）与“全省”要能确定性解析。"""
import os
import unittest

from policy_core import geo, region


class FunctionalZoneCanonTest(unittest.TestCase):
    def test_zone_is_county_level_with_city(self):
        c = region.canonical_levels('江西省赣州市赣州经济技术开发区')
        self.assertEqual(c['county'], '赣州经济技术开发区')
        self.assertEqual(c['city'], '赣州市')
        self.assertEqual(c['province'], '江西省')
        self.assertEqual(c['level'], 'county')

    def test_zone_short_name_without_city_still_resolves(self):
        c = region.canonical_levels('仙女湖区')
        self.assertEqual(c['county'], '仙女湖区')
        self.assertEqual(c['city'], '新余市')

    def test_province_whole_text_is_province_level(self):
        c = region.canonical_levels('江西省全省')
        self.assertEqual(c['province'], '江西省')
        self.assertEqual(c['level'], 'province')

    def test_switch_off_falls_back_to_old_behaviour(self):
        old = os.environ.get('REGION_FUNCTIONAL_ZONES')
        os.environ['REGION_FUNCTIONAL_ZONES'] = '0'
        try:
            c = region.canonical_levels('江西省赣州市赣州经济技术开发区')
            self.assertIsNone(c['county'])
            self.assertEqual(c['city'], '赣州市')
        finally:
            if old is None:
                os.environ.pop('REGION_FUNCTIONAL_ZONES', None)
            else:
                os.environ['REGION_FUNCTIONAL_ZONES'] = old

    def test_all_policy_zone_names_are_mapped(self):
        self.assertEqual(len(geo.FUNCTIONAL_ZONES), 13)
        for zone, city in geo.FUNCTIONAL_ZONES.items():
            c = region.canonical_levels('江西省' + city + zone)
            self.assertEqual(c['county'], zone, zone)
            self.assertEqual(c['city'], city, zone)


class ZoneRelationTest(unittest.TestCase):
    def test_same_zone_is_applicable(self):
        v = region.policy_region_verdict({}, {'region': '赣州经济技术开发区'},
                                         '江西省赣州市赣州经济技术开发区')
        self.assertTrue(v)

    def test_zone_household_vs_province_policy_is_applicable(self):
        v = region.policy_region_verdict({'regions': '江西省全省'}, {}, '江西省赣州市蓉江新区')
        self.assertTrue(v)

    def test_zone_household_vs_other_county_is_disjoint(self):
        v = region.policy_region_verdict({}, {'region': '莲花县'}, '江西省赣州市赣州经济技术开发区')
        self.assertFalse(v)

    def test_other_district_vs_zone_policy_is_disjoint(self):
        # 功能区是独立的县区级口径：同市其他区县与它互不覆盖，属于明确不适用（不会因此误放行）。
        rel = region.region_relation('江西省赣州市章贡区', '赣州经济技术开发区')
        self.assertEqual(rel['relation'], 'disjoint')

    def test_city_only_household_vs_zone_policy_is_not_decided(self):
        # 家庭只到市：无法确认是否在该功能区内，保持 unknown 保留候选。
        rel = region.region_relation('江西省赣州市', '赣州经济技术开发区')
        self.assertEqual(rel['relation'], 'descendant')


if __name__ == '__main__':
    unittest.main()
