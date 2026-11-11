# -*- coding: utf-8 -*-
"""T32 政策生命周期管理 API 端到端测试（Flask test client + sqlite 临时文件库）。

T18 只有 lifecycle 模块级单测，API 层仅覆盖"开关关闭/缺参数"两条路径。这里把
8 条生命周期路由真实走通到临时 sqlite 库，验证：
- POLICY_LIFECYCLE 未设或 0 时 8 条路由统一 404（现网零行为变化）；
- =1 时 impact/versions 的返回结构与计数；
- 停用/归档/恢复状态机往返、dry_run 不写业务行；
- 软删保留数据、恢复已删除政策需 confirm 二次确认、硬删边界；
- 发布/回滚切换 cur_version、draft 首次发布即激活；
- reason/operator 校验、未知 code 404。

约束：不连生产库、不写生产数据。登录态（authentication.require_user /
api._login_user）与 auth 库连接（api._policy_lifecycle_auth_conn）全部用替身注入，
请求只读写 tempfile 下的 sqlite 文件；每个用例独立建库、结束即删。
"""
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from flask import Flask, jsonify
from sqlalchemy import create_engine, text

from models import db
from policy_core import lifecycle, outbox

# ---- 与生产同名的核心表（只建生命周期用到的列，够 _policy/_audit/impact 读取） ----
POLICY_DDL = """
CREATE TABLE "policy" (
    "policy_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "policy_code" TEXT, "policy_name" TEXT, "level" TEXT,
    "category1" TEXT, "category2" TEXT, "category3" TEXT, "attr" TEXT,
    "regions" TEXT, "doc_ref" TEXT, "target" TEXT, "conditions" TEXT,
    "benefit" TEXT, "process" TEXT, "contact" TEXT, "effect_date" TEXT,
    "expire_date" TEXT, "status" TEXT, "cur_version" TEXT, "source" TEXT,
    "level_note" TEXT, "updated_at" TEXT, "updated_by" TEXT, "user_id" INTEGER
)"""

DETAIL_DDL = """
CREATE TABLE "policy_detail" (
    "detail_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "policy_id" INTEGER, "region" TEXT, "benefit" TEXT, "process" TEXT,
    "contact" TEXT, "phone" TEXT, "target" TEXT, "conditions" TEXT, "extra" TEXT
)"""

VERSION_DDL = """
CREATE TABLE "policy_version" (
    "version_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "policy_id" INTEGER, "version_no" TEXT, "content_snapshot" TEXT,
    "effect_date" TEXT, "expire_date" TEXT, "updated_by" TEXT,
    "updated_at" TEXT, "note" TEXT
)"""

AUDIT_DDL = """
CREATE TABLE "audit_log" (
    "log_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "action" TEXT, "target_code" TEXT, "target_name" TEXT,
    "detail" TEXT, "operator" TEXT, "created_at" TEXT
)"""

QA_LOG_DDL = """
CREATE TABLE "qa_log" (
    "qa_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "matched_policies" TEXT, "match_json" TEXT
)"""

RULE_DDL = """
CREATE TABLE "policy_rule" (
    "rule_key" TEXT PRIMARY KEY, "policy_code" TEXT, "status" TEXT
)"""

SCOPE_DDL = """
CREATE TABLE "policy_scope" (
    "policy_code" TEXT, "detail_id" INTEGER, "region_key" TEXT
)"""

VECTOR_DDL = """
CREATE TABLE "policy_vector" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT, "policy_code" TEXT, "status" TEXT
)"""

DDL_LIST = (POLICY_DDL, DETAIL_DDL, VERSION_DDL, AUDIT_DDL, QA_LOG_DDL,
            RULE_DDL, SCOPE_DDL, VECTOR_DDL)

# 种子数据：覆盖 active/suspended/draft/archived/deleted 五种状态与版本链
_ACTIVE = 'S-ZB-001'          # 在用，cur_version=v2，有 v1/v2 两版
_SUSPENDED = 'S-ZB-002'       # 停用
_DRAFT = 'D-ZB-003'           # 草稿，已有 v1（不可硬删）
_ARCHIVED = 'S-ZB-004'        # 归档
_DELETED = 'S-ZB-005'         # 已软删（deleted_at 有值），用于恢复二次确认
_DRAFT_PUBLISH = 'D-ZB-006'   # 草稿，cur_version 为空，有 v1：用于首次发布激活
_DRAFT_BARE = 'D-ZB-007'      # 草稿，无版本、无问答引用：用于硬删成功
_MISSING = 'NOT-EXIST-ZZZ'

_POLICIES = [
    dict(id=1, code=_ACTIVE, name='一号政策', status='在用', version='v2', deleted=None),
    dict(id=2, code=_SUSPENDED, name='二号政策', status='停用', version='v1', deleted=None),
    dict(id=3, code=_DRAFT, name='三号草稿', status='draft', version='v1', deleted=None),
    dict(id=4, code=_ARCHIVED, name='四号归档', status='archived', version='v1', deleted=None),
    dict(id=5, code=_DELETED, name='五号已删', status='deleted', version='v1',
         deleted='2026-01-01 00:00:00'),
    dict(id=6, code=_DRAFT_PUBLISH, name='六号草稿', status='draft', version='', deleted=None),
    dict(id=7, code=_DRAFT_BARE, name='七号草稿', status='draft', version='', deleted=None),
]

_ROUTES = (
    ('post', '/api/policies/S-ZB-001/suspend', {'json': {'reason': '测试原因', 'version_no': 'v1'}}),
    ('post', '/api/policies/S-ZB-001/archive', {'json': {'reason': '测试原因', 'version_no': 'v1'}}),
    ('post', '/api/policies/S-ZB-001/restore', {'json': {'reason': '测试原因', 'version_no': 'v1'}}),
    ('post', '/api/policies/S-ZB-001/delete', {'json': {'reason': '测试原因', 'version_no': 'v1'}}),
    ('post', '/api/policies/S-ZB-001/publish', {'json': {'reason': '测试原因', 'version_no': 'v1'}}),
    ('post', '/api/policies/S-ZB-001/rollback', {'json': {'reason': '测试原因', 'version_no': 'v1'}}),
    ('get', '/api/policies/S-ZB-001/impact', {}),
    ('get', '/api/policies/S-ZB-001/versions', {}),
)


class _FakeUser:
    username = 'tester'
    role = 'admin'
    must_change_password = False
    real_name = '测试员'


@contextmanager
def lifecycle_env(value):
    """临时设置/清除 POLICY_LIFECYCLE。value=None 表示不设置（走默认值 0）。"""
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


class PolicyLifecycleApiTestBase(unittest.TestCase):
    """每个用例一套 Flask app + 独立 sqlite 文件库；请求只命中临时库。"""

    def setUp(self):
        import api as admin_api
        import authentication

        self.admin_api = admin_api
        # 环境默认关闭：开关相关用例再用 lifecycle_env 显式切到 1，避免互相污染
        self._saved_lifecycle = os.environ.get('POLICY_LIFECYCLE')
        os.environ.pop('POLICY_LIFECYCLE', None)
        self.addCleanup(self._restore_lifecycle_env)

        self.tmpdir = tempfile.mkdtemp(prefix='api-lifecycle-test-')
        self.db_path = os.path.join(self.tmpdir, 'policy.db')
        self.engine = create_engine('sqlite:///' + self.db_path)
        with self.engine.begin() as conn:
            for ddl in DDL_LIST:
                conn.execute(text(ddl))
            # 模拟"迁移后"的生产库：outbox 表 + policy.deleted_at 已就绪。
            # 这样 dry_run 用例能严格断言"业务行零写入"，不被建表 DDL 干扰。
            lifecycle.ensure_schema(conn)
        self._seed()

        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY='test', TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite:///' + self.db_path,
            SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(admin_api.bp)

        @self.app.errorhandler(authentication.ApiError)
        def _api_error(exc):
            return jsonify(ok=False, msg=exc.message, code=exc.code), exc.status

        self.client = self.app.test_client()
        for patch in (
                mock.patch.object(authentication, 'require_user', lambda *a, **k: _FakeUser()),
                mock.patch.object(admin_api, '_login_user', lambda: _FakeUser()),
                # 强制不碰 auth 库（生产 qa_log 在另一库）；impact 按"不可确认"处理
                mock.patch.object(admin_api, '_policy_lifecycle_auth_conn', lambda: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def _restore_lifecycle_env(self):
        if self._saved_lifecycle is None:
            os.environ.pop('POLICY_LIFECYCLE', None)
        else:
            os.environ['POLICY_LIFECYCLE'] = self._saved_lifecycle

    def _seed(self):
        with self.engine.begin() as conn:
            conn.execute(
                text('INSERT INTO policy (policy_id, policy_code, policy_name, status, '
                     'cur_version, updated_at, updated_by, deleted_at) VALUES '
                     '(:id, :code, :name, :status, :version, :updated_at, :operator, :deleted)'),
                [dict(item, updated_at='2026-11-01 00:00:00', operator='seed')
                 for item in _POLICIES])
            conn.execute(text('INSERT INTO policy_version (version_id, policy_id, version_no, '
                              'note, updated_by, updated_at) VALUES (:id, :pid, :version, '
                              ':note, :operator, :at)'),
                         [dict(id=row['id'], pid=row['pid'], version=row['version'],
                               note=row['note'], operator='seed', at='2026-11-01 00:00:00')
                          for row in (
                              dict(id=1, pid=1, version='v1', note='新增'),
                              dict(id=2, pid=1, version='v2', note='更新'),
                              dict(id=3, pid=3, version='v1', note='新增'),
                              dict(id=4, pid=6, version='v1', note='新增'))])
            conn.execute(text('INSERT INTO policy_detail (detail_id, policy_id, region) '
                              'VALUES (:id, :pid, :region)'),
                         [dict(id=1, pid=1, region='莲花县'),
                          dict(id=2, pid=1, region='南昌市'),
                          dict(id=3, pid=3, region='莲花县'),
                          dict(id=4, pid=7, region='莲花县')])
            conn.execute(text('INSERT INTO qa_log (qa_id, matched_policies, match_json) '
                              'VALUES (:id, :matched, :json)'),
                         [dict(id=1, matched='["S-ZB-001"]', json='{}'),
                          dict(id=2, matched='["D-ZB-003"]', json='{}')])
            conn.execute(text('INSERT INTO policy_rule (rule_key, policy_code, status) '
                              'VALUES (:key, :code, :status)'),
                         [dict(key='r1', code=_ACTIVE, status='complete'),
                          dict(key='r2', code=_ACTIVE, status='partial'),
                          dict(key='r3', code=_ACTIVE, status='failed'),
                          dict(key='r4', code=_DRAFT, status='partial')])
            conn.execute(text('INSERT INTO policy_scope (policy_code, detail_id, region_key) '
                              'VALUES (:code, :detail, :key)'),
                         [dict(code=_ACTIVE, detail=1, key='jx-pingxiang-lianhua'),
                          dict(code=_ACTIVE, detail=2, key='jx-nanchang'),
                          dict(code=_ACTIVE, detail=2, key='jx-nanchang-xinjian'),
                          dict(code=_DRAFT, detail=3, key='jx-pingxiang-lianhua')])
            conn.execute(text('INSERT INTO policy_vector (id, policy_code, status) '
                              'VALUES (:id, :code, :status)'),
                         [dict(id=1, code=_ACTIVE, status='在用'),
                          dict(id=2, code=_ACTIVE, status='在用'),
                          dict(id=3, code=_DRAFT, status='在用')])

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
        self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- 查询/断言辅助（独立 engine 直读，验证请求提交后的真实落库结果） ----
    def rows(self, sql, params=None):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(sql), params or {}).mappings()]

    def count(self, table):
        return self.rows('SELECT COUNT(*) AS c FROM "%s"' % table)[0]['c']

    def policy_row(self, code):
        found = self.rows('SELECT * FROM policy WHERE policy_code=:code', {'code': code})
        return found[0] if found else None

    def audit_rows(self):
        return self.rows('SELECT * FROM audit_log ORDER BY log_id')

    def outbox_rows(self):
        return self.rows('SELECT * FROM outbox ORDER BY id')

    def body(self, resp):
        payload = resp.get_json()
        self.assertIsNotNone(payload, resp.get_data(as_text=True))
        return payload

    def post(self, code, action, **payload):
        return self.client.post('/api/policies/%s/%s' % (code, action), json=payload)

    def assert_ok(self, resp, expected_status=200):
        self.assertEqual(resp.status_code, expected_status, resp.get_data(as_text=True))
        payload = self.body(resp)
        self.assertTrue(payload.get('ok'), payload)
        return payload

    def assert_error(self, resp, expected_status, expected_code):
        self.assertEqual(resp.status_code, expected_status, resp.get_data(as_text=True))
        payload = self.body(resp)
        self.assertFalse(payload.get('ok'), payload)
        self.assertEqual(payload.get('code'), expected_code, payload)
        return payload

    def assert_all_routes_disabled(self):
        for method, path, kwargs in _ROUTES:
            resp = getattr(self.client, method)(path, **kwargs)
            self.assertEqual(resp.status_code, 404, path)
            payload = self.body(resp)
            self.assertFalse(payload.get('ok'), path)
            self.assertEqual(payload.get('code'), 'policy_lifecycle_disabled', path)


class SwitchTest(PolicyLifecycleApiTestBase):
    def test_all_routes_disabled_by_default(self):
        """未设置开关（默认 0）时 8 条路由统一 404，不读写业务表。"""
        with lifecycle_env(None):
            self.assertFalse(lifecycle.enabled())
            self.assert_all_routes_disabled()
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.outbox_rows(), [])

    def test_all_routes_disabled_when_zero_and_policy_unchanged(self):
        before = self.policy_row(_ACTIVE)
        with lifecycle_env('0'):
            self.assertFalse(lifecycle.enabled())
            self.assert_all_routes_disabled()
        self.assertEqual(self.policy_row(_ACTIVE), before)
        self.assertEqual(self.count('audit_log'), 0)
        self.assertEqual(self.count('outbox'), 0)


class ReadRoutesTest(PolicyLifecycleApiTestBase):
    def test_impact_returns_structure_and_counts(self):
        with lifecycle_env('1'):
            resp = self.client.get('/api/policies/%s/impact' % _ACTIVE)
        payload = self.assert_ok(resp)
        self.assertEqual(payload['policy_code'], _ACTIVE)
        self.assertEqual(payload['policy_name'], '一号政策')
        self.assertEqual(payload['state'], 'active')
        self.assertEqual(payload['status'], '在用')
        self.assertEqual(payload['cur_version'], 'v2')
        self.assertEqual(payload['details'], 2)       # policy_detail 2 行
        self.assertEqual(payload['scope_rows'], 3)    # policy_scope 3 行
        self.assertEqual(payload['rules'], 2)         # 3 条规则里 failed 不算
        self.assertEqual(payload['versions'], 2)      # v1/v2
        self.assertEqual(payload['vectors'], 2)
        self.assertEqual(payload['vector_table'], 'policy_vector')
        self.assertEqual(payload['qa_log'], 1)        # 历史问答引用 1 条
        self.assertTrue(payload['qa_log_available'])

    def test_versions_returns_current_first(self):
        with lifecycle_env('1'):
            resp = self.client.get('/api/policies/%s/versions' % _ACTIVE)
        payload = self.assert_ok(resp)
        self.assertEqual(payload['policy_code'], _ACTIVE)
        items = payload['items']
        self.assertEqual([item['version_no'] for item in items], ['v2', 'v1'])
        self.assertTrue(items[0]['is_current'])
        self.assertFalse(items[1]['is_current'])
        self.assertEqual(sum(1 for item in items if item['is_current']), 1)

    def test_read_routes_unknown_code_404(self):
        with lifecycle_env('1'):
            for path in ('impact', 'versions'):
                resp = self.client.get('/api/policies/%s/%s' % (_MISSING, path))
                self.assert_error(resp, 404, 'policy_not_found')


class SuspendArchiveRestoreTest(PolicyLifecycleApiTestBase):
    def test_suspend_defaults_operator_to_login_user(self):
        with lifecycle_env('1'):
            resp = self.post(_ACTIVE, 'suspend', reason='政策依据变更')
        payload = self.assert_ok(resp)
        self.assertEqual(payload['action'], 'suspend')
        self.assertEqual(payload['state_before'], 'active')
        self.assertEqual(payload['state_after'], 'suspended')
        self.assertEqual(payload['status_before'], '在用')
        self.assertEqual(payload['status_after'], '停用')
        self.assertTrue(payload['audit_logged'])
        self.assertEqual(payload['outbox_events'], 4)
        row = self.policy_row(_ACTIVE)
        self.assertEqual(row['status'], '停用')
        self.assertEqual(row['updated_by'], 'tester')  # operator 缺省取登录用户
        audits = self.audit_rows()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['action'], '暂停政策')
        self.assertEqual(audits[0]['operator'], 'tester')
        events = self.outbox_rows()
        self.assertEqual(len(events), 4)
        self.assertEqual({item['event_type'] for item in events}, set(outbox.EVENT_TYPES))
        for item in events:
            parsed = json.loads(item['payload'])
            self.assertEqual(parsed['policy_code'], _ACTIVE)
            self.assertEqual(parsed['action'], 'suspend')

    def test_suspend_operator_override(self):
        with lifecycle_env('1'):
            resp = self.post(_ACTIVE, 'suspend', reason='政策依据变更', operator='ops-01')
        self.assert_ok(resp)
        self.assertEqual(self.audit_rows()[0]['operator'], 'ops-01')
        self.assertEqual(self.policy_row(_ACTIVE)['updated_by'], 'ops-01')

    def test_reason_required_and_min_length(self):
        with lifecycle_env('1'):
            for action in ('suspend', 'archive', 'delete'):
                self.assert_error(self.post(_ACTIVE, action), 400, 'invalid_reason')
                self.assert_error(self.post(_ACTIVE, action, reason='短'), 400, 'invalid_reason')

    def test_dry_run_writes_no_business_rows(self):
        before = self.policy_row(_ACTIVE)
        audit_before, outbox_before = self.count('audit_log'), self.count('outbox')
        with lifecycle_env('1'):
            resp = self.post(_ACTIVE, 'suspend', reason='预演停用', dry_run=True)
        payload = self.assert_ok(resp)
        self.assertTrue(payload['dry_run'])
        self.assertEqual(payload['state_after'], 'suspended')
        self.assertFalse(payload['audit_logged'])
        self.assertEqual(payload['outbox_events'], 0)
        # 业务行零写入：status/updated_at/updated_by/deleted_at 全部保持原值
        self.assertEqual(self.policy_row(_ACTIVE), before)
        self.assertEqual(self.count('audit_log'), audit_before)
        self.assertEqual(self.count('outbox'), outbox_before)

    def test_state_machine_roundtrip(self):
        with lifecycle_env('1'):
            r1 = self.post(_ACTIVE, 'suspend', reason='暂停政策')
            self.assertEqual(self.assert_ok(r1)['state_after'], 'suspended')
            self.assertEqual(self.policy_row(_ACTIVE)['status'], '停用')

            r2 = self.post(_ACTIVE, 'restore', reason='恢复政策')
            payload = self.assert_ok(r2)
            self.assertEqual(payload['state_before'], 'suspended')
            self.assertEqual(payload['state_after'], 'active')
            self.assertEqual(self.policy_row(_ACTIVE)['status'], '在用')

            r3 = self.post(_ACTIVE, 'archive', reason='归档政策')
            self.assertEqual(self.assert_ok(r3)['state_after'], 'archived')
            self.assertEqual(self.policy_row(_ACTIVE)['status'], 'archived')

            r4 = self.post(_ACTIVE, 'restore', reason='再次恢复')
            self.assertEqual(self.assert_ok(r4)['state_after'], 'active')
            self.assertEqual(self.policy_row(_ACTIVE)['status'], '在用')
        self.assertEqual([row['action'] for row in self.audit_rows()],
                         ['暂停政策', '恢复政策', '归档政策', '恢复政策'])
        self.assertEqual(self.count('outbox'), 16)

    def test_invalid_transitions_409(self):
        with lifecycle_env('1'):
            self.assert_error(self.post(_SUSPENDED, 'suspend', reason='重复停用'),
                              409, 'invalid_transition')
            self.assert_error(self.post(_ACTIVE, 'restore', reason='无需恢复'),
                              409, 'invalid_transition')


class DeleteRestoreTest(PolicyLifecycleApiTestBase):
    def test_soft_delete_keeps_data(self):
        detail_before = self.rows(
            'SELECT * FROM policy_detail WHERE policy_id=1')
        scope_before = self.rows(
            'SELECT * FROM policy_scope WHERE policy_code=:code', {'code': _ACTIVE})
        with lifecycle_env('1'):
            resp = self.post(_ACTIVE, 'delete', reason='政策已废止')
        payload = self.assert_ok(resp)
        self.assertEqual(payload['action'], 'delete')
        self.assertEqual(payload['state_after'], 'deleted')
        self.assertEqual(payload['status_after'], 'deleted')
        self.assertEqual(payload['impact']['details'], 2)
        row = self.policy_row(_ACTIVE)      # 软删：主表行保留，仅标记
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'deleted')
        self.assertIsNotNone(row['deleted_at'])
        self.assertEqual(self.rows('SELECT * FROM policy_detail WHERE policy_id=1'),
                         detail_before)
        self.assertEqual(self.rows('SELECT * FROM policy_scope WHERE policy_code=:code',
                                   {'code': _ACTIVE}), scope_before)
        self.assertEqual(self.audit_rows()[0]['action'], '删除政策')
        self.assertEqual(self.count('outbox'), 4)

    def test_restore_deleted_requires_confirm(self):
        with lifecycle_env('1'):
            resp = self.post(_DELETED, 'restore', reason='恢复旧政策')
            payload = self.assert_error(resp, 409, 'confirmation_required')
            self.assertIn('confirm', payload['msg'])
            self.assertEqual(self.policy_row(_DELETED)['status'], 'deleted')

            resp = self.post(_DELETED, 'restore', reason='恢复旧政策', confirm=True)
            payload = self.assert_ok(resp)
        self.assertEqual(payload['state_before'], 'deleted')
        self.assertEqual(payload['state_after'], 'active')
        self.assertEqual(payload['status_after'], '在用')
        row = self.policy_row(_DELETED)
        self.assertEqual(row['status'], '在用')
        self.assertIsNone(row['deleted_at'])   # 恢复清空软删时间

    def test_delete_already_deleted_409(self):
        with lifecycle_env('1'):
            self.assert_error(self.post(_DELETED, 'delete', reason='重复删除'),
                              409, 'invalid_transition')

    def test_hard_delete_draft_without_versions(self):
        with lifecycle_env('1'):
            resp = self.post(_DRAFT_BARE, 'delete', reason='清理无效草稿', hard=True)
        payload = self.assert_ok(resp)
        self.assertEqual(payload['action'], 'hard_delete')
        self.assertEqual(payload['deleted_details'], 1)
        self.assertIsNone(self.policy_row(_DRAFT_BARE))   # 主表行已物理删除
        self.assertEqual(self.rows('SELECT * FROM policy_detail WHERE policy_id=7'), [])
        self.assertEqual(self.audit_rows()[0]['action'], '硬删除政策')
        self.assertEqual(self.count('outbox'), 4)

    def test_hard_delete_rejections(self):
        with lifecycle_env('1'):
            self.assert_error(self.post(_ACTIVE, 'delete', reason='清理政策', hard=True),
                              409, 'hard_delete_requires_draft')
            # 草稿但已有版本记录：拒绝硬删，保留数据
            self.assert_error(self.post(_DRAFT, 'delete', reason='清理草稿', hard=True),
                              409, 'hard_delete_has_versions')
        self.assertIsNotNone(self.policy_row(_DRAFT))


class PublishRollbackTest(PolicyLifecycleApiTestBase):
    def test_publish_switches_current_version(self):
        with lifecycle_env('1'):
            resp = self.post(_ACTIVE, 'publish', reason='发布新版本', version_no='v1')
        payload = self.assert_ok(resp)
        self.assertEqual(payload['action'], 'publish')
        self.assertEqual(payload['state_before'], 'active')
        self.assertEqual(payload['state_after'], 'active')
        self.assertEqual(payload['cur_version_before'], 'v2')
        self.assertEqual(payload['cur_version_after'], 'v1')
        self.assertEqual(self.policy_row(_ACTIVE)['cur_version'], 'v1')
        self.assertEqual(self.audit_rows()[0]['action'], '发布版本')

    def test_rollback_switches_current_version(self):
        with lifecycle_env('1'):
            resp = self.post(_ACTIVE, 'rollback', reason='回退版本', version_no='v1')
        payload = self.assert_ok(resp)
        self.assertEqual(payload['action'], 'rollback')
        self.assertEqual(payload['cur_version_before'], 'v2')
        self.assertEqual(payload['cur_version_after'], 'v1')
        self.assertEqual(self.policy_row(_ACTIVE)['cur_version'], 'v1')
        events = [json.loads(item['payload']) for item in self.outbox_rows()]
        self.assertTrue(all(item.get('version') == 'v1' for item in events))

    def test_same_version_409(self):
        with lifecycle_env('1'):
            self.assert_error(self.post(_ACTIVE, 'publish', reason='发布新版本',
                                        version_no='v2'), 409, 'version_already_current')
            self.assert_error(self.post(_ACTIVE, 'rollback', reason='回退版本',
                                        version_no='v2'), 409, 'version_already_current')
        self.assertEqual(self.policy_row(_ACTIVE)['cur_version'], 'v2')

    def test_unknown_version_404(self):
        """实现口径：版本行不存在 → 404 version_not_found（任务描述写 409，已报告差异）。"""
        with lifecycle_env('1'):
            for action in ('publish', 'rollback'):
                payload = self.assert_error(
                    self.post(_ACTIVE, action, reason='发布新版本', version_no='v9'),
                    404, 'version_not_found')
                self.assertIn('v9', payload['msg'])
        self.assertEqual(self.policy_row(_ACTIVE)['cur_version'], 'v2')

    def test_draft_first_publish_activates(self):
        with lifecycle_env('1'):
            resp = self.post(_DRAFT_PUBLISH, 'publish', reason='首次发布', version_no='v1')
        payload = self.assert_ok(resp)
        self.assertEqual(payload['state_before'], 'draft')
        self.assertEqual(payload['state_after'], 'active')
        self.assertEqual(payload['status_after'], '在用')
        row = self.policy_row(_DRAFT_PUBLISH)
        self.assertEqual(row['status'], '在用')
        self.assertEqual(row['cur_version'], 'v1')

    def test_missing_version_no_400(self):
        with lifecycle_env('1'):
            for action in ('publish', 'rollback'):
                self.assert_error(self.post(_ACTIVE, action, reason='缺少版本号'),
                                  400, 'missing_version')


class MiscApiTest(PolicyLifecycleApiTestBase):
    def test_write_route_unknown_code_404(self):
        with lifecycle_env('1'):
            payload = self.assert_error(
                self.post(_MISSING, 'suspend', reason='停用未知政策'),
                404, 'policy_not_found')
        self.assertIn(_MISSING, payload['msg'])
        self.assertEqual(self.count('audit_log'), 0)
        self.assertEqual(self.count('outbox'), 0)

    def test_impact_on_suspended_policy_reports_state(self):
        with lifecycle_env('1'):
            resp = self.client.get('/api/policies/%s/impact' % _SUSPENDED)
        payload = self.assert_ok(resp)
        self.assertEqual(payload['state'], 'suspended')
        self.assertEqual(payload['status'], '停用')
        self.assertEqual(payload['details'], 0)
        self.assertEqual(payload['rules'], 0)


if __name__ == '__main__':
    unittest.main()
