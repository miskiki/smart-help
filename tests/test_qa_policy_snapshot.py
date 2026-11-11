# -*- coding: utf-8 -*-
"""问答快照附加政策版本/状态单测（task-29）。

覆盖：开关真值矩阵、编号来源优先级、sqlite 真实只读查询、缺失编号、
去重、异常兜底、既有键与 JSON 序列化不受影响。
"""
import json
import os
import re
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from sqlalchemy import create_engine, text

from result_snapshot import freeze

POLICY_DDL = """
CREATE TABLE policy (
    policy_id INTEGER PRIMARY KEY,
    policy_code VARCHAR(32),
    policy_name VARCHAR(255),
    "level" VARCHAR(8),
    status VARCHAR(16),
    cur_version VARCHAR(16),
    updated_at VARCHAR(32)
)
"""


def _insert_policy(conn, code, name, level, status, version, updated_at):
    conn.execute(text('INSERT INTO policy (policy_code, policy_name, "level", status, cur_version, updated_at) '
                      'VALUES (:code, :name, :level, :status, :version, :updated_at)'),
                 {'code': code, 'name': name, 'level': level, 'status': status,
                  'version': version, 'updated_at': updated_at})


def _meta(*codes):
    return {'table_rows': [{'成员': '政策咨询', '政策编号': code} for code in codes]}


class SnapshotTestBase(unittest.TestCase):
    """sqlite 建 policy 表 + 两行；kb_rag._db 指向临时库，绝不连生产。"""

    def setUp(self):
        self._saved_switch = os.environ.pop('POLICY_QA_SNAPSHOT', None)
        self.addCleanup(self._restore_switch)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = os.path.join(self.tmp.name, 'policy.db')
        self.engine = create_engine('sqlite:///' + db_path)
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(text(POLICY_DDL))
            _insert_policy(conn, 'P-1', '政策一', 'S', '在用', 'v2', '2026-11-01 10:00:00')
            _insert_policy(conn, 'P-2', '政策二', 'X', '停用', 'v1', '2026-10-01 09:00:00')
        patcher = mock.patch('kb_rag._db', return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_switch(self):
        os.environ.pop('POLICY_QA_SNAPSHOT', None)
        if self._saved_switch is not None:
            os.environ['POLICY_QA_SNAPSHOT'] = self._saved_switch

    def _set_switch(self, value):
        if value is None:
            os.environ.pop('POLICY_QA_SNAPSHOT', None)
        else:
            os.environ['POLICY_QA_SNAPSHOT'] = value

    def _freeze(self, meta, answer='回答正文'):
        return freeze(meta, answer)


class SwitchTest(SnapshotTestBase):
    def test_default_on_writes_snapshot(self):
        snapshot = self._freeze(_meta('P-1'))
        self.assertIn('policy_snapshot', snapshot)
        self.assertEqual(snapshot['policy_snapshot']['version'], 'qa-policy-snapshot-1')
        self.assertEqual([item['code'] for item in snapshot['policy_snapshot']['items']], ['P-1'])

    def test_truthy_values_keep_enabled(self):
        for value in ('1', 'true', 'TRUE', 'yes', ''):
            with self.subTest(value=value):
                self._set_switch(value)
                snapshot = self._freeze(_meta('P-1'))
                self.assertIn('policy_snapshot', snapshot)

    def test_off_values_disable(self):
        for value in ('0', 'off', 'OFF', 'false', 'False', 'no', ' no '):
            with self.subTest(value=value):
                self._set_switch(value)
                snapshot = self._freeze(_meta('P-1'))
                self.assertNotIn('policy_snapshot', snapshot)

    def test_off_leaves_existing_key_untouched(self):
        self._set_switch('0')
        snapshot = self._freeze({'match_json': {'policy_snapshot': {'version': 'old'}},
                                 'table_rows': [{'政策编号': 'P-1'}]})
        self.assertEqual(snapshot['policy_snapshot'], {'version': 'old'})


class CodesTest(SnapshotTestBase):
    def test_items_match_db_rows(self):
        snapshot = self._freeze(_meta('P-1', 'P-2'))
        items = snapshot['policy_snapshot']['items']
        self.assertEqual(items, [
            {'code': 'P-1', 'name': '政策一', 'level': 'S', 'status': '在用',
             'cur_version': 'v2', 'updated_at': '2026-11-01 10:00:00'},
            {'code': 'P-2', 'name': '政策二', 'level': 'X', 'status': '停用',
             'cur_version': 'v1', 'updated_at': '2026-10-01 09:00:00'},
        ])
        self.assertEqual(snapshot['policy_snapshot']['missing'], [])

    def test_missing_codes(self):
        snapshot = self._freeze(_meta('P-1', 'GONE'))
        self.assertEqual([item['code'] for item in snapshot['policy_snapshot']['items']], ['P-1'])
        self.assertEqual(snapshot['policy_snapshot']['missing'], ['GONE'])

    def test_no_codes_no_key(self):
        snapshot = self._freeze({'table_rows': [{'成员': '政策咨询'}],
                                 'matched_list': [{'name': '无编号'}],
                                 'matched': [None, '']})
        self.assertNotIn('policy_snapshot', snapshot)

    def test_table_rows_priority_over_matched_list(self):
        snapshot = self._freeze({'table_rows': [{'政策编号': 'P-2'}],
                                 'matched_list': [{'code': 'P-1', 'name': '政策一', 'applicable': True}]})
        self.assertEqual([item['code'] for item in snapshot['policy_snapshot']['items']], ['P-2'])

    def test_export_rows_fallback(self):
        snapshot = self._freeze({'export_rows': [{'政策编号': 'P-1'}]})
        self.assertEqual([item['code'] for item in snapshot['policy_snapshot']['items']], ['P-1'])

    def test_matched_list_fallback(self):
        snapshot = self._freeze({'table_rows': [],
                                 'matched_list': [{'code': 'P-1', 'name': '政策一', 'applicable': True}]})
        self.assertEqual([item['code'] for item in snapshot['policy_snapshot']['items']], ['P-1'])

    def test_matched_fallback(self):
        snapshot = self._freeze({'matched': ['P-2', 'P-1']})
        self.assertEqual([item['code'] for item in snapshot['policy_snapshot']['items']], ['P-2', 'P-1'])

    def test_duplicate_codes_deduped(self):
        snapshot = self._freeze(_meta('P-1', 'P-1', 'P-2'))
        items = snapshot['policy_snapshot']['items']
        self.assertEqual([item['code'] for item in items], ['P-1', 'P-2'])
        self.assertEqual(len(items), 2)

    def test_bad_matched_list_items_ignored(self):
        snapshot = self._freeze({'table_rows': [],
                                 'matched_list': [{'code': 'P-1'}, None, 'P-2', {'code': ''}]})
        self.assertEqual([item['code'] for item in snapshot['policy_snapshot']['items']], ['P-1'])


class RobustnessTest(SnapshotTestBase):
    def test_db_error_does_not_raise(self):
        with mock.patch('kb_rag._db', side_effect=RuntimeError('db down')):
            snapshot = self._freeze(_meta('P-1'), answer='完整回答')
        self.assertNotIn('policy_snapshot', snapshot)
        self.assertEqual(snapshot['answer'], '完整回答')
        self.assertEqual(snapshot['schema_version'], 2)
        self.assertEqual(snapshot['questions'], [])
        self.assertFalse(snapshot['will_missing'])
        self.assertEqual(snapshot['judge_status'], 'complete')

    def test_existing_keys_preserved_and_snapshot_appended_last(self):
        existing = {'kind': 'consultation', 'matched_list': [], 'custom': 'keep'}
        rows = [{'成员': '政策咨询', '政策编号': 'P-1'}]
        meta = {'match_json': existing, 'table_rows': rows,
                'questions': [{'key': 'age'}], 'will_missing': True, 'judge_status': 'partial'}
        snapshot = self._freeze(meta, answer='回答')
        self.assertEqual(snapshot['custom'], 'keep')
        self.assertEqual(snapshot['kind'], 'consultation')
        self.assertEqual(snapshot['schema_version'], 2)
        self.assertIs(snapshot['export_rows'], rows)
        self.assertEqual(snapshot['questions'], [{'key': 'age'}])
        self.assertTrue(snapshot['will_missing'])
        self.assertEqual(snapshot['judge_status'], 'partial')
        self.assertEqual(list(snapshot)[-1], 'policy_snapshot')
        self.assertNotIn('policy_snapshot', existing)

    def test_json_serializable(self):
        snapshot = self._freeze(_meta('P-1', 'GONE'))
        dumped = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual(json.loads(dumped)['policy_snapshot']['missing'], ['GONE'])

    def test_generated_at_format(self):
        snapshot = self._freeze(_meta('P-1'))
        self.assertRegex(snapshot['policy_snapshot']['generated_at'],
                         r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_datetime_column_serialized(self):
        fake_rows = [('P-1', '政策一', 'S', '在用', 'v2', datetime(2026, 1, 2, 3, 4, 5))]
        with mock.patch('result_snapshot._query_policy_rows', return_value=fake_rows):
            snapshot = self._freeze(_meta('P-1'))
        item = snapshot['policy_snapshot']['items'][0]
        self.assertEqual(item['updated_at'], '2026-01-02T03:04:05')
        json.dumps(snapshot, ensure_ascii=False)

    def test_query_is_readonly(self):
        self._freeze(_meta('P-1', 'P-2'))
        with self.engine.connect() as conn:
            rows = conn.execute(text('SELECT policy_code, status, cur_version FROM policy '
                                     'ORDER BY policy_code')).fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [('P-1', '在用', 'v2'), ('P-2', '停用', 'v1')])


if __name__ == '__main__':
    unittest.main()
