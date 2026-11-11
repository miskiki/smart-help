# -*- coding: utf-8 -*-
"""政策生命周期 outbox（T18）：业务写操作与向量/规则/图谱/缓存同步解耦。

为什么要有这一层：现有后台在同一个请求里 commit 之后才去同步向量、规则、图谱和缓存，
每一步失败都被 try 吞掉，侧效应与业务表必然脱钩，也无法重放。这里改成
"业务表写入 + outbox 事件" 同一事务，消费者按事件幂等重放。

设计约束：
- 表结构只有任务要求的 8 列（id/event_type/payload/status/attempts/created_at/updated_at/last_error）；
- status 只有 pending/done/dead 三态，失败重试超过上限进 dead，不无限重试；
- 同一 event_type + 同一 payload 重复入队只保留一条（幂等），避免同一业务事件重复触发；
- 只接收显式传入的 SQLAlchemy Connection，事务由调用方控制（API 用 db.session）；
- 不 import Flask / models，便于 sqlite 单测与未来 CLI worker 复用。
"""
import json
import os
from datetime import datetime

from sqlalchemy import inspect, text

# 允许的事件类型（任务 §outbox：向量重建/规则失效/图谱同步/缓存失效）
EVENT_TYPES = (
    'policy.vector_rebuild',
    'policy.rule_invalidate',
    'policy.kg_sync',
    'policy.cache_invalidate',
)

STATUS_PENDING = 'pending'
STATUS_DONE = 'done'
STATUS_DEAD = 'dead'

# 默认重试上限；可通过环境变量 OUTBOX_MAX_ATTEMPTS 覆盖（worker 侧调参，不影响 API）
DEFAULT_MAX_ATTEMPTS = 3

_MAX_ERROR_CHARS = 500


def max_attempts():
    """失败重试上限：OUTBOX_MAX_ATTEMPTS（默认 3），非法值回退默认。"""
    raw = str(os.environ.get('OUTBOX_MAX_ATTEMPTS', '') or '').strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS
    return value if value >= 1 else DEFAULT_MAX_ATTEMPTS


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _json(payload):
    return json.dumps(payload if payload is not None else {}, ensure_ascii=False,
                      sort_keys=True, default=str)


def _loads(raw):
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {'value': value}


def _ddl(dialect):
    """按方言生成建表语句：PG 用 bigserial，其余（sqlite）用 AUTOINCREMENT。"""
    if dialect == 'postgresql':
        id_column = 'bigserial PRIMARY KEY'
    else:
        id_column = 'INTEGER PRIMARY KEY AUTOINCREMENT'
    return [
        '''CREATE TABLE IF NOT EXISTS outbox (
            id %s,
            event_type varchar(64) NOT NULL,
            payload text NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'pending',
            attempts integer NOT NULL DEFAULT 0,
            created_at varchar(32),
            updated_at varchar(32),
            last_error text
        )''' % id_column,
        'CREATE INDEX IF NOT EXISTS outbox_status_idx ON outbox(status, id)',
    ]


def ensure_schema(conn):
    """建表（幂等）。只建 outbox 自己的表，不动业务表。"""
    for ddl in _ddl(conn.dialect.name):
        conn.execute(text(ddl))
    return True


def enqueue(conn, event_type, payload):
    """入队一条事件，返回事件 id。

    幂等：同 event_type + 同 payload（canonical JSON）已存在则直接返回旧 id，
    不重复插入；已 done/dead 的事件也不重复创建（重放由 worker 显式处理）。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError('unknown outbox event_type: %r' % (event_type,))
    canonical = _json(payload)
    existing = conn.execute(
        text('SELECT id FROM outbox WHERE event_type=:t AND payload=:p ORDER BY id LIMIT 1'),
        {'t': event_type, 'p': canonical}).scalar()
    if existing is not None:
        return int(existing)
    now = _now()
    conn.execute(
        text('INSERT INTO outbox (event_type, payload, status, attempts, created_at, updated_at) '
             "VALUES (:t, :p, 'pending', 0, :c, :u)"),
        {'t': event_type, 'p': canonical, 'c': now, 'u': now})
    new_id = conn.execute(
        text('SELECT id FROM outbox WHERE event_type=:t AND payload=:p ORDER BY id DESC LIMIT 1'),
        {'t': event_type, 'p': canonical}).scalar()
    return int(new_id)


def claim(conn, limit=10):
    """取一批 pending 事件（按 id 升序），返回 [{id,event_type,payload,attempts}]。

    只读语义：不改状态。drain 在同一个事务里按顺序处理；
    PostgreSQL 下追加 FOR UPDATE SKIP LOCKED，避免多 worker 重复领取（sqlite 忽略）。
    """
    try:
        size = max(1, int(limit))
    except (TypeError, ValueError):
        size = 10
    sql = ("SELECT id, event_type, payload, attempts FROM outbox "
           "WHERE status='pending' ORDER BY id LIMIT :limit")
    if conn.dialect.name == 'postgresql':
        sql += ' FOR UPDATE SKIP LOCKED'
    rows = conn.execute(text(sql), {'limit': size}).mappings().all()
    events = []
    for row in rows:
        item = dict(row)
        item['payload'] = _loads(item.get('payload'))
        item['attempts'] = int(item.get('attempts') or 0)
        events.append(item)
    return events


def finish(conn, event_id):
    """标记事件完成；已不是 pending 的行不动，返回是否变更。"""
    result = conn.execute(
        text("UPDATE outbox SET status='done', updated_at=:u, last_error=NULL "
             "WHERE id=:id AND status='pending'"),
        {'u': _now(), 'id': event_id})
    return bool(result.rowcount)


def fail(conn, event_id, error='', attempts_limit=None):
    """标记一次失败：attempts+1，未到上限回 pending，达到上限进 dead。

    返回失败后的状态（pending/done/dead）；事件不存在返回 None。
    已 done 的事件不会被失败写回，避免消费完成后被旧异常覆盖。
    """
    row = conn.execute(
        text('SELECT status, attempts FROM outbox WHERE id=:id'),
        {'id': event_id}).mappings().first()
    if row is None:
        return None
    if row['status'] == STATUS_DONE:
        return STATUS_DONE
    limit = int(attempts_limit or max_attempts())
    if limit < 1:
        limit = 1
    attempts = int(row['attempts'] or 0) + 1
    status = STATUS_DEAD if attempts >= limit else STATUS_PENDING
    message = str(error or '')[: _MAX_ERROR_CHARS] or None
    conn.execute(
        text('UPDATE outbox SET status=:s, attempts=:a, last_error=:e, updated_at=:u WHERE id=:id'),
        {'s': status, 'a': attempts, 'e': message, 'u': _now(), 'id': event_id})
    return status


def drain(conn, handlers, limit=10, attempts_limit=None):
    """消费一批 pending 事件，返回统计 {processed,done,retried,dead,skipped}。

    handlers: {event_type: callable(payload)}；handler 抛任何异常都记失败（最多 attempts_limit 次后进 dead）。
    没有 handler 的事件直接进 dead（属于部署配置错误，重试没有意义）。
    消费完成的事件置 done，重复 drain 不会重复执行（幂等）。
    """
    handlers = handlers or {}
    summary = {'processed': 0, 'done': 0, 'retried': 0, 'dead': 0, 'skipped': 0}
    for event in claim(conn, limit=limit):
        summary['processed'] += 1
        event_type = event['event_type']
        handler = handlers.get(event_type)
        if handler is None:
            fail(conn, event['id'], 'no_handler_for:%s' % event_type, attempts_limit=1)
            summary['skipped'] += 1
            summary['dead'] += 1
            continue
        try:
            handler(event.get('payload') or {})
        except Exception as exc:  # noqa: BLE001 - 消费者失败必须回落为事件重试，不能打断整批
            status = fail(conn, event['id'], '%s: %s' % (type(exc).__name__, exc),
                          attempts_limit=attempts_limit)
            if status == STATUS_DEAD:
                summary['dead'] += 1
            else:
                summary['retried'] += 1
        else:
            finish(conn, event['id'])
            summary['done'] += 1
    return summary


def counts(conn):
    """各状态事件数，供健康检查/测试。表不存在时返回空字典。"""
    try:
        if not inspect(conn).has_table('outbox'):
            return {}
    except Exception:  # noqa: BLE001 - 健康检查接口不应因方言/权限异常 500
        return {}
    rows = conn.execute(text('SELECT status, count(*) FROM outbox GROUP BY status')).all()
    return {row[0]: int(row[1]) for row in rows}
