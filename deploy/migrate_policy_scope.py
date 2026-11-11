#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""policy_scope 结构化地区范围回填工具。

默认 dry-run（只算报告不写库）；--apply 才写 policy_scope 表（幂等，按 policy_code 覆盖）。

用法：
  python deploy/migrate_policy_scope.py                # 只读：全库回填报告
  python deploy/migrate_policy_scope.py --apply        # 写入 policy_scope
  python deploy/migrate_policy_scope.py --apply --limit 100
  python deploy/migrate_policy_scope.py --stats        # 只读表内分布
  python deploy/migrate_policy_scope.py --json         # 机器可读输出
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


def main():
    parser = argparse.ArgumentParser(description='policy_scope 回填')
    parser.add_argument('--apply', action='store_true', help='写库（默认 dry-run）')
    parser.add_argument('--limit', type=int, default=None, help='最多处理多少条政策')
    parser.add_argument('--stats', action='store_true', help='只读表内分布')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    parser.add_argument('--env-file', default=None)
    args = parser.parse_args()

    load_env(args.env_file)
    from policy_core import policy_scope

    if args.stats:
        report = policy_scope.stats()
    else:
        # 存量数据是江西库，裸「全省」按江西解释；全国化后应改为按来源配置，不再默认江西。
        default_province = os.environ.get('POLICY_SCOPE_DEFAULT_PROVINCE') or '江西省'
        report = policy_scope.backfill(dry_run=not args.apply, limit=args.limit,
                                       default_province=default_province)
        report['mode'] = 'apply' if args.apply else 'dry_run'
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print('== policy_scope ==')
        print('mode:', report.get('mode', 'stats'))
        for key in ('policies', 'details', 'scope_rows', 'unresolved', 'ambiguous', 'zones',
                    'national', 'province_only', 'policies_without_scope', 'distinct_regions'):
            if key in report:
                value = report[key]
                print('%-22s %s' % (key, len(value) if isinstance(value, list) else value))
        if report.get('by_level'):
            print('by_level:', json.dumps(report['by_level'], ensure_ascii=False, sort_keys=True))
        for key in ('unresolved', 'ambiguous'):
            items = report.get(key) or []
            if items:
                print(key + '[:20]:', json.dumps(items[:20], ensure_ascii=False, default=str))
        stats = report.get('table')
        if stats:
            print('table:', json.dumps(stats, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
