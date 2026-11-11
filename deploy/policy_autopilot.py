#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""政策自动管理 CLI（L4）：体检 + 受控写队列/状态。

默认 dry-run（只读报告）；--apply 才写库，且要求 POLICY_AUTOPILOT=1。

用法：
  python deploy/policy_autopilot.py health-check            # 只读质量巡检
  python deploy/policy_autopilot.py coverage-report --json   # 只读覆盖报告
  python deploy/policy_autopilot.py expire-scan --days 30    # 只读过期/临期扫描
  python deploy/policy_autopilot.py outbox-drain             # 探测 outbox 依赖
  python deploy/policy_autopilot.py rule-requeue             # 列出缺规则县区（不入队）
  python deploy/policy_autopilot.py stale-draft --hours 72   # 滞留草稿
  python deploy/policy_autopilot.py all --json
  POLICY_AUTOPILOT=1 python deploy/policy_autopilot.py rule-requeue --apply --limit 20

写操作（--apply，且需 POLICY_AUTOPILOT=1）：
  - expire-scan：只停用「已过期」政策（status='停用'）；
  - rule-requeue：只入队/重置 policy_rule_job（不触发编译、不调 LLM）；
  - outbox-drain：交由 policy_core.outbox.drain 决定写什么（T18 未就绪则只探测）。
其余任务恒只读。
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

TASK_NAMES = ('expire-scan', 'outbox-drain', 'health-check',
              'coverage-report', 'rule-requeue', 'stale-draft')


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


def _run_task(name, conn, args):
    from policy_core import autopilot
    if name == 'expire-scan':
        return autopilot.expire_scan(conn, days=args.days, dry_run=not args.apply,
                                     limit=args.limit)
    if name == 'outbox-drain':
        return autopilot.outbox_drain(conn, limit=args.limit, dry_run=not args.apply)
    if name == 'health-check':
        return autopilot.health_check(conn)
    if name == 'coverage-report':
        return autopilot.coverage_report(conn)
    if name == 'rule-requeue':
        return autopilot.rule_requeue(conn, dry_run=not args.apply, limit=args.limit)
    if name == 'stale-draft':
        return autopilot.stale_draft(conn, hours=args.hours)
    raise SystemExit('unknown task: %s' % name)


def _print_report(report, indent=''):
    if not isinstance(report, dict):
        print(indent + str(report))
        return
    skip = {'task', 'generated_at'}
    for key, value in report.items():
        if key in skip:
            continue
        label = indent + key + ':'
        if isinstance(value, dict):
            flat = ', '.join('%s=%s' % (k, v) for k, v in value.items()
                             if not isinstance(v, (list, dict)))
            print(label, flat)
            for k, v in value.items():
                if isinstance(v, dict) and ('count' in v or 'skipped' in v or 'groups' in v):
                    print(indent + '  - ' + k + ':', json.dumps(v, ensure_ascii=False, default=str)[:400])
        elif isinstance(value, list):
            print(label, 'n=%d' % len(value))
            for item in value[:20]:
                print(indent + '  -', json.dumps(item, ensure_ascii=False, default=str)[:400])
        else:
            print(label, value)


def _print_health(report):
    print('== health-check ==')
    print('policy_total:', report.get('policy_total'), ' detail_total:', report.get('detail_total'))
    checks = report.get('checks') or {}
    for name, check in checks.items():
        print('-', name)
        print('  ', json.dumps(check, ensure_ascii=False, default=str)[:900])
    print('issue_total:', report.get('issue_total'), ' verdict:', report.get('verdict'))


def _print_coverage(report):
    print('== coverage-report ==')
    print('totals:', json.dumps(report.get('totals'), ensure_ascii=False))
    print('policy_levels:', json.dumps(report.get('policy_levels'), ensure_ascii=False))
    print('national_gap:', json.dumps(report.get('national_gap'), ensure_ascii=False))
    for prov in report.get('provinces') or []:
        print('-', prov['province'], 'details=%s policies=%s zones=%s'
              % (prov['details'], prov['policies'], prov['zones']),
              'counties %s/%s' % (prov['covered_counties'], prov['expected_counties']),
              'missing=%s' % prov['missing'])
        if prov.get('missing'):
            print('  missing:', json.dumps(prov['missing'], ensure_ascii=False))
        for city in prov.get('cities') or []:
            if city['missing_counties'] or city['details']:
                print('   ', city['city'], '%s/%s' % (city['covered_counties'], city['expected_counties']),
                      'missing=%s' % json.dumps(city['missing_counties'], ensure_ascii=False))
    zones = report.get('zones') or []
    print('zones(n=%d):' % len(zones), json.dumps([z['region'] for z in zones], ensure_ascii=False))
    entries = report.get('province_level_entries') or []
    print('province_level_entries:', json.dumps(entries, ensure_ascii=False))
    unknown = report.get('unknown_regions') or []
    if unknown:
        print('unknown_regions:', json.dumps(unknown, ensure_ascii=False)[:800])


def main(argv=None):
    parser = argparse.ArgumentParser(description='政策自动管理（默认 dry-run）')
    sub = parser.add_subparsers(dest='command')
    for name in TASK_NAMES + ('all',):
        cmd = sub.add_parser(name)
        cmd.add_argument('--apply', action='store_true', help='写库（默认 dry-run，需 POLICY_AUTOPILOT=1）')
        cmd.add_argument('--limit', type=int, default=200, help='单次写/列出的上限')
        cmd.add_argument('--days', type=int, default=None, help='临期窗口天数（expire-scan）')
        cmd.add_argument('--hours', type=int, default=None, help='滞留阈值小时（stale-draft）')
        cmd.add_argument('--json', action='store_true', help='JSON 输出')
        cmd.add_argument('--env-file', default=None)
        cmd.add_argument('--url', default=None, help='覆盖数据库 URL（默认 ADMIN_DB_URL）')
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    load_env(args.env_file)
    from policy_core import autopilot

    if args.apply and not autopilot.apply_allowed():
        print('拒绝写入：--apply 需要 POLICY_AUTOPILOT=1（当前 %r）；'
              'dry-run 可直接运行。' % os.environ.get('POLICY_AUTOPILOT', '0'),
              file=sys.stderr)
        return 2

    eng = autopilot.engine(args.url)
    names = TASK_NAMES if args.command == 'all' else (args.command,)
    reports = {}
    if args.apply:
        with eng.begin() as conn:
            for name in names:
                reports[name] = _run_task(name, conn, args)
    else:
        with eng.connect() as conn:
            for name in names:
                reports[name] = _run_task(name, conn, args)

    if args.json:
        payload = {'command': args.command, 'apply': bool(args.apply), 'reports': reports}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print('mode:', 'apply' if args.apply else 'dry_run')
    for name in names:
        report = reports[name]
        if name == 'health-check':
            _print_health(report)
        elif name == 'coverage-report':
            _print_coverage(report)
        else:
            print('==', name, '==')
            _print_report(report)
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
