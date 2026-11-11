#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""向量表多模型共存主键迁移（docs/redesign/30-custom-model-registry.md §4.5）。

现状：向量表（POLICY_VECTOR_TABLE，生产 policy_vector_qwen3）主键只有 id，
一张表只能装一个嵌入模型的全量向量。多模型共存要求同一张表按 embed_model
行级标记区分、每模型一份全量向量（§R2），主键必须是 (id, embed_model)。

本迁移把主键从 (id) 迁到 (id, embed_model)：
  1) 回填 embed_model 为 NULL/'' 的行（现网无此类行，通用兜底为 'legacy'）
  2) embed_model 置 NOT NULL（复合主键列不可空）
  3) 删旧主键 (id)，建复合主键 (id, embed_model)

幂等：已迁完再跑为无变更。默认只读检查（报缺项 exit 1）；--apply 才动表。

用法：
  python deploy/migrate_vector_multi_model.py            # 只读检查
  python deploy/migrate_vector_multi_model.py --json     # 机器可读
  python deploy/migrate_vector_multi_model.py --apply    # 迁移（幂等）
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


def vector_table():
    """当前向量表名：env POLICY_VECTOR_TABLE 优先，缺省回落 policy_vector。"""
    return os.environ.get('POLICY_VECTOR_TABLE', 'policy_vector').strip() or 'policy_vector'


def pk_columns(engine, table):
    """返回表主键列名列表（无主键返回 []）。"""
    from sqlalchemy import create_engine, text
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT a.attname FROM pg_constraint con "
            "JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=ANY(con.conkey) "
            "WHERE con.contype='p' AND con.conrelid='%s'::regclass "
            "ORDER BY array_position(con.conkey, a.attnum)" % table)).fetchall()
    return [r[0] for r in rows]


def embed_null_count(engine, table):
    from sqlalchemy import text
    with engine.connect() as c:
        return c.execute(text(
            "SELECT count(*) FROM \"%s\" WHERE embed_model IS NULL OR embed_model=''" % table)).scalar()


def snapshot(engine, table):
    pk = pk_columns(engine, table)
    nulls = 0
    try:
        nulls = embed_null_count(engine, table)
    except Exception:
        nulls = -1  # 表不存在 / 无 embed_model 列
    return {
        'table': table,
        'pk': pk,
        'pk_is_composite': pk == ['id', 'embed_model'],
        'pk_is_id_only': pk == ['id'],
        'embed_null_rows': nulls,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='执行迁移（幂等）')
    parser.add_argument('--json', action='store_true', help='机器可读输出')
    args = parser.parse_args()
    load_env()

    import config
    from sqlalchemy import create_engine, text
    engine = create_engine(config.DATABASE_URL)
    table = vector_table()
    before = snapshot(engine, table)
    applied = []

    if args.apply:
        with engine.begin() as conn:
            # 1) 回填空 embed_model（现网无此类行；兜底 'legacy'，不覆盖已有值）
            n = embed_null_count(engine, table)
            if n:
                conn.execute(text(
                    "UPDATE \"%s\" SET embed_model='legacy' "
                    "WHERE embed_model IS NULL OR embed_model=''" % table))
                applied.append('backfill embed_model: %d 行 → legacy' % n)
            # 2) embed_model 置 NOT NULL
            conn.execute(text('ALTER TABLE "%s" ALTER COLUMN embed_model SET NOT NULL' % table))
            applied.append('embed_model SET NOT NULL')
            # 3) 主键 (id) → (id, embed_model)；已复合则跳过
            if before['pk_is_id_only']:
                conn.execute(text('ALTER TABLE "%s" DROP CONSTRAINT "%s_pkey"' % (table, table)))
                conn.execute(text(
                    'ALTER TABLE "%s" ADD PRIMARY KEY (id, embed_model)' % table))
                applied.append('PRIMARY KEY (id) → (id, embed_model)')
            elif before['pk_is_composite']:
                applied.append('主键已是 (id, embed_model)，跳过')

    after = snapshot(engine, table)
    ok = after['pk_is_composite'] and after['embed_null_rows'] == 0
    report = {'before': before, 'after': after, 'applied': applied, 'ok': ok}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('向量表多模型主键迁移（%s）  表=%s' % ('--apply' if args.apply else '只读检查', table))
        print('  迁移前主键: %s   空 embed_model 行: %d' % (before['pk'], before['embed_null_rows']))
        print('  迁移后主键: %s   空 embed_model 行: %d' % (after['pk'], after['embed_null_rows']))
        if applied:
            print('  本次动作:')
            for a in applied:
                print('    - ' + a)
        if not ok:
            print('  ✗ 未达目标态（主键应为 (id, embed_model) 且无空 embed_model），--apply 执行迁移')
            return 1
        print('  ✓ 已是多模型共存结构' + ('（本次迁移完成）' if applied and not before['pk_is_composite'] else '（无需变更）'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
