#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型注册表一次性迁移（docs/redesign/30-custom-model-registry.md §4.1）。

把对话/嵌入/重排模型从 config/model_settings.yaml 的"平台捆绑"提升为数据库一等资源。
默认只读检查（--check 语义）；--apply 才建表，幂等。

覆盖结构：
  models                    模型注册表（chat/embed/rerank 三类）
  uq_models_default         部分唯一索引：每类型至多一个 is_default=TRUE
  audit_log.target_code     既有表列宽 32→64（postgresql 方言幂等 ALTER；
                            sqlite 新环境由 create_all 直接按 64 建表）

用法：
  python deploy/migrate_models.py             # 只读检查
  python deploy/migrate_models.py --json      # 机器可读
  python deploy/migrate_models.py --apply     # 建表（幂等）
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


DDL = (
    """CREATE TABLE IF NOT EXISTS models (
    id            VARCHAR(64) PRIMARY KEY,
    type          VARCHAR(16) NOT NULL,
    name          VARCHAR(100) NOT NULL,
    source        VARCHAR(16) NOT NULL DEFAULT 'remote',
    provider      VARCHAR(32) NOT NULL DEFAULT 'openai',
    base_url      TEXT NOT NULL,
    api_key_enc   TEXT,
    model_name    TEXT NOT NULL,
    dim           INTEGER,
    query_instruction TEXT,
    json_mode     BOOLEAN,
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMP DEFAULT now(),
    updated_at    TIMESTAMP DEFAULT now()
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_models_default ON models(type) WHERE is_default""",
)


def snapshot(engine):
    from sqlalchemy import inspect
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    indexes = set()
    if 'models' in tables:
        for ix in insp.get_indexes('models'):
            indexes.add(ix['name'])
    return {
        'models_table': 'models' in tables,
        'default_unique_index': 'uq_models_default' in indexes,
    }


def _audit_target_code_length(conn):
    """audit_log.target_code 当前列宽（表不存在返回 None）。

    audit_log 由 src/models.py 的 SQLAlchemy 定义经 db.create_all() 建表，
    本脚本 DDL 里没有它的 CREATE TABLE。生产（KingbaseES，postgresql 方言）
    是既有表、列仍 varchar(32)，而 seed 模型 id 最长 43 字符，_audit 写
    target_code=row.id 会截断/溢出，故按方言补一次幂等 ALTER。

    返回：正整数=定长宽度；-1=无界（TEXT，永不截断）；None=表或列缺失。
    """
    from sqlalchemy import inspect
    insp = inspect(conn)
    if 'audit_log' not in insp.get_table_names():
        return None
    for col in insp.get_columns('audit_log'):
        if col['name'] == 'target_code':
            length = getattr(col['type'], 'length', None)
            if length is None:
                return -1
            return int(length)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='建表（幂等）')
    parser.add_argument('--json', action='store_true', help='机器可读输出')
    args = parser.parse_args()
    load_env()

    import config
    from sqlalchemy import create_engine, text
    engine = create_engine(config.DATABASE_URL)
    before = snapshot(engine)
    applied = []
    audit_width_before = None
    if args.apply:
        with engine.begin() as conn:
            for stmt in DDL:
                conn.execute(text(stmt))
            applied = [s.strip().split('\n')[0][:60] for s in DDL]
            # audit_log.target_code 32→64：仅 postgresql 方言（KingbaseES）执行。
            # sqlite 新环境由 create_all 按 models.py 定义（已 64）直接建表，无需 ALTER。
            if conn.dialect.name == 'postgresql':
                audit_width_before = _audit_target_code_length(conn)
                # None = 未能判定列宽（表不存在或 introspect 失败）→ 静默跳过，
                # 报告 audit_target_code.before/after 会显示 None，请人工核对。
                # <64（而非 !=64）：避免把已 >64 的列收窄到 64 导致数据溢出。
                if audit_width_before is not None and 0 <= audit_width_before < 64:
                    conn.execute(text('ALTER TABLE audit_log ALTER COLUMN target_code TYPE VARCHAR(64)'))
                    applied.append('ALTER audit_log.target_code -> VARCHAR(64)')
    after = snapshot(engine)
    audit_width_after = None
    if engine.dialect.name == 'postgresql':
        with engine.connect() as conn:
            audit_width_after = _audit_target_code_length(conn)
    missing = [k for k, v in after.items() if not v]
    # audit_log.target_code 必须够宽：seed id 最长 43，32 宽会写不进审计。
    # 只读检查下宽度靠 introspect，取不到（None）时不能当作通过，要显式报出来。
    audit_narrow = (engine.dialect.name == 'postgresql'
                    and audit_width_after is not None
                    and 0 <= audit_width_after < 64)
    if audit_narrow:
        missing = missing + ['audit_log.target_code']
    report = {
        'before': before, 'after': after, 'applied': applied, 'missing': missing,
        'ok': not missing,
        'audit_target_code': {'before': audit_width_before, 'after': audit_width_after},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('模型注册表迁移（%s）' % ('--apply' if args.apply else '只读检查'))
        for key, label in (('models_table', 'models 表'),
                           ('default_unique_index', 'uq_models_default 唯一索引')):
            mark = '✓' if after[key] else '✗'
            print('  %s %s' % (mark, label))
        if engine.dialect.name == 'postgresql':
            shown = '无界(TEXT)' if audit_width_after == -1 else audit_width_after
            print('  %s audit_log.target_code 宽度（需 ≥64 或无界，实测 %s）'
                  % ('✗' if audit_narrow else '✓', shown))
        if missing:
            print('缺结构：' + '、'.join(missing) + '（--apply 补建）')
            return 1
        print('结构齐全' + ('（本次补建完成）' if applied else '（无需变更）'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
