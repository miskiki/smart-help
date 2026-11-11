"""T17 运行期全国化单测：地区开关、全国区划回落、服务范围、M1 追问门、M3 软过滤。

全部用例用标准库 unittest + 环境变量临时切换，不依赖数据库；
默认环境（REGION_NATIONWIDE 未设 / SERVICE_SCOPE 未设）必须保持现网江西口径。
"""
import contextlib
import os
import unittest

import config
import kg_retrieval
from policy_core import catalog, region
from policy_core.full_flow import region_needs_ask


@contextlib.contextmanager
def switch_env(**values):
    """临时设置开关环境变量；未显式给的 REGION_NATIONWIDE/SERVICE_SCOPE 先清空再恢复。"""
    keys = dict(SERVICE_SCOPE=None, REGION_NATIONWIDE=None)
    keys.update(values)
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class SwitchTest(unittest.TestCase):
    def test_defaults(self):
        with switch_env(REGION_SOFT_FILTER=None):
            self.assertEqual(region.service_scope_mode(), 'jiangxi')
            self.assertFalse(region.nationwide_enabled())
            self.assertTrue(region.soft_filter_enabled())

    def test_region_nationwide_switch(self):
        with switch_env(REGION_NATIONWIDE='1'):
            self.assertTrue(region.nationwide_enabled())
            self.assertEqual(region.service_scope_mode(), 'jiangxi')

    def test_service_scope_national_forces_nationwide(self):
        with switch_env(SERVICE_SCOPE='national'):
            self.assertEqual(region.service_scope_mode(), 'national')
            self.assertTrue(region.nationwide_enabled())

    def test_off_values(self):
        with switch_env(REGION_NATIONWIDE='0', SERVICE_SCOPE='jiangxi'):
            self.assertFalse(region.nationwide_enabled())
            self.assertEqual(region.service_scope_mode(), 'jiangxi')


class NationwideCanonicalTest(unittest.TestCase):
    def test_off_keeps_legacy_province_only(self):
        with switch_env():
            parsed = region.canonical_levels('湖南省株洲市')
            self.assertEqual(parsed['level'], 'province')
            self.assertEqual(parsed['province'], '湖南省')
            self.assertIsNone(parsed['city'])
            self.assertTrue(parsed['outside'])

    def test_on_parses_other_province_city_county(self):
        with switch_env(REGION_NATIONWIDE='1'):
            parsed = region.canonical_levels('广东省深圳市南山区')
            self.assertEqual((parsed['province'], parsed['city'], parsed['county']),
                             ('广东省', '深圳市', '南山区'))
            self.assertEqual(parsed['level'], 'county')
            self.assertTrue(parsed['outside'])
            self.assertFalse(parsed['ambiguous'])

    def test_on_parses_city_without_province(self):
        with switch_env(REGION_NATIONWIDE='1'):
            parsed = region.canonical_levels('株洲市')
            self.assertEqual(parsed['province'], '湖南省')
            self.assertEqual(parsed['city'], '株洲市')
            self.assertEqual(parsed['level'], 'city')

    def test_on_municipality_county_has_no_fake_city(self):
        with switch_env(REGION_NATIONWIDE='1'):
            parsed = region.canonical_levels('北京市朝阳区')
            self.assertEqual(parsed['province'], '北京市')
            self.assertIsNone(parsed['city'])
            self.assertEqual(parsed['county'], '朝阳区')

    def test_on_ambiguous_county_marked_unknown(self):
        with switch_env(REGION_NATIONWIDE='1'):
            parsed = region.canonical_levels('鼓楼区')
            self.assertTrue(parsed['ambiguous'])
            self.assertEqual(parsed['level'], 'unknown')

    def test_on_keeps_jiangxi_legacy_values(self):
        with switch_env(REGION_NATIONWIDE='1'):
            parsed = region.canonical_levels('江西省南昌市新建区')
            self.assertEqual((parsed['province'], parsed['city'], parsed['county']),
                             ('江西省', '南昌市', '新建区'))
            self.assertFalse(parsed['outside'])


class NationwideRelationTest(unittest.TestCase):
    def test_off_legacy_province_compare(self):
        with switch_env():
            result = region.region_relation('广东省深圳市南山区', '湖南省株洲市')
            self.assertEqual(result['relation'], 'disjoint')
            self.assertEqual(result['confidence'], 'medium')

    def test_on_disjoint_across_provinces(self):
        with switch_env(REGION_NATIONWIDE='1'):
            result = region.region_relation('广东省深圳市南山区', '湖南省株洲市')
            self.assertEqual(result['relation'], 'disjoint')
            self.assertEqual(result['confidence'], 'high')

    def test_on_policy_city_covers_county(self):
        with switch_env(REGION_NATIONWIDE='1'):
            result = region.region_relation('广东省深圳市南山区', '深圳市')
            self.assertEqual(result['relation'], 'ancestor')
            self.assertTrue(region.relation_is_applicable(result))

    def test_on_verdict_national_and_foreign(self):
        with switch_env(REGION_NATIONWIDE='1'):
            self.assertTrue(region.policy_region_verdict({'regions': '全国'}, {}, '湖南省株洲市'))
            self.assertFalse(region.policy_region_verdict({'regions': '江西省'}, {}, '湖南省株洲市'))

    def test_zone_overlap_degrades_only_when_nationwide(self):
        instance = {'regions': '赣州经济技术开发区'}
        with switch_env(REGION_FUNCTIONAL_ZONES='1'):
            self.assertFalse(region.policy_region_verdict(instance, {}, '江西省赣州市章贡区'))
        with switch_env(REGION_FUNCTIONAL_ZONES='1', REGION_NATIONWIDE='1'):
            self.assertIsNone(region.policy_region_verdict(instance, {}, '江西省赣州市章贡区'))


class ServiceScopeTest(unittest.TestCase):
    def test_jiangxi_keeps_today_rules(self):
        with switch_env(SERVICE_SCOPE='jiangxi'):
            self.assertEqual(kg_retrieval.service_scope_check({'region': '株洲市'}, region_names=[])[0], 'ask')
            status, reason = kg_retrieval.service_scope_check({'region': '湖南省株洲市'}, region_names=[])
            self.assertEqual(status, 'blocked')
            self.assertIn('仅支持江西省', reason)

    def test_national_does_not_block_other_province(self):
        with switch_env(SERVICE_SCOPE='national'):
            self.assertEqual(kg_retrieval.service_scope_check({'region': '株洲市'}, region_names=[])[0], 'ok')
            self.assertEqual(kg_retrieval.service_scope_check({'region': '湖南省株洲市'}, region_names=[])[0], 'ok')
            self.assertEqual(kg_retrieval.service_scope_check({'region': '北京市朝阳区'}, region_names=[])[0], 'ok')

    def test_national_ambiguous_asks_without_jiangxi_wording(self):
        with switch_env(SERVICE_SCOPE='national'):
            status, reason = kg_retrieval.service_scope_check({'region': '鼓楼区'}, region_names=[])
            self.assertEqual(status, 'ask')
            self.assertIn('多个行政区', reason)
            self.assertNotIn('仅支持江西省', reason)

    def test_national_empty_region_asks(self):
        with switch_env(SERVICE_SCOPE='national'):
            status, reason = kg_retrieval.service_scope_check({'region': ''}, region_names=[])
            self.assertEqual(status, 'ask')
            self.assertIn('缺少家庭所在地区', reason)


class RegionNeedsAskTest(unittest.TestCase):
    def test_province_only_triggers_ask(self):
        with switch_env(REGION_ASK_GATE='1'):
            self.assertTrue(region_needs_ask('ok', True, '江西省'))

    def test_city_county_does_not_trigger(self):
        with switch_env(REGION_ASK_GATE='1'):
            self.assertFalse(region_needs_ask('ok', True, '江西省南昌市新建区'))
            self.assertFalse(region_needs_ask('ok', True, '南昌市'))

    def test_unknown_region_triggers_ask(self):
        with switch_env(REGION_ASK_GATE='1'):
            self.assertTrue(region_needs_ask('ask', True, '株洲市'))
            self.assertTrue(region_needs_ask('ok', True, '火星镇'))

    def test_blocked_or_disabled_does_not_trigger(self):
        with switch_env(REGION_ASK_GATE='1'):
            self.assertFalse(region_needs_ask('blocked', True, '湖南省株洲市'))
        with switch_env(REGION_ASK_GATE='0'):
            self.assertFalse(region_needs_ask('ok', True, '江西省'))


class LegacyDefaultRegionTest(unittest.TestCase):
    def test_config_keeps_legacy_jiangxi_s_default(self):
        # 默认（江西库）行为不变：config 常量保留历史默认，全国化分流在 _policy_default_regions。
        self.assertEqual(config.DEFAULT_POLICY_REGIONS.get('C'), '全国')
        self.assertEqual(config.DEFAULT_POLICY_REGIONS.get('S'), '江西省全省')

    def test_policy_default_regions_national_drops_s(self):
        with switch_env(REGION_NATIONWIDE='1'):
            self.assertNotIn('S', kg_retrieval._policy_default_regions())
        with switch_env(REGION_NATIONWIDE='0'):
            self.assertEqual(kg_retrieval._policy_default_regions().get('S'), '江西省全省')

    def test_legacy_judge_still_uses_jiangxi_default(self):
        with switch_env(REGION_SOFT_FILTER='1'):
            status, _ = kg_retrieval.region_judge({'level': 'S', 'regions': ''}, {}, '江西省南昌市新建区', {})
            self.assertEqual(status, '适用')

    def test_nationwide_judge_treats_s_empty_as_missing(self):
        with switch_env(REGION_NATIONWIDE='1'):
            status, reason = kg_retrieval.region_judge({'level': 'S', 'regions': ''}, {}, '江西省南昌市新建区', {})
            self.assertEqual(status, '地区待细化')
            self.assertIn('无默认规则', reason)


class SoftFilterM3Test(unittest.TestCase):
    def test_region_judge_downgrades_unconfirmed_disjoint(self):
        policy = {'regions': '赣州经济技术开发区'}
        with switch_env(REGION_SOFT_FILTER='1', REGION_FUNCTIONAL_ZONES='1'):
            status, reason = kg_retrieval.region_judge(policy, {}, '江西省赣州市', {}, hh_nodes=[])
            self.assertEqual(status, '地区待细化')
            self.assertIn('程序层', reason)
        with switch_env(REGION_SOFT_FILTER='0', REGION_FUNCTIONAL_ZONES='1'):
            status, _ = kg_retrieval.region_judge(policy, {}, '江西省赣州市', {}, hh_nodes=[])
            self.assertEqual(status, '不适')

    def test_catalog_keeps_candidate_when_program_uncertain(self):
        instance = {'policy': {'regions': '赣州经济技术开发区'}, 'detail': {}}
        with switch_env(REGION_SOFT_FILTER='1', REGION_FUNCTIONAL_ZONES='1'):
            self.assertIsNone(catalog.applicable_region(instance, '江西省赣州市', nodes={}))
        with switch_env(REGION_SOFT_FILTER='0', REGION_FUNCTIONAL_ZONES='1'):
            self.assertFalse(catalog.applicable_region(instance, '江西省赣州市', nodes={}))

    def test_clear_disjoint_still_dropped(self):
        with switch_env(REGION_SOFT_FILTER='1'):
            status, _ = kg_retrieval.region_judge({'regions': '南昌县'}, {}, '江西省南昌市新建区', {}, hh_nodes=[])
            self.assertEqual(status, '不适')


if __name__ == '__main__':
    unittest.main()
