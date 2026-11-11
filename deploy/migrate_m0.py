# -*- coding: utf-8 -*-
"""M0 迁移：建立链路回放索引表 qa_trace（幂等，可重复执行）。

背景：qa_log 表归数据库属主（system）所有，应用账号没有 ALTER 权限；
public schema 对应用账号开放 CREATE，因此 trace 映射独立成表：
    qa_trace(qa_id PK, trace_id, created_at)
应用写入（prepare 时与 qa_log 同事务提交），回放接口按 qa_id 查 trace_id。

用法：
  python deploy/migrate_m0.py --dry-run   # 只检查与打印，不改库
  python deploy/migrate_m0.py             # 实际执行（可重复跑）

生产备注：若 DBA 愿意把 trace_id 并入 qa_log（ALTER TABLE ... ADD COLUMN），
可另行执行；应用代码不依赖该列，两种形态都能工作。
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = 'qa_trace'
INDEX = 'ix_qa_trace_trace_id'
QA_LOG_INDEX = 'ix_qa_log_trace_id'


def load_env(path=None):
    path = Path(path or os.environ.get('SMART_HELP_ENV_FILE') or (ROOT / 'deploy' / 'web.env'))
    if not path.is_file():
        print('WARN: 未找到环境文件 %s，改用现有环境变量' % path)
        return
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())


def _scalar(conn, sql, label, default=None):
    from sqlalchemy import text
    try:
        return conn.execute(text(sql)).scalar()
    except Exception as exc:  # 查询失败必须显式报错，不能假装已迁移
        raise SystemExit('FAIL: 查询 %s 失败: %s' % (label, exc))


def main():
    parser = argparse.ArgumentParser(description='M0: qa_trace 回放索引表迁移')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--env-file', default=None)
    args = parser.parse_args()

    load_env(args.env_file)
    sys.path.insert(0, str(ROOT / 'src'))
    import config  # noqa: F401  Kingbase 版本解析补丁，必须在建 engine 前导入
    from sqlalchemy import create_engine, text

    url = os.environ.get('SMART_HELP_AUTH_DATABASE_URL') or os.environ.get('AUTH_DATABASE_URL')
    if not url:
        print('FAIL: 未找到 SMART_HELP_AUTH_DATABASE_URL（检查 deploy/web.env）')
        return 2

    engine = create_engine(url)
    with engine.connect() as conn:
        print('连接 auth 库: %s@%s' % (
            _scalar(conn, 'SELECT current_user', 'current_user'),
            _scalar(conn, 'SELECT current_database()', 'current_database')))
        has_table = bool(_scalar(
            conn,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='%s'" % TABLE,
            TABLE))
        has_index = bool(_scalar(
            conn,
            "SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
            "AND tablename='%s' AND indexname='%s'" % (TABLE, INDEX),
            INDEX))
        can_create = bool(_scalar(
            conn,
            "SELECT has_schema_privilege(current_user, 'public', 'CREATE')",
            'public CREATE 权限'))
        qa_log_owner = _scalar(
            conn,
            "SELECT tableowner FROM pg_tables WHERE tablename='qa_log'",
            'qa_log 属主', default='')
        has_column = bool(_scalar(
            conn,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='qa_log' AND column_name='trace_id'",
            'qa_log.trace_id'))

    print('%-24s : %s' % (TABLE + ' 表', '已存在' if has_table else '缺失') + ('' if can_create else '  <- 无 public CREATE 权限'))
    print('%-24s : %s' % (INDEX, '已存在' if has_index else '缺失'))
    print('%-24s : %s' % ('qa_log 当前属主', qa_log_owner or '未知'))
    print('%-24s : %s' % ('qa_log.trace_id（可选）', '已存在' if has_column else '缺失（本方案不依赖）'))

    statements = []
    if not has_table:
        statements.append(
            'CREATE TABLE public.%s (qa_id integer PRIMARY KEY, '
            'trace_id varchar(64) NOT NULL, created_at varchar(32))' % TABLE)
    if not has_index:
        statements.append('CREATE INDEX %s ON public.%s (trace_id)' % (INDEX, TABLE))

    if os.environ.get('MIGRATE_QA_LOG_COLUMN') == '1' and not has_column:
        statements.append('ALTER TABLE qa_log ADD COLUMN trace_id varchar(64)')
        statements.append('CREATE INDEX %s ON qa_log (trace_id)' % QA_LOG_INDEX)

    if not statements:
        print('OK: 无需迁移（幂等）')
        return 0
    for sql in statements:
        print(('DRY-RUN ' if args.dry_run else 'EXEC    ') + sql)
    if args.dry_run:
        return 0

    try:
        with engine.begin() as conn:
            for sql in statements:
                conn.execute(text(sql))
    except Exception as exc:
        print('FAIL: 执行迁移失败: %s' % exc)
        print('提示：qa_trace 需要 public schema 的 CREATE 权限；qa_log 加列需要表属主权限。')
        return 1

    with engine.connect() as conn:
        rows = _scalar(conn, 'SELECT count(*) FROM public.%s' % TABLE, TABLE + ' 回读')
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='%s' ORDER BY ordinal_position" % TABLE)).fetchall()
    print('OK: %s 已就绪，列=%s，当前行数=%s' % (TABLE, [c[0] for c in cols], rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
