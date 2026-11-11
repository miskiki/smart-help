# -*- coding: utf-8 -*-
"""T18 政策生命周期单测（标准库 unittest + sqlite 文件库，不连生产库）。

覆盖：开关默认关闭、状态归一、deleted_at 补列、停用/归档/恢复/软删/硬删、
状态流转非法、dry_run 不落库、reason 校验、impact 计数、版本链、发布/回滚、
audit_log 缺失不崩、outbox 事件内容、API 开关关闭 404 与参数校验。
"""
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from flask import Flask, jsonify
from sqlalchemy import create_engine, inspect, text

from policy_core import lifecycle, outbox

_ACTIVE = 'S-ZB-001'
_SUSPENDED = 'S-ZB-002'
_DRAFT = 'D-ZB-003'
_ARCHIVED = 'S-ZB-004'
_DELETED = 'S-ZB-005'
_DRAFT_PUBLISH = 'D-ZB-006'
_DRAFT_WITH_QA = 'D-ZB-007'

_SCHEMA = [
    'CREATE TABLE policy (policy_id INTEGER PRIMARY KEY, policy_code TEXT, '
    'policy_name TEXT, level TEXT, category1 TEXT, status TEXT, cur_version TEXT, '
    'updated_at TEXT, updated_by TEXT, regions TEXT, source TEXT, doc_ref TEXT, '
    'level_note TEXT)',
    'CREATE TABLE policy_detail (detail_id INTEGER PRIMARY KEY, policy_id INTEGER, region TEXT)',
    'CREATE TABLE policy_version (version_id INTEGER PRIMARY KEY, policy_id INTEGER, '
    'version_no TEXT, content_snapshot TEXT, note TEXT, updated_by TEXT, '
    'updated_at TEXT, effect_date TEXT, expire_date TEXT)',
    'CREATE TABLE audit_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, '
    'target_code TEXT, target_name TEXT, detail TEXT, operator TEXT, created_at TEXT)',
    'CREATE TABLE qa_log (qa_id INTEGER PRIMARY KEY, matched_policies TEXT, match_json TEXT)',
    'CREATE TABLE policy_rule (rule_key TEXT PRIMARY KEY, policy_code TEXT, status TEXT)',
    'CREATE TABLE policy_scope (policy_code TEXT, region_key TEXT)',
    'CREATE TABLE policy_vector (id INTEGER PRIMARY KEY, policy_code TEXT, status TEXT)',
]

_POLICIES = [
    dict(id=1, code=_ACTIVE, name='一号政策', status='在用', version='v2', deleted=None),
    dict(id=2, code=_SUSPENDED, name='二号政策', status='停用', version='v1', deleted=None),
    dict(id=3, code=_DRAFT, name='三号草稿', status='draft', version='v1', deleted=None),
    dict(id=4, code=_ARCHIVED, name='四号归档', status='archived', version='v1', deleted=None),
    dict(id=5, code=_DELETED, name='五号已删', status='deleted', version='v1',
         deleted='2026-01-01 00:00:00'),
    dict(id=6, code=_DRAFT_PUBLISH, name='六号草稿', status='draft', version='', deleted=None),
    dict(id=7, code=_DRAFT_WITH_QA, name='七号草稿', status='draft', version='v1', deleted=None),
]


@contextmanager
def lifecycle_env(value):
    """临时设置/清除 POLICY_LIFECYCLE。value=None 表示不设置（走默认值）。"""
    saved = os.environ.get('POLICY_LIFECYCLE')
    try:
        if value is None:
            os.environ.pop('POLICY_LIFECYCLE', None)
        else:
            os.environ['POLICY_LIFECYCLE'] = value
        yield
    finally:
        if saved is None:
            os.environ.pop('POLICY_LIFECYCLE', None)
        else:
            os.environ['POLICY_LIFECYCLE'] = saved


class LifecycleTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='lifecycle-test-')
        self.engine = create_engine('sqlite:///' + os.path.join(self.tmpdir, 'policy.db'))
        with self.engine.begin() as conn:
            for ddl in _SCHEMA:
                conn.execute(text(ddl))
            lifecycle.ensure_schema(conn)  # 补 deleted_at + outbox 表
        with self.engine.begin() as conn:
            conn.execute(
                text('INSERT INTO policy (policy_id, policy_code, policy_name, status, '
                     'cur_version, updated_at, updated_by, deleted_at) VALUES '
                     '(:id, :code, :name, :status, :version, :updated_at, :operator, :deleted)'),
                [dict(item, updated_at='2026-11-01 00:00:00', operator='tester')
                 for item in _POLICIES])
            for row in (
                    dict(id=1, pid=1, version='v1'),
                    dict(id=2, pid=1, version='v2'),
                    dict(id=3, pid=6, version='v1')):
                conn.execute(text(
                    'INSERT INTO policy_version (version_id, policy_id, version_no, note, '
                    'updated_by, updated_at) VALUES (:id, :pid, :version, :note, :operator, :at)'),
                    dict(row, note='新增', operator='tester', at='2026-11-01 00:00:00'))
            conn.execute(text('INSERT INTO policy_detail (detail_id, policy_id, region) '
                              'VALUES (:id, :pid, :region)'),
                         [dict(id=1, pid=1, region='莲花县'),
                          dict(id=2, pid=1, region='南昌市'),
                          dict(id=3, pid=3, region='莲花县')])
            conn.execute(text('INSERT INTO qa_log (qa_id, matched_policies, match_json) '
                              'VALUES (:id, :matched, :json)'),
                         [dict(id=1, matched='["S-ZB-001"]', json='{}'),
                          dict(id=2, matched='["D-ZB-007"]', json='{}')])
            conn.execute(text('INSERT INTO policy_rule (rule_key, policy_code, status) '
                              'VALUES (:key, :code, :status)'),
                         [dict(key='r1', code=_ACTIVE, status='complete'),
                          dict(key='r2', code=_ACTIVE, status='partial'),
                          dict(key='r3', code=_ACTIVE, status='failed'),
                          dict(key='r4', code=_DRAFT, status='partial')])
            conn.execute(text('INSERT INTO policy_scope (policy_code, region_key) '
                              'VALUES (:code, :key)'),
                         [dict(code=_ACTIVE, key='jx-nanchang-xinjian'),
                          dict(code=_ACTIVE, key='jx-pingxiang-lianhua'),
                          dict(code=_ACTIVE, key='jx-ganzhou'),
                          dict(code=_DRAFT, key='jx-pingxiang-lianhua')])
            conn.execute(text('INSERT INTO policy_vector (id, policy_code, status) '
                              'VALUES (:id, :code, :status)'),
                         [dict(id=1, code=_ACTIVE, status='在用'),
                          dict(id=2, code=_ACTIVE, status='在用'),
                          dict(id=3, code=_DRAFT, status='在用')])

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- helpers ----
    def run_lifecycle(self, fn, *args, **kwargs):
        with self.engine.begin() as conn:
            return fn(conn, *args, **kwargs)

    def policy_row(self, code):
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT * FROM policy WHERE policy_code=:code'),
                               {'code': code}).mappings().first()
        return dict(row) if row else None

    def rows(self, sql, params=None):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(sql), params or {}).mappings()]

    def outbox_rows(self):
        return self.rows('SELECT * FROM outbox ORDER BY id')

    def audit_rows(self):
        return self.rows('SELECT * FROM audit_log ORDER BY log_id')

    def count(self, table):
        return self.rows('SELECT count(*) AS c FROM %s' % table)[0]['c']


class LifecycleStateTest(LifecycleTestBase):
    def test_enabled_default_off(self):
        with lifecycle_env(None):
            self.assertFalse(lifecycle.enabled())
        with lifecycle_env('1'):
            self.assertTrue(lifecycle.enabled())
        with lifecycle_env('true'):
            self.assertTrue(lifecycle.enabled())
        with lifecycle_env('0'):
            self.assertFalse(lifecycle.enabled())

    def test_state_aliases(self):
        self.assertEqual(lifecycle.state_of('在用'), 'active')
        self.assertEqual(lifecycle.state_of('停用'), 'suspended')
        self.assertEqual(lifecycle.state_of('草稿'), 'draft')
        self.assertEqual(lifecycle.state_of('deleted'), 'deleted')
        self.assertEqual(lifecycle.state_of('未知状态'), 'unknown')
        self.assertEqual(lifecycle.state_of(None), 'unknown')

    def test_ensure_schema_adds_deleted_at_idempotent(self):
        tmp = tempfile.mkdtemp(prefix='lifecycle-schema-')
        engine = create_engine('sqlite:///' + os.path.join(tmp, 'schema.db'))
        try:
            with engine.begin() as conn:
                conn.execute(text('CREATE TABLE policy (policy_id INTEGER PRIMARY KEY, '
                                  'policy_code TEXT, status TEXT, cur_version TEXT)'))
                lifecycle.ensure_schema(conn)
                lifecycle.ensure_schema(conn)
            with engine.connect() as conn:
                columns = {column['name'] for column in inspect(conn).get_columns('policy')}
            self.assertIn('deleted_at', columns)
        finally:
            engine.dispose()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_suspend_active_ok(self):
        result = self.run_lifecycle(lifecycle.suspend, _ACTIVE, '政策已废止', 'tester')
        self.assertTrue(result['ok'])
        self.assertEqual(result['state_before'], 'active')
        self.assertEqual(result['state_after'], 'suspended')
        self.assertEqual(result['status_after'], '停用')
        self.assertTrue(result['audit_logged'])
        self.assertEqual(result['outbox_events'], 4)
        self.assertEqual(result['impact']['details'], 2)
        self.assertEqual(self.policy_row(_ACTIVE)['status'], '停用')
        audits = self.audit_rows()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['action'], '暂停政策')
        self.assertEqual(len(self.outbox_rows()), 4)

    def test_suspend_invalid_transition(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.suspend, _DRAFT, '尝试停用草稿', 'tester')
        self.assertEqual(ctx.exception.code, 'invalid_transition')
        self.assertEqual(self.policy_row(_DRAFT)['status'], 'draft')
        self.assertEqual(len(self.outbox_rows()), 0)

    def test_suspend_dry_run_no_write(self):
        result = self.run_lifecycle(lifecycle.suspend, _ACTIVE, '预检停用', 'tester',
                                    dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['state_after'], 'suspended')
        self.assertFalse(result['audit_logged'])
        self.assertEqual(result['outbox_events'], 0)
        self.assertEqual(self.policy_row(_ACTIVE)['status'], '在用')
        self.assertEqual(len(self.audit_rows()), 0)
        self.assertEqual(len(self.outbox_rows()), 0)

    def test_suspend_requires_reason(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.suspend, _ACTIVE, '短', 'tester')
        self.assertEqual(ctx.exception.code, 'invalid_reason')
        self.assertEqual(self.policy_row(_ACTIVE)['status'], '在用')

    def test_archive_active_ok(self):
        result = self.run_lifecycle(lifecycle.archive, _ACTIVE, '长期不再使用', 'tester')
        self.assertEqual(result['state_after'], 'archived')
        self.assertEqual(self.policy_row(_ACTIVE)['status'], 'archived')
        self.assertEqual(result['outbox_events'], 4)

    def test_restore_suspended_ok(self):
        result = self.run_lifecycle(lifecycle.restore, _SUSPENDED, '恢复使用', 'tester')
        self.assertEqual(result['state_before'], 'suspended')
        self.assertEqual(result['state_after'], 'active')
        self.assertEqual(self.policy_row(_SUSPENDED)['status'], '在用')

    def test_restore_archived_ok(self):
        result = self.run_lifecycle(lifecycle.restore, _ARCHIVED, '重新启用归档', 'tester')
        self.assertEqual(result['state_after'], 'active')
        self.assertEqual(self.policy_row(_ARCHIVED)['status'], '在用')

    def test_restore_deleted_requires_confirmation(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.restore, _DELETED, '恢复误删政策', 'tester')
        self.assertEqual(ctx.exception.code, 'confirmation_required')
        result = self.run_lifecycle(lifecycle.restore, _DELETED, '恢复误删政策', 'tester',
                                    confirm=True)
        self.assertEqual(result['state_after'], 'active')
        row = self.policy_row(_DELETED)
        self.assertEqual(row['status'], '在用')
        self.assertIsNone(row['deleted_at'])

    def test_soft_delete_and_restore(self):
        result = self.run_lifecycle(lifecycle.delete_policy, _ACTIVE, '政策作废删除', 'tester')
        self.assertEqual(result['state_after'], 'deleted')
        self.assertEqual(result['status_after'], 'deleted')
        row = self.policy_row(_ACTIVE)
        self.assertEqual(row['status'], 'deleted')
        self.assertIsNotNone(row['deleted_at'])
        self.assertEqual(self.count('policy_scope'), 4)  # scope 保留，不级联删
        self.assertEqual(len(self.audit_rows()), 1)
        restored = self.run_lifecycle(lifecycle.restore, _ACTIVE, '撤销删除', 'tester',
                                      confirm=True)
        self.assertEqual(restored['state_after'], 'active')
        row = self.policy_row(_ACTIVE)
        self.assertEqual(row['status'], '在用')
        self.assertIsNone(row['deleted_at'])

    def test_delete_twice_invalid(self):
        self.run_lifecycle(lifecycle.delete_policy, _ACTIVE, '政策作废删除', 'tester')
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.delete_policy, _ACTIVE, '再次删除', 'tester')
        self.assertEqual(ctx.exception.code, 'invalid_transition')

    def test_hard_delete_draft_ok(self):
        result = self.run_lifecycle(lifecycle.delete_policy, _DRAFT, '清理无效草稿', 'tester',
                                    hard=True)
        self.assertEqual(result['action'], 'hard_delete')
        self.assertEqual(result['deleted_details'], 1)
        self.assertIsNone(self.policy_row(_DRAFT))
        self.assertEqual(self.count('policy_detail'), 2)  # 只删了 draft 的明细
        self.assertEqual(self.audit_rows()[0]['action'], '硬删除政策')
        self.assertEqual(len(self.outbox_rows()), 4)

    def test_hard_delete_active_rejected(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.delete_policy, _ACTIVE, '清理在用政策', 'tester',
                               hard=True)
        self.assertEqual(ctx.exception.code, 'hard_delete_requires_draft')
        self.assertIsNotNone(self.policy_row(_ACTIVE))

    def test_hard_delete_with_qa_rejected(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.delete_policy, _DRAFT_WITH_QA, '清理草稿', 'tester',
                               hard=True)
        self.assertEqual(ctx.exception.code, 'hard_delete_has_qa_references')
        self.assertIsNotNone(self.policy_row(_DRAFT_WITH_QA))

    def test_hard_delete_dry_run(self):
        result = self.run_lifecycle(lifecycle.delete_policy, _DRAFT, '预检清理草稿', 'tester',
                                    hard=True, dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['would_delete_details'], 1)
        self.assertIsNotNone(self.policy_row(_DRAFT))
        self.assertEqual(len(self.outbox_rows()), 0)

    def test_impact_counts(self):
        impact = self.run_lifecycle(lifecycle.impact, _ACTIVE)
        self.assertEqual(impact['state'], 'active')
        self.assertEqual(impact['details'], 2)
        self.assertEqual(impact['scope_rows'], 3)
        self.assertEqual(impact['rules'], 2)  # failed 规则不计
        self.assertEqual(impact['versions'], 2)
        self.assertEqual(impact['vectors'], 2)
        self.assertEqual(impact['vector_table'], 'policy_vector')
        self.assertEqual(impact['qa_log'], 1)
        self.assertTrue(impact['qa_log_available'])

    def test_impact_missing_policy(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.impact, 'NOPE-001')
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, 'policy_not_found')

    def test_versions_current_flag(self):
        items = self.run_lifecycle(lifecycle.versions, _ACTIVE)
        self.assertEqual([item['version_no'] for item in items], ['v2', 'v1'])
        self.assertTrue(items[0]['is_current'])
        self.assertFalse(items[1]['is_current'])

    def test_publish_switches_version(self):
        result = self.run_lifecycle(lifecycle.publish, _ACTIVE, 'v1', 'tester',
                                    reason='回退到旧版本')
        self.assertEqual(result['cur_version_before'], 'v2')
        self.assertEqual(result['cur_version_after'], 'v1')
        self.assertEqual(self.policy_row(_ACTIVE)['cur_version'], 'v1')
        self.assertEqual(self.audit_rows()[0]['action'], '发布版本')
        self.assertEqual(len(self.outbox_rows()), 4)

    def test_publish_draft_activates(self):
        result = self.run_lifecycle(lifecycle.publish, _DRAFT_PUBLISH, 'v1', 'tester',
                                    reason='首次发布')
        self.assertEqual(result['state_before'], 'draft')
        self.assertEqual(result['state_after'], 'active')
        row = self.policy_row(_DRAFT_PUBLISH)
        self.assertEqual(row['status'], '在用')
        self.assertEqual(row['cur_version'], 'v1')

    def test_publish_unknown_version(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.publish, _ACTIVE, 'v9', 'tester')
        self.assertEqual(ctx.exception.code, 'version_not_found')

    def test_publish_same_version_rejected(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.publish, _ACTIVE, 'v2', 'tester')
        self.assertEqual(ctx.exception.code, 'version_already_current')

    def test_rollback_switches_version(self):
        result = self.run_lifecycle(lifecycle.rollback, _ACTIVE, 'v1', 'tester')
        self.assertEqual(result['state_after'], 'active')
        self.assertEqual(self.policy_row(_ACTIVE)['cur_version'], 'v1')
        self.assertEqual(self.audit_rows()[0]['action'], '回滚版本')

    def test_rollback_deleted_rejected(self):
        with self.assertRaises(lifecycle.LifecycleError) as ctx:
            self.run_lifecycle(lifecycle.rollback, _DELETED, 'v1', 'tester')
        self.assertEqual(ctx.exception.code, 'invalid_transition')

    def test_audit_table_missing_does_not_break(self):
        with self.engine.begin() as conn:
            conn.execute(text('DROP TABLE audit_log'))
        result = self.run_lifecycle(lifecycle.suspend, _ACTIVE, '没有审计表也要能停用', 'tester')
        self.assertFalse(result['audit_logged'])
        self.assertEqual(self.policy_row(_ACTIVE)['status'], '停用')

    def test_outbox_events_payload_has_no_private_fields(self):
        self.run_lifecycle(lifecycle.suspend, _ACTIVE, '政策依据变更', 'tester')
        rows = self.outbox_rows()
        self.assertEqual({row['event_type'] for row in rows}, set(outbox.EVENT_TYPES))
        for row in rows:
            payload = json.loads(row['payload'])
            self.assertEqual(payload['policy_code'], _ACTIVE)
            self.assertEqual(payload['action'], 'suspend')
            self.assertTrue(set(payload).issubset(
                {'policy_code', 'action', 'reason', 'operator', 'at', 'version'}))


class _FakeUser:
    username = 'tester'
    role = 'admin'
    must_change_password = False
    real_name = '测试员'


class LifecycleApiTest(unittest.TestCase):
    _ROUTES = [
        ('post', '/api/policies/S-ZB-001/suspend'),
        ('post', '/api/policies/S-ZB-001/archive'),
        ('post', '/api/policies/S-ZB-001/restore'),
        ('post', '/api/policies/S-ZB-001/delete'),
        ('post', '/api/policies/S-ZB-001/publish'),
        ('post', '/api/policies/S-ZB-001/rollback'),
        ('get', '/api/policies/S-ZB-001/impact'),
        ('get', '/api/policies/S-ZB-001/versions'),
    ]

    def setUp(self):
        import api as admin_api
        import authentication
        self.admin_api = admin_api
        app = Flask(__name__)
        app.config['SECRET_KEY'] = 'test'
        app.register_blueprint(admin_api.bp)

        @app.errorhandler(authentication.ApiError)
        def _handle_api_error(exc):
            return jsonify(ok=False, msg=exc.message, code=exc.code), exc.status

        self.client = app.test_client()
        for patch in (
                mock.patch.object(authentication, 'require_user', lambda *a, **k: _FakeUser()),
                mock.patch.object(admin_api, '_login_user', lambda: _FakeUser())):
            patch.start()
            self.addCleanup(patch.stop)

    def test_routes_disabled_return_404(self):
        with lifecycle_env('0'):
            for method, path in self._ROUTES:
                kwargs = {'json': {'reason': '测试原因', 'version_no': 'v1'}} \
                    if method == 'post' else {}
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 404, path)
                body = response.get_json()
                self.assertEqual(body.get('code'), 'policy_lifecycle_disabled', path)

    def test_suspend_missing_reason_400(self):
        with lifecycle_env('1'):
            response = self.client.post('/api/policies/S-ZB-001/suspend', json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['code'], 'invalid_reason')

    def test_suspend_short_reason_400(self):
        with lifecycle_env('1'):
            response = self.client.post('/api/policies/S-ZB-001/suspend',
                                        json={'reason': '短'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['code'], 'invalid_reason')

    def test_publish_missing_version_400(self):
        with lifecycle_env('1'):
            response = self.client.post('/api/policies/S-ZB-001/publish',
                                        json={'reason': '发布新版本'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['code'], 'missing_version')


if __name__ == '__main__':
    unittest.main()
