# -*- coding: utf-8 -*-
"""T22 政策批量导入 API 路由单测。

用 Flask test client + sqlite 临时文件库直接走 src/api.py 新增的导入路由；
不连生产库、不写生产数据。覆盖：开关默认关闭 404、预览只读与坏行、
multipart CSV、创建批次校验、提交幂等、报告、回滚、批次不存在 404、
可选备注写审计、异常 500 后 session 可继续使用。
"""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from flask import Flask, jsonify
from sqlalchemy import create_engine, text

from models import db
from policy_core import policy_import

POLICY_DDL = """
CREATE TABLE IF NOT EXISTS "policy" (
    "policy_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "policy_code" TEXT, "policy_name" TEXT, "level" TEXT,
    "category1" TEXT, "category2" TEXT, "category3" TEXT, "attr" TEXT,
    "regions" TEXT, "doc_ref" TEXT, "target" TEXT, "conditions" TEXT,
    "benefit" TEXT, "process" TEXT, "contact" TEXT, "effect_date" TEXT,
    "expire_date" TEXT, "status" TEXT, "cur_version" TEXT, "source" TEXT,
    "level_note" TEXT, "updated_at" TEXT, "updated_by" TEXT, "user_id" INTEGER
)"""

DETAIL_DDL = """
CREATE TABLE IF NOT EXISTS "policy_detail" (
    "detail_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "policy_id" INTEGER, "region" TEXT, "benefit" TEXT, "process" TEXT,
    "contact" TEXT, "phone" TEXT, "target" TEXT, "conditions" TEXT, "extra" TEXT
)"""

VERSION_DDL = """
CREATE TABLE IF NOT EXISTS "policy_version" (
    "version_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "policy_id" INTEGER, "version_no" TEXT, "content_snapshot" TEXT,
    "effect_date" TEXT, "expire_date" TEXT, "updated_by" TEXT,
    "updated_at" TEXT, "note" TEXT
)"""

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS "audit_log" (
    "log_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "action" TEXT, "target_code" TEXT, "target_name" TEXT,
    "detail" TEXT, "operator" TEXT, "created_at" TEXT
)"""

DDLS = (POLICY_DDL, DETAIL_DDL, VERSION_DDL, AUDIT_DDL)

IMPORT_ROUTES = (
    ('post', '/api/policies/import/preview', {'json': {'rows': [{'policy_code': 'X-ZB-001'}]}}),
    ('post', '/api/policies/import', {'json': {'rows': [{'policy_code': 'X-ZB-001'}]}}),
    ('post', '/api/policies/import/nope/commit', {}),
    ('post', '/api/policies/import/nope/rollback', {}),
    ('get', '/api/policies/import/nope', {}),
)


def _row(**overrides):
    row = {'policy_code': 'X-ZB-101', 'policy_name': '导入测试政策',
           'level': 'X', 'regions': '莲花县', 'target': '小微企业',
           'conditions': '在莲花县注册', 'benefit': '补贴 1 万元',
           'process': '线上申请', 'source': 'api-test'}
    row.update(overrides)
    return row


class _FakeUser:
    username = 'tester'
    role = 'admin'
    must_change_password = False
    real_name = '测试员'


@contextmanager
def import_env(value):
    """临时设置/清除 POLICY_IMPORT。value=None 表示不设置（走默认值 0）。"""
    saved = os.environ.get('POLICY_IMPORT')
    try:
        if value is None:
            os.environ.pop('POLICY_IMPORT', None)
        else:
            os.environ['POLICY_IMPORT'] = value
        yield
    finally:
        if saved is None:
            os.environ.pop('POLICY_IMPORT', None)
        else:
            os.environ['POLICY_IMPORT'] = saved


class ImportApiTestBase(unittest.TestCase):
    def setUp(self):
        import api as admin_api
        import authentication

        self.admin_api = admin_api
        self.tmpdir = tempfile.mkdtemp(prefix='import-api-test-')
        self.db_path = os.path.join(self.tmpdir, 'policy.db')
        self.engine = create_engine('sqlite:///' + self.db_path)
        with self.engine.begin() as conn:
            for ddl in DDLS:
                conn.execute(text(ddl))

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
                mock.patch.object(admin_api, '_login_user', lambda: _FakeUser())):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
        self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- 查询/断言辅助 ----
    def rows(self, sql, params=None):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(sql), params or {}).mappings()]

    def count(self, table):
        return self.rows('SELECT COUNT(*) AS c FROM "%s"' % table)[0]['c']

    def table_exists(self, table):
        return bool(self.rows(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = :name",
            {'name': table}))

    def body(self, resp):
        payload = resp.get_json()
        self.assertIsNotNone(payload, resp.get_data(as_text=True))
        return payload

    def create_batch(self, rows=None, **extra):
        payload = {'rows': rows if rows is not None else [_row()], 'source': 'api-test'}
        payload.update(extra)
        with import_env('1'):
            resp = self.client.post('/api/policies/import', json=payload)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return self.body(resp)['batch_id']


class SwitchTest(ImportApiTestBase):
    def test_all_routes_disabled_by_default(self):
        with import_env(None):
            self.assertFalse(policy_import.enabled())
            for method, path, kwargs in IMPORT_ROUTES:
                resp = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(resp.status_code, 404, path)
                self.assertEqual(self.body(resp)['code'], 'policy_import_disabled', path)

    def test_disabled_does_not_create_import_tables(self):
        with import_env('0'):
            for method, path, kwargs in IMPORT_ROUTES:
                getattr(self.client, method)(path, **kwargs)
        tables = self.rows("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name IN ('import_batch', 'import_row')")
        self.assertEqual(tables, [])

    def test_enabled_switch(self):
        with import_env('1'):
            self.assertTrue(policy_import.enabled())


class PreviewTest(ImportApiTestBase):
    def test_preview_json_is_read_only(self):
        rows = [_row(), _row(policy_code='')]
        with import_env('1'):
            resp = self.client.post('/api/policies/import/preview',
                                    json={'rows': rows, 'source': 'api-test'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = self.body(resp)
        self.assertTrue(body['ok'])
        self.assertEqual(body['mode'], 'dry_run')
        self.assertTrue(body['dry_run'])
        self.assertEqual(body['total'], 2)
        self.assertEqual(body['valid'], 1)
        self.assertEqual(body['invalid'], 1)
        self.assertEqual(body['actions']['create'], 1)
        # 预览绝不写库：既不建批次表，也没有业务数据。
        self.assertFalse(self.table_exists('import_batch'))
        self.assertFalse(self.table_exists('import_row'))
        self.assertEqual(self.count('policy'), 0)

    def test_preview_accepts_raw_list(self):
        with import_env('1'):
            resp = self.client.post('/api/policies/import/preview', json=[_row()])
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = self.body(resp)
        self.assertEqual(body['valid'], 1)
        self.assertEqual(body['actions']['create'], 1)

    def test_preview_bad_json_400(self):
        with import_env('1'):
            empty = self.client.post('/api/policies/import/preview', json={'rows': []})
            not_list = self.client.post('/api/policies/import/preview', json={'rows': 'x'})
            no_body = self.client.post('/api/policies/import/preview', data='oops',
                                       content_type='text/plain')
        for resp in (empty, not_list, no_body):
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(self.body(resp)['code'], 'invalid_rows')

    def test_preview_csv_upload(self):
        csv_text = ('政策编号,政策名称,层级,适用地区,目标,条件,补贴标准\n'
                    'X-ZB-201,上传政策,X,莲花县,小微企业,在莲花县注册,补贴 1 万元\n')
        with import_env('1'):
            resp = self.client.post(
                '/api/policies/import/preview',
                data={'file': (io.BytesIO(csv_text.encode('utf-8')), 'rows.csv')},
                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = self.body(resp)
        self.assertEqual(body['valid'], 1)
        self.assertEqual(body['file_name'], 'rows.csv')
        self.assertFalse(self.table_exists('import_batch'))

    def test_preview_rejects_unknown_file_type(self):
        with import_env('1'):
            resp = self.client.post(
                '/api/policies/import/preview',
                data={'file': (io.BytesIO(b'x'), 'rows.txt')},
                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.body(resp)['code'], 'invalid_file_type')


class CreateTest(ImportApiTestBase):
    def test_create_batch_validates_and_stages(self):
        with import_env('1'):
            resp = self.client.post('/api/policies/import',
                                    json={'rows': [_row()], 'source': 'api-test',
                                          'reason': '首次导入'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = self.body(resp)
        self.assertTrue(body['ok'])
        self.assertTrue(body['batch_id'])
        self.assertEqual(body['status'], 'validated')
        self.assertEqual(body['valid'], 1)
        self.assertTrue(body['note_audited'])
        batches = self.rows('SELECT * FROM "import_batch"')
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['status'], 'validated')
        self.assertEqual(batches[0]['operator'], 'tester')
        # 创建批次只写 staging，不写政策业务表。
        self.assertEqual(self.count('policy'), 0)
        audits = self.rows('SELECT "action","operator","detail" FROM "audit_log"')
        self.assertEqual([item['action'] for item in audits], ['导入操作备注'])
        self.assertEqual(audits[0]['operator'], 'tester')
        self.assertEqual(audits[0]['detail'], '首次导入')

    def test_create_without_reason_skips_note_audit(self):
        with import_env('1'):
            resp = self.client.post('/api/policies/import', json={'rows': [_row()]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertFalse(self.body(resp)['note_audited'])
        self.assertEqual(self.rows('SELECT * FROM "audit_log"'), [])

    def test_create_missing_operator_400(self):
        empty_user = type('EmptyUser', (), {'username': '', 'role': 'admin'})()
        with import_env('1'), mock.patch.object(self.admin_api, '_login_user',
                                                lambda: empty_user):
            resp = self.client.post('/api/policies/import', json={'rows': [_row()]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.body(resp)['code'], 'missing_operator')
        self.assertFalse(self.table_exists('import_batch'))


class CommitReportRollbackTest(ImportApiTestBase):
    def test_full_chain_idempotent_report_rollback(self):
        batch_id = self.create_batch()

        with import_env('1'):
            first = self.client.post('/api/policies/import/%s/commit' % batch_id,
                                     json={'reason': '批量导入测试'})
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        body = self.body(first)
        self.assertTrue(body['ok'])
        self.assertEqual(body['status'], 'committed')
        self.assertEqual(body['changed'], 1)
        self.assertEqual(body['created'], 1)
        self.assertFalse(body['idempotent'])
        self.assertTrue(body['note_audited'])
        policies = self.rows('SELECT "policy_code","status","cur_version" FROM "policy"')
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0]['policy_code'], 'X-ZB-101')
        self.assertEqual(policies[0]['status'], '在用')
        self.assertEqual(policies[0]['cur_version'], 'v1')
        self.assertEqual(self.count('policy_detail'), 1)
        self.assertEqual(self.count('policy_version'), 1)
        self.assertEqual(self.count('outbox'), 3)
        audit_actions = [item['action'] for item in
                         self.rows('SELECT "action" FROM "audit_log" ORDER BY "log_id"')]
        self.assertIn('批量导入', audit_actions)
        self.assertIn('导入操作备注', audit_actions)

        # 幂等：二次提交 changed=0，不重复写政策/版本/事件。
        with import_env('1'):
            second = self.client.post('/api/policies/import/%s/commit' % batch_id, json={})
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        again = self.body(second)
        self.assertEqual(again['changed'], 0)
        self.assertEqual(again['created'], 0)
        self.assertTrue(again['idempotent'])
        self.assertEqual(self.count('policy'), 1)
        self.assertEqual(self.count('policy_version'), 1)
        self.assertEqual(self.count('outbox'), 3)

        # 批次报告
        with import_env('1'):
            report_resp = self.client.get('/api/policies/import/%s' % batch_id)
        self.assertEqual(report_resp.status_code, 200, report_resp.get_data(as_text=True))
        report = self.body(report_resp)
        self.assertTrue(report['ok'])
        self.assertEqual(report['batch_id'], batch_id)
        self.assertEqual(report['status'], 'committed')
        self.assertEqual(report['actions']['create'], 1)
        self.assertEqual(report['file_name'], '')

        # 回滚
        with import_env('1'):
            rollback_resp = self.client.post(
                '/api/policies/import/%s/rollback' % batch_id, json={})
        self.assertEqual(rollback_resp.status_code, 200,
                         rollback_resp.get_data(as_text=True))
        rolled = self.body(rollback_resp)
        self.assertEqual(rolled['status'], 'rolled_back')
        self.assertEqual(rolled['removed'], 1)
        self.assertEqual(rolled['restored'], 0)
        self.assertEqual(self.count('policy'), 0)
        self.assertEqual(self.count('policy_detail'), 0)
        self.assertEqual(self.count('policy_version'), 0)
        self.assertEqual(self.count('policy_scope'), 0)
        with import_env('1'):
            after = self.client.get('/api/policies/import/%s' % batch_id)
        self.assertEqual(self.body(after)['status'], 'rolled_back')

    def test_update_existing_policy_writes_version(self):
        with self.engine.begin() as conn:
            conn.execute(text(
                'INSERT INTO "policy" ("policy_code","policy_name","level","regions",'
                '"conditions","target","benefit","process","status","cur_version",'
                '"source","updated_at","updated_by") VALUES '
                '(:code,:name,:level,:regions,:conditions,:target,:benefit,:process,'
                ':status,:cur_version,:source,:now,:operator)'),
                {'code': 'X-ZB-101', 'name': '旧名称', 'level': 'X',
                 'regions': '莲花县', 'conditions': '在莲花县注册', 'target': '小微企业',
                 'benefit': '补贴 1 万元', 'process': '线上申请', 'status': '在用',
                 'cur_version': 'v1', 'source': 'api-test', 'now': '2026-01-01 00:00:00',
                 'operator': 'seed'})
            policy_id = conn.execute(text(
                'SELECT "policy_id" FROM "policy" WHERE "policy_code" = :code'),
                {'code': 'X-ZB-101'}).scalar()
            conn.execute(text(
                'INSERT INTO "policy_version" ("policy_id","version_no",'
                '"content_snapshot","note") VALUES (:pid,:version,:snapshot,:note)'),
                {'pid': policy_id, 'version': 'v1', 'snapshot': '{}', 'note': '初始'})

        batch_id = self.create_batch([_row(policy_name='新名称')])
        with import_env('1'):
            resp = self.client.post('/api/policies/import/%s/commit' % batch_id, json={})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = self.body(resp)
        self.assertEqual(body['changed'], 1)
        self.assertEqual(body['updated'], 1)
        policy = self.rows('SELECT "policy_name","cur_version" FROM "policy"')[0]
        self.assertEqual(policy['policy_name'], '新名称')
        self.assertEqual(policy['cur_version'], 'v2')
        self.assertEqual(self.count('policy_version'), 2)

    def test_commit_dry_run_batch_rejected(self):
        with import_env('1'):
            created = self.client.post('/api/policies/import',
                                       json={'rows': [_row()], 'dry_run': True})
        batch_id = self.body(created)['batch_id']
        with import_env('1'):
            resp = self.client.post('/api/policies/import/%s/commit' % batch_id, json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.body(resp)['code'], 'import_invalid')
        self.assertEqual(self.count('policy'), 0)

    def test_unknown_batch_404(self):
        with import_env('1'):
            commit_resp = self.client.post('/api/policies/import/nope/commit', json={})
            rollback_resp = self.client.post('/api/policies/import/nope/rollback', json={})
            report_resp = self.client.get('/api/policies/import/nope')
        for resp in (commit_resp, rollback_resp, report_resp):
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(self.body(resp)['code'], 'batch_not_found')

    def test_unexpected_error_rolls_back_and_session_recovers(self):
        batch_id = self.create_batch()
        with import_env('1'), mock.patch.object(policy_import, 'commit',
                                                side_effect=RuntimeError('boom')):
            resp = self.client.post('/api/policies/import/%s/commit' % batch_id, json={})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.body(resp)['code'], 'import_failed')
        self.assertEqual(self.count('policy'), 0)
        # session 已回滚：后续报告请求还能正常读到校验后的批次。
        with import_env('1'):
            report_resp = self.client.get('/api/policies/import/%s' % batch_id)
        self.assertEqual(report_resp.status_code, 200,
                         report_resp.get_data(as_text=True))
        self.assertEqual(self.body(report_resp)['status'], 'validated')


if __name__ == '__main__':
    unittest.main()
