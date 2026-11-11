# -*- coding: utf-8 -*-
"""政策自动管理（policy autopilot）内核：六个只读体检/受控写任务。

定位（对应 docs/redesign/20-national-policy-lifecycle-plan.md L4）：
- 本模块只做「体检 + 可控写队列/状态」，默认 dry-run；apply 仅由 CLI 在
  POLICY_AUTOPILOT=1 时打开，且只写自己负责的列/队列表。
- 不直接同步向量/规则/KG（解耦原则）：写业务表后由各自 outbox/worker 消费；
  本模块不 import kb_rag / compile_rules，绝不触发 LLM 调用。
- 过期扫描只停用「已过期」政策（status='停用'），临期只报告；不动明细、不动向量，
  残留向量由 health_check 的 inactive_vector_residue 暴露，等待后续 outbox 级联。

所有任务函数签名统一为 task(conn, ...)，conn 为 SQLAlchemy Connection；
apply 类任务的提交由调用方负责（CLI 用 engine.begin()）。返回结构化 dict：
{task, mode, generated_at, ... , sample 列表一律截断 [:20]}。

开关：
- POLICY_AUTOPILOT=0（默认）→ deploy/policy_autopilot.py 拒绝 --apply；
- POLICY_AUTOPILOT_STALE_HOURS=72 → stale_draft 阈值；
- POLICY_AUTOPILOT_EXPIRING_DAYS=30 → expire_scan 临期窗口；
- POLICY_AUTOPILOT_WORKER=1 → app.py 起后台线程 'policy-autopilot'，每
  POLICY_AUTOPILOT_INTERVAL 秒（默认 3600）跑一轮 run_all；默认关闭，关闭时不建线程、
  不连库（现网零行为变化）。apply 仍由 POLICY_AUTOPILOT=1 单独控制。

模块入口（默认 dry-run 单轮，供 cron/临时排查）：
  python -m policy_core.autopilot --once --json
"""
import argparse
import datetime
import inspect as _pyinspect
import json
import logging
import os
import sys
import threading

from sqlalchemy import create_engine, inspect, text

MAX_SAMPLE = 20
DEFAULT_URL_KEY = 'ADMIN_DB_URL'

TABLES = {
    'policy': 'policy',
    'detail': 'policy_detail',
    'version': 'policy_version',
    'draft': 'policy_draft',
    'scope': 'policy_scope',
    'rule': 'policy_rule',
    'rule_job': 'policy_rule_job',
    'vector_qwen3': 'policy_vector_qwen3',
    'vector_legacy': 'policy_vector',
}


# --------------------------------------------------------------------------
# 连接 / 开关
# --------------------------------------------------------------------------

def database_url():
    """优先 env ADMIN_DB_URL；其次 policy_core.rule_store 的口径；最后 sqlite。"""
    url = os.environ.get(DEFAULT_URL_KEY)
    if url:
        return url
    try:
        from .rule_store import database_url as _rule_url
        return _rule_url()
    except Exception:
        return 'sqlite://'


def _ensure_db_dialect():
    """导入 src/config.py 触发金仓（KingbaseES）方言补丁。

    SQLAlchemy 内置 PG 方言无法解析 KingbaseES 的版本串，直连会抛
    AssertionError: Could not determine version from string 'KingbaseES V008...'；
    config.py 在 import 时给 PGDialect 打版本解析补丁、并把保留字 level 加引号。
    未安装/无需金仓时静默跳过（sqlite/纯 PG 场景无副作用）。
    """
    try:
        import config  # noqa: F401
    except Exception:
        return


def engine(url=None):
    url = url or database_url()
    _ensure_db_dialect()
    return create_engine(url, pool_pre_ping=True)


def connect(url=None):
    """只读任务可用；apply 的提交责任在调用方（conn.commit() / engine.begin()）。"""
    return engine(url).connect()


def apply_allowed():
    """写开关：默认关闭，保持现网语义（CLI 只在 --apply 且开关为 1 时写库）。"""
    return os.environ.get('POLICY_AUTOPILOT', '0') == '1'


def stale_hours():
    try:
        return int(os.environ.get('POLICY_AUTOPILOT_STALE_HOURS', '72'))
    except ValueError:
        return 72


def expiring_days():
    try:
        return int(os.environ.get('POLICY_AUTOPILOT_EXPIRING_DAYS', '30'))
    except ValueError:
        return 30


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def _now(now=None):
    if now is None:
        return datetime.datetime.now()
    if isinstance(now, str):
        return _as_datetime(now) or datetime.datetime.now()
    if isinstance(now, datetime.date) and not isinstance(now, datetime.datetime):
        return datetime.datetime(now.year, now.month, now.day)
    return now


def _table_set(conn):
    try:
        return set(inspect(conn).get_table_names())
    except Exception:
        return set()


def has_table(conn, name):
    return bool(name) and name in _table_set(conn)


def columns(conn, table):
    try:
        return [c['name'] for c in inspect(conn).get_columns(table)]
    except Exception:
        return []


def _rows(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def _scalar(conn, sql, **params):
    row = conn.execute(text(sql), params).first()
    return None if row is None else row[0]


def _one(conn, sql, **params):
    return conn.execute(text(sql), params).first()


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    raw = str(value or '').strip()
    if not raw:
        return None
    raw = raw.replace('/', '-')[:10]
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def _as_datetime(value):
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    raw = str(value or '').strip()
    if not raw:
        return None
    raw = raw.replace('/', '-')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(raw[:19] if len(raw) > 19 else raw, fmt)
        except ValueError:
            continue
    return None


def _base_report(task, dry_run, **fields):
    report = {
        'task': task,
        'mode': 'dry_run' if dry_run else 'apply',
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    report.update(fields)
    return report


def _sample(items):
    return list(items or [])[:MAX_SAMPLE]


# --------------------------------------------------------------------------
# 1. 过期扫描
# --------------------------------------------------------------------------

def expire_scan(conn, days=None, dry_run=True, limit=200, today=None):
    """扫描在用政策的 effect_date/expire_date：已过期→建议停用；临期→仅提示。

    - 数据现状：policy.effect_date/expire_date 为 TEXT 且全空（17 号审计），
      因此必须容忍空结果并说明「字段为空」，不能报 0 条了事。
    - apply 只停用「已过期」且 status='用用' 的行；临期永不动；
      不动 policy_detail、不动向量/规则（残留由 health_check 暴露）。
    """
    today = _as_date(_now(today)) or datetime.date.today()
    window = expiring_days() if days is None else int(days)
    report = _base_report('expire_scan', dry_run, days=window,
                          expired=[], expiring=[], applied=[])
    cols = set(columns(conn, TABLES['policy']))
    if not cols:
        report.update(supported=False, reason='policy 表不存在')
        return report
    missing = [c for c in ('effect_date', 'expire_date') if c not in cols]
    if missing:
        report.update(supported=False, reason='policy 表缺少列: %s' % ','.join(missing))
        return report

    report['policy_total'] = _scalar(conn, 'SELECT count(*) FROM policy') or 0
    all_dates = _rows(conn, 'SELECT effect_date, expire_date FROM policy')
    effect_non_empty = sum(1 for row in all_dates if _as_date(row[0]))
    expire_non_empty = sum(1 for row in all_dates if _as_date(row[1]))
    report['date_fields'] = {
        'effect_date_non_empty': effect_non_empty,
        'expire_date_non_empty': expire_non_empty,
        'rows': len(all_dates),
    }
    active_sql = ('SELECT policy_id, policy_code, policy_name, expire_date '
                  'FROM policy WHERE "status" = :status')
    rows = _rows(conn, active_sql, status='在用')
    report['before'] = {'active': len(rows)}
    horizon = today + datetime.timedelta(days=window)
    for policy_id, code, name, expire_raw in rows:
        expire = _as_date(expire_raw)
        if not expire:
            continue
        item = {'policy_id': policy_id, 'policy_code': code,
                'policy_name': name, 'expire_date': expire.isoformat()}
        if expire <= today:
            item['days_overdue'] = (today - expire).days
            report['expired'].append(item)
        elif expire <= horizon:
            item['days_left'] = (expire - today).days
            report['expiring'].append(item)
    report['expired'].sort(key=lambda x: x['expire_date'])
    report['expiring'].sort(key=lambda x: x['expire_date'])
    report['counts'] = {'expired': len(report['expired']),
                        'expiring': len(report['expiring'])}
    if not report['expired'] and not report['expiring']:
        report['note'] = ('无可用时效数据：effect_date/expire_date 非空分别为 '
                          '%d/%d 行' % (effect_non_empty, expire_non_empty))
    if dry_run:
        report['note_apply'] = 'dry-run：不写库；apply 只会停用已过期政策'
    else:
        applied = []
        for item in report['expired'][:max(0, int(limit))]:
            conn.execute(text('UPDATE policy SET "status" = :status '
                              'WHERE policy_id = :pid AND "status" = :active'),
                         {'status': '停用', 'pid': item['policy_id'], 'active': '在用'})
            applied.append(item['policy_code'])
        report['applied'] = applied
        report['after'] = {'active': _scalar(
            conn, 'SELECT count(*) FROM policy WHERE "status" = :status', status='在用') or 0}
    report['expired'] = _sample(report['expired'])
    report['expiring'] = _sample(report['expiring'])
    return report


# --------------------------------------------------------------------------
# 2. outbox 消费
# --------------------------------------------------------------------------

def outbox_drain(conn, limit=200, dry_run=True, handler=None, handlers=None):
    """消费业务写操作的 outbox（T18 提供 policy_core.outbox.drain）。

    三种模式，按优先级：
    1. handler=callable（测试/未来整批消费者入口）：handler(conn, limit=, dry_run=) -> dict；
    2. handlers={event_type: callable(payload)}（显式事件消费者）：dry-run 只 claim 预览，
       apply 调 outbox.drain(conn, handlers, limit)；
    3. 都不传：只探测 outbox 表与各状态计数，如实标注「消费者未接线」（不消费，
       避免 outbox.drain 把无 handler 的事件直接置 dead）。
    """
    report = _base_report('outbox_drain', dry_run, limit=int(limit),
                          dependency='policy_core.outbox')
    probe = {}
    tables = _table_set(conn)
    for name in ('outbox', 'outbox_event', 'policy_outbox', 'policy_lifecycle_outbox'):
        if name in tables:
            try:
                probe[name] = _scalar(conn, 'SELECT count(*) FROM ' + name)
            except Exception as exc:  # pragma: no cover - 结构异常兜底
                probe[name] = 'err: %s' % exc
    report['table_probe'] = probe

    module = None
    module_error = ''
    try:
        from . import outbox as module
    except Exception as exc:  # pragma: no cover - T18 未合并的降级路径
        module_error = '%s: %s' % (type(exc).__name__, exc)
        report.update(available=False,
                      reason='policy_core.outbox 未就绪（T18 未合并）',
                      import_error=module_error)
        return report
    report['module'] = 'policy_core.outbox'
    if hasattr(module, 'counts'):
        try:
            report['counts'] = module.counts(conn)
        except Exception as exc:  # pragma: no cover - 方言/权限兜底
            report['counts_error'] = '%s: %s' % (type(exc).__name__, exc)

    if handler is not None:
        kwargs = {'limit': int(limit), 'dry_run': bool(dry_run)}
        try:
            result = _call_drain(handler, conn, kwargs)
        except Exception as exc:
            report.update(available=True, mode='handler',
                          error='%s: %s' % (type(exc).__name__, exc))
            return report
        report.update(available=True, mode='handler', result=result)
        if isinstance(result, dict):
            for key in ('drained', 'processed', 'pending', 'failed', 'skipped'):
                if key in result:
                    report[key] = result[key]
        return report

    if handlers:
        report['mode'] = 'drain'
        if dry_run:
            events = module.claim(conn, limit=int(limit)) if hasattr(module, 'claim') else []
            report.update(available=True, claimable=len(events),
                          preview=[{'id': e.get('id'), 'event_type': e.get('event_type')}
                                   for e in events[:MAX_SAMPLE]])
            return report
        try:
            result = module.drain(conn, handlers, limit=int(limit))
        except Exception as exc:
            report.update(available=True, error='%s: %s' % (type(exc).__name__, exc))
            return report
        report.update(available=True, result=result)
        if isinstance(result, dict):
            for key in ('processed', 'done', 'retried', 'dead', 'skipped'):
                if key in result:
                    report[key] = result[key]
        return report

    report.update(available=True, mode='probe',
                  reason='outbox 消费者未接线：需传入 handlers（事件类型→处理函数）才实际消费',
                  note='probe 只读；不调用 drain，避免无 handler 事件被置 dead')
    return report


def _call_drain(handler, conn, kwargs):
    """兼容 handler(conn, limit, dry_run) / handler(conn=..., ...) / handler(limit=...) 三种写法。"""
    try:
        signature = _pyinspect.signature(handler)
        params = signature.parameters
        accepts_conn = 'conn' in params or any(
            p.kind == p.VAR_KEYWORD for p in params.values())
        call_kwargs = dict(kwargs)
        if accepts_conn:
            call_kwargs['conn'] = conn
        elif not any(p.kind == p.VAR_KEYWORD for p in params.values()):
            call_kwargs.pop('conn', None)
        return handler(**call_kwargs)
    except (TypeError, ValueError):
        try:
            return handler(conn, **kwargs)
        except TypeError:
            return handler(**kwargs)


# --------------------------------------------------------------------------
# 3. 质量巡检
# --------------------------------------------------------------------------

def health_check(conn):
    """库级质量巡检：空壳政策、无 scope、重复明细、无版本、孤儿明细、停用残留向量。

    重复组口径沿用 17 号审计：同 (region, conditions, target) 的 policy_detail
    完全同文组（917 组 / 3430 行 / 冗余 2513 行）。
    """
    report = _base_report('health_check', True)
    tables = _table_set(conn)
    checks = {}
    has_policy = 'policy' in tables
    report['policy_total'] = (_scalar(conn, 'SELECT count(*) FROM policy') or 0) if has_policy else 0
    report['detail_total'] = (
        _scalar(conn, 'SELECT count(*) FROM policy_detail') or 0
        if 'policy_detail' in tables and has_policy else 0)

    if not has_policy:
        report['checks'] = {name: {'skipped': 'policy 表不存在'} for name in (
            'empty_shell', 'no_scope', 'duplicate_details', 'no_version',
            'orphan_details', 'inactive_vector_residue')}
        report['issue_total'] = 0
        report['verdict'] = 'ok'
        return report

    # 3.1 空壳政策：0 明细
    if 'policy_detail' in tables:
        rows = _rows(conn, 'SELECT p.policy_code, p.policy_name FROM policy p '
                           'WHERE NOT EXISTS (SELECT 1 FROM policy_detail d '
                           'WHERE d.policy_id = p.policy_id) ORDER BY p.policy_code')
        checks['empty_shell'] = {'count': len(rows),
                                 'sample': _sample([[r[0], r[1]] for r in rows])}
    else:
        checks['empty_shell'] = {'skipped': 'policy_detail 表不存在'}

    # 3.2 无 scope：policy_scope 存在才做
    if 'policy_scope' in tables:
        n = _scalar(conn, 'SELECT count(*) FROM policy p WHERE NOT EXISTS '
                          '(SELECT 1 FROM policy_scope s WHERE s.policy_code = p.policy_code)')
        rows = _rows(conn, 'SELECT p.policy_code FROM policy p WHERE NOT EXISTS '
                                '(SELECT 1 FROM policy_scope s WHERE s.policy_code = p.policy_code) '
                                'ORDER BY p.policy_code')
        checks['no_scope'] = {'count': n or 0, 'sample': _sample([r[0] for r in rows]),
                              'table': 'policy_scope'}
    else:
        checks['no_scope'] = {'skipped': 'policy_scope 表不存在（POLICY_SCOPE 未启用）'}

    # 3.3 重复明细组
    if 'policy_detail' in tables:
        groups = _rows(conn, 'SELECT "region", "conditions", "target", count(*) AS n '
                             'FROM policy_detail GROUP BY "region", "conditions", "target" '
                             'HAVING count(*) > 1 ORDER BY n DESC')
        redundant = sum(int(g[3]) - 1 for g in groups)
        sample = []
        for region, conditions, target, n in _sample(groups):
            sample.append({'region': region, 'conditions': (conditions or '')[:40],
                           'target': (target or '')[:40], 'count': int(n)})
        checks['duplicate_details'] = {
            'groups': len(groups), 'rows': sum(int(g[3]) for g in groups),
            'redundant': redundant, 'sample': sample,
            'criterion': '(region, conditions, target) 完全相同'}
    else:
        checks['duplicate_details'] = {'skipped': 'policy_detail 表不存在'}

    # 3.4 无版本快照
    if 'policy_version' in tables:
        n = _scalar(conn, 'SELECT count(*) FROM policy p WHERE NOT EXISTS '
                          '(SELECT 1 FROM policy_version v WHERE v.policy_id = p.policy_id)')
        rows = _rows(conn, 'SELECT p.policy_code FROM policy p WHERE NOT EXISTS '
                                '(SELECT 1 FROM policy_version v WHERE v.policy_id = p.policy_id) '
                                'ORDER BY p.policy_code')
        checks['no_version'] = {'count': n or 0, 'sample': _sample([r[0] for r in rows])}
    else:
        checks['no_version'] = {'skipped': 'policy_version 表不存在'}

    # 3.5 孤儿明细
    if 'policy_detail' in tables:
        n = _scalar(conn, 'SELECT count(*) FROM policy_detail d WHERE NOT EXISTS '
                          '(SELECT 1 FROM policy p WHERE p.policy_id = d.policy_id)')
        rows = _rows(conn, 'SELECT d.detail_id FROM policy_detail d WHERE NOT EXISTS '
                                '(SELECT 1 FROM policy p WHERE p.policy_id = d.policy_id) '
                                'ORDER BY d.detail_id')
        checks['orphan_details'] = {'count': n or 0, 'sample': _sample([r[0] for r in rows])}
    else:
        checks['orphan_details'] = {'skipped': 'policy_detail 表不存在'}

    # 3.6 停用政策残留向量（两代向量表都存在时分别报，生产读 qwen3 表）
    residue = {}
    for key, table in (('qwen3', TABLES['vector_qwen3']), ('legacy', TABLES['vector_legacy'])):
        if table not in tables:
            residue[key] = {'skipped': '%s 表不存在' % table}
            continue
        n = _scalar(conn, 'SELECT count(*) FROM ' + table + ' v JOIN policy p '
                          'ON p.policy_code = v.policy_code WHERE p."status" <> :status',
                    status='在用')
        rows = _rows(conn, 'SELECT DISTINCT v.policy_code FROM ' + table + ' v JOIN policy p '
                          'ON p.policy_code = v.policy_code WHERE p."status" <> :status '
                          'ORDER BY v.policy_code', status='在用')
        residue[key] = {'count': n or 0, 'sample': _sample([r[0] for r in rows])}
    checks['inactive_vector_residue'] = residue

    issues = 0
    for name, check in checks.items():
        if not isinstance(check, dict):
            continue
        if name == 'inactive_vector_residue':
            for sub in check.values():
                if isinstance(sub, dict) and isinstance(sub.get('count'), int):
                    issues += sub['count']
            continue
        if isinstance(check.get('count'), int):
            issues += check['count']
        elif name == 'duplicate_details':
            issues += check.get('redundant', 0)
    report['checks'] = checks
    report['issue_total'] = issues
    report['verdict'] = 'ok' if issues == 0 else 'attention'
    return report


# --------------------------------------------------------------------------
# 4. 覆盖报告
# --------------------------------------------------------------------------

def _region_stats(conn):
    rows = _rows(conn, 'SELECT "region", count(*) AS n, count(DISTINCT policy_id) AS p '
                       'FROM policy_detail GROUP BY "region"')
    stats = {}
    for region, n, p in rows:
        stats[str(region or '').strip()] = {'details': int(n or 0), 'policies': int(p or 0)}
    return stats


def _region_policy_sets(conn):
    """region -> {policy_id}：用于跨县去重统计省/市政策数（避免逐县 distinct 相加重复）。"""
    sets = {}
    for region, policy_id in _rows(conn, 'SELECT "region", policy_id FROM policy_detail'):
        sets.setdefault(str(region or '').strip(), set()).add(policy_id)
    return sets


def coverage_report(conn, provinces=None):
    """按省/市/县汇总政策/明细与覆盖缺口（当前库全在江西，江西标准县区 100）。

    口径：policy_detail.region 为唯一覆盖事实来源；标准县区名以
    data/regions/china_divisions.json（admin_division）为准，逐府枚举做差集；
    功能区（is_zone/zone canonical）单列，不占标准县区名额。
    """
    from . import admin_division
    report = _base_report('coverage_report', True)
    if not has_table(conn, 'policy_detail'):
        report.update(supported=False, reason='policy_detail 表不存在')
        return report
    stats = _region_stats(conn)
    policy_sets = _region_policy_sets(conn)
    report['totals'] = {
        'policies': _scalar(conn, 'SELECT count(*) FROM policy') or 0,
        'details': sum(s['details'] for s in stats.values()),
        'regions': len(stats),
    }
    levels = {}
    for level, n in _rows(conn, 'SELECT "level", count(*) FROM policy GROUP BY "level"'):
        levels[str(level)] = int(n)
    report['policy_levels'] = levels

    provinces = [p for p in (provinces or admin_division.provinces())]
    by_province = {}
    zones, unknown, province_entries = [], [], []
    for region, stat in stats.items():
        info = admin_division.canonical(region)
        level = info.get('level')
        province = info.get('province')
        if level == admin_division.NATION:
            unknown.append({'region': region, 'level': 'nation', 'details': stat['details']})
            continue
        if level == admin_division.PROVINCE:
            province_entries.append({'region': region,
                                     'province': info.get('province'),
                                     'details': stat['details'],
                                     'policies': stat['policies']})
            continue
        if not province:
            unknown.append({'region': region, 'level': level or 'unknown',
                            'details': stat['details'], 'ambiguous': info.get('ambiguous') or []})
            continue
        bucket = by_province.setdefault(province, {'details': 0, 'policy_ids': set(),
                                                   'counties': {}, 'zones': []})
        bucket['details'] += stat['details']
        bucket['policy_ids'].update(policy_sets.get(region, set()))
        if level == admin_division.COUNTY and (info.get('zone') or admin_division.is_zone(region)):
            bucket['zones'].append({'region': region, 'city': info.get('city'),
                                    'details': stat['details'],
                                    'policies': stat['policies']})
        elif level == admin_division.COUNTY:
            bucket['counties'][info.get('county') or region] = stat
        else:
            unknown.append({'region': region, 'level': level, 'details': stat['details']})

    report['province_level_entries'] = sorted(province_entries, key=lambda x: x['region'])
    report['zones'] = sorted(
        [dict(zone, province=prov) for prov, b in by_province.items() for zone in b['zones']],
        key=lambda x: (-x['details'], x['region']))
    report['unknown_regions'] = sorted(unknown, key=lambda x: -x.get('details', 0))

    rows = []
    total_expected = total_covered = total_missing = 0
    for province in provinces:
        bucket = by_province.get(province, {'details': 0, 'policy_ids': set(),
                                             'counties': {}, 'zones': []})
        cities = []
        prov_expected = prov_covered = prov_missing = 0
        for city in admin_division.cities_of(province):
            expected = list(admin_division.counties_of(city, province))
            if not expected:
                continue
            covered = [c for c in expected if c in bucket['counties']]
            missing = [c for c in expected if c not in bucket['counties']]
            city_details = sum(bucket['counties'][c]['details'] for c in covered)
            city_policy_ids = set()
            for c in covered:
                city_policy_ids.update(policy_sets.get(c, set()))
            city_policies = len(city_policy_ids)
            city_zones = [z for z in bucket['zones'] if z.get('city') == city]
            prov_expected += len(expected)
            prov_covered += len(covered)
            prov_missing += len(missing)
            cities.append({
                'city': city,
                'expected_counties': len(expected),
                'covered_counties': len(covered),
                'missing_counties': missing,
                'details': city_details,
                'policies': city_policies,
                'zones': city_zones,
            })
        covered = sorted(bucket['counties'])
        if not bucket['details'] and not bucket['zones'] and province != '江西省':
            continue
        rows.append({
            'province': province,
            'expected_counties': prov_expected,
            'covered_counties': prov_covered,
            'missing_counties': prov_missing,
            'missing': [c for city in cities for c in city['missing_counties']],
            'covered_sample': covered[:MAX_SAMPLE],
            'details': bucket['details'],
            'policies': len(bucket['policy_ids']),
            'zones': len(bucket['zones']),
            'cities': cities,
        })
        total_expected += prov_expected
        total_covered += prov_covered
        total_missing += prov_missing
    report['provinces'] = [row for row in rows if row['details'] or row['expected_counties']]
    report['national_gap'] = {
        'expected_standard_counties': total_expected,
        'covered_standard_counties': total_covered,
        'missing_standard_counties': total_missing,
    }
    return report


# --------------------------------------------------------------------------
# 5. 规则补队
# --------------------------------------------------------------------------

def _rule_county(region):
    """县区口径与 rule_flow._county 对齐：geo 归一 → 去尾 县/区；功能区返回空。"""
    region = str(region or '').strip()
    if not region:
        return ''
    county = ''
    try:
        from .geo import normalize_address
        county = (normalize_address(region) or {}).get('county') or ''
    except Exception:
        county = ''
    if not county:
        try:
            from . import admin_division
            info = admin_division.canonical(region)
            if info.get('level') != admin_division.COUNTY or info.get('zone'):
                return ''
            county = info.get('county') or ''
        except Exception:
            return ''
    if county.endswith(('县', '区')):
        county = county[:-1]
    return county


def _rule_scope_stats(conn):
    scopes = {}
    for scope, n in _rows(conn, 'SELECT "region_scope", count(*) FROM policy_rule '
                                'GROUP BY "region_scope"'):
        scopes[str(scope or '').strip()] = int(n)
    return scopes


def _rule_jobs(conn):
    jobs = {}
    for region, state, total, done, failed, attempts in _rows(
            conn, 'SELECT "region", state, total, done, failed, attempts FROM policy_rule_job'):
        jobs[str(region)] = {'state': state, 'total': int(total or 0),
                             'done': int(done or 0), 'failed': int(failed or 0),
                             'attempts': int(attempts or 0)}
    return jobs


def rule_requeue(conn, dry_run=True, limit=200, message='autopilot 巡检补队'):
    """找出缺本地规则的县区，入队 policy_rule_job（不触发编译、不调 LLM）。

    - 县区来源：policy_detail.region 归一后的县名集合；
    - 已覆盖：policy_rule.region_scope 的县名（'*' 记 global_rules，但不豁免单县缺口）；
    - 入队语义与 policy_core.rule_store.enqueue 一致（insert pending / failed→pending /
      done 且 total=0 → pending），只是在本 conn 上执行，便于 sqlite 测试与单事务。
    """
    report = _base_report('rule_requeue', dry_run, limit=int(limit),
                          missing=[], enqueued=[], zones_excluded=[])
    tables = _table_set(conn)
    if 'policy_detail' not in tables:
        report.update(supported=False, reason='policy_detail 表不存在')
        return report
    regions = [str(r[0] or '').strip() for r in
               _rows(conn, 'SELECT DISTINCT "region" FROM policy_detail')]
    counties, zones = set(), []
    for region in regions:
        if not region:
            continue
        county = _rule_county(region)
        if county:
            counties.add(county)
        else:
            zones.append(region)
    report['county_total'] = len(counties)
    report['zones_excluded'] = _sample(sorted(zones))
    report['zones_excluded_total'] = len(zones)
    scopes = _rule_scope_stats(conn) if 'policy_rule' in tables else {}
    jobs = _rule_jobs(conn) if 'policy_rule_job' in tables else {}
    covered = sorted(c for c in scopes if c != '*')
    report['global_rules'] = scopes.get('*', 0)
    report['covered'] = covered
    missing = sorted(c for c in counties if c not in set(covered))
    report['missing'] = missing
    report['counts'] = {'county_total': len(counties), 'covered': len(covered),
                        'missing': len(missing)}
    report['before'] = {'jobs': len(jobs),
                        'pending': sum(1 for j in jobs.values() if j['state'] == 'pending'),
                        'failed': sum(1 for j in jobs.values() if j['state'] == 'failed')}
    if not ('policy_rule_job' in tables):
        report.update(supported=False, reason='policy_rule_job 表不存在')
        return report
    if dry_run:
        report['plan'] = [{'county': c, 'job_state': (jobs.get(c) or {}).get('state')}
                          for c in _sample(missing)]
        report['note_apply'] = 'dry-run：不写库；apply 只入队/重置 job，不触发编译'
        return report

    applied = []
    now = datetime.datetime.now()
    for county in missing[:max(0, int(limit))]:
        job = jobs.get(county)
        if job is None:
            conn.execute(text(
                'INSERT INTO policy_rule_job ("region", state, total, done, failed, '
                'attempts, message, created_at) VALUES (:region, :state, 0, 0, 0, 0, '
                ':message, :created)'),
                {'region': county, 'state': 'pending', 'message': message,
                 'created': now})
            applied.append({'county': county, 'action': 'inserted', 'state': 'pending'})
        elif job['state'] in ('failed',) or (job['state'] == 'done' and job['total'] == 0):
            conn.execute(text(
                'UPDATE policy_rule_job SET state = :state, attempts = attempts + 1, '
                'message = :message, started_at = NULL, finished_at = NULL '
                'WHERE "region" = :region'),
                {'region': county, 'state': 'pending', 'message': message})
            applied.append({'county': county, 'action': 'requeued',
                            'state_before': job['state'], 'state': 'pending'})
        else:
            applied.append({'county': county, 'action': 'skipped',
                            'state': job['state']})
    report['enqueued'] = applied
    after = _rule_jobs(conn)
    report['after'] = {'jobs': len(after),
                       'pending': sum(1 for j in after.values() if j['state'] == 'pending'),
                       'failed': sum(1 for j in after.values() if j['state'] == 'failed')}
    return report


# --------------------------------------------------------------------------
# 6. 滞留草稿
# --------------------------------------------------------------------------

def stale_draft(conn, hours=None, now=None):
    """policy_draft 中 confirm_status='待确认' 且滞留超过 hours 的条目。"""
    hours = stale_hours() if hours is None else int(hours)
    current = _now(now)
    report = _base_report('stale_draft', True, threshold_hours=hours, items=[])
    table = TABLES['draft']
    cols = set(columns(conn, table))
    if not cols:
        report.update(supported=False, reason='policy_draft 表不存在')
        return report
    if 'created_at' not in cols:
        report.update(supported=False, reason='policy_draft 缺少 created_at 列')
        return report
    report['draft_total'] = _scalar(conn, 'SELECT count(*) FROM policy_draft') or 0
    by_status = {}
    for status, n in _rows(conn, 'SELECT confirm_status, count(*) FROM policy_draft '
                                'GROUP BY confirm_status'):
        by_status[str(status)] = int(n)
    report['by_confirm_status'] = by_status
    rows = _rows(conn, 'SELECT draft_id, policy_code, policy_name, confirm_status, '
                       'created_at FROM policy_draft WHERE confirm_status = :s',
                 s='待确认')
    cutoff = current - datetime.timedelta(hours=hours)
    items = []
    for draft_id, code, name, status, created_raw in rows:
        created = _as_datetime(created_raw)
        if not created:
            continue
        created = created.replace(microsecond=0)
        if created < cutoff:
            items.append({'draft_id': draft_id, 'policy_code': code,
                          'policy_name': name, 'created_at': created.strftime('%Y-%m-%d %H:%M:%S'),
                          'age_hours': round((current - created).total_seconds() / 3600.0, 1)})
    items.sort(key=lambda x: -x['age_hours'])
    report['pending_total'] = len(items)
    report['items'] = _sample(items)
    report['stale_count'] = len(items)
    if not items:
        report['note'] = '没有滞留草稿'
    return report


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------

def run_all(conn, dry_run=True, limit=200, days=None, hours=None):
    """一次跑完六项体检（CLI all / 未来 worker 的 tick 入口）。"""
    return {
        'expire_scan': expire_scan(conn, days=days, dry_run=dry_run, limit=limit),
        'outbox_drain': outbox_drain(conn, limit=limit, dry_run=dry_run),
        'health_check': health_check(conn),
        'coverage_report': coverage_report(conn),
        'rule_requeue': rule_requeue(conn, dry_run=dry_run, limit=limit),
        'stale_draft': stale_draft(conn, hours=hours),
    }

# --------------------------------------------------------------------------
# 7. 可选定时 worker（默认关闭，零行为变化）
# --------------------------------------------------------------------------

WORKER_VERSION = 'policy-autopilot-worker-1'
WORKER_NAME = 'policy-autopilot'
DEFAULT_INTERVAL = 3600
_WORKER_ON_VALUES = ('1', 'on', 'true', 'yes')

_log = logging.getLogger(__name__)
_thread = None
_stop_event = threading.Event()
_guard = threading.Lock()


def enabled():
    """POLICY_AUTOPILOT_WORKER=1/on/true/yes 才启动后台定时体检；默认关闭。"""
    return (str(os.environ.get('POLICY_AUTOPILOT_WORKER', '') or '')
            .strip().lower() in _WORKER_ON_VALUES)


def interval():
    """两轮体检的间隔秒数（POLICY_AUTOPILOT_INTERVAL，默认 3600，非法/<=0 兜底）。"""
    try:
        value = int(str(os.environ.get('POLICY_AUTOPILOT_INTERVAL', '') or '').strip())
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return value if value >= 1 else DEFAULT_INTERVAL


def _task_summary(report):
    """把一个任务 report 压成 worker 摘要：保留模式/关键计数，不带样本明细。"""
    if not isinstance(report, dict):
        return {'report': str(report)[:120]}
    summary = {}
    for key in ('task', 'supported', 'reason', 'issue_total', 'verdict',
                'stale_count', 'pending_total', 'counts'):
        if key in report:
            value = report[key]
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if not isinstance(v, (list, dict))}
            summary[key] = value
    if 'mode' in report:
        summary['mode'] = report['mode']
    return summary


def _brief_summary(tasks):
    """一行日志用的任务摘要（不展开样本列表）。"""
    parts = []
    for name in sorted(tasks):
        item = tasks[name]
        detail = ''
        counts = item.get('counts')
        if isinstance(counts, dict) and counts:
            detail = ','.join('%s=%s' % (k, counts[k]) for k in sorted(counts))
        elif item.get('issue_total') is not None:
            detail = 'issues=%s' % item['issue_total']
        elif item.get('stale_count') is not None:
            detail = 'stale=%s' % item['stale_count']
        elif item.get('supported') is False:
            detail = 'unsupported'
        parts.append('%s[%s%s]' % (name, item.get('mode', '?'),
                                   (':' + detail) if detail else ''))
    return ' '.join(parts)


def tick(conn=None, url=None):
    """执行一轮六项体检并返回摘要；异常记 logger.exception 后返回 ok=False，不抛出。

    dry_run 由 POLICY_AUTOPILOT 决定：默认 dry-run；=1 时按 apply 跑（只写本模块
    负责的列/队列表）。conn 为 None 时自行开短事务并提交；调用方传入 conn 时提交
    责任在调用方（与 run_all 语义一致）。
    """
    now = datetime.datetime.now()
    apply = apply_allowed()
    mode = 'apply' if apply else 'dry_run'
    try:
        if conn is None:
            own_engine = engine(url)
            try:
                with own_engine.begin() as own_conn:
                    reports = run_all(own_conn, dry_run=not apply)
            finally:
                own_engine.dispose()
        else:
            reports = run_all(conn, dry_run=not apply)
    except Exception as exc:  # noqa: BLE001 - 后台线程不能因单轮异常退出
        _log.exception('policy autopilot worker 单轮体检失败')
        return {'ok': False, 'mode': mode,
                'error': '%s: %s' % (type(exc).__name__, str(exc)[:300]),
                'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'tasks': {}}
    tasks = {}
    for name, report in reports.items():
        tasks[name] = _task_summary(report)
    result = {'ok': True, 'mode': mode,
              'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'tasks': tasks}
    _log.info('policy autopilot worker tick: mode=%s %s', mode, _brief_summary(tasks))
    return result


def _loop():
    gap = interval()
    _log.info('policy autopilot worker 启动：version=%s interval=%ss mode=%s',
              WORKER_VERSION, gap, 'apply' if apply_allowed() else 'dry_run')
    while not _stop_event.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001 - 兜底：后台线程不能因单轮异常退出
            _log.exception('policy autopilot worker 单轮异常')
        _stop_event.wait(gap)
    _log.info('policy autopilot worker 已停止')


def start():
    """启动守护线程（幂等）：开关关闭时返回 False，不建线程、不连库。"""
    global _thread
    if not enabled():
        return False
    with _guard:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        thread = threading.Thread(target=_loop, name=WORKER_NAME, daemon=True)
        _thread = thread
        thread.start()
    return True


def stop(timeout=5.0):
    """通知并等待线程退出；未启动时直接返回 True。"""
    global _thread
    with _guard:
        thread = _thread
        _thread = None
        _stop_event.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    return thread is None or not thread.is_alive()


def main(argv=None):
    """模块入口：跑一轮体检后退出（cron/临时排查用，默认 dry-run）。"""
    parser = argparse.ArgumentParser(
        description='政策自动管理定时体检（单轮，默认 dry-run；写库需 POLICY_AUTOPILOT=1）')
    parser.add_argument('--once', action='store_true', help='只跑一轮后退出（当前唯一模式）')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    parser.add_argument('--url', default=None, help='覆盖数据库 URL（默认 ADMIN_DB_URL）')
    args = parser.parse_args(argv)
    result = tick(url=args.url)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get('ok') else 1
    print('mode:', result.get('mode'))
    print('generated_at:', result.get('generated_at'))
    if not result.get('ok'):
        print('error:', result.get('error'))
        return 1
    for name in sorted(result.get('tasks') or {}):
        print('==', name, '==')
        for key, value in result['tasks'][name].items():
            print(' ', key + ':', json.dumps(value, ensure_ascii=False, default=str)[:400])
    return 0


if __name__ == '__main__':
    sys.exit(main())

