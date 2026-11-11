"""T21 读侧状态过滤单元测试。

覆盖：active 政策规则保留、停用/归档/软删/草稿丢弃、开关回退、policy 表缺失降级、
离线编译回落路径不过滤、rule_store.load_rules 保持不过滤（编译 worker 语义）、
以及 load_rules → evaluate_family 的端到端断言（停用政策不出卡）。

所有 DB 都是临时 sqlite 文件；rule_store.engine 与 kb_rag._db 均被替换，不触碰真实库。
运行：cd /home/adminroot/smart-help && PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_rule_status_filter -v
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from policy_core import rule_eval, rule_store  # noqa: E402

_RULE_TABLE = """CREATE TABLE policy_rule (
    rule_key TEXT, policy_code TEXT, detail_id TEXT, version TEXT, text_hash TEXT,
    region_scope TEXT, rule_json TEXT, status TEXT, missing_fields TEXT, errors TEXT,
    compiler_model TEXT, compiler_rev TEXT)"""

class _KbConn:
    """sqlite 上执行 'SET TRANSACTION READ ONLY' 会报错；测试用的 kb_rag 引擎连接包装：
    忽略该语句（生产 PG 上真实执行只读事务），其余查询透传，便于验证元数据补全路径。"""

    def __init__(self, engine):
        self._engine = engine
        self._conn = None

    def __enter__(self):
        self._conn = self._engine.connect()
        return self

    def __exit__(self, *exc):
        if self._conn is not None:
            self._conn.close()
        return False

    def execute(self, statement, params=None):
        if 'SET TRANSACTION READ ONLY' in str(statement).upper():
            return _EmptyResult()
        return self._conn.execute(statement, params or {})


class _EmptyResult:
    def mappings(self):
        return self

    def __iter__(self):
        return iter(())


class _KbEngine:
    def __init__(self, engine):
        self._engine = engine

    def connect(self):
        return _KbConn(self._engine)

    def dispose(self):
        self._engine.dispose()


_AGE_RULE = {
    'subject': 'member',
    'association': {'field': 'age', 'op': 'gte', 'value': 0},
    'eligibility': {'all': [{'field': 'age', 'op': 'gte', 'value': 18}]},
    'eligibility_complete': True,
    'gaps': [],
    'unresolved': [],
    'benefits': '',
}


class RuleStatusFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='rule-status-')
        self.engine = create_engine('sqlite:///' + os.path.join(self.tmpdir, 't.db'))
        with self.engine.begin() as conn:
            conn.execute(text(_RULE_TABLE))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.engine.dispose()
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------------- fixture helpers ----------------

    def create_policy_table(self, with_status=True):
        columns = 'policy_code TEXT PRIMARY KEY, policy_name TEXT, target TEXT, conditions TEXT'
        if with_status:
            columns += ', status TEXT'
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE policy (' + columns + ')'))

    def add_policy(self, code, status, name=None, target='', conditions='', with_status=True):
        values = dict(policy_code=code, policy_name=name if name is not None else '政策' + code,
                      target=target, conditions=conditions)
        columns = 'policy_code, policy_name, target, conditions'
        params = ':policy_code, :policy_name, :target, :conditions'
        if with_status:
            columns += ', status'
            params += ', :status'
            values['status'] = status
        with self.engine.begin() as conn:
            conn.execute(text('INSERT INTO policy (' + columns + ') VALUES (' + params + ')'), values)

    def add_rule(self, code, scope='莲花县', rule=None, status='complete'):
        with self.engine.begin() as conn:
            conn.execute(text(
                'INSERT INTO policy_rule (rule_key, policy_code, detail_id, version, text_hash, '
                'region_scope, rule_json, status, missing_fields, errors, compiler_model, compiler_rev) '
                'VALUES (:k, :c, :d, :v, :h, :s, :j, :st, NULL, NULL, :m, :r)'),
                dict(k=code + ':main', c=code, d='main', v='v1', h='h-' + code, s=scope,
                     j=json.dumps(rule or {}), st=status, m='test', r='r1'))

    @contextlib.contextmanager
    def patched(self):
        """把 rule_store.engine 与 kb_rag._db 都换成临时 sqlite，绝不访问真实库。"""
        fake_kb = types.ModuleType('kb_rag')
        fake_kb._db = lambda: _KbEngine(self.engine)
        with mock.patch.object(rule_store, 'engine', lambda: self.engine), \
                mock.patch.dict(sys.modules, {'kb_rag': fake_kb}):
            yield

    def load(self, county='莲花县'):
        with self.patched():
            return rule_eval.load_rules(county)

    # ---------------- 基础行为 ----------------

    def test_active_policy_rules_kept(self):
        self.create_policy_table()
        self.add_policy('P1', '在用', name='政策一', target='年龄')
        self.add_rule('P1')
        rows = self.load()
        self.assertEqual([r['policy_code'] for r in rows], ['P1'])
        self.assertEqual(rows[0]['name'], '政策一')
        self.assertEqual(rows[0]['target'], '年龄')

    def test_suspended_archived_deleted_draft_dropped(self):
        self.create_policy_table()
        for code, status in (('P1', '在用'), ('P2', '停用'), ('P3', 'archived'),
                             ('P4', 'deleted'), ('P5', 'draft')):
            self.add_policy(code, status)
        for code in ('P1', 'P2', 'P3', 'P4', 'P5'):
            self.add_rule(code)
        rows = self.load()
        self.assertEqual([r['policy_code'] for r in rows], ['P1'])

    def test_legacy_active_status_aliases_kept(self):
        self.create_policy_table()
        self.add_policy('P1', 'active')
        self.add_policy('P2', '启用')
        self.add_policy('P3', '停用')
        for code in ('P1', 'P2', 'P3'):
            self.add_rule(code)
        rows = self.load()
        self.assertEqual(sorted(r['policy_code'] for r in rows), ['P1', 'P2'])

    def test_unknown_or_missing_status_dropped(self):
        self.create_policy_table()
        self.add_policy('P1', None)
        self.add_policy('P2', '')
        self.add_policy('P3', '待定')
        for code in ('P1', 'P2', 'P3'):
            self.add_rule(code)
        # policy 行缺失的规则同样保守丢弃
        self.add_rule('P9')
        self.assertEqual(self.load(), [])

    def test_missing_policy_row_dropped(self):
        self.create_policy_table()
        self.add_policy('P1', '在用')
        self.add_rule('P1')
        self.add_rule('PX')  # 规则引用的政策不在 policy 表 → 状态无法确认 → 保守丢弃
        rows = self.load()
        self.assertEqual([r['policy_code'] for r in rows], ['P1'])

    def test_all_inactive_returns_empty_not_crash(self):
        self.create_policy_table()
        self.add_policy('P1', '停用')
        self.add_policy('P2', 'deleted')
        self.add_rule('P1')
        self.add_rule('P2')
        self.assertEqual(self.load(), [])

    # ---------------- 开关与降级 ----------------

    def test_switch_off_keeps_all(self):
        self.create_policy_table()
        self.add_policy('P1', '在用')
        self.add_policy('P2', '停用')
        self.add_policy('P3', 'deleted')
        for code in ('P1', 'P2', 'P3'):
            self.add_rule(code)
        with mock.patch.dict(os.environ, {'POLICY_STATUS_FILTER': '0'}):
            rows = self.load()
        self.assertEqual(sorted(r['policy_code'] for r in rows), ['P1', 'P2', 'P3'])

    def test_missing_policy_table_degrades(self):
        # 离线编译回落场景：没有 policy 表 → 无法确认状态 → 跳过过滤，不崩
        self.add_rule('P1')
        self.add_rule('P2')
        rows = self.load()
        self.assertEqual(sorted(r['policy_code'] for r in rows), ['P1', 'P2'])
        self.assertEqual(rows[0]['name'], '')

    def test_policy_table_without_status_column_degrades(self):
        self.create_policy_table(with_status=False)
        self.add_policy('P1', '在用', with_status=False)
        self.add_rule('P1')
        rows = self.load()
        self.assertEqual([r['policy_code'] for r in rows], ['P1'])
        with self.patched():
            self.assertIsNone(rule_store.policy_status_map({'P1'}))

    def test_offline_compiler_fallback_skips_filter(self):
        # 金仓 store 不可用 → 回落 compiler（sqlite 离线）→ 保留旧行为，不因状态未知清空规则
        inactive = [dict(rule_key='P2:main', policy_code='P2', detail_id='main', version='v1',
                         text_hash='h-P2', region_scope='莲花县', rule=_AGE_RULE, status='complete')]
        fake_kb = types.ModuleType('kb_rag')
        fake_kb._db = lambda: _KbEngine(self.engine)
        with mock.patch.object(rule_store, 'load_rules', side_effect=RuntimeError('offline')), \
                mock.patch('policy_core.compiler.load_rules', return_value=inactive), \
                mock.patch.object(rule_store, 'engine', lambda: self.engine), \
                mock.patch.dict(sys.modules, {'kb_rag': fake_kb}):
            rows = rule_eval.load_rules('莲花县')
        self.assertEqual([r['policy_code'] for r in rows], ['P2'])

    # ---------------- store 层语义 ----------------

    def test_rule_store_load_rules_does_not_filter(self):
        # 编译 worker 的 check_stale/all_hashes 需要全部规则 → store.load_rules 保持不过滤
        self.create_policy_table()
        self.add_policy('P1', '在用')
        self.add_policy('P2', '停用')
        self.add_rule('P1')
        self.add_rule('P2')
        with self.patched():
            rows = rule_store.load_rules('莲花县')
        self.assertEqual(sorted(r['policy_code'] for r in rows), ['P1', 'P2'])

    def test_status_helpers(self):
        self.assertTrue(rule_store.status_filter_enabled())
        with mock.patch.dict(os.environ, {'POLICY_STATUS_FILTER': '0'}):
            self.assertFalse(rule_store.status_filter_enabled())
        self.assertTrue(rule_store.is_active_policy_status('在用'))
        self.assertTrue(rule_store.is_active_policy_status(' 在用 '))
        self.assertFalse(rule_store.is_active_policy_status('停用'))
        self.assertFalse(rule_store.is_active_policy_status(None))

    # ---------------- 端到端：停用政策不出卡 ----------------

    def test_evaluate_family_only_active_cards(self):
        self.create_policy_table()
        self.add_policy('P1', '在用', name='年龄测试政策', target='年龄')
        self.add_policy('P2', '停用', name='年龄测试政策', target='年龄')
        self.add_rule('P1', rule=_AGE_RULE)
        self.add_rule('P2', rule=_AGE_RULE)
        hh = {'policy_core': {'members': [{'member_no': 1}],
                              'facts': {'1:age': {'state': 'known', 'value': 40, 'schema_id': 's'}}}}
        rules = self.load()
        self.assertEqual([r['policy_code'] for r in rules], ['P1'])
        cards, _details = rule_eval.evaluate_family(rules, hh)
        self.assertEqual([c['code'] for c in cards], ['P1'])
        # 若直接喂入未过滤规则，P2 本来会出卡——证明拦截点在 load_rules。
        _raw_cards, _raw_details = rule_eval.evaluate_family(
            [dict(r, rule=_AGE_RULE) for r in ({'policy_code': 'P2', 'detail_id': 'main',
                                                'name': '年龄测试政策', 'target': '年龄'},)], hh)
        self.assertEqual([c['code'] for c in _raw_cards], ['P2'])

    def test_status_filtered_event_emitted(self):
        self.create_policy_table()
        self.add_policy('P1', '在用')
        self.add_policy('P2', '停用')
        self.add_rule('P1')
        self.add_rule('P2')
        import chain_trace
        with mock.patch.object(chain_trace, 'event') as fake_event:
            self.load()
        names = [call.args[0] for call in fake_event.call_args_list]
        self.assertIn('rule.status_filtered', names)


if __name__ == '__main__':
    unittest.main()
