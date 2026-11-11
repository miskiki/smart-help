# -*- coding: utf-8 -*-
"""生命周期 → outbox → policy_worker 级联端到端测试（sqlite，不连生产库）。

覆盖"状态/版本变更真的会触发侧效应"这条链路：lifecycle 在同一个事务里投递 4 类事件，
policy_worker 的 4 个 handler 逐一消费（向量重建走 kb_rag 打桩、规则重编译真入队、
KG 队列真 upsert、缓存清理打桩），验证事件闭环、幂等与失败进死信。

为什么规则/KG 用独立的 sidecar 库：handlers 按生产约定自己开连接（rule_store.engine()），
sqlite 同一个库文件在 drain 事务里再开写连接会互相锁死；sidecar 库既保留真实 SQL 路径，
又避免测试环境死锁。
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest import mock

from sqlalchemy import create_engine, text

from policy_core import lifecycle, outbox, policy_worker, rule_store

POLICY_DDL = """
CREATE TABLE IF NOT EXISTS "policy" (
    "policy_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "policy_code" TEXT, "policy_name" TEXT, "level" TEXT,
    "regions" TEXT, "doc_ref" TEXT, "status" TEXT,
    "cur_version" TEXT, "updated_at" TEXT, "updated_by" TEXT, "source" TEXT
)"""

KG_DDL = """
CREATE TABLE IF NOT EXISTS kg_update_queue (
    policy_code TEXT PRIMARY KEY,
    policy_version TEXT,
    requested_at TEXT,
    reason TEXT,
    status TEXT
)"""


@contextmanager
def env(**values):
    """临时设置环境变量；None 表示删除。"""
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class CascadeBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cascade-e2e-')
        # 主库：业务表 + outbox（lifecycle 在这里写状态与事件）
        self.engine = create_engine('sqlite:///' + os.path.join(self.tmpdir, 'policy.db'))
        # sidecar 库：规则队列与 KG 队列（handlers 按生产约定自己开连接）
        self.side = create_engine('sqlite:///' + os.path.join(self.tmpdir, 'side.db'))
        with self.engine.begin() as conn:
            conn.execute(text(POLICY_DDL))
            outbox.ensure_schema(conn)
            conn.execute(text(
                'INSERT INTO policy (policy_code, policy_name, "level", regions, status, '
                'cur_version) VALUES (:code, :name, :level, :regions, :status, :version)'),
                dict(code='X-ZB-300', name='级联测试政策', level='X', regions='莲花县',
                     status='在用', version='v1'))
        with self.side.begin() as conn:
            conn.execute(text(POLICY_DDL))
            conn.execute(text(KG_DDL))
            for ddl in rule_store.SCHEMA:
                conn.execute(text(ddl))
            conn.execute(text(
                'INSERT INTO policy (policy_code, policy_name, "level", regions, status, '
                'cur_version) VALUES (:code, :name, :level, :regions, :status, :version)'),
                dict(code='X-ZB-300', name='级联测试政策', level='X', regions='莲花县',
                     status='在用', version='v1'))
            for key, scope in (('r1', '莲花县'), ('r2', '莲花县'), ('r3', '*')):
                conn.execute(text(
                    'INSERT INTO policy_rule (rule_key, policy_code, detail_id, version, '
                    'text_hash, region_scope, rule_json, status) VALUES '
                    "(:key, 'X-ZB-300', 'main', 'v1', :hash, :scope, '{}', 'complete')"),
                    dict(key=key, hash='h-' + key, scope=scope))

        # 打桩：向量层与缓存层不连 8095，也不碰真实库。
        self.vector_calls = []
        self.patch_kb = mock.patch.multiple(
            'kb_rag',
            sync_policy=mock.Mock(side_effect=lambda *a, **kw: self.vector_calls.append(('sync', a[0]))),
            remove_policy=mock.Mock(side_effect=lambda *a, **kw: self.vector_calls.append(('remove', a[0]))),
            _invalidate_cache=mock.Mock(side_effect=lambda: self.vector_calls.append(('cache',))),
            _db=mock.Mock(side_effect=lambda *a, **kw: self.engine),
        )
        self.patch_kb.start()
        self.fake_modules = mock.patch.dict(sys.modules, {
            'smart_matcher_kb': self._module('smart_matcher_kb', 'invalidate_db_cache'),
            'kg_retrieval': self._module('kg_retrieval', 'refresh_cache'),
        })
        self.fake_modules.start()
        self.store_engine = mock.patch.object(rule_store, 'engine', lambda: self.side)
        self.store_engine.start()

    def tearDown(self):
        self.store_engine.stop()
        self.fake_modules.stop()
        self.patch_kb.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _module(name, attr):
        module = types.ModuleType(name)
        module.calls = []

        def fake():
            module.calls.append(1)
        setattr(module, attr, fake)
        return module

    # ---------------- 断言辅助 ----------------
    def drain(self):
        return policy_worker.process_once(engine_obj=self.engine)

    def outbox_counts(self):
        with self.engine.connect() as conn:
            return outbox.counts(conn)

    def policy_row(self, code='X-ZB-300'):
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT status, cur_version FROM policy '
                                    'WHERE policy_code=:c'), dict(c=code)).fetchone()
        return None if row is None else (row[0], row[1])

    def job_state(self, region):
        with self.side.connect() as conn:
            row = conn.execute(text('SELECT state, attempts FROM policy_rule_job '
                                    'WHERE region=:r'), dict(r=region)).fetchone()
        return None if row is None else (row[0], int(row[1] or 0))

    def kg_row(self, code='X-ZB-300'):
        with self.side.connect() as conn:
            row = conn.execute(text('SELECT policy_version, status FROM kg_update_queue '
                                    'WHERE policy_code=:c'), dict(c=code)).fetchone()
        return None if row is None else (row[0], row[1])

    def has_vector_call(self, name, code='X-ZB-300'):
        return (name, code) in self.vector_calls


class SuspendCascadeTest(CascadeBase):
    def test_suspend_triggers_all_four_side_effects(self):
        with self.engine.begin() as conn:
            result = lifecycle.suspend(conn, 'X-ZB-300', '停用测试', 'tester')
        self.assertEqual(result['state_after'], 'suspended')
        self.assertEqual(self.outbox_counts(), {'pending': 4})

        summary = self.drain()
        self.assertEqual(summary['done'], 4)
        self.assertEqual(summary['dead'], 0)
        self.assertEqual(summary['retried'], 0)
        self.assertEqual(summary['before'], {'pending': 4})
        self.assertEqual(summary['after'], {'done': 4})

        self.assertEqual(self.policy_row()[0], '停用')
        self.assertTrue(self.has_vector_call('remove'))
        # 规则重编译：同县两条规则去重成一条任务，'*' 省级规则跳过。
        self.assertEqual(self.job_state('莲花县'), ('pending', 0))
        self.assertIsNone(self.job_state('*'))
        self.assertEqual(self.kg_row(), ('v1', 'pending'))
        self.assertEqual(len(sys.modules['smart_matcher_kb'].calls), 1)
        self.assertEqual(len(sys.modules['kg_retrieval'].calls), 1)

    def test_second_drain_is_noop(self):
        with self.engine.begin() as conn:
            lifecycle.suspend(conn, 'X-ZB-300', '停用测试', 'tester')
        self.assertEqual(self.drain()['done'], 4)
        summary = self.drain()
        self.assertEqual(summary['processed'], 0)
        self.assertEqual(summary['done'], 0)
        self.assertEqual(self.outbox_counts(), {'done': 4})

    def test_restore_reactivates_policy_and_vector(self):
        with self.engine.begin() as conn:
            lifecycle.suspend(conn, 'X-ZB-300', '停用测试', 'tester')
        self.drain()
        with self.engine.begin() as conn:
            lifecycle.restore(conn, 'X-ZB-300', '恢复测试', 'tester')
        summary = self.drain()
        self.assertEqual(summary['done'], 4)
        self.assertEqual(self.policy_row(), ('在用', 'v1'))
        self.assertTrue(self.has_vector_call('sync'))
        self.assertEqual(self.job_state('莲花县'), ('pending', 0))
        self.assertEqual(self.kg_row()[1], 'pending')


class PublishCascadeTest(CascadeBase):
    def _add_version(self):
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE IF NOT EXISTS policy_version (version_id INTEGER '
                              'PRIMARY KEY AUTOINCREMENT, policy_id INTEGER, version_no TEXT, '
                              'content_snapshot TEXT, updated_at TEXT)'))
            conn.execute(text('INSERT INTO policy_version (policy_id, version_no, content_snapshot) '
                              "VALUES (1, 'v2', '{}')"))
        # 生产里 policy 表只有一份；测试用 sidecar 承接 handler 的独立连接，这里同步 cur_version。
        with self.side.begin() as conn:
            conn.execute(text("UPDATE policy SET cur_version='v2' WHERE policy_code='X-ZB-300'"))

    def test_publish_active_policy_rebuilds_vector(self):
        self._add_version()
        with self.engine.begin() as conn:
            lifecycle.publish(conn, 'X-ZB-300', 'v2', 'tester', '发布 v2')
        summary = self.drain()
        self.assertEqual(summary['done'], 4)
        self.assertEqual(self.policy_row(), ('在用', 'v2'))
        self.assertTrue(self.has_vector_call('sync'))
        self.assertEqual(self.kg_row(), ('v2', 'pending'))
        self.assertEqual(self.job_state('莲花县'), ('pending', 0))

    def test_publish_suspended_policy_keeps_status_and_clears_vector(self):
        self._add_version()
        with self.engine.begin() as conn:
            lifecycle.suspend(conn, 'X-ZB-300', '停用测试', 'tester')
        self.drain()
        with self.engine.begin() as conn:
            lifecycle.publish(conn, 'X-ZB-300', 'v2', 'tester', '发布 v2')
        summary = self.drain()
        self.assertEqual(summary['done'], 4)
        self.assertEqual(self.policy_row(), ('停用', 'v2'))
        self.assertTrue(self.has_vector_call('remove'))


class HandlerResilienceTest(CascadeBase):
    def test_unknown_policy_event_is_consumed(self):
        with self.engine.begin() as conn:
            outbox.enqueue(conn, 'policy.vector_rebuild',
                           dict(policy_code='X-ZB-999', action='suspend'))
        summary = self.drain()
        self.assertEqual(summary['done'], 1)
        self.assertIn(('remove', 'X-ZB-999'), self.vector_calls)

    def test_handler_exception_goes_dead_after_attempts(self):
        with self.engine.begin() as conn:
            outbox.enqueue(conn, 'policy.cache_invalidate', dict(policy_code='X-ZB-300'))
        handlers = {'policy.cache_invalidate': lambda payload: (_ for _ in ()).throw(RuntimeError('boom'))}
        with env(OUTBOX_MAX_ATTEMPTS='1'):
            summary = policy_worker.process_once(engine_obj=self.engine, handlers=handlers)
        self.assertEqual(summary['dead'], 1)
        self.assertEqual(self.outbox_counts(), {'dead': 1})

    def test_cache_partial_failure_still_marks_done(self):
        bad = self._module('bad_module', 'refresh_cache')
        bad.refresh_cache = mock.Mock(side_effect=RuntimeError('broken'))
        with mock.patch.dict(sys.modules, {'kg_retrieval': bad}):
            cleared = policy_worker.handle_cache_invalidate({'policy_code': 'X-ZB-300'})
        self.assertEqual(cleared['ok'], True)
        self.assertEqual(cleared['cleared'], ['kb_rag', 'smart_matcher_kb'])

    def test_kg_sync_upsert_is_idempotent(self):
        payload = dict(policy_code='X-ZB-300', action='publish', version='v2')
        policy_worker.handle_kg_sync(payload)
        policy_worker.handle_kg_sync(payload)
        with self.side.connect() as conn:
            rows = conn.execute(text('SELECT count(*) FROM kg_update_queue')).scalar()
        self.assertEqual(rows, 1)

    def test_rule_invalidate_global_only_queues_nothing(self):
        with self.side.begin() as conn:
            conn.execute(text("DELETE FROM policy_rule WHERE region_scope <> '*'"))
        with self.engine.begin() as conn:
            outbox.enqueue(conn, 'policy.rule_invalidate', dict(policy_code='X-ZB-300'))
        summary = self.drain()
        self.assertEqual(summary['done'], 1)
        with self.side.connect() as conn:
            jobs = conn.execute(text('SELECT count(*) FROM policy_rule_job')).scalar()
        self.assertEqual(jobs, 0)


if __name__ == '__main__':
    unittest.main()