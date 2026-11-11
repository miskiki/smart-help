# -*- coding: utf-8 -*-
"""政策数据治理 dry-run 只读审计单测（sqlite 内存库）。

覆盖：七份报告（含 scope 一致性）计数与结构、--limit 截断、CLI 输出、只读连接、
Kingbase 空串语义分支。
"""
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

from sqlalchemy import create_engine, text

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'policy_data_audit', ROOT / 'deploy' / 'policy_data_audit.py')
pda = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pda)

DDL = (
    'CREATE TABLE policy (policy_id INTEGER PRIMARY KEY, policy_code TEXT, '
    'policy_name TEXT, "level" TEXT, status TEXT, regions TEXT, effect_date TEXT, '
    'expire_date TEXT, cur_version TEXT, updated_at TEXT)',
    'CREATE TABLE policy_detail (detail_id INTEGER PRIMARY KEY, policy_id INTEGER, '
    'region TEXT, conditions TEXT, target TEXT, effect_date TEXT, expire_date TEXT)',
    'CREATE TABLE policy_version (version_id INTEGER PRIMARY KEY, policy_id INTEGER, '
    'version_no TEXT, effect_date TEXT, expire_date TEXT)',
    'CREATE TABLE policy_scope (policy_code TEXT, detail_id TEXT, region_key TEXT, '
    'scope_kind TEXT, scope_level TEXT, source_field TEXT)',
)

POLICIES = (
    (1, 'X-A', '同名政策', 'X', '在用', '新建区', None, None, 'v1', None),
    (2, 'S-A', '同名政策', 'S', '在用', '江西省全省', '', None, 'v1', '2026-01-01'),
    (3, 'S-B', '空壳政策', 'S', '在用', None, None, None, 'v1', None),
    (4, 'X-B', '产业奖补', 'X', '在用', '南昌县', '2026-01-01', '2026-12-31', 'v1', None),
    (6, 'X-D', '外部政策', 'X', '在用', '火星镇', None, None, 'v1', None),
    (7, 'X-E', '功能区政策', 'X', '在用', '赣州经济技术开发区', None, None, 'v1', None),
)

DETAILS = (
    (11, 1, '新建区', 'C1', 'T1', None, None),
    (12, 1, '新建区', 'C1', 'T1', None, None),
    (13, 1, '新建区', 'C2', 'T1', None, None),
    (21, 4, '南昌县', 'C1', 'T1', None, None),
    (31, 2, '江西省全省', 'C3', 'T3', None, None),
    (41, 6, '火星镇', 'C4', 'T4', None, None),
    (51, 7, '赣州经济技术开发区', 'C5', 'T5', None, None),
)

VERSIONS = ((1, 1, 'v1', None, None), (2, 1, 'v1', None, None), (3, 2, 'v2', None, None))
SCOPES = (('X-A', '11', 'X:江西省/南昌市/新建区', 'detail', 'county', 'detail.region'),
          ('S-A', None, 'P:江西省', 'policy', 'province', 'policy.regions'))


POLICY_COLUMNS = ('policy_id', 'policy_code', 'policy_name', 'level', 'status',
                  'regions', 'effect_date', 'expire_date', 'cur_version', 'updated_at')
DETAIL_COLUMNS = ('detail_id', 'policy_id', 'region', 'conditions', 'target',
                  'effect_date', 'expire_date')
VERSION_COLUMNS = ('version_id', 'policy_id', 'version_no', 'effect_date', 'expire_date')
SCOPE_COLUMNS = ('policy_code', 'detail_id', 'region_key', 'scope_kind',
                 'scope_level', 'source_field')


def _execute_many(conn, sql, columns, rows):
    for row in rows:
        conn.execute(text(sql), dict(zip(columns, row)))


def seed(conn, scopes=SCOPES):
    for statement in DDL:
        conn.execute(text(statement))
    _execute_many(conn, 'INSERT INTO policy VALUES (:policy_id, :policy_code, :policy_name, '
                        ':level, :status, :regions, :effect_date, :expire_date, '
                        ':cur_version, :updated_at)', POLICY_COLUMNS, POLICIES)
    _execute_many(conn, 'INSERT INTO policy_detail VALUES (:detail_id, :policy_id, :region, '
                        ':conditions, :target, :effect_date, :expire_date)',
                  DETAIL_COLUMNS, DETAILS)
    _execute_many(conn, 'INSERT INTO policy_version VALUES (:version_id, :policy_id, '
                        ':version_no, :effect_date, :expire_date)', VERSION_COLUMNS, VERSIONS)
    # 列名不能写绑定参数（WHERE/VALUES 才用 :param），否则 sqlite 会拼出 (?, ?, ...) 语法错误。
    _execute_many(conn, 'INSERT INTO policy_scope (policy_code, detail_id, region_key, '
                        'scope_kind, scope_level, source_field) VALUES '
                        '(:policy_code, :detail_id, :region_key, :scope_kind, :scope_level, '
                        ':source_field)', SCOPE_COLUMNS, scopes)


def build_engine(scopes=SCOPES):
    """独立内存库（每个用例各一份，避免造假数据互相污染）。"""
    engine = create_engine('sqlite://')
    conn = engine.connect()
    seed(conn, scopes=scopes)
    conn.commit()
    return engine, conn


class FixtureCase(unittest.TestCase):
    """sqlite 内存库夹具：6 政策 / 7 明细 / 3 版本 / 2 scope 行。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, 'fixture.db')
        cls.engine = create_engine('sqlite:///' + cls.db_path)
        cls.catalog = cls.engine.connect()
        seed(cls.catalog)
        # 夹具连接默认开启事务：不 commit 的话其它连接（fresh()）读到的是空库。
        cls.catalog.commit()

    @classmethod
    def tearDownClass(cls):
        cls.catalog.close()
        cls.engine.dispose()
        cls.tmp.cleanup()

    def fresh(self):
        """每个用例独立只读连接（避免共享连接上的事务状态干扰）。"""
        conn = self.engine.connect()
        self.addCleanup(conn.close)
        return conn


class CoverageReportTest(FixtureCase):
    def test_county_and_province_counts(self):
        data = pda.coverage_profile(self.fresh(), limit=10)
        self.assertEqual(data['summary']['counties_total'], 100)
        self.assertEqual(data['summary']['counties_with_data'], 2)
        self.assertEqual(data['summary']['zones_total'], 13)
        self.assertEqual(data['summary']['zones_with_data'], 1)
        self.assertEqual(data['summary']['province_details'], 7)
        self.assertEqual(data['summary']['province_level_details'], 1)
        self.assertEqual(data['summary']['province_names'], 4)
        self.assertEqual(data['province']['county_details'], 4)
        self.assertEqual(data['province']['zone_details'], 1)
        county = {item['county']: item for item in data['counties']}['新建区']
        self.assertTrue(county['has_data'])
        self.assertEqual(county['details'], 3)
        self.assertEqual(county['names'], 1)
        self.assertEqual(county['city'], '南昌市')

    def test_missing_counties_flagged(self):
        data = pda.coverage_profile(self.fresh(), limit=10)
        missing = data['summary']['missing_counties']
        self.assertEqual(len(missing), 98)
        self.assertIn('东湖区', missing)
        self.assertIn('浔阳区', missing)

    def test_zone_coverage(self):
        data = pda.coverage_profile(self.fresh(), limit=10)
        zones = {item['zone']: item for item in data['zones']}
        self.assertEqual(len(zones), 13)
        self.assertTrue(zones['赣州经济技术开发区']['has_data'])
        self.assertEqual(zones['赣州经济技术开发区']['details'], 1)
        self.assertEqual(zones['赣州经济技术开发区']['city'], '赣州市')
        self.assertFalse(zones['湾里管理局']['has_data'])

    def test_unresolved_region(self):
        data = pda.coverage_profile(self.fresh(), limit=10)
        self.assertEqual(data['summary']['unresolved_regions'], 1)
        self.assertEqual(data['unresolved'][0]['region'], '火星镇')
        self.assertEqual(data['unresolved'][0]['reason'], 'unresolved_region')
        self.assertEqual(data['unresolved'][0]['details'], 1)

    def test_city_rollup(self):
        data = pda.coverage_profile(self.fresh(), limit=10)
        cities = {item['city']: item for item in data['cities']}
        nanchang = cities['南昌市']
        self.assertEqual(nanchang['counties_with_data'], 2)
        self.assertEqual(nanchang['county_details'], 4)
        self.assertEqual(nanchang['county_names'], 2)
        # 赣州市已知功能区 2 个（经开区、蓉江新区）；zone_count 统计维度总数，不按有无明细过滤。
        self.assertEqual(cities['赣州市']['zone_count'], 2)
        self.assertEqual(cities['赣州市']['zone_details'], 1)


class EmptyShellTest(FixtureCase):
    def test_empty_shell_counts(self):
        data = pda.empty_shells(self.fresh(), limit=10)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['in_use'], 1)
        self.assertEqual(data['by_level'], {'S': 1})
        self.assertEqual(data['null_regions'], 1)
        self.assertEqual(data['zero_versions'], 1)
        self.assertEqual(data['zero_scope'], 1)
        self.assertEqual(data['samples'][0]['policy_code'], 'S-B')
        self.assertEqual(data['samples'][0]['versions'], 0)
        self.assertEqual(data['samples'][0]['scope_rows'], 0)

    def test_limit_truncates_samples_only(self):
        data = pda.empty_shells(self.fresh(), limit=0)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['samples'], [])
        self.assertTrue(data['truncated'])


class DuplicateTest(FixtureCase):
    def test_duplicate_group_counts(self):
        data = pda.duplicate_groups(self.fresh(), limit=10)
        self.assertEqual(data['groups'], 1)
        self.assertEqual(data['covered_rows'], 2)
        self.assertEqual(data['redundant_rows'], 1)
        self.assertEqual(data['total_rows'], 7)

    def test_group_locator_and_keep(self):
        group = pda.duplicate_groups(self.fresh(), limit=10)['samples'][0]
        self.assertEqual(group['keep_detail_id'], 11)
        self.assertEqual(group['detail_ids'], [11, 12])
        self.assertEqual(group['codes'], ['X-A'])
        self.assertEqual(group['region'], '新建区')
        self.assertEqual(group['conditions'], 'C1')
        self.assertEqual(group['rows'], 2)

    def test_no_group_when_key_differs(self):
        data = pda.duplicate_groups(self.fresh(), limit=10)
        self.assertEqual(data['groups'], 1)
        self.assertTrue(all(item['conditions'] != 'C2' for item in data['samples']))


class CrossLevelTest(FixtureCase):
    def test_cross_level_pairs(self):
        data = pda.cross_level_names(self.fresh(), limit=10)
        self.assertEqual(data['names'], 1)
        self.assertEqual(data['covered_rows'], 2)
        item = data['samples'][0]
        self.assertEqual(item['policy_name'], '同名政策')
        self.assertEqual(item['codes']['X'], ['X-A'])
        self.assertEqual(item['codes']['S'], ['S-A'])
        self.assertEqual(item['pairs_sample'], [{'x': 'X-A', 's': 'S-A'}])

    def test_unique_name_not_reported(self):
        data = pda.cross_level_names(self.fresh(), limit=10)
        names = [item['policy_name'] for item in data['samples']]
        self.assertNotIn('产业奖补', names)


class TimelinessTest(FixtureCase):
    def test_dates_blocks(self):
        data = pda.timeliness_gaps(self.fresh())
        policy = data['policy']
        self.assertEqual(policy['total'], 6)
        self.assertEqual(policy['effect_present'], 1)
        self.assertEqual(policy['effect_missing'], 5)
        self.assertEqual(policy['expire_present'], 1)
        self.assertEqual(policy['expire_missing'], 5)
        self.assertEqual(data['policy_detail']['effect_missing'], 7)
        self.assertEqual(data['policy_version']['expire_missing'], 3)

    def test_blank_string_is_missing(self):
        # S-A 的 effect_date 是空串（不是 NULL）；两种都应算缺失。
        data = pda.timeliness_gaps(self.fresh())
        self.assertEqual(data['policy']['effect_missing'], 5)

    def test_in_use_by_level(self):
        data = pda.timeliness_gaps(self.fresh())
        rows = {row['level']: row for row in data['in_use_by_level']}
        self.assertEqual(rows['X']['total'], 4)
        self.assertEqual(rows['X']['effect_missing'], 3)
        self.assertEqual(rows['S']['total'], 2)
        self.assertEqual(rows['S']['effect_missing'], 2)

    def test_updated_at_and_version_distribution(self):
        data = pda.timeliness_gaps(self.fresh())
        self.assertEqual(data['policy_updated_at'], {'total': 6, 'present': 1})
        self.assertEqual(data['version_no'], [{'version_no': 'v1', 'rows': 2},
                                             {'version_no': 'v2', 'rows': 1}])
        self.assertEqual(data['cur_version'], [{'cur_version': 'v1', 'rows': 6}])


class SuggestionTest(FixtureCase):
    def test_action_totals(self):
        result = pda.audit(self.fresh(), limit=10)
        suggestions = result['sections']['suggestions']
        # 既有 106 条 + scope 发现：明细缺 scope 6 行（backfill）+ 政策级 scope 不一致 4 个（manual）。
        self.assertEqual(suggestions['total'], 116)
        self.assertEqual(suggestions['by_action'],
                         {'archive': 1, 'backfill': 108, 'manual': 6, 'merge': 1})
        self.assertEqual(suggestions['by_risk'], {'high': 6, 'low': 1, 'medium': 109})

    def test_items_are_actionable_and_safe(self):
        result = pda.audit(self.fresh(), limit=10)
        items = result['sections']['suggestions']['items']
        self.assertEqual(len(items), 10)
        for item in items:
            self.assertIn(item['action'], pda.ACTIONS)
            self.assertIn(item['risk'], ('low', 'medium', 'high'))
            self.assertIn('sheet', item)
            self.assertIsInstance(item['locator'], dict)
            self.assertTrue(item['note'])

    def test_no_write_statement_in_suggestions(self):
        # 建议只描述动作；审计结果里不应携带任何可执行 SQL/写命令字段。
        result = pda.audit(self.fresh(), limit=5)
        blob = json.dumps(result, ensure_ascii=False, default=pda._json_default).lower()
        # 只匹配完整写语句；JSON 里的 "truncated" 字段不是写命令。
        for keyword in ('insert into', 'update ', 'delete from', 'drop table', 'truncate table'):
            self.assertNotIn(keyword, blob)


class AuditResultTest(FixtureCase):
    def test_sections_and_metadata(self):
        result = pda.audit(self.fresh(), limit=10)
        self.assertEqual(result['version'], pda.VERSION)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['limit'], 10)
        self.assertEqual(result['overview'], {'policy': 6, 'policy_detail': 7,
                                              'policy_version': 3, 'policy_scope': 2})
        for key in ('coverage', 'empty_shell', 'duplicates', 'cross_level',
                    'timeliness', 'scope_consistency', 'suggestions'):
            self.assertIn(key, result['sections'])
        scope = result['sections']['scope_consistency']
        self.assertEqual(scope['detail_without_scope']['details'], 6)
        self.assertEqual(scope['orphan_scope_rows']['rows'], 0)
        self.assertEqual(scope['policy_region_mismatch']['policies'], 4)
        self.assertEqual(scope['unresolved_scope_rows']['rows'], 0)
        self.assertIn('sqlite', result['database'])

    def test_json_serializable(self):
        result = pda.audit(self.fresh(), limit=5)
        dumped = json.dumps(result, ensure_ascii=False, default=pda._json_default)
        loaded = json.loads(dumped)
        self.assertEqual(loaded['overview']['policy'], 6)
        self.assertTrue(loaded['sections']['suggestions']['items'])

    def test_limit_applies_to_samples(self):
        result = pda.audit(self.fresh(), limit=2)
        self.assertEqual(len(result['sections']['suggestions']['items']), 2)
        self.assertTrue(result['sections']['suggestions']['truncated'])
        self.assertLessEqual(len(result['sections']['duplicates']['samples']), 2)

    def test_empty_database_does_not_crash(self):
        engine = create_engine('sqlite://')
        conn = engine.connect()
        self.addCleanup(engine.dispose)
        self.addCleanup(conn.close)
        for statement in DDL:
            conn.execute(text(statement))
        result = pda.audit(conn, limit=3)
        self.assertEqual(result['overview']['policy'], 0)
        self.assertEqual(result['sections']['coverage']['summary']['counties_total'], 100)
        self.assertEqual(result['sections']['coverage']['summary']['counties_with_data'], 0)
        self.assertEqual(result['sections']['empty_shell']['total'], 0)
        scope = result['sections']['scope_consistency']
        self.assertEqual(scope['detail_without_scope']['details'], 0)
        self.assertEqual(scope['unresolved_scope_rows']['rows'], 0)
        self.assertEqual(result['sections']['suggestions']['by_action'], {'backfill': 100})
        self.assertEqual(result['sections']['timeliness']['policy']['missing_rate'], 0)


class ReadonlyAndHelpersTest(FixtureCase):
    def test_connect_sets_query_only_and_rejects_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'audit.db')
            engine = create_engine('sqlite:///' + path)
            seed(engine.connect())
            engine.dispose()
            conn = pda.connect('sqlite:///' + path)
            self.addCleanup(pda.close, conn)
            self.assertEqual(int(conn.execute(text('PRAGMA query_only')).scalar()), 1)
            with self.assertRaises(Exception):
                conn.execute(text("INSERT INTO policy (policy_id) VALUES (999)"))

    def test_blank_sql_uses_btrem_for_kingbase(self):
        fake_postgres = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        fake_sqlite = SimpleNamespace(dialect=SimpleNamespace(name='sqlite'))
        # KingbaseES 把 '' 当 NULL：只能靠 BTRIM(col) IS NULL 判断空值。
        self.assertEqual(pda._blank_sql('effect_date', fake_postgres),
                         'BTRIM(effect_date) IS NULL')
        self.assertEqual(pda._present_sql('updated_at', fake_postgres),
                         'BTRIM(updated_at) IS NOT NULL')
        self.assertIn('TRIM(COALESCE', pda._blank_sql('effect_date', fake_sqlite))

    def test_env_file_loading_does_not_override_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test.env')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('AUDIT_TEST_KEY=from_file\n# comment\nBAD_LINE\n')
            关键 = 'AUDIT_TEST_KEY'
            os.environ.pop(关键, None)
            pda.load_env_file(path)
            self.assertEqual(os.environ.get(关键), 'from_file')
            os.environ[关键] = 'from_process'
            pda.load_env_file(path)
            self.assertEqual(os.environ.get(关键), 'from_process')
            os.environ.pop(关键, None)
            pda.load_env_file('/path/does/not/exist.env')  # 不存在时静默

    def test_database_url_explicit(self):
        self.assertEqual(pda.database_url('sqlite://'), 'sqlite://')

    def test_excerpt_collapses_whitespace(self):
        self.assertEqual(pda._excerpt(' a\n\tb  '), 'a b')
        self.assertIsNone(pda._excerpt(None))
        self.assertTrue(pda._excerpt('x' * 200).endswith('…'))


class ScopeConsistencyTest(unittest.TestCase):
    """(g) scope 一致性报告：每项单独造库验证，造数只影响本用例内存库。"""

    def make_conn(self, scopes=SCOPES):
        engine, conn = build_engine(scopes)
        self.addCleanup(engine.dispose)
        self.addCleanup(conn.close)
        return conn

    def test_detail_without_scope_counts(self):
        block = pda.scope_consistency(self.make_conn(), limit=10)['detail_without_scope']
        self.assertEqual(block['details'], 6)
        self.assertEqual(block['policies'], 5)
        self.assertFalse(block['truncated'])
        self.assertEqual(len(block['samples']), 6)
        sample = [row for row in block['samples'] if row['detail_id'] == 12][0]
        self.assertEqual(sample['policy_code'], 'X-A')
        self.assertEqual(sample['region'], '新建区')

    def test_detail_without_scope_sample_limit(self):
        block = pda.scope_consistency(self.make_conn(), limit=3)['detail_without_scope']
        self.assertEqual(len(block['samples']), 3)
        self.assertTrue(block['truncated'])
        block = pda.scope_consistency(self.make_conn(), limit=50)['detail_without_scope']
        self.assertLessEqual(len(block['samples']), pda.SCOPE_SAMPLE_CAP)

    def test_orphan_rows_missing_detail(self):
        scopes = SCOPES + (
            ('X-A', '999', 'X:江西省/南昌市/新建区', 'detail', 'county', 'detail.region'),)
        block = pda.scope_consistency(self.make_conn(scopes), limit=10)['orphan_scope_rows']
        self.assertEqual(block['rows'], 1)
        self.assertEqual(block['detail_ids'], 1)
        self.assertEqual(block['policy_code_mismatch_rows'], 0)
        self.assertTrue(block['detail_id_unique'])
        self.assertEqual(block['duplicate_detail_ids'], 0)
        self.assertEqual(block['match_basis'], 'detail_id')
        self.assertEqual(block['samples'][0]['detail_id'], '999')
        self.assertEqual(block['samples'][0]['reason'], 'missing_detail')

    def test_orphan_policy_code_mismatch(self):
        # detail 11 属于 X-A，却出现在 X-B 的 scope 行：明细存在但政策不一致，不算孤儿、单列错配。
        scopes = (('X-B', '11', 'X:江西省/南昌市/新建区', 'detail', 'county', 'detail.region'),)
        block = pda.scope_consistency(self.make_conn(scopes), limit=10)['orphan_scope_rows']
        self.assertEqual(block['rows'], 0)
        self.assertEqual(block['policy_code_mismatch_rows'], 1)
        self.assertEqual(block['samples'][0]['reason'], 'policy_code_mismatch')
        self.assertEqual(block['samples'][0]['actual_policy_code'], 'X-A')

    def test_policy_region_mismatch_missing_scope(self):
        block = pda.scope_consistency(self.make_conn(scopes=()), limit=10)['policy_region_mismatch']
        by_code = {row['policy_code']: row for row in block['samples']}
        # S-B regions 为空且无 policy 级 scope 行，两侧都空 = 一致，不算不一致。
        self.assertEqual(block['policies'], 5)
        self.assertEqual(block['missing_scope_policies'], 5)
        self.assertEqual(block['extra_scope_policies'], 0)
        self.assertEqual(block['unresolved_region_tokens'], 1)
        self.assertEqual(by_code['X-A']['missing_scope'], ['X:江西省/南昌市/新建区'])
        self.assertEqual(by_code['X-D']['unresolved_tokens'], ['火星镇'])

    def test_policy_region_mismatch_extra_scope(self):
        scopes = SCOPES + (('S-B', '-', 'CN', 'policy', 'nation', 'policy.regions'),)
        block = pda.scope_consistency(self.make_conn(scopes), limit=10)['policy_region_mismatch']
        by_code = {row['policy_code']: row for row in block['samples']}
        self.assertEqual(block['policies'], 5)
        self.assertEqual(block['missing_scope_policies'], 4)
        self.assertEqual(block['extra_scope_policies'], 1)
        self.assertEqual(by_code['S-B']['extra_scope'], ['CN'])
        self.assertEqual(by_code['S-B']['missing_scope'], [])

    def test_unresolved_scope_rows_blank_and_u_key(self):
        scopes = SCOPES + (
            ('X-B', '21', 'X:江西省/南昌市/南昌县', 'detail', None, 'detail.region'),
            ('S-A', '31', 'P:江西省', 'detail', '', 'detail.region'),
            ('X-A', '999', 'U:火星镇', 'detail', 'unknown', 'detail.region'),
        )
        block = pda.scope_consistency(self.make_conn(scopes), limit=10)['unresolved_scope_rows']
        # NULL 与空串都算 scope_level 空（sqlite 侧 TRIM/COALESCE；PG 侧 BTRIM 覆盖 NULL/空串）。
        self.assertEqual(block['rows'], 3)
        self.assertEqual(block['blank_level'], 2)
        self.assertEqual(block['u_key'], 1)
        reasons = {row['reason'] for row in block['samples']}
        self.assertEqual(reasons, {'blank_level', 'u_key'})

    def test_empty_scope_tables_all_zero(self):
        engine = create_engine('sqlite://')
        conn = engine.connect()
        self.addCleanup(engine.dispose)
        self.addCleanup(conn.close)
        for statement in DDL:
            conn.execute(text(statement))
        data = pda.scope_consistency(conn, limit=10)
        self.assertEqual(data['detail_without_scope']['details'], 0)
        self.assertEqual(data['orphan_scope_rows']['rows'], 0)
        self.assertEqual(data['policy_region_mismatch']['policies'], 0)
        self.assertEqual(data['unresolved_scope_rows']['rows'], 0)

    def test_scope_key_matches_policy_scope_mapping(self):
        self.assertEqual(pda._scope_key('新建区'), 'X:江西省/南昌市/新建区')
        self.assertEqual(pda._scope_key('江西省全省'), 'P:江西省')
        self.assertEqual(pda._scope_key('火星镇'), 'U:火星镇')
        self.assertTrue(pda._scope_key('赣州经济技术开发区').startswith('Z:'))

    def test_audit_json_and_suggestions_include_scope(self):
        engine, conn = build_engine()
        self.addCleanup(engine.dispose)
        self.addCleanup(conn.close)
        result = pda.audit(conn, limit=10)
        self.assertIn('scope_consistency', result['sections'])
        blob = json.dumps(result, ensure_ascii=False, default=pda._json_default)
        self.assertIn('scope_consistency', blob)
        suggestions = result['sections']['suggestions']
        self.assertEqual(suggestions['by_action']['manual'], 6)
        self.assertEqual(suggestions['by_action']['backfill'], 108)
        self.assertTrue(suggestions['items'])
        self.assertIn('scope 一致性', pda.format_report(result))


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, 'cli.db')
        engine = create_engine('sqlite:///' + cls.db_path)
        conn = engine.connect()
        seed(conn)
        conn.commit()
        conn.close()
        engine.dispose()
        cls.url = 'sqlite:///' + cls.db_path

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cli_writes_json_file(self):
        out = os.path.join(self.tmp.name, 'report.json')
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pda.main(['--url', self.url, '--env-file', os.devnull,
                             '--json', out, '--limit', '3'])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding='utf-8') as handle:
            result = json.load(handle)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['overview']['policy'], 6)
        self.assertEqual(len(result['sections']['suggestions']['items']), 3)
        self.assertIn('JSON 报告已写入', buffer.getvalue())

    def test_cli_prints_report_by_default(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pda.main(['--url', self.url, '--env-file', os.devnull, '--limit', '3'])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        for section in ('[a] 覆盖画像', '[b] 空壳清单', '[c] 重复组',
                        '[d] 跨级同名', '[e] 时效缺口', '[f] 建议动作',
                        '[g] scope 一致性'):
            self.assertIn(section, output)
        self.assertIn('只读: True', output)

    def test_cli_json_stdout(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pda.main(['--url', self.url, '--env-file', os.devnull,
                             '--json', '-', '--limit', '2'])
        self.assertEqual(code, 0)
        result = json.loads(buffer.getvalue())
        self.assertEqual(result['overview']['policy_detail'], 7)

    def test_cli_rejects_negative_limit(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            pda.main(['--url', self.url, '--env-file', os.devnull, '--limit', '-1'])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_bad_url_returns_2_and_does_not_crash(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = pda.main(['--url', 'sqlite:////nonexistent/dir/x.db',
                             '--env-file', os.devnull, '--json', '-'])
        self.assertEqual(code, 2)
        self.assertIn('只读审计未执行', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
