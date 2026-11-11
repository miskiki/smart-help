"""Integration tests for clarification choices in household_flow.basics()."""
import os
import unittest

from policy_core.member_labels import MEMBER_LABEL_NAME, AGE_GATED_QUESTION


class _Env(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        for name in (MEMBER_LABEL_NAME, AGE_GATED_QUESTION):
            os.environ.pop(name, None)
    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)


class BasicsTest(_Env):
    def test_member_question_uses_name(self):
        from household_flow import basics
        hh = {"members": [{"member_no": 1, "name": "钱七",
                          "fields": {}}], "region": "江西省赣州市章贡区",
              "basic_review": {}}
        questions = basics(hh)
        labels = [q["label"] for q in questions if q["key"].startswith(("age", "health", "labor"))]
        self.assertTrue(labels, "expected age/health/labor questions")
        self.assertTrue(all(label.startswith("成员1（钱七）") for label in labels), labels)

    def test_member_question_without_name(self):
        from household_flow import basics
        hh = {"members": [{"member_no": 2, "name": None, "fields": {}}],
              "region": "江西省赣州市章贡区", "basic_review": {}}
        questions = basics(hh)
        labels = [q["label"] for q in questions if q["key"].startswith(("age", "health", "labor"))]
        self.assertTrue(all(label.startswith("成员2") for label in labels), labels)

    def test_elderly_family_skips_education_review(self):
        from household_flow import basics
        hh = {"members": [{"member_no": 1, "name": "李爷爷",
                          "fields": {"age": 68, "health": "健康", "labor": "无劳动力"}}],
              "region": "江西省赣州市章贡区", "basic_review": {}}
        questions = basics(hh)
        keys = [q["key"] for q in questions]
        self.assertNotIn("education_review:0", keys, keys)
        self.assertIn("identity_review:0", keys, keys)
        self.assertIn("activity_review:0", keys, keys)

    def test_child_family_keeps_education_review(self):
        from household_flow import basics
        hh = {"members": [{"member_no": 1, "name": "张小明",
                          "fields": {"age": 12, "health": "健康", "labor": "无劳动力"}}],
              "region": "江西省赣州市章贡区", "basic_review": {}}
        questions = basics(hh)
        keys = [q["key"] for q in questions]
        self.assertIn("education_review:0", keys, keys)

    def test_unknown_ages_skip_education_review(self):
        from household_flow import basics
        hh = {"members": [{"member_no": 1, "name": "先生", "fields": {}}],
              "region": "江西省赣州市章贡区", "basic_review": {}}
        questions = basics(hh)
        keys = [q["key"] for q in questions]
        self.assertNotIn("education_review:0", keys, keys)

    def test_no_member_everything_none(self):
        from household_flow import basics
        hh = {"members": [], "region": "江西省赣州市章贡区", "basic_review": {}}
        questions = basics(hh)
        self.assertEqual([q["key"] for q in questions], ["identity_review:0", "activity_review:0"])

    def test_switch_off_restores_education_review_for_elderly(self):
        from household_flow import basics
        os.environ[AGE_GATED_QUESTION] = "0"
        hh = {"members": [{"member_no": 1, "name": "李爷爷",
                          "fields": {"age": 68}}],
              "region": "江西省赣州市章贡区", "basic_review": {}}
        questions = basics(hh)
        self.assertIn("education_review:0", [q["key"] for q in questions])