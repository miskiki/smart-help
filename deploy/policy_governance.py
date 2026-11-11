#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""政策数据治理可执行工具（T35）：把部署审计的「建议动作」落成受控命令。

默认 dry-run（只读报告，不执行任何 INSERT/UPDATE/DELETE/DDL）；--apply 才写库，
且必须同时满足：
  1. 环境变量 POLICY_GOVERNANCE=1（默认 0，保持现网语义）；
  2. 命令行 --confirm；
  3. --operator 非空、--reason 不少于 4 个字符（未指定时用动作内置文案）。

写库只调用既有函数，不手写 SQL：
  - 归档状态：policy_core.lifecycle.archive（同时写 audit_log 与 4 类 outbox 事件）；
  - 结构化范围：policy_core.policy_scope.rows_for + replace_policy；
  - 侧效应：policy_core.outbox.enqueue('policy.cache_invalidate', ...)。
SQLite 单测库随 apply 自动补 policy_scope / outbox 表；生产 PG 上这些表已存在
（policy_scope 15493 行，outbox 由 lifecycle.ensure_schema 幂等建）。

动作：
  report-manual         只输出人工处理清单（跨级同名、完全同文重复组），恒只读
  archive-empty-shells  归档「无明细 + regions 空 + 无 scope 行 + 状态在用/停用」空壳政策
  backfill-scope        为「无 policy_scope 行」或 --codes 指定的政策回填结构化范围
  fix-scope-mismatch    重建「政策级 scope 行」与 policy.regions 归一集合不一致的政策范围

退出码：0 成功；1 执行异常（已回滚）；2 参数/开关/连接拒绝；4 部分条目失败（其余已提交）。
"""
import argparse
import datetime
import json
import os
import sys
from collections import Counter, OrderedDict

from sqlalchemy import bindparam, create_engine, inspect, text

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from policy_core import lifecycle, outbox, policy_scope  # noqa: E402

DEFAULT_ENV_FILE = os.path.join(BASE, 'deploy', 'web.env')
JX_PROVINCE = '江西省'
DEFAULT_OPERATOR = 'policy-governance'
DEFAULT_LIMIT = 200
MAX_SAMPLE = 20
SCAN_ITEMS_CAP = 2000
VERSION = 'policy-governance-1'
SWITCH_ENV = 'POLICY_GOVERNANCE'
SWITCH_HINT = ('POLICY_GOVERNANCE=1 才允许 --apply；默认 0 表示只读。'
               '回退：取消 --apply（或把开关设回 0）即恢复只读报告。')
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_PARTIAL = 0, 1, 2, 4

ACTIONS = ('report-manual', 'archive-empty-shells', 'backfill-scope', 'fix-scope-mismatch')
WRITE_ACTIONS = ('archive-empty-shells', 'backfill-scope', 'fix-scope-mismatch')
REASON_DEFAULTS = {
    'archive-empty-shells': '数据治理：归档无明细、无地区文本、无范围行的空壳政策',
    'backfill-scope': '数据治理：按政策地区文本回填结构化范围',
    'fix-scope-mismatch': '数据治理：按政策地区文本重建与文本不一致的结构化范围',
}


class GovernanceRefused(Exception):
    """写操作被开关/确认/参数护栏拒绝（CLI 退出码 2）。"""


# ---------------- 环境 / 数据库连接 ----------------

def load_env(path=None):
    """加载 KEY=VALUE 环境文件；已存在的环境变量优先（与 deploy 其他脚本一致）。"""
    path = path or DEFAULT_ENV_FILE
    if not path or not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())


def _ensure_db_dialect():
    """导入 src/config.py 触发金仓（KingbaseES）PG 方言补丁（见 policy_core.autopilot）。

    不导入时直连金仓会抛
    AssertionError: Could not determine version from string 'KingbaseES V008...'。
    """
    try:
        import config  # noqa: F401
    except Exception:  # noqa: BLE001 - sqlite/无金仓场景静默跳过
        return


def database_url(explicit=None):
    """URL 优先级：--url → ADMIN_DB_URL → config.DATABASE_URL → sqlite://。"""
    if explicit:
        return explicit
    env_url = os.environ.get('ADMIN_DB_URL')
    if env_url:
        return env_url
    try:
        import config
        return config.DATABASE_URL
    except Exception:  # noqa: BLE001
        return 'sqlite://'


def apply_allowed():
    """写开关，默认关闭；每次实时读 env，便于测试与回滚。"""
    return str(os.environ.get(SWITCH_ENV, '0') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def build_engine(url):
    _ensure_db_dialect()
    return create_engine(url, pool_pre_ping=True)


def _set_readonly(conn):
    """dry-run 连接级只读兜底：PG 当前事务 READ ONLY / SQLite query_only。"""
    try:
        if conn.dialect.name == 'postgresql':
            try:
                conn.execute(text('SET TRANSACTION READ ONLY'))
            except Exception:  # noqa: BLE001 - 事务已开始时退化为会话级默认
                conn.execute(text('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY'))
        elif conn.dialect.name == 'sqlite':
            conn.execute(text('PRAGMA query_only = ON'))
    except Exception:  # noqa: BLE001 - 只读设置失败不阻断只读查询；本模块无写语句
        pass


# ---------------- 通用小工具 ----------------

def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _now_text():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _columns(conn, table):
    """{列名}；表不存在返回 None。inspect 在 PG / Kingbase / sqlite 均可用。"""
    try:
        inspector = inspect(conn)
        if not inspector.has_table(table):
            return None
        return {column['name'] for column in inspector.get_columns(table)}
    except Exception:  # noqa: BLE001 - 探测失败按「表不可用」保守处理
        return None


def _has_table(conn, table):
    return _columns(conn, table) is not None


def _fetch(conn, sql, params=None):
    return conn.execute(text(sql), params or {}).mappings().all()


def _fetch_one(conn, sql, params=None):
    return conn.execute(text(sql), params or {}).mappings().first()


def _scalar(conn, sql, params=None):
    return conn.execute(text(sql), params or {}).scalar()


def _blank_sql(column, conn):
    """判空表达式：Kingbase（Oracle 兼容）把 '' 当 NULL，必须 BTRIM IS NULL。"""
    if conn.dialect.name == 'postgresql':
        return 'BTRIM(%s) IS NULL' % column
    return "TRIM(COALESCE(%s, '')) = ''" % column


def _blank(value):
    return not str(value or '').strip()


def _excerpt(value, limit=120):
    if value is None:
        return None
    value = ' '.join(str(value).split())
    return value if len(value) <= limit else value[:limit] + '…'


def _safe_json(report):
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)


def _sample(items, limit):
    return list(items or [])[:max(0, int(limit))]


def _sorted_list(values):
    return sorted(str(v) for v in (values or []))


def _vector_counts(conn, codes):
    """可选向量表计数（policy_vector_qwen3 优先，其次 policy_vector）；失败返回 {}。"""
    codes = [code for code in codes or [] if code]
    out = {}
    if not codes:
        return out
    for table in ('policy_vector_qwen3', 'policy_vector'):
        columns = _columns(conn, table)
        if not columns or 'policy_code' not in columns:
            continue
        try:
            rows = conn.execute(
                text('SELECT policy_code, COUNT(*) AS n FROM %s WHERE policy_code IN :codes '
                     'GROUP BY policy_code' % table).bindparams(
                    bindparam('codes', expanding=True)), {'codes': codes}).mappings().all()
        except Exception:  # noqa: BLE001 - 向量表结构差异不影响治理报告
            continue
        for row in rows:
            out.setdefault(row['policy_code'], {})[table] = int(row['n'] or 0)
    return out


def _policy_inputs(conn, code):
    """读取执行时刻的最新政策行与明细地区（apply 用，避免沿用检测时的陈旧文本）。"""
    columns = _columns(conn, 'policy')
    if columns is None:
        raise LookupError('policy 表不存在')
    wanted = [name for name in ('policy_id', 'policy_code', 'policy_name', 'regions',
                                'status', 'cur_version') if name in columns]
    row = _fetch_one(conn, 'SELECT %s FROM policy WHERE policy_code=:code' % ', '.join(wanted),
                     {'code': code})
    if row is None:
        raise LookupError('政策不存在：%s' % code)
    policy = dict(row)
    details = []
    detail_columns = _columns(conn, 'policy_detail')
    if detail_columns and {'policy_id', 'detail_id'} <= detail_columns:
        region_expr = 'd.region' if 'region' in detail_columns else "''"
        rows = _fetch(conn, 'SELECT d.detail_id AS detail_id, %s AS region FROM policy_detail d '
                            'WHERE d.policy_id=:pid' % region_expr,
                      {'pid': policy.get('policy_id')})
        details = [(str(item['detail_id']), item['region'] or '') for item in rows]
    return policy, details


def _runs_for(policy, details, default_province):
    """按最新政策行/明细计算 scope 行（纯函数包装，便于 apply 内部复用）。"""
    return policy_scope.rows_for(policy.get('policy_code') or '',
                                 policy.get('regions') or '', details,
                                 default_province=default_province)


# ---------------- 检测：空壳政策 ----------------

def scan_empty_shells(conn, limit=MAX_SAMPLE):
    """检测可归档空壳：无明细 + regions 空 + 无 scope 行 + 状态在用/停用。

    空壳定义与 deploy/policy_data_audit.py 的 empty_shell 一致（无明细），
    归档口径再叠加「无地区文本、无结构化范围、状态可流转」，避免把
    「有明细但明细为空」或「已归档/草稿」的政策误纳入。
    """
    policy_columns = _columns(conn, 'policy')
    if policy_columns is None:
        return {'counts': {'total_policies': 0, 'no_detail': 0, 'eligible': 0},
                'items': [], 'truncated': False, 'notes': ['policy 表不存在']}
    detail_exists = _has_table(conn, 'policy_detail')
    scope_exists = _has_table(conn, 'policy_scope')
    version_exists = _has_table(conn, 'policy_version')
    level_expr = 'p."level"' if 'level' in policy_columns else 'NULL'
    regions_expr = 'p.regions' if 'regions' in policy_columns else 'NULL'
    scope_expr = '(SELECT COUNT(*) FROM policy_scope s WHERE s.policy_code = p.policy_code)' \
        if scope_exists else '0'
    version_expr = '(SELECT COUNT(*) FROM policy_version v WHERE v.policy_id = p.policy_id)' \
        if version_exists else '0'
    where = ''
    if detail_exists:
        where = ' WHERE NOT EXISTS (SELECT 1 FROM policy_detail d WHERE d.policy_id = p.policy_id)'
    sql = ('SELECT p.policy_id AS policy_id, p.policy_code AS policy_code, '
           'p.policy_name AS policy_name, %s AS level, p.status AS status, '
           '%s AS regions, %s AS scope_rows, %s AS versions '
           'FROM policy p%s ORDER BY %s, p.policy_code' %
           (level_expr, regions_expr, scope_expr, version_expr, where, level_expr))
    rows = _fetch(conn, sql)
    total_policies = int(_scalar(conn, 'SELECT COUNT(*) FROM policy') or 0)

    eligible, skipped, by_level = [], Counter(), Counter()
    null_regions = zero_scope = zero_versions = 0
    in_use = 0
    for row in rows:
        state = lifecycle.state_of(row['status'])
        regions = row['regions'] or ''
        scope_rows = int(row['scope_rows'] or 0)
        versions = int(row['versions'] or 0)
        level = row['level'] or ''
        by_level[level] += 1
        if _blank(regions):
            null_regions += 1
        if scope_rows == 0:
            zero_scope += 1
        if versions == 0:
            zero_versions += 1
        if row['status'] == '在用':
            in_use += 1
        item = {
            'policy_id': row['policy_id'],
            'policy_code': row['policy_code'],
            'policy_name': row['policy_name'],
            'level': level,
            'status': row['status'],
            'state': state,
            'regions': _excerpt(regions),
            'scope_rows': scope_rows,
            'versions': versions,
        }
        if state in ('active', 'suspended') and _blank(regions) and scope_rows == 0:
            eligible.append(item)
        elif not _blank(regions):
            skipped['has_regions'] += 1
        elif scope_rows > 0:
            skipped['has_scope_rows'] += 1
        else:
            skipped['status_%s' % state] += 1

    vectors = _vector_counts(conn, [item['policy_code'] for item in eligible])
    for item in eligible:
        item['vectors'] = vectors.get(item['policy_code'], {})
    eligible.sort(key=lambda item: (str(item.get('level') or ''), str(item.get('policy_code'))))
    notes = [
        '空壳口径：policy 无明细 + regions 空 + policy_scope 0 行 + 状态可归档（在用/停用）。',
        '归档只改状态并投递 4 类 outbox 事件；向量/规则/图谱由 POLICY_WORKER 消费（默认关闭），'
        '策略级向量会暂时保留。',
    ]
    if not scope_exists:
        notes.append('policy_scope 表不存在：scope_rows 按 0 处理（SQLite 单测库属正常）。')
    return {
        'counts': {
            'total_policies': total_policies,
            'no_detail': len(rows),
            'eligible': len(eligible),
            'in_use': in_use,
            'by_level': dict(sorted(by_level.items())),
            'null_regions': null_regions,
            'zero_scope': zero_scope,
            'zero_versions': zero_versions,
            'skipped': dict(sorted(skipped.items())),
        },
        'items': eligible[:SCAN_ITEMS_CAP],
        'truncated': len(eligible) > SCAN_ITEMS_CAP,
        'notes': notes,
    }


# ---------------- 检测：范围回填 ----------------

def _detail_map(conn):
    """{policy_code: [(detail_id, region_text), ...]}；表缺失返回 {}。"""
    table_columns = _columns(conn, 'policy_detail')
    if table_columns is None or 'policy_id' not in table_columns:
        return {}
    region_expr = 'd.region' if 'region' in table_columns else "''"
    rows = _fetch(conn, 'SELECT d.detail_id AS detail_id, %s AS region, '
                        'p.policy_code AS policy_code FROM policy_detail d '
                        'LEFT JOIN policy p ON p.policy_id = d.policy_id' % region_expr)
    out = {}
    for row in rows:
        code = row['policy_code']
        if not code:
            continue
        out.setdefault(code, []).append((str(row['detail_id']), row['region'] or ''))
    return out


def scan_scope_backfill(conn, codes=None, limit=MAX_SAMPLE, default_province=JX_PROVINCE):
    """检测需要回填 scope 的政策：--codes 指定或「policy_scope 无任何行」。

    dry-run 只做纯函数计算；apply 时由 apply_scope_change 重建并投递缓存失效。
    """
    policy_columns = _columns(conn, 'policy')
    if policy_columns is None:
        return {'counts': {'targets': 0, 'with_rows': 0, 'empty_rows': 0},
                'items': [], 'truncated': False, 'notes': ['policy 表不存在']}
    scope_exists = _has_table(conn, 'policy_scope')
    regions_expr = 'p.regions' if 'regions' in policy_columns else 'NULL'
    name_expr = 'p.policy_name' if 'policy_name' in policy_columns else 'NULL'
    notes = []
    codes = [str(code).strip() for code in (codes or []) if str(code).strip()]
    if codes:
        placeholders = ', '.join(':c%d' % index for index in range(len(codes)))
        params = {'c%d' % index: code for index, code in enumerate(codes)}
        rows = _fetch(conn, 'SELECT p.policy_code AS policy_code, %s AS policy_name, '
                            '%s AS regions FROM policy p WHERE p.policy_code IN (%s) '
                            'ORDER BY p.policy_code' % (name_expr, regions_expr, placeholders),
                      params)
        found = {row['policy_code'] for row in rows}
        missing = [code for code in codes if code not in found]
        if missing:
            notes.append('指定编号不存在：%s' % ', '.join(missing[:20]))
    elif scope_exists:
        rows = _fetch(conn, 'SELECT p.policy_code AS policy_code, %s AS policy_name, '
                            '%s AS regions FROM policy p WHERE NOT EXISTS '
                            '(SELECT 1 FROM policy_scope s WHERE s.policy_code = p.policy_code) '
                            'ORDER BY p.policy_code' % (name_expr, regions_expr))
    else:
        notes.append('policy_scope 表不存在：按「全部政策均无 scope 行」处理；'
                     'apply 会先幂等建表，建议配合 --codes 控制范围。')
        rows = _fetch(conn, 'SELECT p.policy_code AS policy_code, %s AS policy_name, '
                            '%s AS regions FROM policy p ORDER BY p.policy_code' %
                      (name_expr, regions_expr))
    details_map = _detail_map(conn)
    items = []
    with_rows = empty_rows = 0
    planned_total = unresolved_total = 0
    empty_samples = []
    for row in rows:
        code = row['policy_code']
        details = details_map.get(code, [])
        scope_rows = _runs_for({'policy_code': code, 'regions': row['regions']}, details,
                               default_province)
        policy_rows = [item for item in scope_rows if not item.get('detail_id')]
        detail_rows = [item for item in scope_rows if item.get('detail_id')]
        unresolved = _sorted_list(item['region_key'] for item in scope_rows
                                  if str(item.get('region_key') or '').startswith('U:'))
        planned_total += len(scope_rows)
        unresolved_total += len(unresolved)
        item = {
            'policy_code': code,
            'policy_name': row['policy_name'],
            'regions': _excerpt(row['regions']),
            'details': len(details),
            'planned_rows': len(scope_rows),
            'policy_rows': len(policy_rows),
            'detail_rows': len(detail_rows),
            'unresolved_keys': unresolved[:10],
            'status': 'ready' if scope_rows else 'skipped',
            'reason': '' if scope_rows else 'no_regions',
        }
        if scope_rows:
            with_rows += 1
        else:
            empty_rows += 1
            if len(empty_samples) < 10:
                empty_samples.append(code)
        if len(items) < SCAN_ITEMS_CAP:
            items.append(item)
    if empty_rows:
        notes.append('无可用地区文本、计划 0 行的政策 %d 条（apply 跳过）：%s'
                     % (empty_rows, ', '.join(empty_samples)))
    return {
        'counts': {
            'targets': len(rows),
            'with_rows': with_rows,
            'empty_rows': empty_rows,
            'planned_rows': planned_total,
            'unresolved_keys': unresolved_total,
            'scope_table_exists': scope_exists,
        },
        'items': items,
        'truncated': len(rows) > SCAN_ITEMS_CAP,
        'notes': notes,
    }


# ---------------- 检测：范围不一致 ----------------

def _scope_policy_keys(conn):
    """{policy_code: set(policy 级 region_key)}；policy 级行 detail_id 为 ''/'/' 占位。"""
    out = {}
    for row in _fetch(conn, 'SELECT policy_code, detail_id, region_key, scope_kind FROM policy_scope'):
        code = row['policy_code']
        detail_id = row['detail_id']
        kind = str(row['scope_kind'] or '')
        is_policy = (detail_id is None or str(detail_id) in ('', '-') or kind == 'policy')
        if is_policy:
            out.setdefault(code, set()).add(str(row['region_key'] or ''))
    return out


def scan_scope_mismatch(conn, limit=MAX_SAMPLE, default_province=JX_PROVINCE):
    """检测政策级 scope 与 regions 文本归一集合的差异。

    口径：expected = policy_scope.rows_for(regions, 无明细, default_province)。
    default_province=江西省 时裸「全省」归一为 P:江西省（与 deploy/migrate_policy_scope.py 一致）。
    deploy/policy_data_audit.py 不传 default_province，会把裸「全省」记为 U:全省 并报
    X-JY-038 不一致；这是同一事实的两种口径，报告里单列 audit_parity 说明，不重复写入。
    """
    policy_columns = _columns(conn, 'policy')
    if policy_columns is None:
        return {'counts': {'policies': 0, 'mismatch': 0, 'safe_to_fix': 0},
                'items': [], 'truncated': False, 'notes': ['policy 表不存在']}
    if not _has_table(conn, 'policy_scope'):
        return {'counts': {'policies': 0, 'mismatch': 0, 'safe_to_fix': 0,
                           'table_missing': True},
                'items': [], 'truncated': False,
                'notes': ['policy_scope 表不存在：无从比较，apply 需先回填范围（backfill-scope）。']}
    actual_map = _scope_policy_keys(conn)
    details_map = _detail_map(conn)
    regions_expr = 'p.regions' if 'regions' in policy_columns else 'NULL'
    name_expr = 'p.policy_name' if 'policy_name' in policy_columns else 'NULL'
    policies = _fetch(conn, 'SELECT p.policy_code AS policy_code, %s AS policy_name, '
                            '%s AS regions FROM policy p ORDER BY p.policy_code' %
                      (name_expr, regions_expr))
    items, audit_parity = [], []
    for row in policies:
        code = row['policy_code']
        regions = row['regions'] or ''
        actual = actual_map.get(code, set())
        expected = {item['region_key'] for item in
                    policy_scope.rows_for(code, regions, (), default_province=default_province)}
        raw_expected = {item['region_key'] for item in policy_scope.rows_for(code, regions, ())}
        missing = expected - actual
        extra = actual - expected
        if missing or extra:
            details = details_map.get(code, [])
            full_rows = policy_scope.rows_for(code, regions, details,
                                              default_province=default_province)
            safe = bool(full_rows) or not extra
            item = {
                'policy_code': code,
                'policy_name': row['policy_name'],
                'regions': _excerpt(regions),
                'missing_scope': _sorted_list(missing)[:10],
                'extra_scope': _sorted_list(extra)[:10],
                'unresolved_keys': _sorted_list(key for key in expected if key.startswith('U:'))[:10],
                'planned_rows': len(full_rows),
                'scope_rows_before': len(actual),
                'safe': safe,
                'reason': '' if safe else 'would_clear_scope_rows',
            }
            if len(items) < SCAN_ITEMS_CAP:
                items.append(item)
        if raw_expected != actual:
            if len(audit_parity) < SCAN_ITEMS_CAP:
                audit_parity.append({
                    'policy_code': code,
                    'regions': _excerpt(regions),
                    'expected_without_default': _sorted_list(raw_expected)[:10],
                    'actual': _sorted_list(actual)[:10],
                })
    fixable = [item for item in items if item['safe']]
    notes = [
        '比较口径：policy 级 scope 行（detail_id 为 \'\' 或 \'-\'）vs 按 default_province=%s '
        '归一的 regions；不一致只重建 scope 行，不改 policy.regions 文本。' % default_province,
        'audit_parity=%d：deploy/policy_data_audit.py 不启用 default_province，裸「全省」会记 U:全省；'
        '本工具按江西省口径归一为 P:江西省，属口径差异，报告单列不写库。' % len(audit_parity),
        'apply 只重建 safe=True 的政策；期望 0 行但现有 scope 行非空（would_clear_scope_rows）'
        '涉及「删行」风险，跳过并转人工。',
    ]
    return {
        'counts': {
            'policies': len(policies),
            'mismatch': len(items),
            'safe_to_fix': len(fixable),
            'unsafe': len(items) - len(fixable),
            'audit_parity': len(audit_parity),
            'scope_policies': len(actual_map),
        },
        'items': items[:SCAN_ITEMS_CAP],
        'truncated': len(items) > SCAN_ITEMS_CAP,
        'audit_parity_items': audit_parity[:MAX_SAMPLE],
        'notes': notes,
    }


# ---------------- 检测：人工处理清单（只读） ----------------

def scan_report_manual(conn, limit=MAX_SAMPLE):
    """跨级同名与完全同文重复组清单；只输出，不合并、不归档。"""
    policy_columns = _columns(conn, 'policy')
    detail_columns = _columns(conn, 'policy_detail')
    notes = ['只输出清单：重复合并需保留最早 detail_id 并人工确认版本/引用；本动作恒只读。']
    duplicate_groups = duplicate_covered = duplicate_redundant = 0
    cross_names = cross_covered = 0
    items = []
    if detail_columns and policy_columns is not None:
        rows = _fetch(conn, 'SELECT d.detail_id AS detail_id, d.region AS region, '
                            'd.conditions AS conditions, d.target AS target, '
                            'p.policy_code AS policy_code, p.policy_name AS policy_name, '
                            'p."level" AS level FROM policy_detail d '
                            'LEFT JOIN policy p ON p.policy_id = d.policy_id '
                            'ORDER BY d.detail_id')
        groups = OrderedDict()
        for row in rows:
            key = (row['region'], row['conditions'], row['target'])
            groups.setdefault(key, []).append(row)
        dups = [(key, members) for key, members in groups.items() if len(members) > 1]
        duplicate_groups = len(dups)
        duplicate_covered = sum(len(members) for _, members in dups)
        duplicate_redundant = duplicate_covered - duplicate_groups
        dups.sort(key=lambda pair: (-len(pair[1]), str(pair[0][0])))
        for key, members in dups[:limit]:
            codes = list(OrderedDict((member['policy_code'], None) for member in members))
            items.append({
                'kind': 'duplicate_group',
                'region': key[0],
                'conditions': _excerpt(key[1]),
                'target': _excerpt(key[2]),
                'rows': len(members),
                'policies': len(codes),
                'keep_detail_id': members[0]['detail_id'],
                'detail_ids': [member['detail_id'] for member in members],
                'codes': codes,
                'levels': sorted({member['level'] for member in members if member['level']}),
            })
    else:
        notes.append('policy_detail 表不存在：跳过重复组检测。')

    if policy_columns is not None and 'level' in policy_columns:
        rows = _fetch(conn, 'SELECT p.policy_code AS policy_code, p.policy_name AS policy_name, '
                            'p."level" AS level FROM policy p ORDER BY p.policy_name, p."level", '
                            'p.policy_code')
        by_name = OrderedDict()
        for row in rows:
            entry = by_name.setdefault(row['policy_name'], {'levels': {}, 'rows': 0})
            entry['levels'].setdefault(row['level'], []).append(row['policy_code'])
            entry['rows'] += 1
        cross = [(name, entry) for name, entry in by_name.items()
                 if 'X' in entry['levels'] and 'S' in entry['levels']]
        cross_names = len(cross)
        cross_covered = sum(entry['rows'] for _, entry in cross)
        cross.sort(key=lambda pair: (-pair[1]['rows'], pair[0]))
        for name, entry in cross[:limit]:
            items.append({
                'kind': 'cross_level_name',
                'policy_name': name,
                'levels': sorted(entry['levels']),
                'codes': {level: entry['levels'][level] for level in sorted(entry['levels'])},
                'rows': entry['rows'],
            })
    return {
        'counts': {
            'duplicate_groups': duplicate_groups,
            'duplicate_covered_rows': duplicate_covered,
            'duplicate_redundant_rows': duplicate_redundant,
            'cross_level_names': cross_names,
            'cross_level_covered_rows': cross_covered,
            'writable': False,
        },
        'items': items[:SCAN_ITEMS_CAP],
        'truncated': len(items) > SCAN_ITEMS_CAP,
        'notes': notes,
    }


# ---------------- 写操作（只经既有函数） ----------------

def apply_archive(conn, items, limit, operator, reason):
    """逐条 lifecycle.archive：每条一个 SAVEPOINT，失败不拖垮整批。"""
    detail = {'applied': [], 'errors': [], 'skipped': []}
    for item in list(items or [])[:limit]:
        code = item.get('policy_code')
        if not code:
            continue
        try:
            with conn.begin_nested():
                result = lifecycle.archive(conn, code, reason, operator)
        except lifecycle.LifecycleError as exc:
            detail['errors'].append({
                'policy_code': code,
                'error': getattr(exc, 'message', str(exc)),
                'code': getattr(exc, 'code', ''),
            })
        except Exception as exc:  # noqa: BLE001 - 单条失败记录后继续，退出码 4
            detail['errors'].append({
                'policy_code': code,
                'error': '%s: %s' % (type(exc).__name__, exc),
            })
        else:
            impact = result.get('impact') or {}
            detail['applied'].append({
                'policy_code': code,
                'policy_name': result.get('policy_name'),
                'state_before': result.get('state_before'),
                'status_after': result.get('status_after'),
                'outbox_events': result.get('outbox_events'),
                'audit_logged': result.get('audit_logged'),
                'impact': {key: impact.get(key) for key in
                           ('details', 'scope_rows', 'rules', 'versions', 'vectors')},
            })
    return detail


def apply_scope_change(conn, items, limit, default_province, operator, reason, action):
    """重建 scope 行：policy_scope.replace_policy + outbox.policy.cache_invalidate。

    action: backfill-scope | fix-scope-mismatch。backfill 跳过无地区文本政策；
    fix 跳过 would_clear_scope_rows（删除现有范围的判断转人工）。
    """
    policy_scope.ensure_schema(conn)
    outbox.ensure_schema(conn)
    detail = {'applied': [], 'errors': [], 'skipped': []}
    for item in list(items or [])[:limit]:
        code = item.get('policy_code')
        if not code:
            continue
        if action == 'backfill-scope' and not item.get('planned_rows'):
            detail['skipped'].append({'policy_code': code, 'reason': 'no_regions'})
            continue
        if action == 'fix-scope-mismatch' and item.get('safe') is False:
            detail['skipped'].append({'policy_code': code,
                                      'reason': item.get('reason') or 'unsafe_scope_change'})
            continue
        try:
            with conn.begin_nested():
                policy, details = _policy_inputs(conn, code)
                rows = _runs_for(policy, details, default_province)
                if not rows:
                    detail['skipped'].append({'policy_code': code, 'reason': 'no_rows_at_apply'})
                    continue
                written = policy_scope.replace_policy(conn, code, rows)
                event = outbox.enqueue(conn, 'policy.cache_invalidate', {
                    'policy_code': code,
                    'action': action,
                    'reason': str(reason or '')[:400],
                    'operator': str(operator or '')[:64],
                })
        except Exception as exc:  # noqa: BLE001 - 单条失败记录后继续
            detail['errors'].append({
                'policy_code': code,
                'error': '%s: %s' % (type(exc).__name__, exc),
            })
        else:
            detail['applied'].append({
                'policy_code': code,
                'rows_written': written,
                'outbox_event': event,
                'scope_rows_before': item.get('scope_rows_before'),
            })
    return detail


# ---------------- 编排 ----------------

def _reason_for(action, reason):
    return str(reason or '').strip() or REASON_DEFAULTS[action]


def _guard_check(apply, confirm, operator, reason):
    """写护栏：开关 + confirm + operator/reason 校验；不通过抛 GovernanceRefused。"""
    if not apply:
        return
    if not apply_allowed():
        raise GovernanceRefused('拒绝写入：--apply 需要环境变量 POLICY_GOVERNANCE=1（当前 %r）。%s'
                                % (os.environ.get(SWITCH_ENV, '0'), SWITCH_HINT))
    if not confirm:
        raise GovernanceRefused('拒绝写入：--apply 必须同时带 --confirm（二次确认）。')
    if not str(operator or '').strip():
        raise GovernanceRefused('拒绝写入：--operator 不能为空。')
    if reason is not None and str(reason).strip() and len(str(reason).strip()) < 4:
        raise GovernanceRefused('拒绝写入：--reason 不能少于 4 个字符。')


def _archive_section(conn, apply, limit, operator, reason):
    before = scan_empty_shells(conn, limit=limit)
    section = {
        'kind': 'write',
        'summary': '无明细政策 %d 条，其中可归档空壳 %d 条'
                   % (before['counts'].get('no_detail', 0), before['counts'].get('eligible', 0)),
        'before': before['counts'],
        'items': before['items'],
        'notes': list(before['notes']),
        'truncated': before['truncated'],
        'errors': [],
        'skipped': [],
    }
    if not apply:
        planned = before['items'][:limit]
        remaining = max(0, before['counts'].get('eligible', 0) - len(planned))
        section.update({
            'applied': 0,
            'would_change': len(planned),
            'remaining_after': remaining,
            'after': {'projected': True, 'eligible': remaining,
                      'archived': len(planned)},
        })
        return section
    result = apply_archive(conn, before['items'], limit, operator, reason)
    after = scan_empty_shells(conn, limit=limit)
    section.update({
        'applied': len(result['applied']),
        'applied_items': result['applied'][:MAX_SAMPLE],
        'errors': result['errors'],
        'skipped': result['skipped'],
        'after': after['counts'],
        'after_items': after['items'][:MAX_SAMPLE],
    })
    return section


def _backfill_section(conn, apply, limit, operator, reason, codes, default_province):
    before = scan_scope_backfill(conn, codes=codes, limit=limit,
                                 default_province=default_province)
    section = {
        'kind': 'write',
        'summary': '待回填政策 %d 条（可生成 %d 行 scope，空地区 %d 条）'
                   % (before['counts'].get('targets', 0),
                      before['counts'].get('with_rows', 0),
                      before['counts'].get('empty_rows', 0)),
        'before': before['counts'],
        'items': before['items'],
        'notes': list(before['notes']),
        'truncated': before['truncated'],
        'errors': [],
        'skipped': [],
    }
    if not apply:
        planned = [item for item in before['items'][:limit] if item.get('planned_rows')]
        remaining = max(0, before['counts'].get('with_rows', 0) - len(planned))
        section.update({
            'applied': 0,
            'would_change': len(planned),
            'remaining_after': remaining,
            'after': {'projected': True, 'with_rows': remaining,
                      'planned_rows': sum(item.get('planned_rows', 0) for item in planned)},
        })
        return section
    result = apply_scope_change(conn, before['items'], limit, default_province, operator,
                                reason, 'backfill-scope')
    after = scan_scope_backfill(conn, codes=codes, limit=limit,
                                default_province=default_province)
    section.update({
        'applied': len(result['applied']),
        'applied_items': result['applied'][:MAX_SAMPLE],
        'errors': result['errors'],
        'skipped': result['skipped'],
        'after': after['counts'],
        'after_items': after['items'][:MAX_SAMPLE],
    })
    return section


def _mismatch_section(conn, apply, limit, operator, reason, default_province):
    before = scan_scope_mismatch(conn, limit=limit, default_province=default_province)
    section = {
        'kind': 'write',
        'summary': 'scope 不一致政策 %d 条（可安全重建 %d 条），审计口径差异 %d 条'
                   % (before['counts'].get('mismatch', 0),
                      before['counts'].get('safe_to_fix', 0),
                      before['counts'].get('audit_parity', 0)),
        'before': before['counts'],
        'items': before['items'],
        'notes': list(before['notes']),
        'truncated': before['truncated'],
        'errors': [],
        'skipped': [],
    }
    if before.get('audit_parity_items'):
        section['audit_parity_items'] = before['audit_parity_items']
    if not apply:
        planned = [item for item in before['items'][:limit] if item.get('safe')]
        remaining = max(0, before['counts'].get('safe_to_fix', 0) - len(planned))
        section.update({
            'applied': 0,
            'would_change': len(planned),
            'remaining_after': remaining,
            'after': {'projected': True, 'mismatch': remaining,
                      'planned_rows': sum(item.get('planned_rows', 0) for item in planned)},
        })
        return section
    result = apply_scope_change(conn, before['items'], limit, default_province, operator,
                                reason, 'fix-scope-mismatch')
    after = scan_scope_mismatch(conn, limit=limit, default_province=default_province)
    section.update({
        'applied': len(result['applied']),
        'applied_items': result['applied'][:MAX_SAMPLE],
        'errors': result['errors'],
        'skipped': result['skipped'],
        'after': after['counts'],
        'after_items': after['items'][:MAX_SAMPLE],
        'after_audit_parity_items': after.get('audit_parity_items', []),
    })
    return section


def govern(conn, action='all', apply=False, limit=DEFAULT_LIMIT, operator='', reason=None,
           codes=None, default_province=JX_PROVINCE, confirm=False):
    """执行治理编排，返回结构化报告（dry-run 只读；apply 需通过 _guard_check）。"""
    _guard_check(apply, confirm, operator, reason)
    actions = ACTIONS if action == 'all' else (action,)
    report = {
        'command': 'policy-governance',
        'version': VERSION,
        'generated_at': _now(),
        'mode': 'apply' if apply else 'dry-run',
        'action': action,
        'limit': int(limit),
        'operator': str(operator or ''),
        'reason': str(reason or ''),
        'default_province': default_province,
        'codes': list(codes or []),
        'dialect': getattr(conn.dialect, 'name', None),
        'guards': {
            'switch': SWITCH_ENV,
            'switch_enabled': apply_allowed(),
            'confirm': bool(confirm),
            'apply': bool(apply),
        },
        'actions': {},
    }
    for name in actions:
        effective_reason = _reason_for(name, reason) if name in REASON_DEFAULTS else None
        if name == 'report-manual':
            section = scan_report_manual(conn, limit=limit)
            section['applied'] = 0
            section['would_change'] = 0
        elif name == 'archive-empty-shells':
            section = _archive_section(conn, apply, limit, operator, effective_reason)
        elif name == 'backfill-scope':
            section = _backfill_section(conn, apply, limit, operator, effective_reason,
                                        codes, default_province)
        elif name == 'fix-scope-mismatch':
            section = _mismatch_section(conn, apply, limit, operator, effective_reason,
                                        default_province)
        else:  # pragma: no cover - argparse choices 已限制
            raise GovernanceRefused('未知动作：%s' % name)
        section['action'] = name
        section['reason'] = effective_reason
        report['actions'][name] = section
    return report


# ---------------- 报告打印 ----------------

def _format_lines(prefix, values):
    if not isinstance(values, dict):
        return ['%s%s' % (prefix, values)]
    return ['%s%s: %s' % (prefix, key, value) for key, value in values.items()]


def format_report(report):
    lines = ['=' * 86]
    lines.append('政策数据治理报告  mode=%s  action=%s  %s'
                 % (report.get('mode'), report.get('action'), report.get('generated_at')))
    lines.append('数据库=%s  limit=%s  default_province=%s'
                 % (report.get('dialect'), report.get('limit'), report.get('default_province')))
    guards = report.get('guards') or {}
    lines.append('护栏: %s=%s  confirm=%s  operator=%s'
                 % (guards.get('switch'), guards.get('switch_enabled'), guards.get('confirm'),
                    report.get('operator') or '-'))
    lines.append('-' * 86)
    for name in ACTIONS:
        section = (report.get('actions') or {}).get(name)
        if not section:
            continue
        lines.append('[%s] %s' % (name, section.get('summary') or ''))
        lines.extend(_format_lines('  before. ', section.get('before') or {}))
        if section.get('after'):
            lines.extend(_format_lines('  after.  ', section.get('after') or {}))
        if section.get('would_change'):
            lines.append('  would_change=%s  remaining_after=%s'
                         % (section.get('would_change'), section.get('remaining_after')))
        if section.get('applied'):
            lines.append('  applied=%s' % section.get('applied'))
        for item in (section.get('applied_items') or [])[:MAX_SAMPLE]:
            lines.append('  + %s -> %s（outbox=%s）'
                         % (item.get('policy_code'), item.get('status_after'),
                            item.get('outbox_events')))
        if section.get('items') and not section.get('applied_items'):
            for item in section.get('items')[:MAX_SAMPLE]:
                if item.get('kind'):
                    lines.append('  · %s' % _manual_item_line(item))
                else:
                    lines.append('  - %s %s（scope_before=%s planned=%s）'
                                 % (item.get('policy_code'), item.get('policy_name') or '',
                                    item.get('scope_rows_before', item.get('scope_rows')),
                                    item.get('planned_rows')))
        for skipped in (section.get('skipped') or [])[:MAX_SAMPLE]:
            lines.append('  skip %s: %s' % (skipped.get('policy_code'), skipped.get('reason')))
        for error in (section.get('errors') or [])[:MAX_SAMPLE]:
            lines.append('  ERROR %s: %s' % (error.get('policy_code'), error.get('error')))
        for note in section.get('notes') or []:
            lines.append('  note: %s' % note)
        if section.get('truncated'):
            lines.append('  note: 清单已截断，counts 为全量。')
        lines.append('-' * 86)
    return '\n'.join(lines)


def _manual_item_line(item):
    if item.get('kind') == 'cross_level_name':
        return '跨级同名 %s（%d 行，级别 %s）' % (
            item.get('policy_name'), item.get('rows') or 0, ','.join(item.get('levels') or []))
    return '重复组 region=%s rows=%d keep=%s codes=%s' % (
        item.get('region'), item.get('rows') or 0, item.get('keep_detail_id'),
        ','.join(str(code) for code in (item.get('codes') or [])[:5]))


# ---------------- CLI ----------------

def _parse_codes(raw_values):
    codes = []
    for raw in raw_values or []:
        for part in str(raw).split(','):
            part = part.strip()
            if part and part not in codes:
                codes.append(part)
    return codes


def _build_parser():
    parser = argparse.ArgumentParser(
        description='政策数据治理（默认 dry-run 只读；--apply 需 POLICY_GOVERNANCE=1 与 --confirm）')
    parser.add_argument('--report', action='store_true', help='打印人类可读报告（默认行为）')
    parser.add_argument('--apply', action='store_true',
                        help='执行写操作（需 POLICY_GOVERNANCE=1 且 --confirm）')
    parser.add_argument('--confirm', action='store_true', help='--apply 的二次确认')
    parser.add_argument('--action', choices=ACTIONS + ('all',), default='all',
                        help='治理动作（默认 all）')
    parser.add_argument('--operator', default=None,
                        help='操作人（默认 env POLICY_GOVERNANCE_OPERATOR 或 %s）' % DEFAULT_OPERATOR)
    parser.add_argument('--reason', default=None, help='操作原因（不少于 4 字符）')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                        help='单次最多处理条数（默认 %d）' % DEFAULT_LIMIT)
    parser.add_argument('--codes', action='append', default=None,
                        help='指定政策编号（逗号分隔可重复）；用于 backfill-scope')
    parser.add_argument('--default-province', default=None,
                        help='缺省省份（默认 env POLICY_SCOPE_DEFAULT_PROVINCE 或 江西省）')
    parser.add_argument('--json', action='store_true', help='输出 JSON（默认人类可读）')
    parser.add_argument('--env-file', default=None,
                        help='KEY=VALUE 环境文件（默认 deploy/web.env，不存在则忽略）')
    parser.add_argument('--url', default=None, help='数据库 URL（覆盖环境变量与 config）')
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit 不能为负')
    load_env(args.env_file or DEFAULT_ENV_FILE)
    operator = (args.operator or os.environ.get('POLICY_GOVERNANCE_OPERATOR')
                or DEFAULT_OPERATOR).strip()
    reason = (args.reason or '').strip() or None
    default_province = (args.default_province
                        or os.environ.get('POLICY_SCOPE_DEFAULT_PROVINCE')
                        or JX_PROVINCE).strip()
    codes = _parse_codes(args.codes)
    writes = args.action in WRITE_ACTIONS or args.action == 'all'
    apply = bool(args.apply and writes)
    try:
        _guard_check(apply, args.confirm, operator, reason)
    except GovernanceRefused as exc:
        sys.stderr.write('%s\n' % exc)
        return EXIT_REFUSED

    url = database_url(args.url)
    try:
        engine = build_engine(url)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write('无法初始化数据库连接（未执行任何操作）: %s\n' % exc)
        return EXIT_REFUSED

    conn = None
    tx = None
    report = None
    try:
        try:
            conn = engine.connect()
            if apply:
                tx = conn.begin()
            else:
                _set_readonly(conn)
        except Exception as exc:  # noqa: BLE001 - 连接失败：只读/写入都不执行
            sys.stderr.write('无法连接数据库（未执行任何操作）: %s\n' % exc)
            return EXIT_REFUSED
        report = govern(conn, action=args.action, apply=apply, limit=args.limit,
                        operator=operator, reason=reason, codes=codes,
                        default_province=default_province, confirm=bool(args.confirm))
        if tx is not None:
            tx.commit()
    except GovernanceRefused as exc:
        if tx is not None:
            tx.rollback()
        sys.stderr.write('%s\n' % exc)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - 整批回滚，保持幂等可重试
        if tx is not None:
            tx.rollback()
        sys.stderr.write('治理执行失败（已回滚，未写库）: %s\n' % exc)
        return EXIT_ERROR
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001
            pass

    if args.json:
        print(_safe_json(report))
        if args.report:
            sys.stderr.write(format_report(report) + '\n')
    else:
        print(format_report(report))
    errors = sum(len(section.get('errors') or [])
                 for section in (report.get('actions') or {}).values())
    return EXIT_PARTIAL if (apply and errors) else EXIT_OK


if __name__ == '__main__':
    sys.exit(main())