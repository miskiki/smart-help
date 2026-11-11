# -*- coding: utf-8 -*-
"""P1 输入理解层单测：路由矩阵、判据抽取、fallback 与 clarify 条件（不依赖数据库/模型）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import intake  # noqa: E402


def _model(route, subject='unknown', topic='', confidence=0.9):
    def fn(payload):
        return {'route': route, 'subject': subject, 'topic': topic, 'confidence': confidence}
    return fn


def _forbidden(payload):
    raise AssertionError('该输入不应调用模型分类')


class ChatGuardTest(unittest.TestCase):
    def test_greetings_and_self_intro(self):
        self.assertTrue(intake.chat_guard('你好'))
        self.assertTrue(intake.chat_guard('hello，你是谁'))
        self.assertTrue(intake.chat_guard('谢谢'))
        self.assertTrue(intake.chat_guard('你能做什么？'))

    def test_policy_words_never_chat(self):
        self.assertFalse(intake.chat_guard('你好，我家的低保会不会被取消'))
        self.assertFalse(intake.chat_guard('我家3口人'))
        self.assertFalse(intake.chat_guard(''))

    def test_long_or_mixed_text_not_chat(self):
        self.assertFalse(intake.chat_guard('你好' + '，我想问一下政策' * 5))
        self.assertFalse(intake.chat_guard('你好，我今年62岁'))


class RouteRuleTest(unittest.TestCase):
    def test_chat_does_not_call_model(self):
        d = intake.classify('你好', _model_fn=_forbidden)
        self.assertEqual(d['route'], 'chat')
        self.assertEqual(d['source'], 'rule')
        self.assertEqual(d['provided'], [])

    def test_family_description_goes_eligibility(self):
        q = ('我家在江西省吉安市万安县，家里3口人；成员1，62岁，二级残疾，肢体残疾；'
             '成员2，60岁，健康；全家医保参保0人；靠务工收入生活。')
        d = intake.classify(q, _model_fn=_forbidden)
        self.assertEqual(d['route'], 'eligibility')
        self.assertEqual(d['subject'], 'household')
        self.assertTrue(d['has_household_facts'])
        for key in ('region', 'members', 'age', 'health', 'income'):
            self.assertIn(key, d['provided'], key)
        self.assertIn('insurance', d['provided'])
        self.assertNotIn('insurance', d['missing'])
        self.assertEqual(d['region_hint']['province'], '江西省')
        self.assertEqual(d['region_hint']['city'], '吉安市')
        self.assertEqual(d['region_hint']['county'], '万安县')
        self.assertNotIn('region', d['missing'])
        self.assertEqual(d['missing'], [])

    def test_third_person_eligibility(self):
        d = intake.classify('我父亲62岁二级残疾，能申请吗', _model_fn=_forbidden)
        self.assertEqual(d['route'], 'eligibility')
        self.assertEqual(d['subject'], 'third_party')
        self.assertIn('age', d['provided'])

    def test_policy_consultation_phrases(self):
        for q in ('残疾人两项补贴申请流程', '低保政策原文', '临时救助需要什么材料',
                  '补助标准是多少', '低收入家庭认定条件'):
            d = intake.classify(q, _model_fn=_forbidden)
            self.assertEqual(d['route'], 'consultation', q)
            self.assertEqual(d['subject'], 'policy_general', q)
            self.assertEqual(d['missing'], [], q)

    def test_how_to_apply_consultation(self):
        for q in ('低保怎么申请', '残疾证如何办理', '临时救助在哪里办理'):
            self.assertEqual(intake.classify(q, _model_fn=_forbidden)['route'], 'consultation', q)

    def test_model_route_consultation(self):
        d = intake.classify('这个政策我看了半天没看懂', _model_fn=_model('consultation', confidence=0.9))
        self.assertEqual(d['route'], 'consultation')
        self.assertEqual(d['source'], 'model')

    def test_vague_eligibility_becomes_clarify(self):
        for q in ('我能申请吗', '有什么补贴'):
            d = intake.classify(q, _model_fn=_model('eligibility', confidence=0.9))
            self.assertEqual(d['route'], 'clarify', q)
            self.assertEqual(d['missing'], ['region', 'members', 'age', 'health', 'labor', 'income', 'insurance'], q)

    def test_eligibility_with_region_stays_eligibility(self):
        d = intake.classify('我家在万安县，能领什么补贴', _model_fn=_forbidden)
        self.assertEqual(d['route'], 'eligibility')
        self.assertIn('region', d['provided'])

    def test_consultation_not_clarified(self):
        d = intake.classify('低保申请条件是什么', _model_fn=_forbidden)
        self.assertEqual(d['route'], 'consultation')

    def test_history_household_prevents_clarify(self):
        d = intake.classify('那我能申请吗', has_household=True, _model_fn=_model('eligibility', confidence=0.9))
        self.assertEqual(d['route'], 'eligibility')


class FallbackTest(unittest.TestCase):
    def test_model_error_falls_back_by_signal(self):
        self.assertEqual(intake.classify('我家3口人，能领什么', _model_fn=_forbidden)['route'], 'eligibility')
        d = intake.classify('这个政策我没看懂', _model_fn=lambda p: (_ for _ in ()).throw(RuntimeError('boom')))
        self.assertEqual(d['route'], 'consultation')
        self.assertEqual(d['source'], 'fallback')

    def test_low_confidence_uses_fallback(self):
        d = intake.classify('这个政策我没看懂', _model_fn=_model('eligibility', confidence=0.3))
        self.assertEqual(d['route'], 'consultation')
        self.assertEqual(d['source'], 'fallback')

    def test_never_raises_on_bad_input(self):
        for q in (None, '', '   ', '🙂' * 40, 'x' * 5000, '我家' + '3口人，' * 200):
            d = intake.classify(q, _model_fn=_model('consultation', confidence=0.9))
            self.assertIn(d['route'], ('chat', 'consultation', 'eligibility', 'clarify'))
            self.assertIsInstance(d['reason'], str)
            self.assertLessEqual(len(d['reason']), 40)

    def test_decision_has_no_qualification_conclusion(self):
        d = intake.classify('我家在万安县，3口人，62岁残疾，能领什么补贴', _model_fn=_forbidden)
        for forbidden in ('answer', 'judgement', 'applicable', 'eligible', 'matched'):
            self.assertNotIn(forbidden, d)


class SignalExtractTest(unittest.TestCase):
    def test_extract_signal_keys(self):
        d = intake.classify('我家4口人，住在莲花县，两个孩子在上学，父亲62岁患慢性病，'
                            '家里靠务工收入，有医保，住的是危房，有3亩耕地，配偶是弱劳动力。',
                            _model_fn=_forbidden)
        for key in ('region', 'members', 'age', 'health', 'labor', 'income', 'insurance',
                    'education', 'housing', 'land'):
            self.assertIn(key, d['provided'], key)

    def test_missing_order_stable(self):
        d = intake.classify('我家在万安县，能领什么补贴', _model_fn=_forbidden)
        self.assertEqual(d['missing'], ['members', 'age', 'health', 'labor', 'income', 'insurance'])

    def test_clarify_text(self):
        d = intake.classify('我能申请吗', _model_fn=_model('eligibility', confidence=0.9))
        text = intake.clarify_text(d)
        self.assertIn('请先告诉我', text)
        self.assertIn('省/市/县', text)
        self.assertLessEqual(len(text), 220)
        self.assertEqual(text, intake.clarify_text(d))


if __name__ == '__main__':
    unittest.main()
