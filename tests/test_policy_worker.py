# -*- coding: utf-8 -*-
"""outbox 消费者（policy_worker）测试：开关、消费语义、四类 handler、失败重试。"""
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from unittest import mock

from sqlalchemy import inspect, text
from sqlalchemy import create_engine

from policy_core import outbox, policy_worker


def _engine():
    folder = tempfile.mkdtemp(prefix='policy_worker_')
    return create_engine('sqlite:///' + os.path.join(folder, 'worker.db'))


class SwitchTest(unittest.TestCase):
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('POLICY_WORKER', None)
            self.assertFalse(policy_worker.enabled())

    def test_on_values(self):
        for value in ('1', 'on', 'true', 'yes', 'TRUE'):
            with mock.patch.dict(os.environ, {'POLICY_WORKER': value}, clear=False):
                self.assertTrue(policy_worker.enabled(), value)

    def test_off_values(self):
        for value in ('0', 'off', 'false', 'no', ''):
            with mock.patch.dict(os.environ, {'POLICY_WORKER': value}, clear=False):
                self.assertFalse(policy_worker.enabled(), value)

    def test_interval_and_batch_guards(self):
        with mock.patch.dict(os.environ, {'POLICY_WORKER_INTERVAL': '5',
                                          'POLICY_WORKER_BATCH': '3'}, clear=False):
            self.assertEqual(policy_worker.interval(), 5)
            self.assertEqual(policy_worker.batch_size(), 3)
        with mock.patch.dict(os.environ, {'POLICY_WORKER_INTERVAL': 'bad',
                                          'POLICY_WORKER_BATCH': '-1'}, clear=False):
            self.assertEqual(policy_worker.interval(), 15)
            self.assertEqual(policy_worker.batch_size(), 10)


class DrainTest(unittest.TestCase):
    def setUp(self):
        self.engine = _engine()
        with self.engine.begin() as conn:
            outbox.ensure_schema(conn)

    def _enqueue(self, event_type, payload):
        with self.engine.begin() as conn:
            return outbox.enqueue(conn, event_type, payload)

    def test_process_once_runs_handler_and_marks_done(self):
        seen = []
        event_id = self._enqueue('policy.vector_rebuild', {'policy_code': 'X-ZB-001'})
        handlers = {'policy.vector_rebuild': lambda payload: seen.append(payload['policy_code'])}
        summary = policy_worker.process_once(self.engine, handlers=handlers)
        self.assertEqual(seen, ['X-ZB-001'])
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(summary['done'], 1)
        with self.engine.connect() as conn:
            status = conn.execute(text('SELECT status FROM outbox WHERE id=:id'),
                                  {'id': event_id}).scalar()
        self.assertEqual(status, 'done')

    def test_failure_retries_then_dead(self):
        handler = mock.Mock(side_effect=RuntimeError('boom'))
        self._enqueue('policy.cache_invalidate', {'policy_code': 'X-ZB-001'})
        handlers = {'policy.cache_invalidate': handler}
        first = policy_worker.process_once(self.engine, handlers=handlers)
        self.assertEqual(first['retried'], 1)
        with mock.patch.dict(os.environ, {'OUTBOX_MAX_ATTEMPTS': '1'}, clear=False):
            second = policy_worker.process_once(self.engine, handlers=handlers)
        self.assertEqual(second['dead'], 1)
        self.assertEqual(handler.call_count, 2)

    def test_unknown_event_type_goes_dead(self):
        # 部署缺 handler：直接 dead，不无限重试
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO outbox (event_type,payload,status,attempts,created_at,updated_at) "
                "VALUES ('policy.kg_sync','{}','pending',0,'t','t')"))
        summary = policy_worker.process_once(self.engine, handlers={})
        self.assertEqual(summary['skipped'], 1)
        self.assertEqual(summary['dead'], 1)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT status FROM outbox WHERE event_type='policy.kg_sync'")).scalar(), 'dead')

    def test_idempotent_repeat_drain(self):
        calls = []
        self._enqueue('policy.cache_invalidate', {'policy_code': 'A'})
        summary = policy_worker.process_once(
            self.engine, handlers={'policy.cache_invalidate': lambda p: calls.append(p)})
        self.assertEqual(summary['done'], 1)
        again = policy_worker.process_once(
            self.engine, handlers={'policy.cache_invalidate': lambda p: calls.append(p)})
        self.assertEqual(again['processed'], 0)
        self.assertEqual(len(calls), 1)


class VectorHandlerTest(unittest.TestCase):
    def _fake_kb(self):
        fake = types.ModuleType('kb_rag')
        fake.remove_policy = mock.Mock(return_value={'ok': True, 'deleted': 1})
        fake.sync_policy = mock.Mock(return_value={'ok': True, 'chunks': 2})
        return fake

    def test_active_policy_rebuilds_vector(self):
        fake = self._fake_kb()
        with mock.patch.dict(sys.modules, {'kb_rag': fake}), \
                mock.patch.object(policy_worker, '_policy_row',
                                  return_value=('名', '文号', 'v2', '在用')):
            result = policy_worker.handle_vector_rebuild({'policy_code': 'X-ZB-001'})
        self.assertTrue(result['ok'])
        fake.sync_policy.assert_called_once_with('X-ZB-001', '名', '文号', '在用', 'v2', None)
        fake.remove_policy.assert_not_called()

    def test_suspend_deletes_vector_without_sync(self):
        fake = self._fake_kb()
        with mock.patch.dict(sys.modules, {'kb_rag': fake}):
            result = policy_worker.handle_vector_rebuild(
                {'policy_code': 'X-ZB-001', 'action': 'suspend'})
        self.assertTrue(result['ok'])
        fake.remove_policy.assert_called_once_with('X-ZB-001')
        fake.sync_policy.assert_not_called()

    def test_missing_policy_cleans_stale_vector(self):
        fake = self._fake_kb()
        with mock.patch.dict(sys.modules, {'kb_rag': fake}), \
                mock.patch.object(policy_worker, '_policy_row', return_value=None):
            policy_worker.handle_vector_rebuild({'policy_code': 'GONE'})
        fake.remove_policy.assert_called_once_with('GONE')
        fake.sync_policy.assert_not_called()

    def test_non_active_status_does_not_rebuild(self):
        fake = self._fake_kb()
        with mock.patch.dict(sys.modules, {'kb_rag': fake}), \
                mock.patch.object(policy_worker, '_policy_row',
                                  return_value=('名', '', 'v1', '停用')):
            policy_worker.handle_vector_rebuild({'policy_code': 'X-ZB-001', 'action': 'publish'})
        fake.remove_policy.assert_called_once_with('X-ZB-001')
        fake.sync_policy.assert_not_called()

    def test_missing_code_is_reported(self):
        fake = self._fake_kb()
        with mock.patch.dict(sys.modules, {'kb_rag': fake}):
            result = policy_worker.handle_vector_rebuild({})
        self.assertFalse(result['ok'])


class RuleInvalidateHandlerTest(unittest.TestCase):
    def test_enqueues_distinct_regions_and_skips_global(self):
        from policy_core import rule_store
        fake_conn = mock.MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        fake_conn.execute.return_value.fetchall.return_value = [('莲花县',), ('莲花县',), ('*',)]
        engine = mock.MagicMock()
        engine.connect.return_value = fake_conn
        enqueued = []
        with mock.patch.object(rule_store, 'engine', return_value=engine), \
                mock.patch.object(rule_store, 'enqueue',
                                  side_effect=lambda region, **kw: (enqueued.append(region), 'pending')[1]):
            result = policy_worker.handle_rule_invalidate({'policy_code': 'X-ZB-001'})
        self.assertTrue(result['ok'])
        self.assertEqual(enqueued, ['莲花县'])
        self.assertEqual(result['global_rules'], 1)


class KgSyncHandlerTest(unittest.TestCase):
    def test_missing_queue_table_is_skipped(self):
        from policy_core import rule_store
        engine = _engine()
        with mock.patch.object(rule_store, 'engine', return_value=engine):
            result = policy_worker.handle_kg_sync({'policy_code': 'X-ZB-001'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['skipped'], 'kg_update_queue_missing')

    def test_upserts_pending_job(self):
        from policy_core import rule_store
        engine = _engine()
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE policy (policy_code varchar(64), cur_version varchar(32))'))
            conn.execute(text('INSERT INTO policy VALUES (:c,:v)'), {'c': 'X-ZB-001', 'v': 'v3'})
            conn.execute(text('CREATE TABLE kg_update_queue (policy_code varchar(64) PRIMARY KEY)'))
            conn.execute(text('ALTER TABLE kg_update_queue ADD COLUMN policy_version varchar(32)'))
            conn.execute(text('ALTER TABLE kg_update_queue ADD COLUMN requested_at varchar(32)'))
            conn.execute(text('ALTER TABLE kg_update_queue ADD COLUMN reason varchar(200)'))
            conn.execute(text('ALTER TABLE kg_update_queue ADD COLUMN status varchar(16)'))
        with mock.patch.object(rule_store, 'engine', return_value=engine):
            policy_worker.handle_kg_sync({'policy_code': 'X-ZB-001'})
            policy_worker.handle_kg_sync({'policy_code': 'X-ZB-001'})
        with engine.connect() as conn:
            rows = conn.execute(text(
                'SELECT policy_version,status FROM kg_update_queue WHERE policy_code=:c'),
                {'c': 'X-ZB-001'}).fetchall()
        self.assertEqual(rows, [('v3', 'pending')])


class CacheInvalidateHandlerTest(unittest.TestCase):
    def test_returns_ok_even_if_some_modules_fail(self):
        result = policy_worker.handle_cache_invalidate({})
        self.assertTrue(result['ok'])
        self.assertIsInstance(result['cleared'], list)


class StartTest(unittest.TestCase):
    """app.py 启动钩子：关闭时不建线程/不连库；开启时起守护线程并建表（幂等）。"""

    def tearDown(self):
        policy_worker.stop()

    def test_disabled_does_not_start(self):
        policy_worker.stop()
        engine = _engine()
        with mock.patch.dict(os.environ, {'POLICY_WORKER': '0'}, clear=False):
            self.assertFalse(policy_worker.start(engine))
        with engine.connect() as conn:
            self.assertFalse(inspect(conn).has_table('outbox'))

    def test_enabled_starts_thread_and_creates_schema(self):
        policy_worker.stop()
        engine = _engine()
        with mock.patch.dict(os.environ, {'POLICY_WORKER': '1',
                                          'POLICY_WORKER_INTERVAL': '3600'}, clear=False):
            self.assertTrue(policy_worker.start(engine))
            deadline = time.monotonic() + 3
            created = False
            while time.monotonic() < deadline:
                with engine.connect() as conn:
                    if inspect(conn).has_table('outbox'):
                        created = True
                        break
                time.sleep(0.05)
            self.assertTrue(created, '启动钩子应在后台建 outbox 表')
            names = [t.name for t in threading.enumerate()]
            self.assertIn('policy-worker', names)
            self.assertFalse(policy_worker.start(engine), '重复启动应幂等返回 False')


if __name__ == '__main__':
    unittest.main()
