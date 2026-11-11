# -*- coding: utf-8 -*-
"""claims 三段式整体替换散文正文时，不再白白生成一次模型 advice（仍保留免责句/不清楚字段语义）。"""
import os
import unittest

from policy_core import full_advice


def rows(n=1):
    return [dict(code='X-ZB-%03d' % i, name='政策%d' % i, member='本人', tier='B',
                 satisfied=[], missing=[], basis='') for i in range(n)]


def _env(**kw):
    old = {}
    for k, v in kw.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


def _restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class SkipModelAdviceTest(unittest.TestCase):
    def tearDown(self):
        _restore(getattr(self, '_old', {}))

    def test_skip_when_claims_render_and_rows(self):
        self._old = _env(ANSWER_STYLE=None)
        self.assertTrue(full_advice.skip_model_advice(rows(), {}))

    def test_no_skip_without_rows(self):
        self._old = _env(ANSWER_STYLE=None)
        self.assertFalse(full_advice.skip_model_advice([], {}))

    def test_no_skip_in_classic_style(self):
        self._old = _env(ANSWER_STYLE='classic')
        self.assertFalse(full_advice.skip_model_advice(rows(), {}))

    def test_no_skip_when_scope_blocked(self):
        self._old = _env(ANSWER_STYLE=None)
        self.assertFalse(full_advice.skip_model_advice(rows(), {'scope_blocked': True}))


class PrepareClaimStateTest(unittest.TestCase):
    def test_first_turn_sets_disclaimer_and_round(self):
        st, coverage = {}, {}
        disclaimer = full_advice.prepare_claim_state(st, coverage, {})
        self.assertTrue(disclaimer)
        self.assertTrue(coverage['disclaimer'])
        self.assertEqual(coverage['round'], 1)
        self.assertTrue(st['disclaimer_shown'])
        self.assertEqual(coverage['unclear_facts'], {})

    def test_later_turn_no_disclaimer(self):
        st, coverage = {'disclaimer_shown': True, 'advice_turn': 1}, {}
        self.assertFalse(full_advice.prepare_claim_state(st, coverage, {}))
        self.assertFalse(coverage['disclaimer'])
        self.assertEqual(coverage['round'], 2)

    def test_unclear_facts_still_mapped(self):
        st = {'asked': {'household:income': {'answer': '不清楚'}}}
        coverage = {}
        full_advice.prepare_claim_state(st, coverage, {'income': {'label': '家庭收入'}})
        self.assertEqual(coverage['unclear_facts'], {'家庭：家庭收入': '家庭收入'})

    def test_idempotent_when_coverage_already_marked(self):
        st = {}
        coverage = {'disclaimer': False, 'round': 3}
        self.assertFalse(full_advice.prepare_claim_state(st, coverage, {}))
        self.assertEqual(coverage['round'], 3)
        self.assertNotIn('disclaimer_shown', st)


if __name__ == '__main__':
    unittest.main()
