# -*- coding: utf-8 -*-
"""deploy/migrate_policy_admin.py 一次性结构迁移脚本测试。"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, 'deploy', 'migrate_policy_admin.py')


def _run(db_path, *args):
    env = dict(os.environ)
    env['ADMIN_DB_URL'] = 'sqlite:///%s' % db_path
    env['POLICY_ENV_FILE'] = os.path.join(BASE, 'deploy', 'no-such-env-file.env')
    env.pop('POLICY_DATABASE_URL', None)
    proc = subprocess.run([sys.executable, SCRIPT] + list(args),
                          cwd=BASE, env=env, capture_output=True, text=True, timeout=120)
    return proc


def _json(proc):
    text = proc.stdout or ''
    start = text.find('{')
    if start < 0:
        return {}
    return json.loads(text[start:])


class MigrateAdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'admin.db')
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE policy (policy_code varchar(64) PRIMARY KEY, '
                     'policy_name varchar(200), status varchar(16))')
        conn.execute("INSERT INTO policy VALUES ('X-ZB-001','测试政策','在用')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_mode_is_read_only(self):
        proc = _run(self.db, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = _json(proc)
        self.assertEqual(report['mode'], 'check')
        self.assertEqual(sorted(report['missing_before']),
                         sorted(['import_batch', 'import_row', 'outbox',
                                 'policy.deleted_at', 'policy_scope']))
        self.assertFalse(report['ok'])
        # 只读：库结构未变
        conn = sqlite3.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute('PRAGMA table_info(policy)')}
        conn.close()
        self.assertNotIn('outbox', names)
        self.assertNotIn('import_batch', names)
        self.assertNotIn('deleted_at', cols)

    def test_apply_creates_structures_and_is_idempotent(self):
        proc = _run(self.db, '--apply', '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = _json(proc)
        self.assertEqual(report['mode'], 'apply')
        self.assertEqual(report['missing_after'], [])
        self.assertTrue(report['ok'])
        conn = sqlite3.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute('PRAGMA table_info(policy)')}
        conn.close()
        for table in ('outbox', 'import_batch', 'import_row', 'policy_scope'):
            self.assertIn(table, names)
        self.assertIn('deleted_at', cols)

        second = _json(_run(self.db, '--apply', '--json'))
        self.assertEqual(second['missing_before'], [])
        self.assertEqual(second['missing_after'], [])

    def test_plain_output_mentions_missing(self):
        proc = _run(self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('政策管理结构', proc.stdout)
        self.assertIn('policy.deleted_at', proc.stdout)
        self.assertIn('--apply', proc.stdout)

    def test_apply_survives_missing_policy_table(self):
        os.remove(self.db)
        sqlite3.connect(self.db).close()
        proc = _run(self.db, '--apply', '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = _json(proc)
        self.assertIn('policy.deleted_at', report['missing_after'])
        self.assertFalse(report['ok'])

    def test_bad_database_url_reports_error(self):
        proc = _run(os.path.join(self.tmp.name, 'no-such-dir', 'x.db'), '--json')
        # 连接失败属环境错误：rc=2 且报告里带 error
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = _json(proc)
        self.assertIn('error', report)


if __name__ == '__main__':
    unittest.main()
