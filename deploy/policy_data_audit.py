#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""政策数据治理 dry-run 只读报告（T27）。

七份报告（全部只读，不建表、不写库、不改 status）：
  a 覆盖画像     按省 / 设区市 / 县区 / 功能区统计明细行与唯一政策名，标出 0 明细县区；
  b 空壳清单     无 policy_detail 的政策（code/name/level/status/versions/scope 行）；
  c 重复组       同 (region, conditions, target) 完全同文的明细组及保留/归档建议；
  d 跨级同名     同名同时存在于 X 与 S 两级的 code 对；
  e 时效缺口     effect_date / expire_date 为空的在用政策（按 level）及版本分布；
  f 建议动作     每条带 sheet/行定位 + action(archive/merge/backfill/manual)，不执行写操作；
  g scope 一致性 明细缺 scope 行 / 孤儿 scope 行 / 政策级 scope 与 regions 文本差异 / 归一失败残留。

只读保证：
  - 只执行 SELECT；PG 连接先 SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY，
    SQLite 先 PRAGMA query_only = ON（设置失败也不影响，因为代码里没有任何写语句）；
  - 不 ensure_schema、不建表、不 UPDATE/DELETE/INSERT、不触发向量重建。

用法：
  python deploy/policy_data_audit.py --report                    # 人类可读报告（默认行为）
  python deploy/policy_data_audit.py --json /tmp/audit.json      # JSON 落文件
  python deploy/policy_data_audit.py --json - --limit 30         # JSON 到标准输出
  python deploy/policy_data_audit.py --url postgresql+psycopg2://... --json out.json
说明：默认读取 deploy/web.env（KEY=VALUE，已存在的环境变量优先）；--url 覆盖连接串。
"""
import argparse
import datetime
import json
import os
import sys
from collections import Counter, OrderedDict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

VERSION = 'policy-data-audit-2'
ACTIONS = ('archive', 'merge', 'backfill', 'manual')
ACTION_RISK = {'archive': 'low', 'merge': 'medium', 'backfill': 'medium', 'manual': 'high'}
JX_PROVINCE = '江西省'
PROVINCE_SCOPES = ('江西省全省', '全省')
EXCERPT_LIMIT = 120


def _excerpt(value, limit=EXCERPT_LIMIT):
    if value is None:
        return None
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit] + '…'


def load_env_file(path=None):
    """加载 KEY=VALUE 环境文件；已存在的环境变量优先（与 deploy/migrate_policy_scope.py 一致）。"""
    if not path or not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())


def database_url(explicit=None):
    if explicit:
        return explicit
    import config  # 延迟导入：只有需要读环境时才触发 config 的 DB URL 解析
    return config.DATABASE_URL


def connect(url=None):
    """建立只读连接：连接级只读事务特性 + 本模块无任何写语句。"""
    from sqlalchemy import create_engine, text
    engine = create_engine(url, pool_pre_ping=True)
    conn = engine.connect()
    try:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY'))
        elif conn.dialect.name == 'sqlite':
            conn.execute(text('PRAGMA query_only = ON'))
    except Exception:
        # 只读设置失败不回退成写模式；本模块只发 SELECT，失败仅意味着数据库侧兜底缺失。
        pass
    return conn


def close(conn):
    try:
        engine = conn.engine
        conn.close()
        engine.dispose()
    except Exception:
        pass


def _fetch(conn, sql):
    from sqlalchemy import text
    return conn.execute(text(sql)).mappings().all()


def _blank_sql(column, conn):
    """判定字段为空（NULL / 空串 / 纯空白）的 SQL 表达式。

    KingbaseES（Oracle 兼容模式）把 '' 当 NULL，所以 `TRIM(COALESCE(col, '')) = ''`
    恒为 NULL（既不为真也不为假）；必须用 BTRIM(col) IS NULL——它同时覆盖 NULL、
    空串与纯空白。SQLite/MySQL 仍用标准 TRIM/COALESCE。
    """
    if conn.dialect.name == 'postgresql':
        return 'BTRIM(%s) IS NULL' % column
    return "TRIM(COALESCE(%s, '')) = ''" % column


def _present_sql(column, conn):
    if conn.dialect.name == 'postgresql':
        return 'BTRIM(%s) IS NOT NULL' % column
    return "TRIM(COALESCE(%s, '')) <> ''" % column


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _division():
    from policy_core import admin_division
    return admin_division


def _empty_bucket():
    return {'details': 0, 'names': set(), 'policies': set()}


def _merge_bucket(target, source):
    target['details'] += source['details']
    target['names'].update(source['names'])
    target['policies'].update(source['policies'])


def _bucket_view(bucket, **extra):
    row = {'details': bucket['details'], 'names': len(bucket['names']),
           'policies': len(bucket['policies'])}
    row.update(extra)
    return row


def coverage_profile(conn, limit=50, division=None):
    """(a) 覆盖画像：明细按标准行政区聚合，标出 0 明细县区与无法归一的地区文本。"""
    ad = division or _division()
    rows = _fetch(conn, '''
        SELECT d.region AS region, p.policy_code AS policy_code, p.policy_name AS policy_name
        FROM policy_detail d
        LEFT JOIN policy p ON p.policy_id = d.policy_id''')
    by_region = OrderedDict()
    for row in rows:
        region = row['region'] if row['region'] is not None else ''
        bucket = by_region.setdefault(region, _empty_bucket())
        bucket['details'] += 1
        if row['policy_name']:
            bucket['names'].add(row['policy_name'])
        if row['policy_code']:
            bucket['policies'].add(row['policy_code'])

    county_stats, zone_stats, province_stats = {}, {}, _empty_bucket()
    unresolved, seen_unresolved = [], set()
    unresolved_names, unresolved_policies = set(), set()
    for region, bucket in by_region.items():
        if region in PROVINCE_SCOPES:
            _merge_bucket(province_stats, bucket)
            continue
        parsed = ad.canonical(region) if region else {
            'level': 'unknown', 'province': None, 'city': None, 'county': None, 'zone': False}
        if parsed.get('zone'):
            zone_stats[region] = (bucket, parsed.get('city'))
        elif parsed.get('province') == JX_PROVINCE and parsed.get('level') == ad.COUNTY:
            county = parsed.get('county') or region
            if county in county_stats:
                _merge_bucket(county_stats[county][0], bucket)
            else:
                county_stats[county] = (bucket, parsed.get('city'))
        elif parsed.get('province') == JX_PROVINCE and parsed.get('level') == ad.PROVINCE:
            _merge_bucket(province_stats, bucket)
        else:
            reason = 'outside_jiangxi' if parsed.get('province') else 'unresolved_region'
            unresolved.append(_bucket_view(bucket, region=region, level=parsed.get('level'),
                                           province=parsed.get('province'), reason=reason))
            seen_unresolved.add(region)
            unresolved_names.update(bucket['names'])
            unresolved_policies.update(bucket['policies'])

    known_zones = ad.known_functional_zones()  # 名称 -> 所属设区市
    counties, missing_counties = [], []
    all_names, all_policies = set(), set()
    county_details_total = 0
    for city in ad.cities_of(JX_PROVINCE):
        for county in ad.counties_of(city, JX_PROVINCE):
            bucket, _ = county_stats.get(county, (_empty_bucket(), city))
            has_data = bucket['details'] > 0
            counties.append(_bucket_view(bucket, county=county, city=city, has_data=has_data))
            if not has_data:
                missing_counties.append(county)
            county_details_total += bucket['details']
            all_names.update(bucket['names'])
            all_policies.update(bucket['policies'])
    standard = {item['county'] for item in counties}
    for county, (bucket, city) in county_stats.items():
        if county in standard:
            continue
        _merge = _bucket_view(bucket, region=county, level='county',
                              province=JX_PROVINCE, reason='nonstandard_county')
        if county not in seen_unresolved:
            unresolved.append(_merge)
            seen_unresolved.add(county)
            unresolved_names.update(bucket['names'])
            unresolved_policies.update(bucket['policies'])

    zones, missing_zones = [], []
    zone_detail_total, zone_names, zone_policies = 0, set(), set()
    zone_name_sets = {}
    for zone, city in known_zones.items():
        bucket, parsed_city = zone_stats.pop(zone, (_empty_bucket(), None))
        zones.append(_bucket_view(bucket, zone=zone, city=city or parsed_city,
                                  has_data=bucket['details'] > 0))
        if bucket['details'] == 0:
            missing_zones.append(zone)
        zone_name_sets[zone] = bucket['names']
        zone_detail_total += bucket['details']
        zone_names.update(bucket['names'])
        zone_policies.update(bucket['policies'])
    for zone, (bucket, parsed_city) in zone_stats.items():
        zones.append(_bucket_view(bucket, zone=zone, city=parsed_city,
                                  has_data=bucket['details'] > 0))
        zone_name_sets[zone] = bucket['names']
        zone_detail_total += bucket['details']
        zone_names.update(bucket['names'])
        zone_policies.update(bucket['policies'])

    cities = []
    for item in ad.cities_of(JX_PROVINCE):
        city_counties = [c for c in counties if c['city'] == item]
        city_zones = [z for z in zones if z['city'] == item]
        names, policies = set(), set()
        for c in city_counties:
            bucket, _ = county_stats.get(c['county'], (_empty_bucket(), c['city']))
            names.update(bucket['names'])
            policies.update(bucket['policies'])
        for z in city_zones:
            names.update(zone_name_sets.get(z['zone'], set()))
        cities.append({
            'city': item,
            'counties_total': len(city_counties),
            'counties_with_data': len([c for c in city_counties if c['has_data']]),
            'county_details': sum(c['details'] for c in city_counties),
            'county_names': len(names),
            'zone_count': len(city_zones),
            'zone_details': sum(z['details'] for z in city_zones),
        })

    # 未归一地区的政策同样是省域政策，不能因地区文本脏就漏出画像之外。
    province_names = (set(all_names) | zone_names | province_stats['names']
                      | unresolved_names)
    province_policies = (set(all_policies) | zone_policies | province_stats['policies']
                         | unresolved_policies)
    # 省域明细合计包含无法归一的地区文本（否则明细数会被静默吞掉，掩盖清洗缺口）。
    unresolved_details = sum(item['details'] for item in unresolved)
    province = {
        'province': JX_PROVINCE,
        'details': (county_details_total + zone_detail_total + province_stats['details']
                    + unresolved_details),
        'unresolved_details': unresolved_details,
        'names': len(province_names),
        'policies': len(province_policies),
        'county_details': county_details_total,
        'zone_details': zone_detail_total,
        'province_level_details': province_stats['details'],
    }
    unresolved.sort(key=lambda item: -item['details'])
    return {
        'summary': {
            'counties_total': len(counties),
            'counties_with_data': len(counties) - len(missing_counties),
            'missing_counties': missing_counties,
            'zones_total': len(zones),
            'zones_with_data': len(zones) - len(missing_zones),
            'missing_zones': missing_zones,
            'province_details': province['details'],
            'province_level_details': province['province_level_details'],
            'province_names': province['names'],
            'unresolved_regions': len(unresolved),
        },
        'province': province,
        'cities': cities,
        'counties': counties,
        'zones': zones,
        'unresolved': unresolved[:limit],
        'unresolved_truncated': len(unresolved) > limit,
    }


def empty_shells(conn, limit=50):
    """(b) 空壳清单：无明细政策；同时检查版本快照与 policy_scope 行。"""
    rows = _fetch(conn, '''
        SELECT p.policy_id AS policy_id, p.policy_code AS policy_code,
               p.policy_name AS policy_name, p."level" AS level,
               p.status AS status, p.regions AS regions,
               (SELECT COUNT(*) FROM policy_version v WHERE v.policy_id = p.policy_id) AS versions,
               (SELECT COUNT(*) FROM policy_scope s WHERE s.policy_code = p.policy_code) AS scope_rows
        FROM policy p
        WHERE NOT EXISTS (SELECT 1 FROM policy_detail d WHERE d.policy_id = p.policy_id)
        ORDER BY p."level", p.policy_code''')
    items = []
    for row in rows:
        items.append({
            'policy_id': row['policy_id'],
            'policy_code': row['policy_code'],
            'policy_name': row['policy_name'],
            'level': row['level'],
            'status': row['status'],
            'regions': row['regions'],
            'versions': int(row['versions'] or 0),
            'scope_rows': int(row['scope_rows'] or 0),
        })
    by_level = Counter(item['level'] or '' for item in items)
    return {
        'total': len(items),
        'in_use': len([i for i in items if i['status'] == '在用']),
        'by_level': dict(sorted(by_level.items())),
        'null_regions': len([i for i in items if not (i['regions'] or '').strip()]),
        'zero_versions': len([i for i in items if i['versions'] == 0]),
        'zero_scope': len([i for i in items if i['scope_rows'] == 0]),
        'samples': items[:limit],
        'truncated': len(items) > limit,
    }


def duplicate_groups(conn, limit=50):
    """(c) 重复组：同 (region, conditions, target) 完全同文的明细组；保留最早 detail_id。"""
    rows = _fetch(conn, '''
        SELECT d.detail_id AS detail_id, d.region AS region, d.conditions AS conditions,
               d.target AS target, p.policy_code AS policy_code,
               p.policy_name AS policy_name, p."level" AS level
        FROM policy_detail d
        LEFT JOIN policy p ON p.policy_id = d.policy_id
        ORDER BY d.detail_id''')
    groups = OrderedDict()
    for row in rows:
        key = (row['region'], row['conditions'], row['target'])
        groups.setdefault(key, []).append(row)
    dups = [(key, members) for key, members in groups.items() if len(members) > 1]
    covered = sum(len(members) for _, members in dups)
    dups.sort(key=lambda pair: (-len(pair[1]), str(pair[0][0]), str(pair[0][1])))

    samples = []
    for key, members in dups[:limit]:
        codes = list(OrderedDict((m['policy_code'], None) for m in members))
        samples.append({
            'region': key[0],
            'conditions': _excerpt(key[1]),
            'target': _excerpt(key[2]),
            'rows': len(members),
            'policies': len(codes),
            'keep_detail_id': members[0]['detail_id'],
            'detail_ids': [m['detail_id'] for m in members],
            'codes': codes,
            'levels': sorted({m['level'] for m in members if m['level']}),
        })
    return {
        'groups': len(dups),
        'covered_rows': covered,
        'redundant_rows': covered - len(dups),
        'total_rows': len(rows),
        'samples': samples,
        'truncated': len(dups) > limit,
    }


def cross_level_names(conn, limit=50):
    """(d) 跨级同名：同一 policy_name 同时出现在 X 与 S 级的 code 对。"""
    rows = _fetch(conn, '''
        SELECT p.policy_code AS policy_code, p.policy_name AS policy_name,
               p."level" AS level
        FROM policy p
        ORDER BY p.policy_name, p."level", p.policy_code''')
    by_name = OrderedDict()
    for row in rows:
        entry = by_name.setdefault(row['policy_name'], {'levels': {}, 'rows': 0})
        entry['levels'].setdefault(row['level'], []).append(row['policy_code'])
        entry['rows'] += 1
    items = []
    for name, entry in by_name.items():
        if 'X' in entry['levels'] and 'S' in entry['levels']:
            pairs = []
            for x_code in entry['levels']['X'][:5]:
                for s_code in entry['levels']['S'][:5]:
                    pairs.append({'x': x_code, 's': s_code})
            items.append({
                'policy_name': name,
                'levels': sorted(entry['levels']),
                'codes': {level: entry['levels'][level] for level in sorted(entry['levels'])},
                'rows': entry['rows'],
                'pairs_sample': pairs[:10],
            })
    items.sort(key=lambda item: (-item['rows'], item['policy_name']))
    return {
        'names': len(items),
        'covered_rows': sum(item['rows'] for item in items),
        'samples': items[:limit],
        'truncated': len(items) > limit,
    }


def _dates_block(conn, table):
    row = _fetch(conn, '''
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS effect_missing,
               SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS expire_missing
        FROM %s''' % (_blank_sql('effect_date', conn), _blank_sql('expire_date', conn), table))[0]
    total = int(row['total'] or 0)
    effect_missing = int(row['effect_missing'] or 0)
    expire_missing = int(row['expire_missing'] or 0)
    return {
        'total': total,
        'effect_missing': effect_missing,
        'effect_present': total - effect_missing,
        'expire_missing': expire_missing,
        'expire_present': total - expire_missing,
        'missing_rate': round(effect_missing / total, 4) if total else 0,
    }


def timeliness_gaps(conn):
    """(e) 时效缺口：effect/expire 为空的在用政策（按 level）+ 版本分布。"""
    in_use = []
    for row in _fetch(conn, '''
            SELECT p."level" AS level, COUNT(*) AS total,
                   SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS effect_missing,
                   SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS expire_missing
            FROM policy p WHERE p.status = '在用'
            GROUP BY p."level" ORDER BY p."level"''' % (
            _blank_sql('effect_date', conn), _blank_sql('expire_date', conn))):
        total = int(row['total'] or 0)
        in_use.append({
            'level': row['level'],
            'total': total,
            'effect_missing': int(row['effect_missing'] or 0),
            'expire_missing': int(row['expire_missing'] or 0),
        })
    updated = _fetch(conn, '''
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS present
        FROM policy''' % _present_sql('updated_at', conn))[0]
    version_no = [{'version_no': row['version_no'], 'rows': int(row['rows'] or 0)}
                  for row in _fetch(conn, '''
        SELECT version_no AS version_no, COUNT(*) AS rows FROM policy_version
        GROUP BY version_no ORDER BY rows DESC''')]
    cur_version = [{'cur_version': row['cur_version'], 'rows': int(row['rows'] or 0)}
                   for row in _fetch(conn, '''
        SELECT cur_version AS cur_version, COUNT(*) AS rows FROM policy
        GROUP BY cur_version ORDER BY rows DESC''')]
    return {
        'policy': _dates_block(conn, 'policy'),
        'policy_detail': _dates_block(conn, 'policy_detail'),
        'policy_version': _dates_block(conn, 'policy_version'),
        'in_use_by_level': in_use,
        'policy_updated_at': {'total': int(updated['total'] or 0),
                              'present': int(updated['present'] or 0)},
        'version_no': version_no,
        'cur_version': cur_version,
    }


def overview(conn):
    """四张主表行数概览（只读 count）。"""
    counts = OrderedDict()
    for table in ('policy', 'policy_detail', 'policy_version', 'policy_scope'):
        counts[table] = int(_fetch(conn, 'SELECT COUNT(*) AS rows FROM %s' % table)[0]['rows'] or 0)
    return dict(counts)


SCOPE_SAMPLE_CAP = 20

# 只取四列，兼容尚未迁移 scope_level 的历史库/测试库；生产 policy_scope 这些列均存在。
_SCOPE_COLUMNS = 'policy_code, detail_id, region_key, scope_level'


def _scope_detail_key(detail_id):
    """scope 表的 detail_id 规范：政策级行是 '' / '-' / NULL，其余是明细 id。"""
    if detail_id is None:
        return ''
    text = str(detail_id).strip()
    return '' if text in ('', '-') else text


def _split_regions():
    """复用 policy_scope 的多值拆分口径；导入失败时回落等价实现（审计工具可独立运行）。"""
    try:
        from policy_core.policy_scope import split_regions
        return split_regions
    except Exception:
        pass
    import re
    pattern = re.compile(r'[、,，;；/|\n\r]+')

    def fallback(value):
        out, seen = [], set()
        for token in pattern.split(value or ''):
            token = token.strip().strip('；;，,、')
            if token in ('', '-') or token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out
    return fallback


def _scope_key(text):
    """与 policy_scope._row 一致的归一键；导入失败时按 admin_division 结果本地等价实现。"""
    try:
        from policy_core.policy_scope import _row as scope_row
        return scope_row('', '', 'policy', 'policy.regions', text)['region_key']
    except Exception:
        pass
    ad = _division()
    parsed = ad.canonical(text)
    if parsed.get('national'):
        return 'CN'
    level = parsed.get('level')
    if level == ad.UNKNOWN:
        return 'U:' + str(text)
    if level == ad.PROVINCE:
        return 'P:' + (parsed.get('province') or str(text))
    if level == ad.CITY:
        return 'C:%s/%s' % (parsed.get('province') or '', parsed.get('city') or str(text))
    key = 'X:%s/%s/%s' % (parsed.get('province') or '', parsed.get('city') or '',
                          parsed.get('county') or str(text))
    if parsed.get('zone'):
        key = 'Z:' + key[2:]
    return key


def _sample_block(samples, total, sample_limit, **extra):
    block = dict(extra)
    block['samples'] = samples[:sample_limit]
    block['truncated'] = total > len(block['samples'])
    return block


def scope_consistency(conn, limit=50):
    """(g) policy_scope 一致性（只读）。

    四项：
      1 明细缺 scope 行：policy_detail 有地区文本且有政策，但 policy_scope 无 (政策, detail_id) 行；
      2 孤儿 scope 行：scope.detail_id 指向不存在的明细；detail_id 不唯一时按 (政策, detail_id) 核对；
      3 政策级 scope 与 regions 文本不一致：两侧都归一成 region_key 后比较集合差异；
      4 归一失败残留：scope_level 空（NULL/空串，按方言判空）或 region_key 为 U: 前缀。
    只执行 SELECT；样例最多 SCOPE_SAMPLE_CAP 条，计数不受截断影响。
    """
    sample_limit = max(0, min(int(limit if limit is not None else 50), SCOPE_SAMPLE_CAP))
    details = _fetch(conn, """
        SELECT d.detail_id AS detail_id, d.region AS region,
               p.policy_code AS policy_code
        FROM policy_detail d
        LEFT JOIN policy p ON p.policy_id = d.policy_id
        ORDER BY d.detail_id""")
    scope_rows = _fetch(conn, 'SELECT ' + _SCOPE_COLUMNS + ' FROM policy_scope')

    detail_pairs, detail_ids = set(), []
    for row in details:
        detail_id = str(row['detail_id'])
        detail_ids.append(detail_id)
        if row['policy_code'] is not None:
            detail_pairs.add((row['policy_code'], detail_id))
    detail_id_set = set(detail_ids)
    duplicate_detail_ids = len(detail_ids) - len(detail_id_set)
    unique_detail_ids = duplicate_detail_ids == 0
    detail_owner = {}
    for row in details:
        detail_owner.setdefault(str(row['detail_id']), row['policy_code'])

    scope_detail, scope_policy_keys = [], {}
    for row in scope_rows:
        code = row['policy_code'] or ''
        detail_id = _scope_detail_key(row['detail_id'])
        if detail_id:
            scope_detail.append({'policy_code': code, 'detail_id': detail_id,
                                 'region_key': row['region_key'] or '',
                                 'scope_level': row['scope_level']})
        else:
            scope_policy_keys.setdefault(code, set()).add(row['region_key'] or '')
    scope_detail_pairs = {(row['policy_code'], row['detail_id']) for row in scope_detail}

    # 1) 明细缺 scope 行（只算有地区文本且有政策的明细；空地区由空壳/覆盖报告负责）。
    missing_scope = []
    for row in details:
        region = row['region'] if row['region'] is not None else ''
        if row['policy_code'] is None or not str(region).strip():
            continue
        detail_id = str(row['detail_id'])
        if (row['policy_code'], detail_id) in scope_detail_pairs:
            continue
        missing_scope.append({'policy_code': row['policy_code'],
                              'detail_id': row['detail_id'],
                              'region': region})
    detail_without_scope = _sample_block(
        missing_scope, len(missing_scope), sample_limit,
        details=len(missing_scope),
        policies=len({row['policy_code'] for row in missing_scope}))

    # 2) 孤儿 scope 行：先自查 detail_id 唯一性；唯一按 detail_id，否则按 (政策, detail_id)。
    orphans, mismatch = [], []
    for row in scope_detail:
        if unique_detail_ids:
            if row['detail_id'] not in detail_id_set:
                orphans.append(dict(row, reason='missing_detail'))
            elif detail_owner.get(row['detail_id']) != row['policy_code']:
                mismatch.append(dict(row, reason='policy_code_mismatch',
                                     actual_policy_code=detail_owner.get(row['detail_id'])))
        elif (row['policy_code'], row['detail_id']) not in detail_pairs:
            orphans.append(dict(row, reason='missing_detail'))
    orphan_rows = orphans + mismatch
    orphan_scope_rows = _sample_block(
        orphan_rows, len(orphan_rows), sample_limit,
        rows=len(orphans),
        detail_ids=len({row['detail_id'] for row in orphans}),
        policy_code_mismatch_rows=len(mismatch),
        detail_id_unique=unique_detail_ids,
        duplicate_detail_ids=duplicate_detail_ids,
        match_basis='detail_id' if unique_detail_ids else '(policy_code, detail_id)')

    # 3) 政策级 scope 与 regions 文本不一致：两侧都归一成 region_key 后比较集合。
    split = _split_regions()
    key_cache, policies = {}, []
    for row in _fetch(conn, 'SELECT policy_code, regions FROM policy ORDER BY policy_code'):
        policies.append((row['policy_code'], row['regions'] or ''))
    mismatch_policies, missing_policies, extra_policies = [], set(), set()
    unresolved_region_tokens = unresolved_scope_keys = 0
    for code, regions in policies:
        expected = set()
        unresolved_tokens = []
        for token in split(regions):
            if token not in key_cache:
                key_cache[token] = _scope_key(token)
            key = key_cache[token]
            expected.add(key)
            if key.startswith('U:') and len(key) > 2:
                unresolved_tokens.append(token)
        unresolved_region_tokens += len(unresolved_tokens)
        actual = scope_policy_keys.get(code, set())
        unresolved_scope_keys += len([key for key in actual if key.startswith('U:')])
        missing = expected - actual
        extra = actual - expected
        if not missing and not extra:
            continue
        if missing:
            missing_policies.add(code)
        if extra:
            extra_policies.add(code)
        mismatch_policies.append({
            'policy_code': code,
            'regions': _excerpt(regions),
            'missing_scope': sorted(missing)[:10],
            'extra_scope': sorted(extra)[:10],
            'unresolved_tokens': unresolved_tokens[:10],
        })
    policy_region_mismatch = _sample_block(
        mismatch_policies, len(mismatch_policies), sample_limit,
        policies=len(mismatch_policies),
        missing_scope_policies=len(missing_policies),
        extra_scope_policies=len(extra_policies),
        unresolved_region_tokens=unresolved_region_tokens,
        unresolved_scope_keys=unresolved_scope_keys)

    # 4) 归一失败残留：scope_level 空（按方言判空）或 region_key 以 U: 开头。
    # 匹配行数通常很少（生产 scope_level NOT NULL、U: 键是词表外文本），全取后在内存里截断。
    blank = _blank_sql('scope_level', conn)
    blank_level = int(_fetch(conn, 'SELECT COUNT(*) AS rows FROM policy_scope WHERE %s'
                             % blank)[0]['rows'] or 0)
    u_key = int(_fetch(conn, "SELECT COUNT(*) AS rows FROM policy_scope WHERE region_key LIKE 'U:%'"
                       )[0]['rows'] or 0)
    residue_rows = _fetch(conn, """
        SELECT policy_code, detail_id, region_key, scope_level
        FROM policy_scope
        WHERE (%s) OR (region_key LIKE 'U:%%')
        ORDER BY policy_code, detail_id""" % blank)
    residue_samples = []
    for row in residue_rows:
        level = row['scope_level']
        is_blank = level is None or not str(level).strip()
        is_u = str(row['region_key'] or '').startswith('U:')
        residue_samples.append({
            'policy_code': row['policy_code'],
            'detail_id': row['detail_id'],
            'region_key': row['region_key'],
            'scope_level': level,
            'reason': 'blank_level+u_key' if is_blank and is_u
                      else ('blank_level' if is_blank else 'u_key'),
        })
    unresolved_scope_rows = _sample_block(
        residue_samples, len(residue_samples), sample_limit,
        rows=len(residue_samples), blank_level=blank_level, u_key=u_key)

    return {
        'detail_without_scope': detail_without_scope,
        'orphan_scope_rows': orphan_scope_rows,
        'policy_region_mismatch': policy_region_mismatch,
        'unresolved_scope_rows': unresolved_scope_rows,
    }


def build_suggestions(sections, limit=50):
    """(f) 建议动作清单：只给建议，不执行任何写操作。"""
    edge = sections['coverage']['summary']
    timeliness = sections['timeliness']
    full_counts = {
        'archive': int(sections['empty_shell']['total']),
        'merge': int(sections['duplicates']['groups']),
        'manual': int(sections['cross_level']['names']) + int(edge['unresolved_regions']),
        'backfill': len([row for row in timeliness['in_use_by_level']
                         if row['effect_missing'] or row['expire_missing']])
                    + len(edge['missing_counties'])
                    + (1 if timeliness['policy_detail']['effect_missing'] or timeliness['policy_detail']['expire_missing'] else 0)
                    + (1 if timeliness['policy_version']['effect_missing'] or timeliness['policy_version']['expire_missing'] else 0),
    }
    # scope 一致性发现同样计入建议动作：缺行按 backfill，孤儿/文本不一致/归一残留按 manual。
    scope = sections.get('scope_consistency') or {}
    scope_detail = scope.get('detail_without_scope') or {}
    scope_orphan = scope.get('orphan_scope_rows') or {}
    scope_mismatch = scope.get('policy_region_mismatch') or {}
    scope_residue = scope.get('unresolved_scope_rows') or {}
    full_counts['backfill'] += int(scope_detail.get('details') or 0)
    full_counts['manual'] += (int(scope_orphan.get('rows') or 0)
                              + int(scope_orphan.get('policy_code_mismatch_rows') or 0)
                              + int(scope_mismatch.get('policies') or 0)
                              + int(scope_residue.get('rows') or 0))
    items = []
    for shell in sections['empty_shell']['samples']:
        items.append({
            'sheet': 'empty_shell',
            'locator': {'policy_code': shell['policy_code'], 'policy_id': shell['policy_id']},
            'action': 'archive',
            'risk': ACTION_RISK['archive'],
            'note': '无明细的空壳政策，建议归档/软删并隔离其策略级向量；不物理删除，保留可追溯。',
        })
    for group in sections['duplicates']['samples']:
        items.append({
            'sheet': 'policy_detail',
            'locator': {'detail_id': group['keep_detail_id'],
                        'duplicate_detail_ids': group['detail_ids'],
                        'policy_codes': group['codes']},
            'action': 'merge',
            'risk': ACTION_RISK['merge'],
            'note': '同 (region, conditions, target) 完全重复 %d 行；保留最早 detail_id，其余归档，合并前留版本快照。' % group['rows'],
        })
    for item in sections['cross_level']['samples']:
        items.append({
            'sheet': 'policy',
            'locator': {'policy_name': item['policy_name'], 'codes': item['codes']},
            'action': 'manual',
            'risk': ACTION_RISK['manual'],
            'note': '同名跨 X/S 级，先人工确认是否同一制度，再决定合并（policy_family）或保留各级实现。',
        })
    for level_row in sections['timeliness']['in_use_by_level']:
        if not (level_row['effect_missing'] or level_row['expire_missing']):
            continue
        items.append({
            'sheet': 'policy',
            'locator': {'level': level_row['level']},
            'action': 'backfill',
            'risk': ACTION_RISK['backfill'],
            'note': '在用政策 effect 空 %d 行 / expire 空 %d 行；回填日期或显式标注 unknown。' % (
                level_row['effect_missing'], level_row['expire_missing']),
        })
    for table in ('policy_detail', 'policy_version'):
        block = timeliness[table]
        if block['effect_missing'] or block['expire_missing']:
            items.append({
                'sheet': table,
                'locator': {'table': table},
                'action': 'backfill',
                'risk': ACTION_RISK['backfill'],
                'note': '%s effect 空 %d 行 / expire 空 %d 行；回填日期或显式标注 unknown。' % (
                    table, block['effect_missing'], block['expire_missing']),
            })
    for county in sections['coverage']['summary']['missing_counties']:
        items.append({
            'sheet': 'coverage',
            'locator': {'county': county},
            'action': 'backfill',
            'risk': ACTION_RISK['backfill'],
            'note': '该县区 0 条政策明细（上游未收录）；补录或显式标注暂不可用，避免默认回落。',
        })
    for region in sections['coverage']['unresolved'][:limit]:
        items.append({
            'sheet': 'coverage',
            'locator': {'region': region['region'], 'reason': region['reason']},
            'action': 'manual',
            'risk': ACTION_RISK['manual'],
            'note': '明细地区文本无法落入标准行政区；人工确认/清洗后再决定归属，勿静默丢弃。',
        })
    for row in scope_detail.get('samples') or []:
        items.append({
            'sheet': 'policy_scope',
            'locator': {'policy_code': row['policy_code'],
                        'detail_id': row['detail_id'], 'region': row['region']},
            'action': 'backfill',
            'risk': ACTION_RISK['backfill'],
            'note': '明细有地区文本但 policy_scope 无 detail 行；按政策重算 scope，不手写 region_key。',
        })
    for row in scope_orphan.get('samples') or []:
        items.append({
            'sheet': 'policy_scope',
            'locator': {'policy_code': row['policy_code'], 'detail_id': row['detail_id'],
                        'region_key': row['region_key'], 'reason': row['reason']},
            'action': 'manual',
            'risk': ACTION_RISK['manual'],
            'note': 'scope 行指向不存在的明细或与明细所属政策不一致；人工确认来源后重建/清除。',
        })
    for row in scope_mismatch.get('samples') or []:
        items.append({
            'sheet': 'policy',
            'locator': {'policy_code': row['policy_code'],
                        'missing_scope': row['missing_scope'],
                        'extra_scope': row['extra_scope'],
                        'unresolved_tokens': row['unresolved_tokens']},
            'action': 'manual',
            'risk': ACTION_RISK['manual'],
            'note': '政策级 scope 与 regions 文本归一结果不一致；以 regions 为业务事实重算 scope。',
        })
    for row in scope_residue.get('samples') or []:
        items.append({
            'sheet': 'policy_scope',
            'locator': {'policy_code': row['policy_code'], 'detail_id': row['detail_id'],
                        'region_key': row['region_key'], 'reason': row['reason']},
            'action': 'manual',
            'risk': ACTION_RISK['manual'],
            'note': 'scope 行归一失败（scope_level 空或 U: 键）；补行政区词表/清洗后重算，勿按未归一文本召回。',
        })
    total = sum(full_counts.values())
    by_action = dict(sorted((action, count) for action, count in full_counts.items() if count))
    by_risk = Counter()
    for action, count in full_counts.items():
        if count:
            by_risk[ACTION_RISK[action]] += count
    items = items[:limit]
    return {
        'total': total,
        'by_action': by_action,
        'by_risk': dict(sorted(by_risk.items())),
        'items': items,
        'truncated': total > len(items),
    }


def audit(conn, limit=50, division=None):
    """跑六份报告，返回可直接 JSON 化的 dict（不写任何东西）。"""
    coverage = coverage_profile(conn, limit=limit, division=division)
    empty = empty_shells(conn, limit=limit)
    duplicates = duplicate_groups(conn, limit=limit)
    cross_level = cross_level_names(conn, limit=limit)
    timeliness = timeliness_gaps(conn)
    sections = {'coverage': coverage, 'empty_shell': empty, 'duplicates': duplicates,
                'cross_level': cross_level, 'timeliness': timeliness}
    # 新增报告放在既有键之后，不改既有六份报告的结构与语义。
    sections['scope_consistency'] = scope_consistency(conn, limit=limit)
    sections['suggestions'] = build_suggestions(sections, limit=limit)
    overview_counts = overview(conn)
    try:
        database = conn.engine.url.render_as_string(hide_password=True)
    except Exception:
        database = None
    return {
        'version': VERSION,
        'generated_at': _now(),
        'read_only': True,
        'database': database,
        'limit': limit,
        'overview': overview_counts,
        'sections': sections,
    }


def _json_default(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    try:
        import decimal
        if isinstance(value, decimal.Decimal):
            return float(value)
    except Exception:
        pass
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def format_report(result):
    """人类可读报告：每节先给总量与结论，再给样本，便于直接贴巡检记录。"""
    sections = result['sections']
    coverage = sections['coverage']
    summary = coverage['summary']
    lines = ['== 政策数据治理只读审计 %s ==' % result['version'],
             '数据库: %s' % (result.get('database') or '?'),
             '生成时间: %s  只读: %s' % (result['generated_at'], result['read_only'])]
    overview = result.get('overview') or {}
    if overview:
        lines.append('表行数: ' + ' / '.join('%s %d' % (name, count)
                                            for name, count in overview.items()))
    lines.append('')
    lines.append('[a] 覆盖画像')
    lines.append('  县区: %d/%d 有明细；功能区: %d/%d；明细合计 %d 行（省级 %d）；无法归一地区: %d' % (
        summary['counties_with_data'], summary['counties_total'],
        summary['zones_with_data'], summary['zones_total'],
        summary['province_details'], summary['province_level_details'],
        summary['unresolved_regions']))
    if summary['missing_counties']:
        lines.append('  0 明细县区: %s' % '、'.join(summary['missing_counties']))
    for city in coverage['cities']:
        lines.append('  %-4s 县区 %2d/%2d  明细 %5d  政策名 %4d  功能区 %2d/%5d' % (
            city['city'], city['counties_with_data'], city['counties_total'],
            city['county_details'], city['county_names'],
            city['zone_count'], city['zone_details']))
    for zone in coverage['zones'][:5]:
        lines.append('  功能区 %s(%s) 明细 %d 名 %d' % (
            zone['zone'], zone['city'] or '?', zone['details'], zone['names']))
    if len(coverage['zones']) > 5:
        lines.append('  … 其余功能区见 JSON')
    for region in coverage['unresolved'][:5]:
        lines.append('  未归一地区 %r (%s) 明细 %d' % (
            region['region'], region['reason'], region['details']))

    empty = sections['empty_shell']
    lines.append('')
    lines.append('[b] 空壳清单: %d 条（在用 %d；regions 空 %d；无版本 %d；无 scope %d）' % (
        empty['total'], empty['in_use'], empty['null_regions'],
        empty['zero_versions'], empty['zero_scope']))
    for item in empty['samples'][:5]:
        lines.append('  %s %s level=%s status=%s versions=%d scope=%d regions=%r' % (
            item['policy_code'], item['policy_name'], item['level'], item['status'],
            item['versions'], item['scope_rows'], item['regions']))
    if empty['truncated']:
        lines.append('  … 另有 %d 条见 JSON' % (empty['total'] - len(empty['samples'])))

    duplicates = sections['duplicates']
    lines.append('')
    lines.append('[c] 重复组: %d 组 / 覆盖 %d 行 / 冗余 %d 行（明细共 %d 行）' % (
        duplicates['groups'], duplicates['covered_rows'],
        duplicates['redundant_rows'], duplicates['total_rows']))
    for group in duplicates['samples'][:5]:
        lines.append('  %s | %s 组 %d 行，保留 detail_id=%s，codes=%s' % (
            group['region'], (group['conditions'] or '')[:30], group['rows'],
            group['keep_detail_id'], group['codes'][:4]))
    if duplicates['truncated']:
        lines.append('  … 其余 %d 组见 JSON' % (duplicates['groups'] - len(duplicates['samples'])))

    cross = sections['cross_level']
    lines.append('')
    lines.append('[d] 跨级同名: %d 个名称 / 覆盖 %d 行' % (cross['names'], cross['covered_rows']))
    for item in cross['samples'][:5]:
        lines.append('  %s  X:%s  S:%s' % (
            item['policy_name'], item['codes'].get('X', [])[:3], item['codes'].get('S', [])[:3]))
    if cross['truncated']:
        lines.append('  … 其余 %d 个名称见 JSON' % (cross['names'] - len(cross['samples'])))

    timeliness = sections['timeliness']
    lines.append('')
    lines.append('[e] 时效缺口')
    for table in ('policy', 'policy_detail', 'policy_version'):
        block = timeliness[table]
        lines.append('  %-14s effect 非空 %d/%d  expire 非空 %d/%d' % (
            table, block['effect_present'], block['total'],
            block['expire_present'], block['total']))
    for row in timeliness['in_use_by_level']:
        lines.append('  在用 level=%s 共 %d 行：effect 空 %d，expire 空 %d' % (
            row['level'], row['total'], row['effect_missing'], row['expire_missing']))
    lines.append('  updated_at 非空 %d/%d；version_no 分布 %s；cur_version 分布 %s' % (
        timeliness['policy_updated_at']['present'], timeliness['policy_updated_at']['total'],
        {row['version_no']: row['rows'] for row in timeliness['version_no']},
        {row['cur_version']: row['rows'] for row in timeliness['cur_version']}))

    suggestions = sections['suggestions']
    lines.append('')
    lines.append('[f] 建议动作: %d 条（%s）' % (
        suggestions['total'], suggestions['by_action']))
    for item in suggestions['items'][:10]:
        lines.append('  [%s/%s] %s %s' % (
            item['action'], item['risk'], item['sheet'], item['locator']))
    if suggestions['truncated']:
        lines.append('  … 其余 %d 条见 JSON' % (suggestions['total'] - len(suggestions['items'])))

    scope = sections.get('scope_consistency') or {}
    gap = scope.get('detail_without_scope') or {}
    orphan = scope.get('orphan_scope_rows') or {}
    mismatch = scope.get('policy_region_mismatch') or {}
    residue = scope.get('unresolved_scope_rows') or {}
    lines.append('')
    lines.append('[g] scope 一致性: 明细缺 scope %d 行（%d 个政策）；孤儿 scope 行 %d（detail_id 唯一=%s，口径 %s）；'
                 '政策级 scope 与 regions 不一致 %d 政策（缺 %d / 多 %d；未归一 token %d）；'
                 '归一失败残留 %d 行（level 空 %d / U: 键 %d）' % (
                     gap.get('details', 0), gap.get('policies', 0),
                     orphan.get('rows', 0), orphan.get('detail_id_unique'),
                     orphan.get('match_basis'), mismatch.get('policies', 0),
                     mismatch.get('missing_scope_policies', 0),
                     mismatch.get('extra_scope_policies', 0),
                     mismatch.get('unresolved_region_tokens', 0),
                     residue.get('rows', 0), residue.get('blank_level', 0),
                     residue.get('u_key', 0)))
    for row in (gap.get('samples') or [])[:3]:
        lines.append('  缺 scope %s detail=%s region=%r' % (
            row['policy_code'], row['detail_id'], row['region']))
    for row in (orphan.get('samples') or [])[:3]:
        lines.append('  孤儿/错配 scope %s detail=%s key=%r reason=%s' % (
            row['policy_code'], row['detail_id'], row['region_key'], row.get('reason')))
    for row in (mismatch.get('samples') or [])[:3]:
        lines.append('  scope 不一致 %s regions=%r 缺=%s 多=%s' % (
            row['policy_code'], row['regions'], row['missing_scope'], row['extra_scope']))
    for row in (residue.get('samples') or [])[:3]:
        lines.append('  归一失败 %s detail=%s key=%r reason=%s' % (
            row['policy_code'], row['detail_id'], row['region_key'], row.get('reason')))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='政策数据治理 dry-run 只读报告（不写库）')
    parser.add_argument('--report', action='store_true', help='打印人类可读报告（默认行为）')
    parser.add_argument('--json', metavar='PATH', default=None,
                        help="JSON 报告输出路径；'-' 表示标准输出")
    parser.add_argument('--limit', type=int, default=50, help='各清单样本条数上限（默认 50）')
    parser.add_argument('--env-file', default=None,
                        help='KEY=VALUE 环境文件；默认 deploy/web.env（不存在则忽略）')
    parser.add_argument('--url', default=None, help='数据库 URL（覆盖环境变量与 config.DATABASE_URL）')
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit 不能为负')

    env_file = args.env_file
    if env_file is None:
        default_env = os.path.join(BASE, 'deploy', 'web.env')
        env_file = default_env if os.path.exists(default_env) else None
    if env_file:
        load_env_file(env_file)

    try:
        conn = connect(database_url(args.url))
    except Exception as exc:  # 连接失败绝不重试写操作：只读审计直接失败退出
        sys.stderr.write('无法连接数据库（只读审计未执行）: %s\n' % exc)
        return 2
    try:
        result = audit(conn, limit=args.limit)
    finally:
        close(conn)

    if args.json == '-':
        print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
        if args.report:
            sys.stderr.write(format_report(result) + '\n')
        return 0
    if args.report or not args.json:
        print(format_report(result))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, default=_json_default)
            handle.write('\n')
        print('JSON 报告已写入: %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
