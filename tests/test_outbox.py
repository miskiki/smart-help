# -*- coding: utf-8 -*-
"""T18 policy outbox 单测（标准库 unittest + sqlite 文件库，不连生产库）。

覆盖：建表幂等、入队幂等/未知类型、claim 顺序、finish/fail 重试上限、drain
成功/无 handler/失败后重试成功、重复 drain 幂等、状态计数、重试上限环境变量。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine, text

from policy_core import outbox


class OutboxSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='outbox-test-')
        self.engine = create_engine('sqlite:///' + os.path.join(self.tmpdir, 'outbox.db'))
        with self.engine.begin() as conn:
            outbox.ensure_schema(conn)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rows(self):
        with self.engine.connect() as conn:
            return [dict(row) for row in
                    conn.execute(text('SELECT * FROM outbox ORDER BY id')).mappings()]

    def test_ensure_schema_idempotent(self):
        with self.engine.begin() as conn:
            outbox.ensure_schema(conn)
            outbox.ensure_schema(conn)
        self.assertEqual(self._rows(), [])

    def test_enqueue_creates_pending_row(self):
        with self.engine.begin() as conn:
            event_id = outbox.enqueue(conn, 'policy.cache_invalidate',
                                      {'policy_code': 'S-ZB-001'})
        self.assertIsInstance(event_id, int)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'pending')
        self.assertEqual(rows[0]['attempts'], 0)
        self.assertIn('S-ZB-001', rows[0]['payload'])

    def test_enqueue_idempotent_same_payload(self):
        payload = {'policy_code': 'S-ZB-001', 'action': 'suspend'}
        with self.engine.begin() as conn:
            first = outbox.enqueue(conn, 'policy.rule_invalidate', payload)
            second = outbox.enqueue(conn, 'policy.rule_invalidate', dict(payload))
        self.assertEqual(first, second)
        self.assertEqual(len(self._rows()), 1)

    def test_enqueue_distinct_payload_two_rows(self):
        with self.engine.begin() as conn:
            first = outbox.enqueue(conn, 'policy.kg_sync', {'policy_code': 'S-ZB-001'})
            second = outbox.enqueue(conn, 'policy.kg_sync', {'policy_code': 'S-ZB-002'})
        self.assertNotEqual(first, second)
        self.assertEqual(len(self._rows()), 2)

    def test_enqueue_rejects_unknown_event_type(self):
        with self.engine.begin() as conn:
            with self.assertRaises(ValueError):
                outbox.enqueue(conn, 'policy.unknown', {'policy_code': 'S-ZB-001'})
        self.assertEqual(self._rows(), [])

    def test_all_event_types_accepted(self):
        with self.engine.begin() as conn:
            for event_type in outbox.EVENT_TYPES:
                outbox.enqueue(conn, event_type, {'policy_code': 'S-ZB-001'})
        self.assertEqual(len(self._rows()), len(outbox.EVENT_TYPES))

    def test_claim_returns_pending_in_order(self):
        with self.engine.begin() as conn:
            outbox.enqueue(conn, 'policy.vector_rebuild', {'policy_code': 'C-1'})
            outbox.enqueue(conn, 'policy.vector_rebuild', {'policy_code': 'C-2'})
            outbox.enqueue(conn, 'policy.vector_rebuild', {'policy_code': 'C-3'})
        with self.engine.begin() as conn:
            events = outbox.claim(conn, limit=2)
        self.assertEqual([event['payload']['policy_code'] for event in events], ['C-1', 'C-2'])
        self.assertTrue(all(isinstance(event['payload'], dict) for event in events))

    def test_finish_marks_done(self):
        with self.engine.begin() as conn:
            event_id = outbox.enqueue(conn, 'policy.cache_invalidate', {'policy_code': 'C-1'})
        with self.engine.begin() as conn:
            self.assertTrue(outbox.finish(conn, event_id))
        row = self._rows()[0]
        self.assertEqual(row['status'], 'done')
        with self.engine.begin() as conn:
            self.assertFalse(outbox.finish(conn, event_id))

    def test_fail_retries_until_dead(self):
        with self.engine.begin() as conn:
            event_id = outbox.enqueue(conn, 'policy.kg_sync', {'policy_code': 'C-1'})
        with self.engine.begin() as conn:
            status = outbox.fail(conn, event_id, 'boom', attempts_limit=2)
        self.assertEqual(status, 'pending')
        with self.engine.begin() as conn:
            status = outbox.fail(conn, event_id, 'boom again', attempts_limit=2)
        self.assertEqual(status, 'dead')
        row = self._rows()[0]
        self.assertEqual(row['attempts'], 2)
        self.assertIn('boom again', row['last_error'])

    def test_fail_does_not_overwrite_done(self):
        with self.engine.begin() as conn:
            event_id = outbox.enqueue(conn, 'policy.kg_sync', {'policy_code': 'C-1'})
        with self.engine.begin() as conn:
            outbox.finish(conn, event_id)
            status = outbox.fail(conn, event_id, 'late failure')
        self.assertEqual(status, 'done')
        self.assertEqual(self._rows()[0]['status'], 'done')

    def test_drain_runs_handler_and_is_idempotent(self):
        with self.engine.begin() as conn:
            outbox.enqueue(conn, 'policy.cache_invalidate', {'policy_code': 'C-1'})
        seen = []
        with self.engine.begin() as conn:
            summary = outbox.drain(conn, {'policy.cache_invalidate': seen.append})
        self.assertEqual(summary['done'], 1)
        self.assertEqual(seen, [{'policy_code': 'C-1'}])
        with self.engine.begin() as conn:
            again = outbox.drain(conn, {'policy.cache_invalidate': seen.append})
        self.assertEqual(again['processed'], 0)
        self.assertEqual(self._rows()[0]['status'], 'done')

    def test_drain_without_handler_goes_dead(self):
        with self.engine.begin() as conn:
            outbox.enqueue(conn, 'policy.rule_invalidate', {'policy_code': 'C-1'})
        with self.engine.begin() as conn:
            summary = outbox.drain(conn, {})
        self.assertEqual(summary['skipped'], 1)
        self.assertEqual(summary['dead'], 1)
        row = self._rows()[0]
        self.assertEqual(row['status'], 'dead')
        self.assertIn('no_handler_for', row['last_error'])

    def test_drain_retries_then_succeeds(self):
        with self.engine.begin() as conn:
            outbox.enqueue(conn, 'policy.vector_rebuild', {'policy_code': 'C-1'})

        def failing(_payload):
            raise RuntimeError('index offline')

        def succeeding(_payload):
            return True

        with self.engine.begin() as conn:
            first = outbox.drain(conn, {'policy.vector_rebuild': failing}, attempts_limit=3)
        self.assertEqual(first['retried'], 1)
        self.assertEqual(self._rows()[0]['status'], 'pending')
        with self.engine.begin() as conn:
            second = outbox.drain(conn, {'policy.vector_rebuild': succeeding}, attempts_limit=3)
        self.assertEqual(second['done'], 1)
        self.assertEqual(self._rows()[0]['status'], 'done')

    def test_counts_by_status(self):
        with self.engine.begin() as conn:
            first = outbox.enqueue(conn, 'policy.kg_sync', {'policy_code': 'C-1'})
            outbox.enqueue(conn, 'policy.kg_sync', {'policy_code': 'C-2'})
        with self.engine.begin() as conn:
            outbox.finish(conn, first)
        with self.engine.connect() as conn:
            counts = outbox.counts(conn)
        self.assertEqual(counts.get('done'), 1)
        self.assertEqual(counts.get('pending'), 1)

    def test_max_attempts_env(self):
        with mock.patch.dict(os.environ, {'OUTBOX_MAX_ATTEMPTS': '5'}):
            self.assertEqual(outbox.max_attempts(), 5)
        with mock.patch.dict(os.environ, {'OUTBOX_MAX_ATTEMPTS': '0'}):
            self.assertEqual(outbox.max_attempts(), outbox.DEFAULT_MAX_ATTEMPTS)
        with mock.patch.dict(os.environ, {'OUTBOX_MAX_ATTEMPTS': 'not-a-number'}):
            self.assertEqual(outbox.max_attempts(), outbox.DEFAULT_MAX_ATTEMPTS)


if __name__ == '__main__':
    unittest.main()
