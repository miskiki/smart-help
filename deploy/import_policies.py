#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""政策批量导入命令行（T19）。

默认 dry-run：只解析、校验并与库内 diff 出报告，不写任何表。
--commit 显式提交（单事务写 policy/policy_detail/policy_version/policy_scope/outbox），
--rollback 按批次版本链回滚，--report 查看批次结果。

开关：POLICY_IMPORT 默认 0；未开启时本命令直接退出，不做任何读写。
环境变量：ADMIN_DB_URL 指向目标库；POLICY_ENV_FILE 可覆盖 env 文件路径。

用法：
  python deploy/import_policies.py --file policies.csv                 # dry-run 报告
  python deploy/import_policies.py --file policies.csv --commit        # 提交
  python deploy/import_policies.py --batch-id <id> --report            # 查看批次
  python deploy/import_policies.py --batch-id <id> --rollback          # 回滚批次
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


def print_report(report, as_json=False):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    print('== 政策批量导入 ==')
    for key in ('batch_id', 'mode', 'status', 'dry_run', 'total', 'valid', 'invalid',
                'conflict', 'changed', 'created', 'updated', 'noop', 'restored',
                'removed', 'skipped_count', 'source', 'file_name', 'committed_at'):
        if key in report:
            print('%-14s %s' % (key, report[key]))
    actions = report.get('actions')
    if actions:
        print('actions       %s' % json.dumps(actions, ensure_ascii=False, sort_keys=True))
    for item in report.get('errors') or []:
        print('错误 row %s %s: %s' % (item.get('row_no'), item.get('policy_code') or '',
                                      '；'.join(item.get('errors') or [])))
    for item in report.get('warnings') or []:
        print('提醒 row %s %s: %s' % (item.get('row_no'), item.get('policy_code') or '',
                                      '；'.join(item.get('warnings') or [])))
    for item in report.get('unresolved') or []:
        print('待核地区 row %s %s: %s' % (item.get('row_no'), item.get('policy_code') or '',
                                         item.get('raw_text')))
    for item in report.get('changed') or []:
        print('将变更 row %s %s: %s' % (item.get('row_no'), item.get('policy_code') or '',
                                       ','.join(item.get('changed_fields') or [])))
    for item in report.get('skipped') or []:
        print('回滚跳过 row %s %s: %s' % (item.get('row_no'), item.get('policy_code') or '',
                                         item.get('reason')))


def main():
    parser = argparse.ArgumentParser(description='政策批量导入（默认 dry-run）')
    parser.add_argument('--file', default=None, help='导入文件：.xlsx / .csv / .json')
    parser.add_argument('--source', default='', help='批次来源标识')
    parser.add_argument('--dry-run', action='store_true', help='只校验不写库（默认）')
    parser.add_argument('--commit', action='store_true', help='提交批次（写库）')
    parser.add_argument('--batch-id', default=None, help='批次 id（回滚/报告用）')
    parser.add_argument('--rollback', action='store_true', help='回滚指定批次')
    parser.add_argument('--report', action='store_true', help='查看指定批次报告')
    parser.add_argument('--operator', default=None, help='操作人（默认 POLICY_IMPORT_OPERATOR 或 cli）')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    parser.add_argument('--env-file', default=None)
    args = parser.parse_args()

    load_env(args.env_file)
    from policy_core import policy_import

    if not policy_import.enabled():
        print('POLICY_IMPORT 未开启（默认 0）；如需执行请显式设置 POLICY_IMPORT=1。',
              file=sys.stderr)
        return 2

    operator = args.operator or os.environ.get('POLICY_IMPORT_OPERATOR') or 'cli'
    write_actions = [bool(args.commit), bool(args.rollback)]
    if sum(write_actions) > 1:
        print('ERROR: --commit 与 --rollback 不能同时使用', file=sys.stderr)
        return 1
    if (args.rollback or args.report) and not args.batch_id:
        print('ERROR: --rollback/--report 需要 --batch-id', file=sys.stderr)
        return 1
    if not (args.rollback or args.report) and not args.file:
        print('ERROR: 需要 --file（dry-run/commit）或 --batch-id + --report/--rollback',
              file=sys.stderr)
        return 1

    try:
        if args.rollback:
            eng = policy_import.engine()
            with eng.begin() as conn:
                report = policy_import.rollback(conn, args.batch_id, operator)
        elif args.report:
            eng = policy_import.engine()
            with eng.connect() as conn:
                report = policy_import.batch_report(conn, args.batch_id)
        else:
            rows = policy_import.load_rows(args.file)
            file_name = os.path.basename(args.file)
            if args.commit:
                # 提交：staging + 校验 + 提交放在同一事务里；异常自动整体回滚。
                eng = policy_import.engine()
                with eng.begin() as conn:
                    created = policy_import.create_batch(
                        conn, rows, source=args.source, file_name=file_name,
                        operator=operator, dry_run=False)
                    policy_import.validate_batch(conn, created['batch_id'])
                    report = policy_import.commit(conn, created['batch_id'], operator)
            else:
                eng = policy_import.engine()
                with eng.connect() as conn:
                    report = policy_import.preview(conn, rows, source=args.source,
                                                   file_name=file_name)
                report['mode'] = 'dry_run'
    except Exception as exc:  # CLI 边界：打印可读错误，不把 traceback 当正常输出
        print('ERROR: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
        return 1

    print_report(report, as_json=args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
