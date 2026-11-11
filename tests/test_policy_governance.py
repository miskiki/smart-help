# -*- coding: utf-8 -*-
"""deploy/policy_governance.py 单测（sqlite 内存/临时文件库，不触生产库）。

覆盖：
- report 动作：JSON 结构、dry-run 不建表不写库、limit 截断
- archive-empty-shells：dry-run 清单、apply 归档 + 4 类 outbox + audit、幂等、跳过口径
- backfill-scope：按 regions/明细生成 scope 行、match 可判定、--codes 限定、空地区跳过
- fix-scope-mismatch：错误 scope 检出/重建、全省口径 audit_parity、would_clear_scope_rows 跳过
- 写护栏：POLICY_GOVERNANCE 开关、--confirm、operator/reason 校验（CLI 退出码 2）
- report-manual：只读输出重复组/跨级同名
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from sqlalchemy import create_engine, inspect, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'policy_governance', os.path.join(ROOT, 'deploy', 'policy_governance.py'))
gov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gov)

policy_scope = gov.policy_scope

DDL = [
    '''CREATE TABLE policy (
        policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
        policy_code TEXT, policy_name TEXT, "level" TEXT, regions TEXT,
        status TEXT, cur_version TEXT, updated_at TEXT, updated_by TEXT, source TEXT)''',
    '''CREATE TABLE policy_detail (
        detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
        policy_id INTEGER, region TEXT, conditions TEXT, target TEXT)''',
    '''CREATE TABLE policy_version (
        version_id INTEGER PRIMARY KEY AUTOINCREMENT,
        policy_id INTEGER, version_no TEXT)''',
    '''CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT, target_code TEXT, target_name TEXT, detail TEXT,
        operator TEXT, created_at TEXT)''',
]

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_no_such_env_file')


class GovernanceBase(unittest.TestCase):
    """每个用例一个临时 sqlite 文件库（CLI 需要 URL 可重入）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, 'policy.db')
        self.url = 'sqlite:///' + self.db_path
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            for ddl in DDL:
                conn.execute(text(ddl))
        self._env_backup = os.environ.get(gov.SWITCH_ENV)
        os.environ.pop(gov.SWITCH_ENV, None)

    def tearDown(self):
        if self._env_backup is None:
            os.environ.pop(gov.SWITCH_ENV, None)
        else:
            os.environ[gov.SWITCH_ENV] = self._env_backup

    # ---------- 种子数据 ----------

    def add_policy(self, code, name='政策', level='X', regions=None, status='在用',
                   cur_version='v1'):
        with self.engine.begin() as conn:
            result = conn.execute(text(
                'INSERT INTO policy (policy_code, policy_name, "level", regions, status, '
                'cur_version) VALUES (:code, :name, :level, :regions, :status, :version)'),
                {'code': code, 'name': name, 'level': level, 'regions': regions,
                 'status': status, 'version': cur_version})
            return result.lastrowid

    def add_detail(self, policy_id, region, conditions='条件', target='目标'):
        with self.engine.begin() as conn:
            conn.execute(text('INSERT INTO policy_detail (policy_id, region, conditions, target) '
                              'VALUES (:pid, :region, :conditions, :target)'),
                         {'pid': policy_id, 'region': region, 'conditions': conditions,
                          'target': target})

    def put_scope(self, code, regions, details=()):
        """预置 policy_scope 行（模拟历史回填结果）。"""
        with self.engine.begin() as conn:
            policy_scope.ensure_schema(conn)
            rows = policy_scope.rows_for(code, regions, details, default_province='江西省')
            policy_scope.replace_policy(conn, code, rows)

    # ---------- 执行 ----------

    def govern(self, action='all', apply=False, **kwargs):
        params = {'limit': 20, 'operator': 'tester', 'reason': None,
                  'default_province': '江西省', 'confirm': True}
        params.update(kwargs)
        with self.engine.begin() as conn:
            return gov.govern(conn, action=action, apply=apply, **params)

    def policy_status(self, code):
        with self.engine.connect() as conn:
            return conn.execute(text('SELECT status FROM policy WHERE policy_code=:code'),
                                {'code': code}).scalar()

    def scalar(self, sql):
        with self.engine.connect() as conn:
            return conn.execute(text(sql)).scalar()

    def rows(self, sql):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(sql)).mappings().all()]

    def has_table(self, name):
        with self.engine.connect() as conn:
            return inspect(conn).has_table(name)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gov.main(argv)
        return code, out.getvalue(), err.getvalue()

    def base_argv(self, *extra):
        return list(extra) + ['--url', self.url, '--env-file', ENV_FILE]


class ReportTests(GovernanceBase):
    def test_dry_run_report_has_all_sections_and_guards(self):
        self.add_policy('X-R-1', '有地区', regions='南昌市新建区')
        report = self.govern(action='all', apply=False)
        self.assertEqual(report['command'], 'policy-governance')
        self.assertEqual(report['mode'], 'dry-run')
        self.assertEqual(set(report['actions']),
                         {'report-manual', 'archive-empty-shells',
                          'backfill-scope', 'fix-scope-mismatch'})
        self.assertFalse(report['guards']['switch_enabled'])
        self.assertEqual(report['actions']['archive-empty-shells']['applied'], 0)
        json.dumps(report, ensure_ascii=False, default=str)

    def test_dry_run_creates_no_tables_and_no_rows(self):
        self.add_policy('X-R-1', regions=None)
        self.govern(action='all', apply=False)
        self.assertFalse(self.has_table('policy_scope'))
        self.assertFalse(self.has_table('outbox'))
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM audit_log'), 0)
        self.assertEqual(self.policy_status('X-R-1'), '在用')

    def test_limit_caps_items_and_remaining_after(self):
        for index in range(3):
            self.add_policy('X-L-%d' % index, regions=None)
        report = self.govern(action='archive-empty-shells', limit=2)
        section = report['actions']['archive-empty-shells']
        self.assertEqual(section['before']['eligible'], 3)
        self.assertEqual(section['would_change'], 2)
        self.assertEqual(section['remaining_after'], 1)

    def test_report_manual_lists_duplicates_and_cross_level_names(self):
        pid_a = self.add_policy('X-D-1', '重复政策', level='X', regions='南昌市新建区')
        pid_b = self.add_policy('S-D-2', '重复政策', level='S', regions='南昌市新建区')
        self.add_detail(pid_a, '南昌市新建区', conditions='同一条件', target='同一目标')
        self.add_detail(pid_b, '南昌市新建区', conditions='同一条件', target='同一目标')
        report = self.govern(action='report-manual')
        counts = report['actions']['report-manual']['counts']
        self.assertGreaterEqual(counts['duplicate_groups'], 1)
        self.assertGreaterEqual(counts['cross_level_names'], 1)
        self.assertFalse(report['actions']['report-manual']['counts']['writable'])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM audit_log'), 0)


class ArchiveTests(GovernanceBase):
    def test_dry_run_lists_only_eligible_shells(self):
        self.add_policy('X-A-1', '空壳', level='S', regions=None)
        pid = self.add_policy('X-A-2', '有明细', regions=None)
        self.add_detail(pid, '南昌市新建区')
        self.add_policy('X-A-3', '有地区', regions='南昌市新建区')
        self.add_policy('X-A-4', '已归档', regions=None, status='archived')
        section = self.govern(action='archive-empty-shells')['actions']['archive-empty-shells']
        self.assertEqual(section['before']['eligible'], 1)
        self.assertEqual([item['policy_code'] for item in section['items']], ['X-A-1'])
        self.assertEqual(section['before']['skipped'].get('has_regions'), 1)
        self.assertEqual(section['before']['skipped'].get('status_archived'), 1)

    def test_apply_archives_shell_and_cascades(self):
        self.add_policy('X-A-1', '空壳', level='S', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            report = self.govern(action='archive-empty-shells', apply=True)
        section = report['actions']['archive-empty-shells']
        self.assertEqual(section['applied'], 1)
        self.assertEqual(section['after']['eligible'], 0)
        self.assertEqual(self.policy_status('X-A-1'), 'archived')
        types = [row['event_type'] for row in self.rows('SELECT event_type FROM outbox')]
        self.assertEqual(sorted(types), sorted(gov.outbox.EVENT_TYPES))
        audit = self.rows('SELECT action, target_code, operator FROM audit_log')
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['target_code'], 'X-A-1')
        self.assertEqual(audit[0]['operator'], 'tester')

    def test_apply_is_idempotent(self):
        self.add_policy('X-A-1', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            first = self.govern(action='archive-empty-shells', apply=True)
            second = self.govern(action='archive-empty-shells', apply=True)
        self.assertEqual(first['actions']['archive-empty-shells']['applied'], 1)
        self.assertEqual(second['actions']['archive-empty-shells']['applied'], 0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM outbox'), len(gov.outbox.EVENT_TYPES))

    def test_skips_shell_with_existing_scope_rows(self):
        self.add_policy('X-A-1', regions=None)
        self.put_scope('X-A-1', '南昌市新建区')
        section = self.govern(action='archive-empty-shells')['actions']['archive-empty-shells']
        self.assertEqual(section['before']['eligible'], 0)
        self.assertEqual(section['before']['skipped'].get('has_scope_rows'), 1)

    def test_apply_limit_only_archives_bounded_count(self):
        for index in range(3):
            self.add_policy('X-A-%d' % index, regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            report = self.govern(action='archive-empty-shells', apply=True, limit=2)
        self.assertEqual(report['actions']['archive-empty-shells']['applied'], 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM policy WHERE status='archived'"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM policy WHERE status='在用'"), 1)


class BackfillTests(GovernanceBase):
    def test_dry_run_does_not_create_scope_table(self):
        self.add_policy('X-B-1', regions='南昌市新建区')
        section = self.govern(action='backfill-scope')['actions']['backfill-scope']
        self.assertEqual(section['before']['with_rows'], 1)
        self.assertEqual(section['would_change'], 1)
        self.assertFalse(self.has_table('policy_scope'))

    def test_apply_generates_scope_rows_and_match(self):
        pid = self.add_policy('X-B-1', regions='南昌市新建区')
        self.add_detail(pid, '南昌市新建区')
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            report = self.govern(action='backfill-scope', apply=True)
        section = report['actions']['backfill-scope']
        self.assertEqual(section['applied'], 1)
        self.assertEqual(section['applied_items'][0]['rows_written'], 2)
        with self.engine.connect() as conn:
            scopes = policy_scope.load_scopes(conn, ['X-B-1'])
        result = policy_scope.match(scopes['X-B-1'], '新建区')
        self.assertTrue(result['applicable'])
        self.assertEqual(result['relation'], 'same')
        events = self.rows("SELECT payload FROM outbox WHERE event_type='policy.cache_invalidate'")
        self.assertEqual(len(events), 1)
        self.assertIn('X-B-1', events[0]['payload'])

    def test_codes_filter_limits_targets(self):
        self.add_policy('X-B-1', regions='南昌市新建区')
        self.add_policy('X-B-2', regions='南昌市东湖区')
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            report = self.govern(action='backfill-scope', apply=True, codes=['X-B-1'])
        section = report['actions']['backfill-scope']
        self.assertEqual(section['before']['targets'], 1)
        self.assertEqual(section['applied'], 1)
        with self.engine.connect() as conn:
            scopes = policy_scope.load_scopes(conn, ['X-B-2'])
        self.assertEqual(scopes['X-B-2'], {'policy': [], 'detail': []})

    def test_apply_skips_policy_without_regions(self):
        self.add_policy('X-B-1', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            report = self.govern(action='backfill-scope', apply=True)
        section = report['actions']['backfill-scope']
        self.assertEqual(section['before']['empty_rows'], 1)
        self.assertEqual(section['applied'], 0)
        self.assertEqual(section['skipped'][0]['reason'], 'no_regions')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM policy_scope'), 0)


class MismatchTests(GovernanceBase):
    def test_detects_wrong_scope_and_fixes(self):
        pid = self.add_policy('X-M-1', regions='南昌市新建区')
        self.add_detail(pid, '南昌市新建区')
        self.put_scope('X-M-1', '南昌市东湖区', details=[('1', '南昌市东湖区')])
        before = self.govern(action='fix-scope-mismatch')['actions']['fix-scope-mismatch']
        self.assertEqual(before['before']['mismatch'], 1)
        self.assertTrue(before['items'][0]['safe'])
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            after = self.govern(action='fix-scope-mismatch', apply=True)
        section = after['actions']['fix-scope-mismatch']
        self.assertEqual(section['applied'], 1)
        self.assertEqual(section['after']['mismatch'], 0)
        keys = [row['region_key'] for row in self.rows('SELECT region_key FROM policy_scope')]
        self.assertIn('X:江西省/南昌市/新建区', keys)
        self.assertNotIn('X:江西省/南昌市/东湖区', keys)

    def test_province_text_parity_is_reported_not_written(self):
        self.add_policy('X-M-2', regions='全省')
        self.put_scope('X-M-2', '')  # 建 policy_scope 表；裸「全省」按江西省口径期望 P:江西省
        before = self.govern(action='fix-scope-mismatch')['actions']['fix-scope-mismatch']
        self.assertEqual(before['before']['audit_parity'], 1)
        self.assertEqual(before['audit_parity_items'][0]['policy_code'], 'X-M-2')
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            after = self.govern(action='fix-scope-mismatch', apply=True)
        section = after['actions']['fix-scope-mismatch']
        self.assertEqual(section['after']['mismatch'], 0)
        parity = section['after_audit_parity_items']
        self.assertEqual([item['policy_code'] for item in parity], ['X-M-2'])
        self.assertEqual(parity[0]['actual'], ['P:江西省'])

    def test_unsafe_change_is_skipped(self):
        # regions 为空但已有 scope 行：重建等于删行，apply 应跳过转人工。
        self.add_policy('X-M-3', regions=None)
        self.put_scope('X-M-3', '南昌市新建区')
        before = self.govern(action='fix-scope-mismatch')['actions']['fix-scope-mismatch']
        self.assertEqual(before['before']['unsafe'], 1)
        self.assertFalse(before['items'][0]['safe'])
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            after = self.govern(action='fix-scope-mismatch', apply=True)
        section = after['actions']['fix-scope-mismatch']
        self.assertEqual(section['applied'], 0)
        self.assertEqual(section['skipped'][0]['reason'], 'would_clear_scope_rows')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM policy_scope'), 1)


class GuardTests(GovernanceBase):
    def test_apply_refused_without_switch(self):
        self.add_policy('X-G-1', regions=None)
        with self.assertRaises(gov.GovernanceRefused) as ctx:
            self.govern(action='archive-empty-shells', apply=True)
        self.assertIn(gov.SWITCH_ENV, str(ctx.exception))
        self.assertEqual(self.policy_status('X-G-1'), '在用')

    def test_apply_refused_without_confirm(self):
        self.add_policy('X-G-1', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            with self.assertRaises(gov.GovernanceRefused):
                self.govern(action='archive-empty-shells', apply=True, confirm=False)
        self.assertEqual(self.policy_status('X-G-1'), '在用')

    def test_reason_too_short_refused(self):
        self.add_policy('X-G-1', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            with self.assertRaises(gov.GovernanceRefused):
                self.govern(action='archive-empty-shells', apply=True, reason='ab')

    def test_main_apply_requires_switch_and_confirm(self):
        self.add_policy('X-G-1', regions=None)
        code, _, err = self.run_cli(self.base_argv(
            '--apply', '--confirm', '--action', 'archive-empty-shells'))
        self.assertEqual(code, 2)
        self.assertIn(gov.SWITCH_ENV, err)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            code, _, err = self.run_cli(self.base_argv(
                '--apply', '--action', 'archive-empty-shells'))
        self.assertEqual(code, 2)
        self.assertIn('--confirm', err)
        self.assertEqual(self.policy_status('X-G-1'), '在用')

    def test_main_reason_too_short_refused(self):
        self.add_policy('X-G-1', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            code, _, err = self.run_cli(self.base_argv(
                '--apply', '--confirm', '--action', 'archive-empty-shells',
                '--operator', 'cli', '--reason', 'ab'))
        self.assertEqual(code, 2)
        self.assertIn('reason', err)

    def test_main_connection_failure_exit_2(self):
        code, _, err = self.run_cli([
            '--report', '--url', 'sqlite:////no/such/dir/policy.db',
            '--env-file', ENV_FILE])
        self.assertEqual(code, 2)
        self.assertIn('无法连接数据库', err)


class CliTests(GovernanceBase):
    def test_main_apply_archives_shell(self):
        self.add_policy('X-C-1', '命令行空壳', level='S', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            code, out, err = self.run_cli(self.base_argv(
                '--apply', '--confirm', '--action', 'archive-empty-shells',
                '--operator', 'cli-tester', '--reason', '命令行归档测试', '--json'))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report['mode'], 'apply')
        self.assertEqual(report['actions']['archive-empty-shells']['applied'], 1)
        self.assertEqual(self.policy_status('X-C-1'), 'archived')

    def test_main_json_report_parses(self):
        self.add_policy('X-C-1', regions='南昌市新建区')
        code, out, err = self.run_cli(self.base_argv('--report', '--json', '--action', 'all'))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report['mode'], 'dry-run')
        self.assertFalse(report['guards']['switch_enabled'])
        self.assertIn('report-manual', report['actions'])

    def test_main_report_manual_degrades_apply_to_readonly(self):
        self.add_policy('X-C-1', regions=None)
        with mock.patch.dict(os.environ, {gov.SWITCH_ENV: '1'}):
            code, out, err = self.run_cli(self.base_argv(
                '--apply', '--confirm', '--action', 'report-manual', '--json'))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report['mode'], 'dry-run')
        self.assertFalse(self.has_table('outbox'))


if __name__ == '__main__':
    unittest.main()
