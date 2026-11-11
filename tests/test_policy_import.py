# -*- coding: utf-8 -*-
"""T19 政策批量导入管线单测。

全部使用 sqlite：内存库覆盖 validate/commit/rollback 语义，临时文件库覆盖 CLI 端到端。
不连生产库、不写生产数据。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, text

from policy_core import admin_division, policy_import, policy_scope

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

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
    "contact" TEXT, "phone" TEXT, "target" TEXT, "conditions" TEXT,
    "effect_date" TEXT, "expire_date" TEXT, "extra" TEXT
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


class ImportTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        with self.engine.begin() as conn:
            for ddl in DDLS:
                conn.execute(text(ddl))
            policy_import.ensure_schema(conn)
            policy_scope.ensure_schema(conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # ---- 构造 ----
    def _row(self, **overrides):
        row = {'policy_code': 'X-ZB-001', 'policy_name': '测试政策',
               'level': 'X', 'regions': '莲花县',
               'target': '小微企业', 'conditions': '在莲花县注册',
               'benefit': '补贴 1 万元', 'process': '线上申请',
               'source': 'unit-test'}
        row.update(overrides)
        return row

    def _seed_policy(self, code='S-JX-001', name='现有政策', level='S',
                     regions='江西省', conditions='在江西注册', target='小微企业',
                     benefit='补贴', process='线上', cur_version='v1', status='在用',
                     source='seed'):
        with self.engine.begin() as conn:
            conn.execute(text(
                'INSERT INTO "policy" ("policy_code","policy_name","level","regions",'
                '"conditions","target","benefit","process","status","cur_version",'
                '"source","updated_at","updated_by") VALUES (:code,:name,:level,:regions,'
                ':conditions,:target,:benefit,:process,:status,:cur_version,:source,:now,'
                ':operator)'),
                {'code': code, 'name': name, 'level': level, 'regions': regions,
                 'conditions': conditions, 'target': target, 'benefit': benefit,
                 'process': process, 'status': status, 'cur_version': cur_version,
                 'source': source, 'now': '2026-01-01 00:00:00', 'operator': 'seed'})
            policy_id = conn.execute(text(
                'SELECT "policy_id" FROM "policy" WHERE "policy_code" = :code'),
                {'code': code}).scalar()
            conn.execute(text(
                'INSERT INTO "policy_detail" ("policy_id","region","conditions","target",'
                '"benefit","process") VALUES (:pid,:region,:conditions,:target,:benefit,'
                ':process)'),
                {'pid': policy_id, 'region': regions, 'conditions': conditions,
                 'target': target, 'benefit': benefit, 'process': process})
            conn.execute(text(
                'INSERT INTO "policy_version" ("policy_id","version_no","content_snapshot",'
                '"note") VALUES (:pid,:version,:snapshot,:note)'),
                {'pid': policy_id, 'version': cur_version, 'snapshot': '{}', 'note': '初始'})
        return policy_id

    # ---- 查询 ----
    def _fetch(self, sql, params=None):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]

    def _scalar(self, sql, params=None):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).scalar()

    def _count(self, table):
        return self._scalar('SELECT COUNT(*) FROM "%s"' % table)

    # ---- 管线 ----
    def _preview(self, rows):
        with self.engine.connect() as conn:
            return policy_import.preview(conn, rows)

    def _create_validate(self, rows, dry_run=False):
        with self.engine.begin() as conn:
            created = policy_import.create_batch(
                conn, rows, source='unit-test', file_name='rows.json',
                operator='tester', dry_run=dry_run)
            report = policy_import.validate_batch(conn, created['batch_id'])
        return created['batch_id'], report

    def _commit_rows(self, rows):
        batch_id, _ = self._create_validate(rows)
        with self.engine.begin() as conn:
            report = policy_import.commit(conn, batch_id, 'tester')
        return batch_id, report

    def _status(self, batch_id):
        return self._scalar('SELECT "status" FROM "import_batch" WHERE "id" = :id',
                            {'id': batch_id})

    # ---- 文件 ----
    def _write(self, name, data, encoding=None):
        path = os.path.join(self.tmp.name, name)
        if isinstance(data, bytes):
            with open(path, 'wb') as handle:
                handle.write(data)
        else:
            with open(path, 'w', encoding=encoding or 'utf-8') as handle:
                handle.write(data)
        return path


class SwitchAndLoadingTest(ImportTestBase):
    def test_enabled_switch_default_off(self):
        original = os.environ.pop('POLICY_IMPORT', None)
        try:
            self.assertFalse(policy_import.enabled())
            for value in ('1', 'true', 'YES', 'on'):
                with mock.patch.dict(os.environ, {'POLICY_IMPORT': value}):
                    self.assertTrue(policy_import.enabled())
            with mock.patch.dict(os.environ, {'POLICY_IMPORT': '0'}):
                self.assertFalse(policy_import.enabled())
        finally:
            if original is not None:
                os.environ['POLICY_IMPORT'] = original

    def test_schema_is_idempotent(self):
        with self.engine.begin() as conn:
            policy_import.ensure_schema(conn)
            policy_import.ensure_schema(conn)
        columns = [row['name'] for row in self._fetch(
            "SELECT name FROM pragma_table_info('import_batch')")]
        self.assertIn('dry_run', columns)
        self.assertIn('status', columns)

    def test_load_csv_with_chinese_headers(self):
        content = ('政策编号,政策名称,层级,适用地区,申请条件,补贴标准,办理流程,文件文号,来源\n'
                   'X-ZB-001,测试政策,县级,莲花县,在莲注册,补贴1万,线上,莲府发〔2026〕1号,测试\n')
        rows = policy_import.load_rows(self._write('policies.csv', content))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['policy_code'], 'X-ZB-001')
        self.assertEqual(row['policy_name'], '测试政策')
        self.assertEqual(row['level'], '县级')
        self.assertEqual(row['regions'], '莲花县')
        self.assertEqual(row['conditions'], '在莲注册')
        self.assertEqual(row['benefit'], '补贴1万')
        self.assertEqual(row['process'], '线上')
        self.assertEqual(row['doc_ref'], '莲府发〔2026〕1号')
        self.assertEqual(row['source'], '测试')

    def test_load_csv_gb18030(self):
        content = '政策编号,政策名称,层级,适用地区,申请条件\nX-ZB-001,测试政策,县级,莲花县,在莲注册\n'
        path = self._write('gb.csv', content.encode('gb18030'))
        rows = policy_import.load_rows(path)
        self.assertEqual(rows[0]['policy_name'], '测试政策')

    def test_load_json_list_and_rows_object(self):
        rows = [self._row()]
        path = self._write('list.json', json.dumps(rows, ensure_ascii=False))
        self.assertEqual(policy_import.load_rows(path)[0]['policy_code'], 'X-ZB-001')
        path = self._write('wrapped.json', json.dumps({'rows': rows}, ensure_ascii=False))
        self.assertEqual(policy_import.load_rows(path)[0]['policy_name'], '测试政策')

    def test_load_xlsx(self):
        import openpyxl
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(['政策编号', '政策名称', '层级', '适用地区', '申请条件'])
        sheet.append(['X-ZB-001', '测试政策', '县级', '莲花县', '在莲注册'])
        path = os.path.join(self.tmp.name, 'policies.xlsx')
        workbook.save(path)
        rows = policy_import.load_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['policy_code'], 'X-ZB-001')
        self.assertEqual(rows[0]['regions'], '莲花县')

    def test_load_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            policy_import.load_rows(os.path.join(self.tmp.name, 'missing.csv'))
        with self.assertRaises(ValueError):
            policy_import.load_rows(self._write('policies.txt', 'x'))
        with self.assertRaises(ValueError):
            policy_import.load_rows(self._write('noheader.csv', '1,2,3\n4,5,6\n'))

    def test_normalize_keeps_provided_fields_only(self):
        normalized = policy_import.normalize_row({'政策编号': 'x-zb-001', '政策名称': '测试政策',
                                                  '层级': '县级'})
        self.assertEqual(normalized['policy_code'], 'x-zb-001')
        self.assertNotIn('regions', normalized)


class ValidatePreviewTest(ImportTestBase):
    def test_preview_counts_and_isolates_invalid_rows(self):
        rows = [
            self._row(),
            self._row(policy_code=''),
            self._row(policy_code='X-ZB-003', level='乡镇'),
            self._row(policy_code='X-ZB-004', level='S', regions=''),
            self._row(policy_code='X-ZB-005', level='X', regions='莲花县',
                      effect_date='2026-06-01', expire_date='2026-01-01'),
        ]
        report = self._preview(rows)
        self.assertEqual(report['total'], 5)
        self.assertEqual(report['valid'], 1)
        self.assertEqual(report['invalid'], 4)
        self.assertEqual(report['actions']['create'], 1)
        self.assertEqual(len(report['errors']), 4)
        self.assertEqual(self._count('policy'), 0)

    def test_preview_writes_no_business_rows(self):
        report = self._preview([self._row()])
        self.assertEqual(report['valid'], 1)
        self.assertEqual(self._count('policy'), 0)
        self.assertEqual(self._count('policy_detail'), 0)
        self.assertEqual(self._count('policy_version'), 0)
        self.assertEqual(self._count('import_batch'), 0)

    def test_national_policy_without_regions_defaults_to_nationwide(self):
        report = self._preview([self._row(level='国家', regions='', policy_code='C-ZB-001')])
        self.assertEqual(report['valid'], 1)
        self.assertEqual(report['actions']['create'], 1)
        warnings = report['warnings'][0]['warnings']
        self.assertTrue(any('全国' in item for item in warnings))

    def test_unresolved_region_is_warning_not_blocking(self):
        report = self._preview([self._row(regions='火星镇', policy_code='X-ZB-010')])
        self.assertEqual(report['valid'], 1)
        self.assertEqual(report['actions']['create'], 1)
        self.assertEqual(report['unresolved'][0]['raw_text'], '火星镇')

    def test_duplicate_codes_are_conflicts(self):
        rows = [self._row(policy_code='X-ZB-001'), self._row(policy_code='X-ZB-001')]
        report = self._preview(rows)
        self.assertEqual(report['valid'], 0)
        self.assertEqual(report['conflict'], 2)
        self.assertEqual(report['actions']['conflict'], 2)
        self.assertTrue(all('重复政策编号' in item for item in
                            [error for row in report['errors'] for error in row['errors']]))

    def test_existing_policy_update_and_noop(self):
        self._seed_policy(code='S-JX-001', source='unit-test')
        self._seed_policy(code='S-JX-002', name='另一政策', source='unit-test')
        unchanged = self._row(policy_code='S-JX-001', policy_name='现有政策', level='S',
                              regions='江西省', conditions='在江西注册', benefit='补贴',
                              process='线上')
        changed = self._row(policy_code='S-JX-002', policy_name='改名政策', level='S',
                            regions='江西省', conditions='在江西注册', benefit='补贴',
                            process='线上')
        report = self._preview([unchanged, changed])
        self.assertEqual(report['valid'], 2)
        self.assertEqual(report['actions']['noop'], 1)
        self.assertEqual(report['actions']['update'], 1)
        changed_fields = report['changed'][0]['changed_fields']
        self.assertEqual(changed_fields, ['policy_name'])

    def test_existing_policy_details_diff(self):
        self._seed_policy()
        report = self._preview([self._row(policy_code='S-JX-001', policy_name='现有政策',
                                          level='S', regions='江西省',
                                          conditions='新的申请条件')])
        changed = report['changed'][0]['changed_fields']
        self.assertIn('conditions', changed)
        self.assertIn('details', changed)

    def test_invalid_level_and_date(self):
        report = self._preview([
            self._row(policy_code='X-ZB-020', level='X', effect_date='2026/13/40'),
            self._row(policy_code='X-ZB-021', level='S', regions='江西省',
                      effect_date='2026-06-01', expire_date='2026-01-01'),
        ])
        self.assertEqual(report['invalid'], 2)
        self.assertEqual(report['valid'], 0)

    def test_status_alias_normalized(self):
        report = self._preview([
            self._row(policy_code='X-ZB-030', status='启用'),
            self._row(policy_code='X-ZB-031', status='废止'),
        ])
        self.assertEqual(report['valid'], 2)
        self.assertEqual(report['actions']['create'], 2)

    def test_details_json_parsed_and_validated(self):
        details = [{'地区': '莲花县', '申请条件': '条件A', '适用对象': '对象A', '补贴标准': '1000元'},
                   {'地区': '安源区', '申请条件': '条件B'}]
        report = self._preview([self._row(policy_code='X-ZB-040',
                                          details=json.dumps(details, ensure_ascii=False))])
        self.assertEqual(report['valid'], 1)
        broken = self._preview([self._row(policy_code='X-ZB-041', details='not-json')])
        self.assertEqual(broken['invalid'], 1)
        missing = self._preview([self._row(
            policy_code='X-ZB-042', details=json.dumps([{'地区': '莲花县'}]))])
        self.assertEqual(missing['invalid'], 1)

    def test_multiple_existing_rows_same_code_invalid(self):
        self._seed_policy(code='S-JX-001', name='政策A')
        self._seed_policy(code='S-JX-001', name='政策B')
        report = self._preview([self._row(policy_code='S-JX-001', policy_name='政策A',
                                          level='S', regions='江西省')])
        self.assertEqual(report['invalid'], 1)
        self.assertIn('同编号', report['errors'][0]['errors'][0])


class CommitTest(ImportTestBase):
    def test_commit_creates_policy_version_scope_outbox(self):
        details = [{'地区': '莲花县', '申请条件': '条件A', '适用对象': '对象A'},
                   {'地区': '安源区', '申请条件': '条件B'}]
        batch_id, report = self._commit_rows([self._row(
            details=json.dumps(details, ensure_ascii=False))])
        self.assertEqual(report['status'], 'committed')
        self.assertEqual(report['changed'], 1)
        self.assertEqual(report['created'], 1)
        self.assertEqual(self._status(batch_id), 'committed')

        policies = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                               {'code': 'X-ZB-001'})
        self.assertEqual(len(policies), 1)
        policy = policies[0]
        self.assertEqual(policy['policy_name'], '测试政策')
        self.assertEqual(policy['level'], 'X')
        self.assertEqual(policy['regions'], '莲花县')
        self.assertEqual(policy['status'], '在用')
        self.assertEqual(policy['cur_version'], 'v1')
        self.assertEqual(policy['source'], 'unit-test')
        self.assertEqual(policy['updated_by'], 'tester')

        self.assertEqual(self._count('policy_detail'), 2)
        versions = self._fetch('SELECT * FROM "policy_version" WHERE "policy_id" = :pid',
                               {'pid': policy['policy_id']})
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]['version_no'], 'v1')
        snapshot = json.loads(versions[0]['content_snapshot'])
        self.assertEqual(snapshot['policy_code'], 'X-ZB-001')
        self.assertEqual(len(snapshot['details']), 2)

        scope_rows = self._fetch('SELECT * FROM "policy_scope" WHERE "policy_code" = :code',
                                 {'code': 'X-ZB-001'})
        self.assertTrue(scope_rows)
        self.assertTrue(any('江西' in (row.get('province') or '') for row in scope_rows))

        outbox = self._fetch('SELECT "event_type" FROM "outbox"')
        self.assertEqual(len(outbox), 3)
        self.assertEqual({row['event_type'] for row in outbox},
                         set(policy_import.OUTBOX_EVENTS))
        self.assertGreaterEqual(self._count('audit_log'), 1)

    def test_commit_update_writes_old_snapshot(self):
        self._seed_policy()
        _, report = self._commit_rows([self._row(
            policy_code='S-JX-001', policy_name='改名政策', level='S', regions='江西省',
            conditions='新的申请条件', target='小微企业', benefit='补贴')])
        self.assertEqual(report['updated'], 1)
        self.assertEqual(report['created'], 0)

        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'S-JX-001'})[0]
        self.assertEqual(policy['policy_name'], '改名政策')
        self.assertEqual(policy['conditions'], '新的申请条件')
        self.assertEqual(policy['cur_version'], 'v2')
        versions = self._fetch('SELECT * FROM "policy_version" WHERE "policy_id" = :pid '
                               'ORDER BY "version_id"', {'pid': policy['policy_id']})
        self.assertEqual(len(versions), 2)
        self.assertIn('导入批次', versions[-1]['note'])
        snapshot = json.loads(versions[-1]['content_snapshot'])
        self.assertEqual(snapshot['policy_name'], '现有政策')
        details = self._fetch('SELECT * FROM "policy_detail" WHERE "policy_id" = :pid',
                              {'pid': policy['policy_id']})
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]['conditions'], '新的申请条件')

    def test_second_commit_is_idempotent(self):
        batch_id, _ = self._commit_rows([self._row()])
        counts = (self._count('policy'), self._count('policy_version'),
                  self._count('outbox'))
        with self.engine.begin() as conn:
            report = policy_import.commit(conn, batch_id, 'tester')
        self.assertTrue(report['idempotent'])
        self.assertEqual(report['changed'], 0)
        self.assertEqual(counts, (self._count('policy'), self._count('policy_version'),
                                  self._count('outbox')))

    def test_detail_dates_persisted_and_reimport_is_noop(self):
        rows = [self._row(effect_date='2026-06-01', expire_date='2027-05-31')]
        _, report = self._commit_rows(rows)
        self.assertEqual(report['created'], 1)
        pid = self._scalar('SELECT "policy_id" FROM "policy" WHERE "policy_code" = :code',
                           {'code': 'X-ZB-001'})
        detail = self._fetch('SELECT * FROM "policy_detail" WHERE "policy_id" = :pid',
                             {'pid': pid})[0]
        self.assertEqual(detail['effect_date'], '2026-06-01')
        self.assertEqual(detail['expire_date'], '2027-05-31')
        # 同一内容再导一次：明细签名含效期，落库后必须对得上，否则会无谓升版本。
        _, report2 = self._commit_rows(rows)
        self.assertEqual(report2['changed'], 0)
        self.assertEqual(report2['noop'], 1)
        self.assertEqual(self._count('policy_version'), 1)

    def test_commit_noop_changes_nothing(self):
        self._seed_policy(source='unit-test')
        _, report = self._commit_rows([self._row(
            policy_code='S-JX-001', policy_name='现有政策', level='S', regions='江西省',
            conditions='在江西注册', benefit='补贴', process='线上')])
        self.assertEqual(report['changed'], 0)
        self.assertEqual(report['noop'], 1)
        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'S-JX-001'})[0]
        self.assertEqual(policy['cur_version'], 'v1')
        self.assertEqual(self._count('policy_version'), 1)

    def test_conflict_rows_never_written(self):
        rows = [self._row(policy_code='X-ZB-001'),
                self._row(policy_code='X-ZB-001'),
                self._row(policy_code='X-ZB-002')]
        _, report = self._commit_rows(rows)
        self.assertEqual(report['valid'], 1)
        self.assertEqual(report['conflict'], 2)
        self.assertEqual(report['created'], 1)
        codes = [row['policy_code'] for row in self._fetch('SELECT "policy_code" FROM "policy"')]
        self.assertEqual(codes, ['X-ZB-002'])

    def test_commit_staging_auto_validates(self):
        with self.engine.begin() as conn:
            created = policy_import.create_batch(conn, [self._row()], source='auto',
                                                 file_name='auto.json', operator='tester')
        with self.engine.begin() as conn:
            report = policy_import.commit(conn, created['batch_id'], 'tester')
        self.assertEqual(report['changed'], 1)
        self.assertEqual(report['status'], 'committed')

    def test_dry_run_batch_cannot_commit(self):
        batch_id, _ = self._create_validate([self._row()], dry_run=True)
        with self.assertRaises(ValueError):
            with self.engine.begin() as conn:
                policy_import.commit(conn, batch_id, 'tester')
        self.assertEqual(self._count('policy'), 0)

    def test_national_and_city_levels_committed(self):
        rows = [self._row(policy_code='C-ZB-001', level='国家', regions=''),
                self._row(policy_code='S-ZB-001', level='省级', regions='江西省'),
                self._row(policy_code='U-ZB-001', level='市级', regions='南昌市'),
                self._row(policy_code='X-ZB-001', level='县级', regions='莲花县')]
        _, report = self._commit_rows(rows)
        self.assertEqual(report['created'], 4)
        levels = {row['level'] for row in self._fetch('SELECT "level" FROM "policy"')}
        self.assertEqual(levels, {'C', 'S', 'U', 'X'})
        national = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                               {'code': 'C-ZB-001'})[0]
        self.assertEqual(national['regions'], '全国')

    def test_dates_and_status_normalized_in_db(self):
        _, report = self._commit_rows([self._row(
            policy_code='X-ZB-050', status='启用',
            effect_date='2026/01/05', expire_date='2027-12-31')])
        self.assertEqual(report['created'], 1)
        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'X-ZB-050'})[0]
        self.assertEqual(policy['status'], '在用')
        self.assertEqual(policy['effect_date'], '2026-01-05')
        self.assertEqual(policy['expire_date'], '2027-12-31')

    def test_unresolved_region_preserved_for_manual_review(self):
        _, report = self._commit_rows([self._row(policy_code='X-ZB-060', regions='火星镇')])
        self.assertEqual(report['created'], 1)
        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'X-ZB-060'})[0]
        self.assertEqual(policy['regions'], '火星镇')
        scope_rows = self._fetch('SELECT * FROM "policy_scope" WHERE "policy_code" = :code',
                                 {'code': 'X-ZB-060'})
        self.assertTrue(scope_rows)
        self.assertTrue(any(row['scope_level'] == admin_division.UNKNOWN and
                            (row.get('region_key') or '').startswith('U:')
                            for row in scope_rows))

    def test_batch_report_lists_plan(self):
        batch_id, _ = self._create_validate([self._row(policy_code='X-ZB-070'),
                                             self._row(policy_code='X-ZB-070')])
        with self.engine.connect() as conn:
            report = policy_import.batch_report(conn, batch_id)
        self.assertEqual(report['status'], 'validated')
        self.assertEqual(report['total'], 2)
        self.assertEqual(report['actions']['conflict'], 2)
        self.assertEqual(len(report['rows']), 2)


class RollbackTest(ImportTestBase):
    def _rollback(self, batch_id):
        with self.engine.begin() as conn:
            return policy_import.rollback(conn, batch_id, 'tester')

    def test_rollback_create_removes_new_rows(self):
        batch_id, _ = self._commit_rows([self._row()])
        self.assertEqual(self._count('policy'), 1)
        report = self._rollback(batch_id)
        self.assertEqual(report['removed'], 1)
        self.assertEqual(report['restored'], 0)
        self.assertEqual(self._count('policy'), 0)
        self.assertEqual(self._count('policy_detail'), 0)
        self.assertEqual(self._count('policy_version'), 0)
        self.assertEqual(self._count('policy_scope'), 0)
        self.assertEqual(self._status(batch_id), 'rolled_back')
        payloads = [row['payload'] for row in self._fetch('SELECT "payload" FROM "outbox"')]
        self.assertTrue(any('rollback_delete' in (payload or '') for payload in payloads))

    def test_rollback_update_restores_previous_version(self):
        self._seed_policy()
        batch_id, _ = self._commit_rows([self._row(
            policy_code='S-JX-001', policy_name='改名政策', level='S', regions='江西省',
            conditions='新的申请条件', target='小微企业', benefit='补贴')])
        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'S-JX-001'})[0]
        self.assertEqual(policy['cur_version'], 'v2')
        report = self._rollback(batch_id)
        self.assertEqual(report['restored'], 1)
        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'S-JX-001'})[0]
        self.assertEqual(policy['policy_name'], '现有政策')
        self.assertEqual(policy['conditions'], '在江西注册')
        self.assertEqual(policy['cur_version'], 'v1')
        details = self._fetch('SELECT * FROM "policy_detail" WHERE "policy_id" = :pid',
                              {'pid': policy['policy_id']})
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]['conditions'], '在江西注册')
        scope_rows = self._fetch('SELECT * FROM "policy_scope" WHERE "policy_code" = :code',
                                 {'code': 'S-JX-001'})
        self.assertTrue(any('江西' in (row.get('province') or '') for row in scope_rows))
        self.assertEqual(self._status(batch_id), 'rolled_back')

    def test_rollback_update_skipped_when_changed_after_batch(self):
        self._seed_policy()
        batch_id, _ = self._commit_rows([self._row(
            policy_code='S-JX-001', policy_name='改名政策', level='S', regions='江西省',
            conditions='新的申请条件', target='小微企业', benefit='补贴')])
        # 模拟批次后的正式更新：版本号推进到 v3（真实变更都会走版本链）。
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE "policy" SET "policy_name" = :name, '
                              '"cur_version" = :version WHERE "policy_code" = :code'),
                         {'name': '人工又改', 'version': 'v3', 'code': 'S-JX-001'})
        report = self._rollback(batch_id)
        self.assertEqual(report['restored'], 0)
        self.assertEqual(report['skipped_count'], 1)
        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'S-JX-001'})[0]
        self.assertEqual(policy['policy_name'], '人工又改')
        self.assertEqual(self._status(batch_id), 'rolled_back')

    def test_rollback_create_skipped_when_extra_version(self):
        batch_id, _ = self._commit_rows([self._row()])
        policy = self._fetch('SELECT * FROM "policy" WHERE "policy_code" = :code',
                             {'code': 'X-ZB-001'})[0]
        with self.engine.begin() as conn:
            conn.execute(text('INSERT INTO "policy_version" ("policy_id","version_no",'
                              '"content_snapshot","note") VALUES (:pid,:v,:snap,:note)'),
                         {'pid': policy['policy_id'], 'v': 'v2', 'snap': '{}',
                          'note': '后续人工版本'})
        report = self._rollback(batch_id)
        self.assertEqual(report['removed'], 0)
        self.assertEqual(report['skipped_count'], 1)
        self.assertEqual(self._count('policy'), 1)

    def test_rollback_rejects_uncommitted_batch(self):
        batch_id, _ = self._create_validate([self._row()])
        with self.assertRaises(ValueError):
            self._rollback(batch_id)

    def test_rollback_twice_rejected(self):
        batch_id, _ = self._commit_rows([self._row()])
        self._rollback(batch_id)
        with self.assertRaises(ValueError):
            self._rollback(batch_id)


class CliTest(unittest.TestCase):
    """CLI 端到端：临时 sqlite 文件库 + 子进程，不接触生产库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, 'import.db')
        self.db_url = 'sqlite:///' + self.db_path
        engine = create_engine(self.db_url)
        with engine.begin() as conn:
            for ddl in DDLS:
                conn.execute(text(ddl))
            policy_import.ensure_schema(conn)
            policy_scope.ensure_schema(conn)
        engine.dispose()

    def _fetch(self, sql):
        engine = create_engine(self.db_url)
        try:
            with engine.connect() as conn:
                return [dict(row) for row in conn.execute(text(sql)).mappings().all()]
        finally:
            engine.dispose()

    def _count(self, table):
        return self._fetch('SELECT COUNT(*) AS n FROM "%s"' % table)[0]['n']

    def _write_csv(self, name, lines):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w', encoding='utf-8-sig') as handle:
            handle.write('\n'.join(lines) + '\n')
        return path

    def _env(self, switch='1'):
        env = dict(os.environ)
        env['POLICY_IMPORT'] = switch
        env['ADMIN_DB_URL'] = self.db_url
        env['POLICY_ENV_FILE'] = os.path.join(self.tmp.name, 'no-such.env')
        env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + str(ROOT)
        return env

    def _run(self, args, switch='1'):
        return subprocess.run(
            [PYTHON, str(ROOT / 'deploy' / 'import_policies.py')] + args,
            capture_output=True, text=True, cwd=str(ROOT), env=self._env(switch),
            timeout=120)

    def _csv(self, name='policies.csv'):
        return self._write_csv(name, [
            '政策编号,政策名称,层级,适用地区,申请条件,补贴标准',
            'X-ZB-001,CLI测试政策,县级,莲花县,在莲注册,补贴1万',
        ])

    def test_cli_switch_off_refuses_to_run(self):
        proc = self._run(['--file', self._csv()], switch='0')
        self.assertEqual(proc.returncode, 2)
        self.assertIn('POLICY_IMPORT', proc.stderr)
        self.assertEqual(self._count('policy'), 0)

    def test_cli_dry_run_is_default_and_writes_nothing(self):
        proc = self._run(['--file', self._csv(), '--json'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['mode'], 'dry_run')
        self.assertEqual(report['valid'], 1)
        self.assertEqual(report['actions']['create'], 1)
        self.assertEqual(self._count('policy'), 0)
        self.assertEqual(self._count('import_batch'), 0)

    def test_cli_commit_report_rollback_flow(self):
        proc = self._run(['--file', self._csv(), '--commit', '--json'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['status'], 'committed')
        self.assertEqual(report['created'], 1)
        batch_id = report['batch_id']
        self.assertEqual(self._count('policy'), 1)
        self.assertEqual(self._count('policy_version'), 1)
        self.assertEqual(self._count('outbox'), 3)

        proc = self._run(['--batch-id', batch_id, '--report', '--json'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'], 'committed')

        proc = self._run(['--batch-id', batch_id, '--rollback', '--json'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['status'], 'rolled_back')
        self.assertEqual(report['removed'], 1)
        self.assertEqual(self._count('policy'), 0)

        proc = self._run(['--batch-id', batch_id, '--report', '--json'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'], 'rolled_back')

    def test_cli_invalid_arguments(self):
        proc = self._run(['--commit', '--rollback', '--file', self._csv()])
        self.assertEqual(proc.returncode, 1)
        proc = self._run(['--report'])
        self.assertEqual(proc.returncode, 1)

    def test_cli_missing_file_reports_error(self):
        proc = self._run(['--file', os.path.join(self.tmp.name, 'missing.csv')])
        self.assertEqual(proc.returncode, 1)
        self.assertIn('ERROR', proc.stderr)

    def test_cli_dry_run_reports_invalid_rows(self):
        path = self._write_csv('bad.csv', [
            '政策编号,政策名称,层级,适用地区',
            'X-ZB-002,坏行,乡镇,莲花县',
        ])
        proc = self._run(['--file', path, '--json'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['valid'], 0)
        self.assertEqual(report['invalid'], 1)
        self.assertEqual(self._count('policy'), 0)


if __name__ == '__main__':
    unittest.main()
