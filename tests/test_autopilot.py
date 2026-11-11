# -*- coding: utf-8 -*-
"""政策自动管理（policy autopilot）单测：sqlite 内存库 + CLI 冒烟。

覆盖：
- expire_scan / outbox_drain / health_check / coverage_report /
  rule_requeue / stale_draft 六项任务：空库、有数据、dry-run/apply 差异、幂等；
- env 开关（POLICY_AUTOPILOT）与 CLI 参数/JSON 输出。
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine, text

from policy_core import autopilot

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FULL_DDL = {
    'policy': [
        'CREATE TABLE policy (policy_id INTEGER PRIMARY KEY, policy_code TEXT, '
        'policy_name TEXT, "level" TEXT, regions TEXT, "status" TEXT, '
        'effect_date TEXT, expire_date TEXT, updated_at TEXT, cur_version TEXT)'
    ],
    'policy_detail': [
        'CREATE TABLE policy_detail (detail_id INTEGER PRIMARY KEY, policy_id INTEGER, '
        '"region" TEXT, "conditions" TEXT, "target" TEXT, effect_date TEXT, expire_date TEXT)'
    ],
    'policy_version': [
        'CREATE TABLE policy_version (version_id INTEGER PRIMARY KEY, policy_id INTEGER, '
        'version_no TEXT, effect_date TEXT, expire_date TEXT)'
    ],
    'policy_draft': [
        'CREATE TABLE policy_draft (draft_id INTEGER PRIMARY KEY, policy_code TEXT, '
        'policy_name TEXT, confirm_status TEXT, created_at TEXT, "status" TEXT)'
    ],
    'policy_rule': [
        'CREATE TABLE policy_rule (rule_key TEXT PRIMARY KEY, policy_code TEXT, '
        '"region_scope" TEXT, text_hash TEXT, "status" TEXT, rule_json TEXT)'
    ],
    'policy_rule_job': [
        'CREATE TABLE policy_rule_job ("region" TEXT PRIMARY KEY, state TEXT, '
        'total INTEGER, done INTEGER, failed INTEGER, attempts INTEGER, message TEXT, '
        'created_at TEXT, started_at TEXT, finished_at TEXT)'
    ],
    'policy_scope': [
        'CREATE TABLE policy_scope (policy_code TEXT, "detail_id" TEXT, scope_kind TEXT)'
    ],
    'policy_vector_qwen3': [
        'CREATE TABLE policy_vector_qwen3 (policy_code TEXT, "detail_id" TEXT)'
    ],
    'policy_vector': [
        'CREATE TABLE policy_vector (policy_code TEXT, "detail_id" TEXT)'
    ],
    'outbox': [
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type varchar(64) NOT NULL, payload text NOT NULL, "
        "status varchar(16) NOT NULL DEFAULT 'pending', "
        "attempts integer NOT NULL DEFAULT 0, created_at varchar(32), "
        "updated_at varchar(32), last_error text)"
    ],
}


class Db(object):
    """sqlite 内存库夹具：同线程单连接，跨 engine.begin() 数据持续。"""

    def __init__(self):
        self.engine = create_engine('sqlite://')

    def create(self, *tables):
        with self.engine.begin() as conn:
            for table in tables:
                for stmt in FULL_DDL[table]:
                    conn.execute(text(stmt))
        return self

    def ddl(self, statements):
        with self.engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
        return self

    def sql(self, *statements):
        with self.engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
        return self

    def run(self, func, **kwargs):
        with self.engine.begin() as conn:
            return func(conn, **kwargs)

    def rows(self, sql, **params):
        with self.engine.begin() as conn:
            return conn.execute(text(sql), params).fetchall()

    def scalar(self, sql, **params):
        with self.engine.begin() as conn:
            return conn.execute(text(sql), params).scalar()


class ExpireScanTest(unittest.TestCase):
    def setUp(self):
        self.db = Db().create('policy')

    def test_empty_field_note(self):
        self.db.sql(
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\") "
            "VALUES (1, 'X-1', '政策一', '在用')")
        report = self.db.run(autopilot.expire_scan, today=autopilot.datetime.date(2026, 11, 8))
        self.assertEqual(report['task'], 'expire_scan')
        self.assertTrue(report['mode'])
        self.assertEqual(report['counts'], {'expired': 0, 'expiring': 0})
        self.assertEqual(report['date_fields']['effect_date_non_empty'], 0)
        self.assertEqual(report['date_fields']['expire_date_non_empty'], 0)
        self.assertIn('无可用时效数据', report['note'])
        self.assertEqual(report['applied'], [])

    def test_scan_apply_and_idempotent(self):
        self.db.sql(
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\", expire_date) "
            "VALUES (1, 'X-1', '已过期政策', '在用', '2026-10-01')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\", expire_date) "
            "VALUES (2, 'X-2', '临期政策', '在用', '2026-11-20')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\", expire_date) "
            "VALUES (3, 'X-3', '已停用过期', '停用', '2026-10-01')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\") "
            "VALUES (4, 'X-4', '无日期', '在用')")
        today = autopilot.datetime.date(2026, 11, 8)
        dry = self.db.run(autopilot.expire_scan, today=today, days=30)
        self.assertEqual(dry['counts'], {'expired': 1, 'expiring': 1})
        self.assertEqual(dry['expired'][0]['policy_code'], 'X-1')
        self.assertEqual(dry['expiring'][0]['days_left'], 12)
        self.assertEqual(dry['applied'], [])
        self.assertEqual(self.db.scalar("SELECT count(*) FROM policy WHERE \"status\" = '在用'"), 3)

        applied = self.db.run(autopilot.expire_scan, today=today, days=30, dry_run=False)
        self.assertEqual(applied['applied'], ['X-1'])
        self.assertEqual(applied['after']['active'], 2)
        self.assertEqual(self.db.scalar(
            "SELECT \"status\" FROM policy WHERE policy_id = 1"), '停用')
        self.assertEqual(self.db.scalar(
            "SELECT \"status\" FROM policy WHERE policy_id = 2"), '在用')

        again = self.db.run(autopilot.expire_scan, today=today, days=30, dry_run=False)
        self.assertEqual(again['applied'], [])

    def test_apply_limit(self):
        self.db.sql(
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\", expire_date) "
            "VALUES (1, 'X-1', 'P1', '在用', '2026-09-01')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\", expire_date) "
            "VALUES (2, 'X-2', 'P2', '在用', '2026-09-02')")
        report = self.db.run(autopilot.expire_scan, today=autopilot.datetime.date(2026, 11, 8),
                             dry_run=False, limit=1)
        self.assertEqual(len(report['applied']), 1)
        self.assertEqual(self.db.scalar("SELECT count(*) FROM policy WHERE \"status\" = '停用'"), 1)

    def test_missing_date_columns(self):
        db = Db().ddl(['CREATE TABLE policy (policy_id INTEGER PRIMARY KEY, policy_code TEXT)'])
        report = db.run(autopilot.expire_scan)
        self.assertFalse(report['supported'])
        self.assertIn('缺少列', report['reason'])


class HealthCheckTest(unittest.TestCase):
    def setUp(self):
        self.db = Db().create('policy', 'policy_detail', 'policy_version',
                              'policy_vector_qwen3', 'policy_vector')
    def _seed(self):
        self.db.sql(
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"level\", \"status\") "
            "VALUES (1, 'P-1', '有明细', 'X', '在用')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"level\", \"status\") "
            "VALUES (2, 'P-2', '空壳', 'X', '在用')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"level\", \"status\") "
            "VALUES (3, 'P-3', '无版本', 'S', '在用')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"level\", \"status\") "
            "VALUES (4, 'P-OLD', '停用政策', 'X', '停用')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\", \"conditions\", \"target\") "
            "VALUES (11, 1, '章贡区', '条件A', '对象A')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\", \"conditions\", \"target\") "
            "VALUES (12, 1, '章贡区', '条件A', '对象A')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\", \"conditions\", \"target\") "
            "VALUES (13, 999, '南康区', '条件B', '对象B')",
            "INSERT INTO policy_version (version_id, policy_id, version_no) VALUES (1, 1, 'v1')",
            "INSERT INTO policy_version (version_id, policy_id, version_no) VALUES (2, 2, 'v1')",
            "INSERT INTO policy_vector_qwen3 (policy_code, \"detail_id\") VALUES ('P-1', '11')",
            "INSERT INTO policy_vector_qwen3 (policy_code, \"detail_id\") VALUES ('P-OLD', '41')",
            "INSERT INTO policy_vector (policy_code, \"detail_id\") VALUES ('P-OLD', '41')")

    def test_checks(self):
        self._seed()
        report = self.db.run(autopilot.health_check)
        self.assertEqual(report['policy_total'], 4)
        self.assertEqual(report['detail_total'], 3)
        checks = report['checks']
        self.assertEqual(checks['empty_shell']['count'], 3)
        self.assertEqual(checks['empty_shell']['sample'][0], ['P-2', '空壳'])
        self.assertIn('skipped', checks['no_scope'])
        self.assertEqual(checks['duplicate_details']['groups'], 1)
        self.assertEqual(checks['duplicate_details']['rows'], 2)
        self.assertEqual(checks['duplicate_details']['redundant'], 1)
        self.assertIn('(region, conditions, target)', checks['duplicate_details']['criterion'])
        self.assertEqual(checks['no_version']['count'], 2)
        self.assertEqual(checks['no_version']['sample'], ['P-3', 'P-OLD'])
        self.assertEqual(checks['orphan_details']['count'], 1)
        residue = checks['inactive_vector_residue']
        self.assertEqual(residue['qwen3']['count'], 1)
        self.assertEqual(residue['qwen3']['sample'], ['P-OLD'])
        self.assertEqual(residue['legacy']['count'], 1)
        self.assertEqual(report['issue_total'], 9)
        self.assertEqual(report['verdict'], 'attention')
        self.assertEqual(report['mode'], 'dry_run')  # 巡检恒只读，mode 仅作标识

    def test_scope_table_present(self):
        self.db.create('policy_scope')
        self._seed()
        self.db.sql("INSERT INTO policy_scope (policy_code) VALUES ('P-1')")
        report = self.db.run(autopilot.health_check)
        self.assertEqual(report['checks']['no_scope']['count'], 3)
        self.assertEqual(report['checks']['no_scope']['table'], 'policy_scope')

    def test_no_policy_table(self):
        db = Db()
        report = db.run(autopilot.health_check)
        self.assertEqual(report['policy_total'], 0)
        self.assertEqual(report['issue_total'], 0)
        self.assertEqual(report['verdict'], 'ok')
        for name in ('empty_shell', 'no_scope', 'duplicate_details', 'no_version',
                     'orphan_details', 'inactive_vector_residue'):
            self.assertIn('skipped', report['checks'][name])


class CoverageReportTest(unittest.TestCase):
    def test_jiangxi_gap_zones_and_province_entry(self):
        db = Db().create('policy', 'policy_detail')
        db.sql(
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"level\") "
            "VALUES (1, 'X-1', 'P1', 'X')",
            "INSERT INTO policy (policy_id, policy_code, policy_name, \"level\") "
            "VALUES (2, 'S-1', 'P2', 'S')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") VALUES (11, 1, '章贡区')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") VALUES (12, 1, '章贡区')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") "
            "VALUES (13, 1, '赣州经济技术开发区')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") "
            "VALUES (14, 2, '江西省全省')")
        report = db.run(autopilot.coverage_report)
        self.assertEqual(report['task'], 'coverage_report')
        self.assertEqual(report['totals'], {'policies': 2, 'details': 4, 'regions': 3})
        self.assertEqual(report['policy_levels'], {'S': 1, 'X': 1})
        self.assertEqual(report['national_gap'],
                         {'expected_standard_counties': 100,
                          'covered_standard_counties': 1,
                          'missing_standard_counties': 99})
        self.assertEqual(len(report['provinces']), 1)
        prov = report['provinces'][0]
        self.assertEqual(prov['province'], '江西省')
        self.assertEqual(prov['expected_counties'], 100)
        self.assertEqual(prov['covered_counties'], 1)
        self.assertEqual(prov['missing_counties'], 99)
        self.assertIn('章贡区', prov['covered_sample'])
        self.assertEqual(prov['zones'], 1)
        self.assertEqual([z['region'] for z in report['zones']], ['赣州经济技术开发区'])
        self.assertEqual(report['zones'][0]['city'], '赣州市')
        self.assertEqual([e['region'] for e in report['province_level_entries']],
                         ['江西省全省'])
        self.assertEqual(report['unknown_regions'], [])
        gz = [c for c in prov['cities'] if c['city'] == '赣州市'][0]
        self.assertEqual(gz['covered_counties'], 1)
        self.assertEqual(len(gz['zones']), 1)

    def test_missing_detail_table(self):
        db = Db().create('policy')
        report = db.run(autopilot.coverage_report)
        self.assertFalse(report['supported'])
        self.assertIn('policy_detail', report['reason'])


class RuleRequeueTest(unittest.TestCase):
    def setUp(self):
        self.db = Db().create('policy_detail', 'policy_rule', 'policy_rule_job')
        self.db.sql(
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") VALUES (1, 1, '万安县')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") VALUES (2, 1, '于都县')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") VALUES (3, 1, '章贡区')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") "
            "VALUES (4, 1, '赣州经济技术开发区')",
            "INSERT INTO policy_detail (detail_id, policy_id, \"region\") VALUES (5, 1, '江西省全省')",
            "INSERT INTO policy_rule (rule_key, \"region_scope\", \"status\") VALUES ('r1', '万安', 'complete')",
            "INSERT INTO policy_rule (rule_key, \"region_scope\", \"status\") VALUES ('r2', '*', 'complete')",
            "INSERT INTO policy_rule_job (\"region\", state, total, done, failed, attempts) "
            "VALUES ('于都', 'failed', 5, 3, 2, 1)",
            "INSERT INTO policy_rule_job (\"region\", state, total, done, failed, attempts) "
            "VALUES ('章贡', 'done', 0, 0, 0, 1)")

    def test_dry_run_plan(self):
        report = self.db.run(autopilot.rule_requeue)
        self.assertEqual(report['county_total'], 3)
        self.assertEqual(report['zones_excluded_total'], 2)
        self.assertEqual(report['global_rules'], 1)
        self.assertEqual(report['covered'], ['万安'])
        self.assertEqual(report['missing'], ['于都', '章贡'])
        self.assertEqual(report['before'], {'jobs': 2, 'pending': 0, 'failed': 1})
        self.assertEqual([p['county'] for p in report['plan']], ['于都', '章贡'])
        self.assertEqual(self.db.scalar("SELECT count(*) FROM policy_rule_job"), 2)

    def test_apply_insert_and_requeue(self):
        report = self.db.run(autopilot.rule_requeue, dry_run=False)
        actions = {item['county']: item for item in report['enqueued']}
        self.assertEqual(actions['于都']['action'], 'requeued')
        self.assertEqual(actions['于都']['state_before'], 'failed')
        self.assertEqual(actions['章贡']['action'], 'requeued')
        self.assertEqual(report['after'], {'jobs': 2, 'pending': 2, 'failed': 0})
        self.assertEqual(self.db.scalar(
            "SELECT attempts FROM policy_rule_job WHERE \"region\" = '于都'"), 2)
        self.assertEqual(self.db.scalar(
            "SELECT state FROM policy_rule_job WHERE \"region\" = '章贡'"), 'pending')
        again = self.db.run(autopilot.rule_requeue, dry_run=False)
        self.assertEqual({i['action'] for i in again['enqueued']}, {'skipped'})

    def test_apply_insert_missing_job(self):
        self.db.sql("DELETE FROM policy_rule_job")
        report = self.db.run(autopilot.rule_requeue, dry_run=False)
        self.assertEqual({i['action'] for i in report['enqueued']}, {'inserted'})
        self.assertEqual(report['after']['pending'], 2)
        self.assertEqual(self.db.scalar(
            "SELECT attempts FROM policy_rule_job WHERE \"region\" = '于都'"), 0)

    def test_apply_limit(self):
        report = self.db.run(autopilot.rule_requeue, dry_run=False, limit=1)
        self.assertEqual(len(report['enqueued']), 1)
        self.assertEqual(report['after']['pending'], 1)

    def test_done_nonzero_is_skipped(self):
        self.db.sql("UPDATE policy_rule_job SET total = 9, done = 9 WHERE \"region\" = '章贡'")
        report = self.db.run(autopilot.rule_requeue, dry_run=False)
        actions = {item['county']: item for item in report['enqueued']}
        self.assertEqual(actions['章贡']['action'], 'skipped')
        self.assertEqual(actions['于都']['action'], 'requeued')
        self.assertEqual(self.db.scalar(
            "SELECT state FROM policy_rule_job WHERE \"region\" = '章贡'"), 'done')

    def test_no_detail_table(self):
        db = Db()
        report = db.run(autopilot.rule_requeue)
        self.assertFalse(report['supported'])


class StaleDraftTest(unittest.TestCase):
    def setUp(self):
        self.db = Db().create('policy_draft')
        self.db.sql(
            "INSERT INTO policy_draft (draft_id, policy_code, policy_name, confirm_status, created_at) "
            "VALUES (1, 'D-1', '旧草稿', '待确认', '2026-11-01 09:00:00')",
            "INSERT INTO policy_draft (draft_id, policy_code, policy_name, confirm_status, created_at) "
            "VALUES (2, 'D-2', '新草稿', '待确认', '2026-11-07 09:00:00')",
            "INSERT INTO policy_draft (draft_id, policy_code, policy_name, confirm_status, created_at) "
            "VALUES (3, 'D-3', '已确认旧草稿', '已确认', '2026-10-01 09:00:00')")

    def test_stale_72h(self):
        report = self.db.run(autopilot.stale_draft, now='2026-11-08 10:00:00')
        self.assertEqual(report['threshold_hours'], 72)
        self.assertEqual(report['draft_total'], 3)
        self.assertEqual(report['by_confirm_status'], {'待确认': 2, '已确认': 1})
        self.assertEqual(report['stale_count'], 1)
        self.assertEqual(report['items'][0]['draft_id'], 1)
        self.assertEqual(report['items'][0]['policy_code'], 'D-1')
        self.assertGreater(report['items'][0]['age_hours'], 168)

    def test_custom_hours(self):
        report = self.db.run(autopilot.stale_draft, hours=24, now='2026-11-08 10:00:00')
        self.assertEqual([i['draft_id'] for i in report['items']], [1, 2])

    def test_missing_created_at(self):
        db = Db().ddl(['CREATE TABLE policy_draft (draft_id INTEGER PRIMARY KEY, '
                       'policy_code TEXT, policy_name TEXT, confirm_status TEXT)'])
        report = db.run(autopilot.stale_draft)
        self.assertFalse(report['supported'])
        self.assertIn('created_at', report['reason'])


class OutboxDrainTest(unittest.TestCase):
    def test_probe_without_handlers(self):
        db = Db().create('outbox')
        db.sql("INSERT INTO outbox (event_type, payload, status) "
               "VALUES ('policy.vector_rebuild', '{}', 'pending')",
               "INSERT INTO outbox (event_type, payload, status) "
               "VALUES ('policy.rule_invalidate', '{}', 'done')")
        report = db.run(autopilot.outbox_drain)
        self.assertTrue(report['available'])
        self.assertEqual(report['mode'], 'probe')
        self.assertEqual(report['table_probe'], {'outbox': 2})
        self.assertEqual(report['counts'], {'pending': 1, 'done': 1})
        self.assertIn('消费者未接线', report['reason'])
        self.assertEqual(
            db.scalar("SELECT count(*) FROM outbox WHERE status = 'pending'"), 1)

    def test_handlers_dry_run_then_apply(self):
        db = Db().create('outbox')
        db.sql("INSERT INTO outbox (event_type, payload, status) "
               "VALUES ('policy.vector_rebuild', '{\"a\": 1}', 'pending')",
               "INSERT INTO outbox (event_type, payload, status) "
               "VALUES ('policy.rule_invalidate', '{\"b\": 2}', 'pending')")
        handlers = {'policy.vector_rebuild': lambda payload: None,
                    'policy.rule_invalidate': lambda payload: None}
        dry = db.run(autopilot.outbox_drain, handlers=handlers, dry_run=True)
        self.assertEqual(dry['mode'], 'drain')
        self.assertEqual(dry['claimable'], 2)
        self.assertEqual(len(dry['preview']), 2)
        self.assertEqual(
            db.scalar("SELECT count(*) FROM outbox WHERE status = 'pending'"), 2)
        applied = db.run(autopilot.outbox_drain, handlers=handlers, dry_run=False)
        self.assertEqual(applied['processed'], 2)
        self.assertEqual(applied['done'], 2)
        self.assertEqual(
            db.scalar("SELECT count(*) FROM outbox WHERE status = 'done'"), 2)

    def test_fake_handler(self):
        db = Db()

        def handler(conn, limit=200, dry_run=True):
            return {'drained': 5, 'pending': 2}

        report = db.run(autopilot.outbox_drain, handler=handler, dry_run=False, limit=10)
        self.assertTrue(report['available'])
        self.assertEqual(report['mode'], 'handler')
        self.assertEqual(report['drained'], 5)
        self.assertEqual(report['pending'], 2)
        self.assertEqual(report['result'], {'drained': 5, 'pending': 2})

    def test_handler_error_captured(self):
        db = Db()

        def boom(conn, limit=200, dry_run=True):
            raise RuntimeError('boom')

        report = db.run(autopilot.outbox_drain, handler=boom)
        self.assertTrue(report['available'])
        self.assertIn('RuntimeError: boom', report['error'])

    def test_module_missing_degrades(self):
        import builtins
        db = Db()
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if fromlist and 'outbox' in fromlist:
                raise ImportError('simulated: T18 missing')
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch('builtins.__import__', side_effect=fake_import):
            report = db.run(autopilot.outbox_drain)
        self.assertFalse(report['available'])
        self.assertIn('T18', report['reason'])


class RunAllTest(unittest.TestCase):
    def test_six_tasks(self):
        db = Db()
        report = db.run(autopilot.run_all)
        self.assertEqual(set(report), {'expire_scan', 'outbox_drain', 'health_check',
                                       'coverage_report', 'rule_requeue', 'stale_draft'})


class SwitchTest(unittest.TestCase):
    def test_apply_allowed(self):
        with mock.patch.dict(os.environ, {'POLICY_AUTOPILOT': '0'}):
            self.assertFalse(autopilot.apply_allowed())
        with mock.patch.dict(os.environ, {'POLICY_AUTOPILOT': '1'}):
            self.assertTrue(autopilot.apply_allowed())
        env = dict(os.environ)
        env.pop('POLICY_AUTOPILOT', None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(autopilot.apply_allowed())
            self.assertEqual(autopilot.stale_hours(), 72)
            self.assertEqual(autopilot.expiring_days(), 30)
        with mock.patch.dict(os.environ, {'POLICY_AUTOPILOT_STALE_HOURS': 'bad',
                                          'POLICY_AUTOPILOT_EXPIRING_DAYS': 'x'}):
            self.assertEqual(autopilot.stale_hours(), 72)
            self.assertEqual(autopilot.expiring_days(), 30)


def _load_cli():
    path = os.path.join(BASE, 'deploy', 'policy_autopilot.py')
    spec = importlib.util.spec_from_file_location('policy_autopilot_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='autopilot-')
        self.path = os.path.join(self.tmp, 'autopilot.db')
        self.url = 'sqlite:///' + self.path
        engine = create_engine(self.url)
        with engine.begin() as conn:
            for table in ('policy', 'policy_detail'):
                for stmt in FULL_DDL[table]:
                    conn.execute(text(stmt))
            conn.execute(text(
                "INSERT INTO policy (policy_id, policy_code, policy_name, \"status\") "
                "VALUES (1, 'X-1', 'P1', '在用')"))
        engine.dispose()
        self.cli = _load_cli()

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if argv:
                code = self.cli.main(argv + ['--url', self.url])
            else:
                code = self.cli.main([])
        return code, out.getvalue(), err.getvalue()

    def test_health_check_json(self):
        code, out, _ = self._main(['health-check', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload['command'], 'health-check')
        self.assertFalse(payload['apply'])
        report = payload['reports']['health-check']
        self.assertEqual(report['policy_total'], 1)
        self.assertIn('checks', report)

    def test_apply_requires_switch(self):
        with mock.patch.dict(os.environ, {'POLICY_AUTOPILOT': '0'}):
            code, _, err = self._main(['expire-scan', '--apply'])
        self.assertEqual(code, 2)
        self.assertIn('拒绝写入', err)
        self.assertIn('POLICY_AUTOPILOT=1', err)

    def test_all_dry_run_json(self):
        code, out, _ = self._main(['all', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload['reports']), {
            'expire-scan', 'outbox-drain', 'health-check',
            'coverage-report', 'rule-requeue', 'stale-draft'})
        self.assertFalse(payload['apply'])

    def test_no_command(self):
        code, out, _ = self._main([])
        self.assertEqual(code, 2)
        self.assertIn('usage:', out)


if __name__ == '__main__':
    unittest.main()
