# -*- coding: utf-8 -*-
"""规则路径可用性判定单测：县级规则不足时，能否只用已完成的编译结果出结果。

背景：政策库里没有某县区的县级政策时，该县永远只有 39 条省级规则，旧门槛
（县级规则 >= 20）导致每次请求都回退 170-240s 全模型链路。编译任务已完成
（done）说明该地区适用政策集合已经算全，此时用现有规则出结果不会漏掉县级政策。
红线：编译未完成、任务失败、没有任何规则时一律不放行（继续回退旧链路）。
"""
import os
import unittest

from policy_core import rule_readiness


def rules(local=0, province=39):
    return ([{'region_scope': '青山湖'}] * local) + ([{'region_scope': '*'}] * province)


def job(state='done', total=0, done=0, failed=0):
    return dict(state=state, total=total, done=done, failed=failed)


class ToggleMixin(object):
    def setUp(self):
        self._saved = os.environ.get('RULE_PROVINCE_ONLY')

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('RULE_PROVINCE_ONLY', None)
        else:
            os.environ['RULE_PROVINCE_ONLY'] = self._saved


class RuleReadinessTest(ToggleMixin, unittest.TestCase):
    def test_local_rules_enough_is_ready_without_job(self):
        d = rule_readiness.assess(rules(20), None)
        self.assertTrue(d['ready'])
        self.assertEqual(d['state'], 'ready')
        self.assertEqual(d['local'], 20)
        self.assertEqual(d['province'], 39)

    def test_province_only_when_job_done_and_no_county_policy(self):
        d = rule_readiness.assess(rules(0), job(total=0))
        self.assertTrue(d['ready'])
        self.assertEqual(d['state'], 'ready_province_only')
        self.assertEqual(d['local'], 0)

    def test_sparse_county_rules_after_completed_job_are_ready(self):
        d = rule_readiness.assess(rules(5), job(total=5, done=5))
        self.assertTrue(d['ready'])
        self.assertEqual(d['state'], 'ready_sparse')

    def test_pending_job_is_not_ready(self):
        d = rule_readiness.assess(rules(0), job(state='pending'))
        self.assertFalse(d['ready'])
        self.assertEqual(d['state'], 'pending')

    def test_running_job_is_not_ready(self):
        self.assertFalse(rule_readiness.assess(rules(0), job(state='running'))['ready'])

    def test_missing_job_is_not_ready(self):
        d = rule_readiness.assess(rules(0), None)
        self.assertFalse(d['ready'])

    def test_failed_job_is_not_ready(self):
        d = rule_readiness.assess(rules(0), job(state='failed'))
        self.assertFalse(d['ready'])
        self.assertEqual(d['state'], 'failed')

    def test_no_rules_at_all_is_not_ready(self):
        d = rule_readiness.assess(rules(0, province=0), job(total=0))
        self.assertFalse(d['ready'])
        self.assertEqual(d['state'], 'no_rules')

    def test_toggle_off_restores_old_threshold(self):
        os.environ['RULE_PROVINCE_ONLY'] = '0'
        d = rule_readiness.assess(rules(0), job(total=0))
        self.assertFalse(d['ready'])
        self.assertEqual(d['state'], 'disabled')
        self.assertTrue(rule_readiness.assess(rules(20), job(total=0))['ready'])

    def test_empty_region_scope_is_treated_as_province(self):
        items = [{'region_scope': ''}, {'region_scope': None}]
        d = rule_readiness.assess(items, job(total=0))
        self.assertEqual(d['province'], 2)
        self.assertTrue(d['ready'])


if __name__ == '__main__':
    unittest.main()
