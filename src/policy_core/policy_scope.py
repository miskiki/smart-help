# -*- coding: utf-8 -*-
"""政策地区范围结构化层（P4 全国化基础）。

问题：policy.regions / policy_detail.region 是自由文本（573 / 109 个取值），
召回用 LIKE、过滤用五套口径，无法跨省、无法表达市/全国层级、无法审计。
本模块把它们归一成 `policy_scope` 结构化行：一行 = 一条政策（或明细）的一个地区范围。

设计原则：
- 只做数据与纯判定，不改运行期匹配行为；接入由调用方用开关控制；
- 归一复用 admin_division（全国区划维表），无法解析的值保留为 level=unknown + 待审队列，不静默丢弃；
- 判定沿用 region.py 的并集语义：任一范围适用 → 适用；全部明确不适用 → 不适用；其余 unknown（保留候选）；
- 功能区与 host 县区可能重叠：政策是功能区、家庭是同市其它县区时判 unknown（保留、不误放行）。

对外接口：
  ensure_schema(conn=None)                 建表（幂等）
  split_regions(text)                      多值地区文本 -> token 列表
  rows_for(policy_code, regions, details)  计算单个政策的 scope 行（纯函数）
  replace_policy(conn, code, rows)         覆盖写某个政策的 scope 行
  load_scopes(conn, codes)                 批量读 {code: {'policy': [...], 'detail': [...]}}
  match(scopes, household)                 政策范围 vs 家庭地区 -> 三态判定
  backfill(dry_run=True)                   全库回填 + 报告
  stats()                                  表内分布统计
"""
import json
import os
import re

from sqlalchemy import create_engine, text

try:  # 金仓版本串补丁，必须在建 engine 前导入
    import config  # noqa: F401
except Exception:  # 独立脚本环境没有 config
    pass

from . import admin_division

VERSION = 'policy-scope-1'

SCOPE_KIND_POLICY = 'policy'
SCOPE_KIND_DETAIL = 'detail'

# Kingbase 兼容 Oracle 模式把空串当 NULL，而 detail_id 在主键里不能为空；
# 政策级行用 '-' 占位，读出时还原成 ''。
_DETAIL_NONE = '-'

# 多值分隔符：顿号/中英文逗号/分号/斜杠/竖线/换行；空格不作为分隔（"江西省 赣州市" 应整体归一）。
_SPLIT_RE = re.compile(r'[、,，;；/|\n\r]+')
_BLANK_TOKENS = {'', '-', '—', '无', '不限', '全省', '全国'}  # '全省'/'全国' 由 canonical 解释，不丢

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS policy_scope (
        policy_code   varchar(64)  NOT NULL,
        detail_id     varchar(64)  NOT NULL DEFAULT '',
        scope_kind    varchar(16)  NOT NULL,
        source_field  varchar(32)  NOT NULL,
        raw_text      text,
        scope_level   varchar(16)  NOT NULL,
        province      varchar(64),
        city          varchar(64),
        county        varchar(64),
        region_key    varchar(200) NOT NULL,
        zone          boolean      NOT NULL DEFAULT false,
        confidence    varchar(8),
        updated_at    timestamp    DEFAULT current_timestamp,
        PRIMARY KEY (policy_code, detail_id, region_key)
    )""",
    'CREATE INDEX IF NOT EXISTS policy_scope_region_idx ON policy_scope(region_key, scope_kind)',
    'CREATE INDEX IF NOT EXISTS policy_scope_level_idx ON policy_scope(scope_level)',
]

_ENGINE = None


def database_url():
    explicit = os.environ.get('ADMIN_DB_URL')
    if explicit:
        return explicit
    try:
        url = config.DATABASE_URL
        if url.startswith('postgresql'):
            return url
    except Exception:
        pass
    return 'sqlite:///' + os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       '..', '..', 'data', 'policy_db', 'smart_policy.db')


def engine(url=None):
    global _ENGINE
    if url:
        return create_engine(url, pool_pre_ping=True)
    if _ENGINE is None:
        _ENGINE = create_engine(database_url(), pool_pre_ping=True, pool_size=4, max_overflow=2)
    return _ENGINE


def ensure_schema(conn=None, url=None):
    own = conn is None
    if own:
        conn = engine(url).begin()
    try:
        for statement in _SCHEMA:
            conn.execute(text(statement))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def split_regions(value):
    """把 policy.regions 一类多值文本拆成 token 列表（保序去重）。"""
    out, seen = [], set()
    for token in _SPLIT_RE.split(value or ''):
        token = token.strip().strip('；;，,、')
        if token in ('', '-'):
            continue
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _row(policy_code, detail_id, scope_kind, source_field, raw_text, default_province=None):
    parsed = admin_division.canonical(raw_text, default_province=default_province)
    level = parsed['level']
    if parsed.get('national'):
        key = 'CN'
    elif level == admin_division.UNKNOWN:
        key = 'U:' + raw_text
    elif level == admin_division.PROVINCE:
        key = 'P:' + (parsed['province'] or raw_text)
    elif level == admin_division.CITY:
        key = 'C:%s/%s' % (parsed['province'] or '', parsed['city'] or raw_text)
    else:
        key = 'X:%s/%s/%s' % (parsed['province'] or '', parsed['city'] or '',
                              parsed['county'] or raw_text)
        if parsed.get('zone'):
            key = 'Z:' + key[2:]
    confidence = 'high' if level != admin_division.UNKNOWN and not parsed.get('ambiguous') else 'low'
    return {
        'policy_code': policy_code,
        'detail_id': '' if detail_id is None else str(detail_id),
        'scope_kind': scope_kind,
        'source_field': source_field,
        'raw_text': raw_text,
        'level': level,
        'province': parsed.get('province'),
        'city': parsed.get('city'),
        'county': parsed.get('county'),
        'region_key': key,
        'zone': bool(parsed.get('zone')),
        'confidence': confidence,
    }


def rows_for(policy_code, regions='', details=(), default_province=None):
    """计算单个政策的 scope 行（纯函数）。

    regions: policy.regions 文本；details: [(detail_id, region_text), ...]。
    """
    rows, seen = [], set()

    def add(row):
        key = (row['detail_id'], row['region_key'])
        if key not in seen:
            seen.add(key)
            rows.append(row)

    for token in split_regions(regions):
        add(_row(policy_code, '', SCOPE_KIND_POLICY, 'policy.regions', token,
                 default_province=default_province))
    for detail_id, region in details:
        for token in split_regions(region):
            add(_row(policy_code, detail_id, SCOPE_KIND_DETAIL, 'detail.region', token,
                     default_province=default_province))
    return rows


def replace_policy(conn, policy_code, rows):
    conn.execute(text('DELETE FROM policy_scope WHERE policy_code = :code'),
                 {'code': policy_code})
    if not rows:
        return 0
    conn.execute(text('''
        INSERT INTO policy_scope
            (policy_code, detail_id, scope_kind, source_field, raw_text,
             scope_level, province, city, county, region_key, zone, confidence)
        VALUES
            (:policy_code, :detail_id, :scope_kind, :source_field, :raw_text,
             :scope_level, :province, :city, :county, :region_key, :zone, :confidence)
    '''), [dict(row, scope_level=row['level'],
                 detail_id=_DETAIL_NONE if row.get('detail_id') in ('', None) else row['detail_id'])
           for row in rows])
    return len(rows)


def load_scopes(conn, codes):
    """读取若干政策的结构化范围。返回 {code: {'policy': rows, 'detail': rows}}。"""
    codes = [c for c in codes if c]
    out = {code: {'policy': [], 'detail': []} for code in codes}
    if not codes:
        return out
    from sqlalchemy import bindparam
    statement = text('''
        SELECT policy_code, detail_id, scope_kind, source_field, raw_text, scope_level,
               province, city, county, region_key, zone, confidence
          FROM policy_scope WHERE policy_code IN :codes
    ''').bindparams(bindparam('codes', expanding=True))
    rows = conn.execute(statement, {'codes': codes}).mappings().all()
    for row in rows:
        item = dict(row)
        item['level'] = item.pop('scope_level')
        if item['detail_id'] == _DETAIL_NONE:
            item['detail_id'] = ''
        out.setdefault(item['policy_code'], {'policy': [], 'detail': []})
        out[item['policy_code']][item['scope_kind']].append(item)
    return out


def _relation_one(policy_row, household):
    """单条 scope 行 vs 家庭 canonical，返回 (relation, applicable, reason)。"""
    policy = {'level': policy_row['level'], 'province': policy_row['province'],
              'city': policy_row['city'], 'county': policy_row['county'],
              'national': policy_row['level'] == admin_division.NATION}
    result = admin_division.relation(policy, household)
    relation = result['relation']
    if relation == 'disjoint' and policy_row.get('zone') and household.get('city') \
            and household.get('city') == policy_row.get('city'):
        # 功能区通常由 host 县区部分区域组成：同市其它县区不判死，保留候选。
        return 'unknown', None, 'zone_host_overlap_possible'
    applicable = result['applicable']
    if relation == 'disjoint':
        applicable = False  # admin_division 只把 ancestor/same 记 True；本层把明确不适用收敛成 False
    return relation, applicable, result['reason']


def _combine(rows, household):
    """并集语义：任一 applicable → True；全部明确 disjoint → False；否则 None。"""
    if not rows:
        return None, 'empty', None
    reasons, saw_disjoint = [], True
    for row in rows:
        relation, applicable, reason = _relation_one(row, household)
        if applicable is True:
            return True, relation, row
        if applicable is not False:
            saw_disjoint = False
        reasons.append(reason)
    if saw_disjoint:
        return False, 'disjoint', rows[0]
    return None, 'unknown', rows[0]


def match(scopes, household):
    """政策级+明细级复合判定。

    返回 {'applicable': True/False/None, 'relation': str, 'reason': str, 'scope': row}
    - detail 优先：明细适用且政策级不冲突 → True；明细不适用且政策级不适用 → False；
    - 没有明细行时只看政策级；
    - 政策级空 + 明细空 → None（交给缺省/追问）。
    """
    if isinstance(household, str):
        household = admin_division.canonical(household)
    policy_rows = scopes.get('policy') or []
    detail_rows = scopes.get('detail') or []
    if not policy_rows and not detail_rows:
        return {'applicable': None, 'relation': 'unknown', 'reason': 'no_scope', 'scope': None}
    policy_result = _combine(policy_rows, household)
    if not detail_rows:
        applicable, relation, row = policy_result
        reason = 'policy_only'
        if applicable is None and row is not None and row.get('zone'):
            reason = 'zone_host_overlap_possible'
        return {'applicable': applicable, 'relation': relation, 'reason': reason,
                'scope': row}
    detail_result = _combine(detail_rows, household)
    if detail_result[0] is True and policy_result[0] is not False:
        return {'applicable': True, 'relation': detail_result[1], 'reason': 'detail_match',
                'scope': detail_result[2]}
    if detail_result[0] is False and policy_result[0] is not True:
        return {'applicable': False, 'relation': detail_result[1], 'reason': 'detail_disjoint',
                'scope': detail_result[2]}
    return {'applicable': None, 'relation': 'unknown', 'reason': 'composite_unknown',
            'scope': detail_result[2] or policy_result[2]}


def _rows_of(conn, sql, params=None):
    return conn.execute(text(sql), params or {}).mappings().all()


def backfill(conn=None, dry_run=True, limit=None, url=None, default_province=None):
    """全库回填 policy_scope，返回报告（dry_run 只统计不写入）。"""
    own = conn is None
    if own:
        conn = engine(url).connect()
    try:
        if not dry_run:
            ensure_schema(conn if not own else conn, url=url)
        sql = 'SELECT policy_id, policy_code, policy_name, regions FROM policy ORDER BY policy_id'
        if limit:
            sql += ' LIMIT %d' % int(limit)
        policies = _rows_of(conn, sql)
        details = _rows_of(conn, '''
            SELECT policy_id, detail_id, region FROM policy_detail
             WHERE region IS NOT NULL AND length(region) > 0
        ''')
        by_policy = {}
        for row in details:
            by_policy.setdefault(row['policy_id'], []).append((row['detail_id'], row['region']))
        report = {'version': VERSION, 'dry_run': bool(dry_run), 'policies': len(policies),
                  'details': len(details), 'scope_rows': 0, 'unresolved': [], 'ambiguous': [],
                  'zones': 0, 'national': 0, 'province_only': 0, 'by_level': {},
                  'default_province': default_province}
        all_rows = []
        for policy in policies:
            rows = rows_for(policy['policy_code'], policy['regions'] or '',
                            by_policy.get(policy['policy_id'], ()),
                            default_province=default_province)
            all_rows.extend(rows)
            for row in rows:
                report['by_level'][row['level']] = report['by_level'].get(row['level'], 0) + 1
                if row['level'] == admin_division.UNKNOWN:
                    report['unresolved'].append({'policy_code': row['policy_code'],
                                                 'detail_id': row['detail_id'],
                                                 'raw_text': row['raw_text']})
                if row['zone']:
                    report['zones'] += 1
                if row['level'] == admin_division.NATION:
                    report['national'] += 1
                if row['level'] == admin_division.PROVINCE:
                    report['province_only'] += 1
            if not dry_run:
                replace_policy(conn, policy['policy_code'], rows)
        report['scope_rows'] = len(all_rows)
        report['policies_without_scope'] = sum(
            1 for policy in policies
            if not any(row['policy_code'] == policy['policy_code'] for row in all_rows))
        report['distinct_regions'] = len({row['region_key'] for row in all_rows})
        if not dry_run and own:
            conn.commit()
        return report
    finally:
        if own:
            conn.close()


def stats(url=None):
    conn = engine(url).connect()
    try:
        ensure_schema(conn)
        total = conn.execute(text('SELECT count(*) FROM policy_scope')).scalar()
        by_level = conn.execute(text(
            'SELECT scope_level, count(*) AS c FROM policy_scope GROUP BY scope_level ORDER BY c DESC'
        )).mappings().all()
        return {'total': total, 'by_level': {row['scope_level']: row['c'] for row in by_level}}
    finally:
        conn.close()
