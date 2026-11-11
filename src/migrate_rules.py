"""把原型 SQLite 规则库镜像到金仓 policy_rule 表。

用法：
  python migrate_rules.py            # 建表 + 全量镜像（幂等）
  python migrate_rules.py --verify   # 只比对两边行数
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from policy_core import rule_store  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
SQLITE = os.path.join(ROOT, 'eval', 'compiled_rules.db')


def sqlite_rows():
    if not os.path.exists(SQLITE):
        return []
    conn = sqlite3.connect(SQLITE)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('SELECT * FROM policy_rule').fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        data = dict(row)
        rule = data.get('rule_json')
        if isinstance(rule, str):
            try:
                import json
                data['rule_json'] = json.loads(rule)
            except ValueError:
                pass
        out.append(dict(
            key=data.get('key'),
            policy_code=data.get('policy_code'),
            detail_id=data.get('detail_id'),
            version=data.get('version'),
            text_hash=data.get('text_hash'),
            county=data.get('county'),
            status=data.get('status'),
            rule=data.get('rule_json'),
            errors=data.get('errors'),
            compiler_model=data.get('compiler_model'),
        ))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verify', action='store_true')
    a = ap.parse_args()
    rule_store.ensure_schema()
    print('金仓表已就绪: policy_rule / policy_rule_job')
    if a.verify:
        print('金仓统计:', rule_store.stats())
        print('SQLite 行数:', len(sqlite_rows()))
        return
    rows = sqlite_rows()
    ok = 0
    for record in rows:
        if isinstance(record.get('errors'), str):
            import json
            try:
                record['errors'] = json.loads(record['errors'])
            except ValueError:
                record['errors'] = [record['errors']]
        try:
            rule_store.save(record)
            ok += 1
        except Exception as exc:  # 单条失败不阻塞整体迁移
            print('  迁移失败 %s: %s' % (record.get('key'), str(exc)[:120]))
    print('镜像完成: %d/%d 条' % (ok, len(rows)))
    print('金仓统计:', rule_store.stats())


if __name__ == '__main__':
    main()
