"""规则库存储层：金仓（KingbaseES）后端。

为什么要有这一层：C1/C2 阶段规则存在代码目录的 SQLite 文件里（424KB），
Web 服务多进程读不到、没有备份、政策改版后无法与 policy 表联合比对。
这里把规则搬进金仓的 policy_rule / policy_rule_job 两张新表，
原有 69 张表一概不动。

对外接口：
  ensure_schema()            建表（幂等）
  save(record)               写一条规则（存在则覆盖）
  load_rules(county)         取某县可求值规则
  all_hashes()               取全部 (text_hash, region_scope)，供编译复用
  stats()                    规则库统计
  enqueue(region) / claim() / finish()   懒编译任务队列
"""
import json
import os

from sqlalchemy import bindparam, create_engine, inspect, text

try:  # 金仓的 version 串不被 SQLAlchemy 识别，config 在导入时打了 PGDialect 补丁
    import config  # noqa: F401
except Exception:  # 独立脚本里没有 config 时退回默认方言
    pass

_ENGINE = None


def database_url():
    """只从环境变量读取连接串，不在源码里保存任何默认账号/口令。

    规则库依赖 Kingbase；部署时由 deploy/web.env 的 ADMIN_DB_URL 提供（模板见
    deploy/web.env.example）。缺失时给出可操作的报错，而不是回退到硬编码凭据。
    """
    url = os.environ.get('ADMIN_DB_URL')
    if not url:
        raise RuntimeError(
            'ADMIN_DB_URL 未配置：规则库需要 Kingbase 连接串，'
            '请先 `set -a && . deploy/web.env && set +a` 或显式 export ADMIN_DB_URL')
    return url


def engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(database_url(), pool_pre_ping=True, pool_size=4, max_overflow=2)
    return _ENGINE


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS policy_rule (
        rule_key      varchar(160) PRIMARY KEY,
        policy_code   varchar(64)  NOT NULL,
        detail_id     varchar(64),
        version       varchar(64),
        text_hash     varchar(96)  NOT NULL,
        region_scope  varchar(64),
        rule_json     text         NOT NULL,
        status        varchar(16)  NOT NULL,
        missing_fields text,
        errors        text,
        compiler_model varchar(96),
        compiler_rev  varchar(16),
        compiled_at   timestamp DEFAULT current_timestamp
    )""",
    "CREATE INDEX IF NOT EXISTS policy_rule_scope_idx ON policy_rule(region_scope, status)",
    "CREATE INDEX IF NOT EXISTS policy_rule_code_idx ON policy_rule(policy_code)",
    "CREATE INDEX IF NOT EXISTS policy_rule_hash_idx ON policy_rule(text_hash)",
    """CREATE TABLE IF NOT EXISTS policy_rule_job (
        region      varchar(64) PRIMARY KEY,
        state       varchar(16) NOT NULL DEFAULT 'pending',
        total       int DEFAULT 0,
        done        int DEFAULT 0,
        failed      int DEFAULT 0,
        attempts    int DEFAULT 0,
        message     varchar(600),
        created_at  timestamp DEFAULT current_timestamp,
        started_at  timestamp,
        finished_at timestamp
    )""",
]

COLUMNS = ('rule_key', 'policy_code', 'detail_id', 'version', 'text_hash', 'region_scope',
           'rule_json', 'status', 'missing_fields', 'errors', 'compiler_model', 'compiler_rev')


def ensure_schema():
    with engine().begin() as conn:
        for ddl in SCHEMA:
            conn.execute(text(ddl))


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str) if value is not None else None


def save(record):
    """写一条规则。record 沿用编译器的字段名（key/county/rule/errors）。"""
    row = dict(
        rule_key=record.get('key') or '%s:%s' % (record['policy_code'], record.get('detail_id') or 'main'),
        policy_code=record['policy_code'],
        detail_id=record.get('detail_id') or 'main',
        version=record.get('version') or '',
        text_hash=record.get('text_hash') or '',
        region_scope=record.get('county') or record.get('region_scope') or '*',
        rule_json=_json(record.get('rule')) or '{}',
        status=record.get('status') or 'failed',
        missing_fields=_json(record.get('missing_fields')),
        errors=_json(record.get('errors') or []),
        compiler_model=record.get('compiler_model') or '',
        compiler_rev=record.get('compiler_rev') or '',
    )
    sql = ('INSERT INTO policy_rule (%s) VALUES (%s) '
           'ON CONFLICT (rule_key) DO UPDATE SET policy_code=EXCLUDED.policy_code,'
           'text_hash=EXCLUDED.text_hash,region_scope=EXCLUDED.region_scope,'
           'rule_json=EXCLUDED.rule_json,status=EXCLUDED.status,'
           'missing_fields=EXCLUDED.missing_fields,errors=EXCLUDED.errors,'
           'compiler_model=EXCLUDED.compiler_model,compiler_rev=EXCLUDED.compiler_rev,'
           'compiled_at=current_timestamp') % (
               ','.join(COLUMNS), ','.join(':' + c for c in COLUMNS))
    with engine().begin() as conn:
        conn.execute(text(sql), row)


def _row_dict(row):
    data = dict(zip(COLUMNS, row))
    try:
        data['rule'] = json.loads(data.pop('rule_json') or '{}')
    except (TypeError, ValueError):
        data['rule'] = {}
    for key in ('errors', 'missing_fields'):
        raw = data.get(key)
        try:
            data[key] = json.loads(raw) if raw else ([] if key == 'errors' else None)
        except (TypeError, ValueError):
            pass
    return data


# ---------------- 读侧政策状态过滤（T21） ----------------
# 为什么过滤不在本模块 load_rules 内做：编译 worker 的 check_stale/all_hashes 需要看到全部
# 已编译规则；若这里过滤，停用政策恢复后会被误判成"无规则"而重复编译。因此这里只提供状态
# 口径与查询助手，由出卡读取路径（rule_eval.load_rules）决定是否过滤。

# 现网 policy.status 用中文"在用"；兼容生命周期/历史数据里的英文同义值。
ACTIVE_POLICY_STATUSES = ('在用', 'active', '启用')


def status_filter_enabled():
    """T21 读侧状态过滤开关：POLICY_STATUS_FILTER=0 回旧行为（默认开）。"""
    return os.environ.get('POLICY_STATUS_FILTER', '1') != '0'


def is_active_policy_status(status):
    return str(status or '').strip() in ACTIVE_POLICY_STATUSES


def policy_status_map(codes):
    """按政策编号查 policy.status；policy 表/status 列不存在或查询失败返回 None。

    返回 None 表示"无法确认状态"，调用方应跳过过滤保持旧行为：sqlite 离线编译回落路径
    没有 policy 表，若强行过滤会把规则路径整体打空。
    """
    codes = [c for c in (codes or ()) if c]
    if not codes:
        return {}
    try:
        with engine().connect() as conn:
            inspector = inspect(conn)
            if not inspector.has_table('policy'):
                return None
            columns = {column['name'] for column in inspector.get_columns('policy')}
            if 'status' not in columns:
                return None
            stmt = text('SELECT policy_code, status FROM policy WHERE policy_code IN :codes')\
                .bindparams(bindparam('codes', expanding=True))
            rows = conn.execute(stmt, dict(codes=list(codes))).mappings().fetchall()
        return {row['policy_code']: row['status'] for row in rows}
    except Exception:
        return None


def load_rules(county):
    """某县可求值规则：region_scope 等于该县，或 '*'（省级/全国）。"""
    sql = ('SELECT %s FROM policy_rule WHERE status <> \'failed\' '
           'AND (region_scope = :county OR region_scope = \'*\')') % ','.join(COLUMNS)
    with engine().connect() as conn:
        rows = conn.execute(text(sql), dict(county=county)).fetchall()
    return [_row_dict(r) for r in rows]


def count_rules(county):
    sql = ('SELECT count(*) FROM policy_rule WHERE status <> \'failed\' '
           'AND (region_scope = :county OR region_scope = \'*\')')
    with engine().connect() as conn:
        return int(conn.execute(text(sql), dict(county=county)).scalar() or 0)


def all_hashes():
    with engine().connect() as conn:
        rows = conn.execute(text('SELECT text_hash, region_scope FROM policy_rule WHERE status <> \'failed\'')).fetchall()
    return {r[0]: r[1] for r in rows}


def stats():
    with engine().connect() as conn:
        by_status = dict(conn.execute(text('SELECT status, count(*) FROM policy_rule GROUP BY status')).fetchall())
        by_region = dict(conn.execute(
            text('SELECT region_scope, count(*) FROM policy_rule WHERE status <> \'failed\' GROUP BY region_scope')).fetchall())
        total = int(conn.execute(text('SELECT count(*) FROM policy_rule')).scalar() or 0)
    return dict(total=total, by_status=by_status, by_region=by_region)


# ---------------- 懒编译任务队列 ----------------

def enqueue(region, message='', force=False):
    """登记一个待编译区域。已完成的区域除非 force，否则不重复登记。"""
    with engine().begin() as conn:
        current = conn.execute(text('SELECT state, attempts FROM policy_rule_job WHERE region=:r'),
                               dict(r=region)).fetchone()
        if current is None:
            conn.execute(text('INSERT INTO policy_rule_job (region,state,message) VALUES (:r,\'pending\',:m)'),
                         dict(r=region, m=message[:600]))
            return 'pending'
        state, attempts = current[0], int(current[1] or 0)
        if state == 'pending':
            return 'pending'
        if state == 'running':
            return 'running'
        if force or state in ('failed', 'stale'):
            conn.execute(text('UPDATE policy_rule_job SET state=\'pending\', message=:m, '
                              'attempts=:a, finished_at=NULL WHERE region=:r'),
                         dict(r=region, m=message[:600], a=attempts + 1))
            return 'pending'
        return state


def claim():
    """原子认领一个 pending 任务，返回 region 或 None。"""
    with engine().begin() as conn:
        row = conn.execute(text(
            'SELECT region FROM policy_rule_job WHERE state=\'pending\' '
            'ORDER BY created_at LIMIT 1')).fetchone()
        if not row:
            return None
        region = row[0]
        changed = conn.execute(text(
            'UPDATE policy_rule_job SET state=\'running\', started_at=current_timestamp, '
            'attempts=attempts+1 WHERE region=:r AND state=\'pending\''), dict(r=region)).rowcount
        return region if changed else None


def finish(region, state, total=0, done=0, failed=0, message=''):
    with engine().begin() as conn:
        conn.execute(text(
            'UPDATE policy_rule_job SET state=:s, total=:t, done=:d, failed=:f, message=:m, '
            'finished_at=current_timestamp WHERE region=:r'),
            dict(r=region, s=state, t=total, d=done, f=failed, m=str(message)[:600]))


def job(region):
    with engine().connect() as conn:
        row = conn.execute(text('SELECT region,state,total,done,failed,attempts,message,'
                                'created_at,started_at,finished_at FROM policy_rule_job WHERE region=:r'),
                           dict(r=region)).fetchone()
    if not row:
        return None
    keys = ('region', 'state', 'total', 'done', 'failed', 'attempts', 'message',
            'created_at', 'started_at', 'finished_at')
    return dict(zip(keys, row))


def jobs():
    with engine().connect() as conn:
        rows = conn.execute(text('SELECT region,state,total,done,failed,attempts,message,'
                                 'created_at,started_at,finished_at FROM policy_rule_job '
                                 'ORDER BY created_at')).fetchall()
    keys = ('region', 'state', 'total', 'done', 'failed', 'attempts', 'message',
            'created_at', 'started_at', 'finished_at')
    return [dict(zip(keys, r)) for r in rows]
