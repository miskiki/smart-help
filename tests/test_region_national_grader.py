# -*- coding: utf-8 -*-
"""全国化模式下的"属地不符"判定：外省家庭不再被当成本服务范围外。

历史（江西-only）语义：家庭地区明确在江西省外 → explicit_other → D 档硬拦截。
全国化语义：是否适配送由"政策自身适用范围 vs 家庭地区"判定，因此：
  - 全国化开启时 explicit_other 一律 False（不预判）
  - 政策只覆盖其他地区（policy_region_disjoint）→ 仍然 D 档、不出卡（不误放行）
  - 江西模式（默认）行为完全不变
"""
import os
import unittest
from contextlib import contextmanager

from policy_core import grader, region


@contextmanager
def scope_env(**values):
    keys = ('SERVICE_SCOPE', 'REGION_NATIONWIDE')
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        for key, value in values.items():
            if value is not None:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ExplicitOtherTest(unittest.TestCase):
    def test_jiangxi_mode_keeps_legacy_semantics(self):
        with scope_env():
            self.assertTrue(region.explicit_other('湖南省株洲市'))
            self.assertTrue(region.explicit_other('户籍在湖南'))
            self.assertFalse(region.explicit_other('江西省赣州市章贡区'))
            self.assertFalse(region.explicit_other(''))

    def test_national_mode_never_presumes_outside(self):
        with scope_env(SERVICE_SCOPE='national'):
            self.assertFalse(region.explicit_other('湖南省株洲市'))
            self.assertFalse(region.explicit_other('户籍在湖南'))
            self.assertFalse(region.explicit_other('广东省广州市天河区'))

    def test_region_nationwide_switch_also_clears_presumption(self):
        with scope_env(REGION_NATIONWIDE='1'):
            self.assertFalse(region.explicit_other('湖南省株洲市'))


class GraderRegionStatusTest(unittest.TestCase):
    def test_jiangxi_mode_outside_household_is_explicit_other(self):
        with scope_env():
            info = grader.region_status({}, {}, '湖南省株洲市')
        self.assertEqual(info['status'], grader.REGION_OTHER)
        self.assertFalse(info['verdict'])

    def test_national_mode_outside_household_is_not_explicit_other(self):
        with scope_env(SERVICE_SCOPE='national'):
            info = grader.region_status({}, {}, '湖南省株洲市')
        self.assertNotEqual(info['status'], grader.REGION_OTHER)

    def test_national_mode_disjoint_policy_still_fails(self):
        """政策只覆盖湖南、家庭在江西：仍应明确不适用（D 档），不能因全国化而误放行。"""
        with scope_env(SERVICE_SCOPE='national'):
            info = grader.region_status({'regions': '湖南省'}, {}, '江西省赣州市章贡区')
        self.assertEqual(info['status'], grader.REGION_OTHER)
        self.assertFalse(info['verdict'])

    def test_national_mode_grade_blocks_disjoint_policy(self):
        grade = grader.grade_conditions([], region_info=dict(status=grader.REGION_OTHER, verdict=False))
        self.assertEqual(grade['tier'], grader.TIER_D)
        self.assertIn('region_explicit_other', grade['blockers'])


if __name__ == '__main__':
    unittest.main()
