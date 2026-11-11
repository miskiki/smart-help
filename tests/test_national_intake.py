# -*- coding: utf-8 -*-
"""T24 全国政策数据接入 CLI 单测。

全部离线运行：CSV/XLSX/JSON 都写在临时目录，数据库只用临时 sqlite 文件，
不连生产库、不写生产数据；--env-file 一律指向临时空文件，避免读到
deploy/web.env。数据库相关断言通过临时 sqlite 复查表行验证。
"""
import contextlib
import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

from policy_core import policy_import, policy_scope

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    'national_intake_t24', ROOT / 'deploy' / 'national_intake.py')
intake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(intake)

# 与 tests/test_policy_import.py 保持一致的 sqlite DDL（不连生产库）。
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

NATIONAL_HEADERS = [
    '政策编号', '政策名称', '政策层级', '政策所属行政区域', '政策适用条件',
    '政策适用对象', '补贴标准', '补贴单位', '补贴周期', '政策生效日期',
    '政策失效日期', '政策制定主体部门', '政策实施部门',
]


def national_row(code='TEST-001', name='莲花县就业帮扶补贴', level='县级',
                 region='莲花县', conditions='脱贫人口且稳定就业',
                 target='脱贫劳动力', benefit='1000', unit='元', cycle='月',
                 effect='2026-01-01', expire='2026-12-31',
                 issuer='县人社局', implementer='县就业局'):
    return [code, name, level, region, conditions, target, benefit, unit,
            cycle, effect, expire, issuer, implementer]


class IntakeTestBase(unittest.TestCase):
    ENV_KEYS = ('ADMIN_DB_URL', 'POLICY_IMPORT', 'NATIONAL_INTAKE_DIR',
                'POLICY_IMPORT_OPERATOR', 'NATIONAL_INTAKE_SOURCE',
                'POLICY_ENV_FILE')

    def setUp(self):
        self._saved_env = {key: os.environ.pop(key, None)
                           for key in self.ENV_KEYS}
        policy_import._ENGINE = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.incoming = self.root / 'incoming'
        self.incoming.mkdir()
        self.empty_env = self.root / 'empty.env'
        self.empty_env.write_text('', encoding='utf-8')
        # 默认扫描临时目录，避免测试碰到仓库 data/incoming 或生产环境变量。
        os.environ['NATIONAL_INTAKE_DIR'] = str(self.incoming)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        policy_import._ENGINE = None
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- 调用入口 ----
    def run_intake(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = intake.main(list(argv) + ['--env-file', str(self.empty_env)])
        return code, out.getvalue(), err.getvalue()

    def run_json(self, argv):
        code, out, err = self.run_intake(list(argv) + ['--json'])
        report = json.loads(out)  # --json 模式 stdout 必须是完整 JSON 文档
        return code, report, err

    # ---- 构造输入 ----
    def write_csv(self, name, headers, rows, encoding='utf-8-sig'):
        path = self.incoming / name
        with path.open('w', encoding=encoding, newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def write_national_csv(self, name='national.csv', rows=None):
        return self.write_csv(name, NATIONAL_HEADERS,
                              rows if rows is not None else [national_row()])

    # ---- sqlite ----
    def make_db(self):
        db_path = self.root / 'intake.sqlite'
        engine = create_engine('sqlite:///' + str(db_path))
        try:
            with engine.begin() as conn:
                for ddl in DDLS:
                    conn.execute(text(ddl))
                policy_import.ensure_schema(conn)
                policy_scope.ensure_schema(conn)
        finally:
            engine.dispose()
        os.environ['ADMIN_DB_URL'] = 'sqlite:///' + str(db_path)
        policy_import._ENGINE = None
        return db_path

    def db_scalar(self, db_path, sql, params=None):
        engine = create_engine('sqlite:///' + str(db_path))
        try:
            with engine.connect() as conn:
                return conn.execute(text(sql), params or {}).scalar()
        finally:
            engine.dispose()

    def db_rows(self, db_path, sql, params=None):
        engine = create_engine('sqlite:///' + str(db_path))
        try:
            with engine.connect() as conn:
                return conn.execute(text(sql), params or {}).fetchall()
        finally:
            engine.dispose()


class FieldMappingTest(IntakeTestBase):
    def test_national_chinese_headers_normalized(self):
        row = national_row()
        self.write_national_csv(rows=[row])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_total'], 1)
        self.assertEqual(report['rows_ok'], 1)
        self.assertEqual(report['level_distribution']['X'], 1)
        self.assertEqual(report['covered_provinces'], 1)
        self.assertEqual(report['provinces'], ['江西省'])

    def test_english_aliases(self):
        headers = ['policy_code', 'policy_name', 'level', 'region',
                   'conditions', 'target', 'benefit', 'effect_date',
                   'expire_date']
        row = ['EN-001', '英文列政策', '省级', '江西省', '条件A', '对象A',
               '2000', '2026-01-01', '2026-12-31']
        self.write_csv('english.csv', headers, [row])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_ok'], 1)
        self.assertEqual(report['level_distribution']['S'], 1)

    def test_ignored_headers_not_error(self):
        headers = NATIONAL_HEADERS + ['年份', '享受户数', '帮扶方式', '最高限额/元']
        row = national_row() + ['2026', '100', '就业帮扶', '5000']
        self.write_csv('extra.csv', headers, [row])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_total'], 1)
        self.assertEqual(report['rows_ok'], 1)

    def test_map_to_import_content_fallback_and_departments(self):
        raw, warnings = intake._map_to_import({
            'policy_code': 'M-001', 'policy_name': '内容兜底政策', 'level': '县级',
            'regions': '莲花县', 'content': '政策正文内容',
            'issuer': '县人社局', 'implementer': '县就业局',
        })
        self.assertEqual(raw['conditions'], '政策正文内容')
        self.assertIn('制定部门：县人社局', raw['level_note'])
        self.assertIn('实施部门：县就业局', raw['level_note'])
        self.assertEqual(raw['details'][0]['conditions'], '政策正文内容')
        self.assertTrue(any('作为申请条件兜底' in item for item in warnings))

    def test_map_to_import_keeps_conditions_over_content(self):
        raw, warnings = intake._map_to_import({
            'policy_code': 'M-002', 'policy_name': '两份都有', 'level': '县级',
            'regions': '莲花县', 'content': '正文', 'conditions': '正式条件',
            'target': '对象A',
        })
        self.assertEqual(raw['conditions'], '正式条件')
        self.assertTrue(any('政策内容未导入' in item for item in warnings))

    def test_map_to_import_no_details_without_conditions_target(self):
        raw, _warnings = intake._map_to_import({
            'policy_code': 'M-003', 'policy_name': '只有补贴', 'level': '县级',
            'regions': '莲花县', 'benefit': '500 元',
        })
        self.assertNotIn('details', raw)

    def test_cell_text_normalizes_number_and_date(self):
        self.assertEqual(intake._cell_text(5000.0), '5000')
        self.assertEqual(intake._cell_text(' 文本 '), '文本')
        self.assertEqual(intake._cell_text(None), '')


class HeaderAndParsingTest(IntakeTestBase):
    def test_title_row_before_header_preserves_physical_row_no(self):
        path = self.incoming / 'title.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['江西省 2026 年全国政策数据表'])
            writer.writerow(NATIONAL_HEADERS)
            writer.writerow(national_row())
            bad = national_row(code='TEST-002', name='')
            writer.writerow(bad)
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 1, err)
        self.assertEqual(report['rows_total'], 2)
        self.assertEqual(report['errors'][0]['row_no'], 4)
        self.assertIn('政策名称缺失', '；'.join(report['errors'][0]['errors']))

    def test_missing_header_is_file_error(self):
        self.write_csv('noheader.csv', ['政策内容', '备注'], [['正文', 'x']])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 1, err)
        self.assertEqual(report['file_errors'][0]['file'], 'noheader.csv')
        self.assertIn('未找到表头', report['file_errors'][0]['error'])
        self.assertEqual(report['rows_total'], 0)

    def test_duplicate_header_columns_first_wins_with_warning(self):
        headers = NATIONAL_HEADERS + ['名称']
        row = national_row() + ['重复列名称']
        self.write_csv('dupheader.csv', headers, [row])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_ok'], 1)
        self.assertTrue(any('重复映射' in item.get('warnings', [''])[0]
                            for item in report['warnings'] if item.get('warnings')))

    def test_blank_rows_skipped(self):
        path = self.incoming / 'blank.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(NATIONAL_HEADERS)
            writer.writerow(national_row())
            writer.writerow([''] * len(NATIONAL_HEADERS))
            writer.writerow(national_row(code='TEST-002', name='政策二'))
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_total'], 2)

    def test_gb18030_encoding(self):
        path = self.incoming / 'gbk.csv'
        lines = [','.join(NATIONAL_HEADERS), ','.join(national_row())]
        path.write_bytes(('\n'.join(lines) + '\n').encode('gb18030'))
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_ok'], 1)

    def test_json_input_rows(self):
        payload = [
            {'policy_code': 'J-001', 'policy_name': 'JSON政策', 'level': '省级',
             'regions': '江西省', 'conditions': '条件', 'target': '对象',
             'effect_date': '2026-01-01', 'expire_date': '2026-12-31'},
        ]
        (self.incoming / 'rows.json').write_text(
            json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_ok'], 1)
        self.assertEqual(report['level_distribution']['S'], 1)

    def test_xlsx_input(self):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '全国政策'
        sheet.append(NATIONAL_HEADERS)
        sheet.append(national_row())
        path = self.incoming / 'national.xlsx'
        workbook.save(path)
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_total'], 1)
        self.assertEqual(report['rows_ok'], 1)


class ValidationReportTest(IntakeTestBase):
    def test_missing_required_field_reports_file_and_row(self):
        self.write_national_csv(rows=[national_row(name='')])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 1, err)
        self.assertEqual(report['rows_error'], 1)
        item = report['errors'][0]
        self.assertEqual(item['file'], 'national.csv')
        self.assertEqual(item['row_no'], 2)
        self.assertIn('政策名称缺失', '；'.join(item['errors']))

    def test_invalid_level_reported(self):
        self.write_national_csv(rows=[national_row(level='镇级')])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 1, err)
        self.assertEqual(report['rows_error'], 1)
        self.assertIn('层级必须为', '；'.join(report['errors'][0]['errors']))

    def test_level_chinese_aliases(self):
        rows = [national_row(code='L-1', level='国家级', region='全国'),
                national_row(code='L-2', level='省级', region='江西省'),
                national_row(code='L-3', level='市级', region='赣州市'),
                national_row(code='L-4', level='县级', region='莲花县'),
                national_row(code='L-5', level='县区级', region='莲花县')]
        self.write_national_csv(rows=rows)
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['level_distribution'],
                         {'C': 1, 'S': 1, 'U': 1, 'X': 2, 'unknown': 0})

    def test_region_distribution_buckets(self):
        rows = [national_row(code='R-1', level='国家级', region='全国'),
                national_row(code='R-2', level='省级', region='江西省'),
                national_row(code='R-3', level='市级', region='赣州市'),
                national_row(code='R-4', level='县级', region='莲花县'),
                national_row(code='R-5', level='县级', region='南昌经济技术开发区'),
                national_row(code='R-6', level='县级', region='火星镇')]
        self.write_national_csv(rows=rows)
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['region_distribution'],
                         {'nation': 1, 'province': 1, 'city': 1, 'county': 1,
                          'zone': 1, 'unknown': 1})
        # 只有江西省能确认省份；火星镇以 U: 键保留进 unresolved 提醒。
        self.assertEqual(report['covered_provinces'], 1)
        self.assertEqual(report['provinces'], ['江西省'])
        self.assertTrue(any(item['raw_text'] == '火星镇'
                            for item in report['unresolved']))

    def test_duplicate_code_within_file_isolated(self):
        rows = [national_row(code='DUP-1', name='政策甲'),
                national_row(code='DUP-1', name='政策乙')]
        self.write_national_csv(rows=rows)
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 1, err)
        self.assertEqual(report['rows_error'], 2)
        self.assertEqual(report['duplicate_codes'], 1)
        self.assertEqual(report['duplicate_code_list'][0]['code'], 'DUP-1')
        for item in report['errors']:
            self.assertIn('同批次重复政策编号', '；'.join(item['errors']))

    def test_duplicate_code_across_files_is_warning_only(self):
        self.write_national_csv('a.csv', [national_row(code='CROSS-1')])
        self.write_national_csv('b.csv', [national_row(code='CROSS-1',
                                                       region='赣州市',
                                                       level='市级')])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_ok'], 2)
        self.assertEqual(report['duplicate_codes'], 1)
        self.assertEqual(report['duplicate_code_list'][0]['count'], 2)
        self.assertEqual(len(report['duplicate_code_list'][0]['files']), 2)
        self.assertTrue(any('多个文件' in text
                            for item in report['warnings']
                            for text in item.get('warnings', [])))

    def test_date_conflict_expire_before_effect(self):
        self.write_national_csv(rows=[national_row(effect='2026-01-01',
                                                   expire='2025-12-31')])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 1, err)
        self.assertEqual(report['date_conflicts'], 1)
        self.assertEqual(report['date_conflict_list'][0]['kind'],
                         'expire_before_effect')
        self.assertIn('失效日期不能早于生效日期',
                      '；'.join(report['errors'][0]['errors']))

    def test_date_conflict_same_code_inconsistent(self):
        self.write_national_csv('a.csv', [national_row(code='DATE-1',
                                                       effect='2026-01-01')])
        self.write_national_csv('b.csv', [national_row(code='DATE-1',
                                                       effect='2026-02-01')])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_ok'], 2)
        kinds = [item['kind'] for item in report['date_conflict_list']]
        self.assertIn('code_dates_inconsistent', kinds)

    def test_empty_file_ok(self):
        self.write_national_csv(rows=[])
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['rows_total'], 0)
        self.assertEqual(report['files_total'], 1)


class ScanModeTest(IntakeTestBase):
    def test_scan_dir_from_env_and_ignore_hidden_temp(self):
        target = self.root / 'shared'
        target.mkdir()
        with (target / 'ok.csv').open('w', encoding='utf-8-sig',
                                      newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(NATIONAL_HEADERS)
            writer.writerow(national_row())
        (target / '.hidden.csv').write_text('x', encoding='utf-8')
        (target / '~$tmp.xlsx').write_text('x', encoding='utf-8')
        (target / 'notes.txt').write_text('x', encoding='utf-8')
        os.environ['NATIONAL_INTAKE_DIR'] = str(target)
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['files_total'], 1)
        self.assertEqual(report['intake_dir'], str(target.resolve()))

    def test_auto_create_dir_and_readme(self):
        target = self.root / 'brand-new'
        os.environ['NATIONAL_INTAKE_DIR'] = str(target)
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['files_total'], 0)
        self.assertTrue(target.is_dir())
        readme = target / 'README.md'
        self.assertTrue(readme.is_file())
        self.assertIn('全国政策数据接入', readme.read_text(encoding='utf-8'))

    def test_multiple_file_arguments(self):
        first = self.write_national_csv('one.csv', [national_row(code='F-1')])
        second = self.write_national_csv('two.csv', [national_row(code='F-2')])
        code, report, err = self.run_json(
            ['--offline', '--file', str(first), '--file', str(second)])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['files_total'], 2)
        self.assertEqual(report['rows_ok'], 2)

    def test_missing_file_exit_2(self):
        code, out, err = self.run_intake(
            ['--offline', '--file', str(self.root / 'nope.csv')])
        self.assertEqual(code, 2)
        self.assertIn('文件不存在', err)

    def test_dir_and_file_conflict_exit_2(self):
        path = self.write_national_csv()
        code, out, err = self.run_intake(
            ['--offline', '--file', str(path), '--dir', str(self.incoming)])
        self.assertEqual(code, 2)
        self.assertIn('不能同时使用', err)

    def test_offline_and_apply_conflict_exit_2(self):
        code, out, err = self.run_intake(['--offline', '--apply'])
        self.assertEqual(code, 2)
        self.assertIn('不能同时使用', err)

    def test_unsupported_extension_reported(self):
        path = self.root / 'policy.txt'
        path.write_text('x', encoding='utf-8')
        code, report, err = self.run_json(['--offline', '--file', str(path)])
        self.assertEqual(code, 1, err)
        self.assertIn('不支持的文件类型', report['file_errors'][0]['error'])

    def test_no_files_json_report_shape(self):
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['version'], 'national-intake-1')
        self.assertEqual(report['mode'], 'offline')
        self.assertEqual(report['db']['status'], 'offline')
        self.assertFalse(report['write'])
        self.assertIn('未写库', report['note'])

    def test_text_report_prints_counts_and_not_written(self):
        self.write_national_csv()
        code, out, err = self.run_intake(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertIn('未写库', out)
        self.assertIn('行数', out)
        self.assertIn('覆盖省份', out)
        self.assertIn('文件明细', out)


class OfflineBehaviorTest(IntakeTestBase):
    def test_offline_does_not_require_database(self):
        os.environ['ADMIN_DB_URL'] = 'sqlite:///' + str(
            self.root / 'missing-dir' / 'db.sqlite')
        self.write_national_csv()
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['db']['status'], 'offline')

    def test_json_output_is_machine_readable(self):
        self.write_national_csv()
        code, report, err = self.run_json(['--offline'])
        self.assertEqual(code, 0, err)
        for key in ('version', 'mode', 'files_total', 'rows_total', 'rows_ok',
                    'rows_error', 'level_distribution', 'region_distribution',
                    'covered_provinces', 'duplicate_codes', 'date_conflicts',
                    'errors', 'warnings', 'unresolved', 'db', 'note'):
            self.assertIn(key, report)
        self.assertEqual(report['mode'], 'offline')


class DatabaseModeTest(IntakeTestBase):
    def test_apply_requires_switch(self):
        self.write_national_csv()
        code, out, err = self.run_intake(['--apply'])
        self.assertEqual(code, 2)
        self.assertIn('POLICY_IMPORT', err)

    def test_database_unavailable_falls_back_offline_exit_4(self):
        os.environ['ADMIN_DB_URL'] = 'sqlite:///' + str(
            self.root / 'missing-dir' / 'db.sqlite')
        self.write_national_csv()
        code, report, err = self.run_json([])
        self.assertEqual(code, 4, err)
        self.assertEqual(report['db']['status'], 'unavailable')
        self.assertIn('未写库', report['note'])
        self.assertIn('数据库不可用', err)

    def test_dry_run_does_not_write(self):
        db_path = self.make_db()
        self.write_national_csv()
        code, report, err = self.run_json([])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['mode'], 'dry_run')
        self.assertEqual(report['db']['status'], 'ok')
        self.assertFalse(report['write'])
        self.assertEqual(report['actions']['create'], 1)
        self.assertEqual(self.db_scalar(db_path, 'SELECT COUNT(*) FROM "policy"'), 0)
        self.assertEqual(
            self.db_scalar(db_path, 'SELECT COUNT(*) FROM "import_batch"'), 0)

    def test_apply_refused_when_offline_errors(self):
        db_path = self.make_db()
        os.environ['POLICY_IMPORT'] = '1'
        self.write_national_csv(rows=[national_row(code='OK-1'),
                                      national_row(code='BAD-1', name='')])
        code, report, err = self.run_json(['--apply'])
        self.assertEqual(code, 1, err)
        self.assertIn('存在错误行', err)
        self.assertIn('未写库', report['note'])
        self.assertEqual(self.db_scalar(db_path, 'SELECT COUNT(*) FROM "policy"'), 0)
        self.assertEqual(
            self.db_scalar(db_path, 'SELECT COUNT(*) FROM "import_batch"'), 0)

    def test_apply_end_to_end_then_idempotent(self):
        db_path = self.make_db()
        os.environ['POLICY_IMPORT'] = '1'
        self.write_national_csv()
        code, report, err = self.run_json(['--apply'])
        self.assertEqual(code, 0, err)
        self.assertTrue(report['write'])
        self.assertEqual(report['note'], '已写库')
        self.assertEqual(len(report['applied']), 1)
        self.assertEqual(report['applied'][0]['created'], 1)

        policy = self.db_rows(
            db_path,
            'SELECT "policy_code","policy_name","level","regions","conditions",'
            '"target","benefit","effect_date","expire_date","level_note",'
            '"status","cur_version","source" FROM "policy"')
        self.assertEqual(len(policy), 1)
        row = policy[0]
        self.assertEqual(row[0], 'TEST-001')
        self.assertEqual(row[1], '莲花县就业帮扶补贴')
        self.assertEqual(row[2], 'X')
        self.assertEqual(row[3], '莲花县')
        self.assertIn('脱贫人口', row[4])
        self.assertEqual(row[5], '脱贫劳动力')
        self.assertEqual(row[8], '2026-12-31')
        self.assertIn('制定部门：县人社局', row[9])
        self.assertIn('实施部门：县就业局', row[9])
        self.assertEqual(row[10], '在用')
        self.assertEqual(row[11], 'v1')

        self.assertEqual(
            self.db_scalar(db_path, 'SELECT COUNT(*) FROM "policy_detail"'), 1)
        self.assertEqual(
            self.db_scalar(db_path, 'SELECT COUNT(*) FROM "policy_version"'), 1)
        batch = self.db_rows(
            db_path, 'SELECT "status","total","valid","invalid","dry_run",'
                     '"source","file_name","operator" FROM "import_batch"')
        self.assertEqual(batch[0][0], 'committed')
        self.assertEqual(batch[0][1], 1)
        self.assertEqual(batch[0][2], 1)
        self.assertEqual(batch[0][3], 0)
        self.assertEqual(batch[0][4], 0)
        self.assertEqual(batch[0][5], 'national-intake')
        self.assertEqual(batch[0][6], 'national.csv')
        events = self.db_rows(
            db_path, 'SELECT "event_type","status" FROM "outbox" ORDER BY "event_type"')
        event_types = {item[0] for item in events}
        self.assertTrue({'policy.vector_rebuild', 'policy.rule_invalidate',
                         'policy.kg_sync'}.issubset(event_types))
        self.assertEqual(len(events), 3)

        # 第二次 apply：不重复建政策，但上游 policy_import 的明细 diff 会把
        # 行级效期回填进 _effective_details，而落库的 policy_detail 不保存
        # effect_date/expire_date，导致重导被判为 details 变更（update）。
        # 这是 T19 引擎的既有口径，本任务不能改 policy_import.py，故只断言
        # “不新增政策行”，并在 docs/22 记录该幂等边界。
        code, report, err = self.run_json(['--apply'])
        self.assertEqual(code, 0, err)
        self.assertEqual(report['applied'][0]['created'], 0)
        self.assertEqual(report['applied'][0]['updated'], 1)
        self.assertEqual(report['applied'][0]['noop'], 0)
        self.assertEqual(self.db_scalar(db_path, 'SELECT COUNT(*) FROM "policy"'), 1)
        self.assertEqual(
            self.db_scalar(db_path, 'SELECT COUNT(*) FROM "import_batch"'), 2)


if __name__ == '__main__':
    unittest.main()
