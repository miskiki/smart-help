# -*- coding: utf-8 -*-
"""全国行政区划 resolver 单测（P4 地区维度层）。"""
import unittest

from policy_core import admin_division as ad


class CanonicalTest(unittest.TestCase):
    def test_full_county_path(self):
        got = ad.canonical('江西省赣州市章贡区')
        self.assertEqual(got['level'], 'county')
        self.assertEqual((got['province'], got['city'], got['county']), ('江西省', '赣州市', '章贡区'))
        self.assertFalse(got['zone'])

    def test_city_infers_province(self):
        got = ad.canonical('赣州市')
        self.assertEqual((got['level'], got['province'], got['city']), ('city', '江西省', '赣州市'))

    def test_county_without_city_infers_hierarchy(self):
        got = ad.canonical('莲花县')
        self.assertEqual((got['level'], got['province'], got['city']), ('county', '江西省', '萍乡市'))

    def test_other_province_city(self):
        got = ad.canonical('湖南省株洲市')
        self.assertEqual((got['level'], got['province'], got['city']), ('city', '湖南省', '株洲市'))
        short = ad.canonical('株洲市')
        self.assertEqual((short['province'], short['city']), ('湖南省', '株洲市'))

    def test_province_scope(self):
        got = ad.canonical('江西省全省')
        self.assertEqual((got['level'], got['province']), ('province', '江西省'))

    def test_national_scope(self):
        got = ad.canonical('全国')
        self.assertEqual(got['level'], 'nation')
        self.assertTrue(got['national'])

    def test_ambiguous_county_stays_unknown(self):
        got = ad.canonical('朝阳区')
        self.assertEqual(got['level'], 'unknown')
        self.assertIn('朝阳区', got['ambiguous'])

    def test_municipality_county(self):
        got = ad.canonical('北京市朝阳区')
        self.assertEqual((got['level'], got['province'], got['county']), ('county', '北京市', '朝阳区'))

    def test_functional_zone_is_county_level(self):
        got = ad.canonical('赣州经济技术开发区')
        self.assertEqual((got['level'], got['province'], got['city'], got['county']),
                         ('county', '江西省', '赣州市', '赣州经济技术开发区'))
        self.assertTrue(got['zone'])

    def test_known_zone_without_suffix(self):
        got = ad.canonical('仙女湖区')
        self.assertEqual((got['level'], got['province'], got['city'], got['county']),
                         ('county', '江西省', '新余市', '仙女湖区'))
        self.assertTrue(got['zone'])

    def test_known_zone_with_city_prefix(self):
        got = ad.canonical('鹰潭市信江新区管委会')
        self.assertEqual((got['level'], got['city'], got['county']),
                         ('county', '鹰潭市', '信江新区管委会'))

    def test_zone_from_other_province(self):
        got = ad.canonical('郑州经济技术开发区')
        self.assertEqual((got['level'], got['province'], got['city']), ('county', '河南省', '郑州市'))

    def test_province_short_alias(self):
        self.assertEqual(ad.canonical('海南')['province'], '海南省')
        self.assertEqual(ad.canonical('内蒙古')['province'], '内蒙古自治区')

    def test_street_name_not_a_province(self):
        self.assertEqual(ad.canonical('上海路88号')['level'], 'unknown')
        self.assertEqual(ad.canonical('海南路')['level'], 'unknown')

    def test_province_directly_administered_county(self):
        got = ad.canonical('河南省济源市')
        self.assertEqual((got['level'], got['province'], got['city']), ('county', '河南省', None))

    def test_empty_is_unknown(self):
        self.assertEqual(ad.canonical('')['level'], 'unknown')
        self.assertEqual(ad.canonical('不清楚')['level'], 'unknown')


class RelationTest(unittest.TestCase):
    def test_national_covers_any_region(self):
        got = ad.relation('全国', '湖南省株洲市')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])

    def test_province_covers_county(self):
        got = ad.relation('江西省全省', '章贡区')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])

    def test_city_covers_county(self):
        got = ad.relation('赣州市', '章贡区')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])

    def test_same_county(self):
        got = ad.relation('章贡区', '赣州市章贡区')
        self.assertEqual(got['relation'], 'same')
        self.assertTrue(got['applicable'])

    def test_policy_more_specific_is_descendant(self):
        got = ad.relation('章贡区', '赣州市')
        self.assertEqual(got['relation'], 'descendant')
        self.assertIsNone(got['applicable'])

    def test_different_city_disjoint(self):
        self.assertEqual(ad.relation('赣州市', '南昌市')['relation'], 'disjoint')

    def test_different_county_disjoint(self):
        self.assertEqual(ad.relation('章贡区', '南康区')['relation'], 'disjoint')

    def test_province_mismatch_disjoint(self):
        got = ad.relation('江西省', '湖南省株洲市')
        self.assertEqual(got['relation'], 'disjoint')
        self.assertIsNone(got['applicable'])

    def test_zone_policy_more_specific(self):
        got = ad.relation('赣州经济技术开发区', '赣州市')
        self.assertEqual(got['relation'], 'descendant')

    def test_zone_and_county_disjoint(self):
        self.assertEqual(ad.relation('章贡区', '赣州经济技术开发区')['relation'], 'disjoint')

    def test_unknown_input_is_unknown(self):
        got = ad.relation('某某新区', '赣州市')
        self.assertEqual(got['relation'], 'unknown')

    def test_other_province_policy(self):
        got = ad.relation('湖南省株洲市', '株洲市天元区')
        self.assertEqual(got['relation'], 'ancestor')
        self.assertTrue(got['applicable'])


class CatalogTest(unittest.TestCase):
    def test_catalog_covers_mainland(self):
        self.assertEqual(len(ad.provinces()), 31)
        self.assertIn('江西省', ad.provinces())
        self.assertIn('章贡区', ad.counties_of('赣州市', '江西省'))
        self.assertGreater(len(ad.cities_of('江西省')), 10)


if __name__ == '__main__':
    unittest.main()
