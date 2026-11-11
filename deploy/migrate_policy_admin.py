#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""政策管理结构一次性迁移（生命周期 / 导入管线 / 结构化地区范围）。

默认只读检查（--check 语义）；--apply 才建表/补列，幂等。
建好之后 API 请求内的 ensure_schema 变成 no-op 级兜底，避免每次写请求都做 DDL、
在生产 Kingbase 上反复取 policy 表锁。

覆盖结构：
  outbox            生命周期/导入事件队列
  import_batch      导入批次
  import_row        导入明细行
  policy_scope      结构化地区范围（已单独回填过，这里只兜底建表）
  policy.deleted_at 软删时间戳（缺失才补列）

用法：
  python deploy/migrate_policy_admin.py             # 只读检查
  python deploy/migrate_policy_admin.py --json      # 机器可读
  python deploy/migrate_policy_admin.py --apply     # 建结构（幂等）
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_env(path=None):
    path = path or os.environ.get('POLICY_ENV_FILE') or os.path.join(BASE, 'deploy', 'web.env')
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())


STRUCTURES = ('outbox', 'import_batch', 'import_row', 'policy_scope')


def snapshot(conn):
    """读结构现状：表是否存在 + policy.deleted_at 列是否存在。"""
    from sqlalchemy import inspect
    table_names = set(inspect(conn).get_table_names() or [])
    tables = {name: name in table_names for name in STRUCTURES}
    columns = set()
    if 'policy' in table_names:
        columns = {col['name'] for col in inspect(conn).get_columns('policy')}
    return {'tables': tables, 'policy_deleted_at': 'deleted_at' in columns,
            'policy_table': 'policy' in table_names}


def missing_structures(snap):
    missing = [name for name, ok in snap['tables'].items() if not ok]
    if not snap['policy_deleted_at']:
        missing.append('policy.deleted_at')
    return missing


def apply_schema(conn):
    from policy_core import lifecycle, policy_import, policy_scope
    changed = False
    if lifecycle.ensure_schema(conn):
        changed = True
    policy_import.ensure_schema(conn)
    policy_scope.ensure_schema(conn)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description='政策管理结构一次性迁移')
    parser.add_argument('--apply', action='store_true', help='建表/补列（默认只读检查）')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    parser.add_argument('--env-file', default=None)
    parser.add_argument('--url', default=None, help='覆盖数据库连接串')
    args = parser.parse_args(argv)

    load_env(args.env_file)
    try:
        import config  # noqa: F401  触发 Kingbase/PG12 方言补丁
        from policy_core import rule_store
        from sqlalchemy import create_engine
    except Exception as error:  # pragma: no cover - 环境问题
        print('无法加载运行环境：%s' % error, file=sys.stderr)
        return 2

    url = args.url or rule_store.database_url()
    engine = create_engine(url, pool_pre_ping=True)
    report = {'url': url, 'mode': 'apply' if args.apply else 'check'}
    try:
        with engine.connect() as conn:
            before = snapshot(conn)
            report['missing_before'] = missing_structures(before)
            if args.apply:
                with engine.begin() as write_conn:
                    report['created'] = apply_schema(write_conn)
            after = snapshot(conn)
            report['missing_after'] = missing_structures(after)
            report['tables'] = after['tables']
            report['policy_deleted_at'] = after['policy_deleted_at']
            report['policy_table'] = after['policy_table']
    except Exception as error:
        report['error'] = '%s: %s' % (type(error).__name__, error)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print('迁移失败：%s' % report['error'], file=sys.stderr)
        return 2
    finally:
        try:
            engine.dispose()
        except Exception:
            pass

    report['ok'] = not report['missing_after']
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print('== 政策管理结构 ==')
        print('mode:            %s' % report['mode'])
        print('missing_before:  %s' % (', '.join(report['missing_before']) or '（无）'))
        print('missing_after:   %s' % (', '.join(report['missing_after']) or '（无）'))
        for name, ok in sorted(report['tables'].items()):
            print('  %-14s %s' % (name, 'ok' if ok else '缺失'))
        print('  %-14s %s' % ('policy.deleted_at', 'ok' if report['policy_deleted_at'] else '缺失'))
        if not report['ok'] and not args.apply:
            print('提示：加 --apply 才会建结构。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
