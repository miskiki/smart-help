# -*- coding: utf-8 -*-
"""政策生命周期服务（T18）：状态机 + 版本发布/回滚 + 影响预检。

为什么要有这一层（见 docs/redesign/20-national-policy-lifecycle-plan.md §4）：
现有后台只有"停用/更新/新增"三个分支，没有归档、软删、恢复，也没有发布/回滚；
所有侧效应（向量、规则、图谱、缓存）都在业务 commit 之后各自 try，失败即脱钩。
本模块把状态与版本变更收拢为：事务内写业务表 + audit_log + outbox 事件，
消费者幂等重放；同时提供 impact 预检，删除/停用前先看会影响多少问答与索引。

设计约束：
- 开关 POLICY_LIFECYCLE 默认 0（关闭=与现网完全一致），API 关闭时返回 404 disabled；
- 只接收显式传入的 SQLAlchemy Connection，事务由调用方控制（API 用 db.session）；
- 不 import Flask / models，便于 sqlite 单测和未来 CLI（policy_autopilot）复用；
- 不删除、不放宽既有判定口径；本模块只做管理侧状态与版本，不参与问答匹配判定；
- FP=0：开关关闭时链路零改动；所有函数不触碰检索/规则/向量表内容，只写 outbox 事件。
"""
import json
import os
import re
from datetime import datetime

from sqlalchemy import inspect, text

from . import outbox

# 生命周期状态（对外的规范状态名）
STATES = ('draft', 'active', 'suspended', 'archived', 'deleted')

# 允许的状态流转：draft 只能由 publish 激活；deleted 只能 restore 回 active。
TRANSITIONS = {
    'draft': ('active', 'deleted'),
    'active': ('suspended', 'archived', 'deleted'),
    'suspended': ('active', 'archived', 'deleted'),
    'archived': ('active', 'deleted'),
    'deleted': ('active',),
}

# 读取旧数据时的状态兼容：库里历史值仍是 在用/停用，新状态也接受。
STATUS_ALIASES = {
    '在用': 'active', 'active': 'active', '启用': 'active',
    '停用': 'suspended', 'suspended': 'suspended',
    'draft': 'draft', '草稿': 'draft',
    'archived': 'archived', '归档': 'archived',
    'deleted': 'deleted', '已删除': 'deleted',
}

# 写回库里的状态值：active/suspended 沿用现网中文值，确保既有链路
# （检索、台账、状态筛选均按 status='在用' 过滤）在恢复后真正重新参与检索；
# deleted 按任务/设计文档要求写英文，archived/draft 为新状态。
_WRITE_STATUS = {
    'draft': 'draft',
    'active': '在用',
    'suspended': '停用',
    'archived': 'archived',
    'deleted': 'deleted',
}

_ACTION_CN = {
    'suspend': '暂停政策',
    'archive': '归档政策',
    'restore': '恢复政策',
    'delete': '删除政策',
    'hard_delete': '硬删除政策',
    'publish': '发布版本',
    'rollback': '回滚版本',
}

_MAX_REASON_BYTES = 400


class LifecycleError(Exception):
    """生命周期可预期错误（404 不存在 / 409 状态冲突 / 400 参数错误）。"""

    def __init__(self, message, status=400, code='invalid_request'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def enabled():
    """功能开关 POLICY_LIFECYCLE，默认 0（关）。每次调用实时读 env，便于测试与回滚。"""
    raw = str(os.environ.get('POLICY_LIFECYCLE', '0') or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def state_of(raw_status):
    """库里 status 文本 → 规范状态；未知值返回 'unknown'（不猜测）。"""
    value = str(raw_status or '').strip()
    if value in STATUS_ALIASES:
        return STATUS_ALIASES[value]
    return STATUS_ALIASES.get(value.lower(), 'unknown')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------- schema / 通用查询 ----------------

def _table_columns(conn, table):
    """{列名集合}；表不存在返回 None。inspect 在 PG/sqlite 都可用。"""
    try:
        inspector = inspect(conn)
        if not inspector.has_table(table):
            return None
        return {column['name'] for column in inspector.get_columns(table)}
    except Exception:  # noqa: BLE001 - 探测失败按"表不可用"处理，不阻断只读接口
        return None


def _count_optional(conn, table, sql, params):
    """表不存在返回 None，存在则计数；用于 impact 的可选级联表。"""
    if _table_columns(conn, table) is None:
        return None
    try:
        return int(conn.execute(text(sql), params).scalar() or 0)
    except Exception:  # noqa: BLE001 - 级联表结构差异不应让预检 500
        return None


def _vector_count(conn, policy_code):
    """向量表可按存在则计数：优先新表 policy_vector_qwen3，其次旧表 policy_vector。"""
    for table in ('policy_vector_qwen3', 'policy_vector'):
        columns = _table_columns(conn, table)
        if not columns or 'policy_code' not in columns:
            continue
        try:
            count = int(conn.execute(
                text('SELECT count(*) FROM %s WHERE policy_code=:code' % table),
                {'code': policy_code}).scalar() or 0)
            return count, table
        except Exception:  # noqa: BLE001
            return None, table
    return None, None


def _qa_count(conn, policy_code, auth_conn=None):
    """历史问答引用数：qa_log 在生产属 auth 库，优先用传入的 auth_conn，其次同库。"""
    pattern = '%' + str(policy_code or '') + '%'
    for candidate in (auth_conn, conn):
        if candidate is None:
            continue
        columns = _table_columns(candidate, 'qa_log')
        if not columns:
            continue
        clauses = []
        if 'matched_policies' in columns:
            clauses.append('matched_policies LIKE :pattern')
        if 'match_json' in columns:
            clauses.append('match_json LIKE :pattern')
        if not clauses:
            return None
        try:
            return int(candidate.execute(
                text('SELECT count(*) FROM qa_log WHERE ' + ' OR '.join(clauses)),
                {'pattern': pattern}).scalar() or 0)
        except Exception:  # noqa: BLE001 - 权限/结构异常都按不可确认处理（保守拒绝硬删）
            continue
    return None


def ensure_schema(conn):
    """幂等建/补生命周期需要的结构：outbox 表 + policy.deleted_at 列。

    为什么用 ALTER：生产 policy 表没有 deleted_at；PG 支持 ADD COLUMN IF NOT EXISTS，
    sqlite 不支持 IF NOT EXISTS 但这里只在"列不存在"时才执行，单测同样安全。
    注意：只补列，不改任何既有列类型，不建 audit_log（按"表存在则写"约定）。
    """
    created = outbox.ensure_schema(conn)
    columns = _table_columns(conn, 'policy')
    if columns and 'deleted_at' not in columns:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('ALTER TABLE policy ADD COLUMN IF NOT EXISTS deleted_at varchar(32)'))
        else:
            conn.execute(text('ALTER TABLE policy ADD COLUMN deleted_at varchar(32)'))
    return bool(created)


def _policy(conn, code, for_update=False):
    """按 policy_code 读主表；只选取已知存在的列，避免历史库列差异。"""
    policy_code = str(code or '').strip()
    columns = _table_columns(conn, 'policy')
    if columns is None:
        raise LifecycleError('policy 表不存在', 500, 'policy_table_missing')
    wanted = ['policy_id', 'policy_code', 'policy_name', 'status', 'cur_version']
    if 'deleted_at' in columns:
        wanted.append('deleted_at')
    sql = 'SELECT %s FROM policy WHERE policy_code=:code' % ', '.join(wanted)
    if for_update and conn.dialect.name == 'postgresql':
        sql += ' FOR UPDATE'
    row = conn.execute(text(sql), {'code': policy_code}).mappings().first()
    if row is None:
        raise LifecycleError('政策不存在：%s' % policy_code, 404, 'policy_not_found')
    policy = dict(row)
    policy['state'] = state_of(policy.get('status'))
    return policy


# ---------------- audit_log / outbox ----------------

def _audit(conn, action, policy, detail, operator):
    """写审计日志；audit_log 表不存在时静默跳过（设计文档要求"表存在则写"）。"""
    columns = _table_columns(conn, 'audit_log')
    if not columns:
        return False
    fields = {'action': action, 'target_code': policy.get('policy_code') or '',
              'target_name': policy.get('policy_name') or ''}
    if 'detail' in columns:
        fields['detail'] = json.dumps(detail, ensure_ascii=False, default=str)[:2000]
    if 'operator' in columns:
        fields['operator'] = str(operator or '')[:64]
    if 'created_at' in columns:
        fields['created_at'] = _now()
    names = ', '.join(fields)
    placeholders = ', '.join(':' + key for key in fields)
    conn.execute(text('INSERT INTO audit_log (%s) VALUES (%s)' % (names, placeholders)), fields)
    return True


def _enqueue_cascade(conn, policy_code, action, reason, operator, version=''):
    """状态/版本变化后投递 4 类侧效应事件；payload 只含业务标识与操作人，不含家庭隐私。"""
    base = {
        'policy_code': str(policy_code or ''),
        'action': action,
        'reason': str(reason or '')[: _MAX_REASON_BYTES],
        'operator': str(operator or '')[:64],
        'at': _now(),
    }
    if version:
        base['version'] = str(version)
    for event_type in outbox.EVENT_TYPES:
        outbox.enqueue(conn, event_type, dict(base))
    return len(outbox.EVENT_TYPES)


# ---------------- 参数校验 / 结果 ----------------

def _validate_actor(reason, operator, require_reason=True):
    if not str(operator or '').strip():
        raise LifecycleError('缺少操作人', 400, 'missing_operator')
    text_reason = str(reason or '').strip()
    if require_reason and len(text_reason) < 4:
        raise LifecycleError('原因必填且不少于 4 个字符', 400, 'invalid_reason')
    return text_reason


def _result(policy, action, state_before, state_after, dry_run, impact, audit=False,
            events=0, extra=None):
    result = {
        'ok': True,
        'policy_code': policy.get('policy_code'),
        'policy_name': policy.get('policy_name'),
        'action': action,
        'state_before': state_before,
        'state_after': state_after,
        'status_before': policy.get('status'),
        'status_after': _WRITE_STATUS.get(state_after),
        'cur_version': policy.get('cur_version'),
        'dry_run': bool(dry_run),
        'audit_logged': bool(audit),
        'outbox_events': int(events),
        'allowed_next_states': list(TRANSITIONS.get(state_before, ())),
        'impact': impact,
    }
    if extra:
        result.update(extra)
    return result


# ---------------- 只读：影响预检 / 版本链 ----------------

def impact(conn, code, auth_conn=None):
    """受影响数量预检（只读）：问答引用 / 明细 / scope 行 / 规则 / 向量 / 版本。"""
    policy = _policy(conn, code)
    code_value = policy.get('policy_code')
    result = {
        'policy_code': code_value,
        'policy_name': policy.get('policy_name'),
        'state': policy.get('state'),
        'status': policy.get('status'),
        'cur_version': policy.get('cur_version'),
    }
    result['details'] = _count_optional(
        conn, 'policy_detail', 'SELECT count(*) FROM policy_detail WHERE policy_id=:id',
        {'id': policy.get('policy_id')})
    result['scope_rows'] = _count_optional(
        conn, 'policy_scope', 'SELECT count(*) FROM policy_scope WHERE policy_code=:code',
        {'code': code_value})
    result['rules'] = _count_optional(
        conn, 'policy_rule',
        "SELECT count(*) FROM policy_rule WHERE policy_code=:code AND status <> 'failed'",
        {'code': code_value})
    result['versions'] = _count_optional(
        conn, 'policy_version', 'SELECT count(*) FROM policy_version WHERE policy_id=:id',
        {'id': policy.get('policy_id')})
    vectors, vector_table = _vector_count(conn, code_value)
    result['vectors'] = vectors
    result['vector_table'] = vector_table
    qa_log = _qa_count(conn, code_value, auth_conn=auth_conn)
    result['qa_log'] = qa_log
    result['qa_log_available'] = qa_log is not None
    return result


def _version_sort_key(value):
    raw = str(value or '')
    match = re.match(r'^v?(\d+)$', raw)
    return (int(match.group(1)) if match else -1, raw)


def versions(conn, code):
    """版本链（只读），当前版本置 is_current=True。"""
    policy = _policy(conn, code)
    columns = _table_columns(conn, 'policy_version')
    if columns is None or 'version_no' not in columns:
        return []
    wanted = ['version_id', 'version_no', 'note', 'updated_by', 'updated_at',
              'effect_date', 'expire_date']
    selected = [name for name in wanted if name in columns]
    rows = conn.execute(
        text('SELECT %s FROM policy_version WHERE policy_id=:id' % ', '.join(selected)),
        {'id': policy.get('policy_id')}).mappings().all()
    current = str(policy.get('cur_version') or '')
    items = [dict(row) for row in rows]
    for item in items:
        item['is_current'] = str(item.get('version_no') or '') == current
    items.sort(key=lambda item: _version_sort_key(item.get('version_no')), reverse=True)
    return items


# ---------------- 状态流转 ----------------

def _transition(conn, code, target, action, reason, operator, dry_run=False, extra=None):
    """通用状态流转：校验 → 预检 → 写 status/audit/outbox（dry_run 只返回计划）。"""
    _validate_actor(reason, operator)
    ensure_schema(conn)
    policy = _policy(conn, code, for_update=not dry_run)
    state_before = policy.get('state')
    if target not in TRANSITIONS.get(state_before, ()):
        raise LifecycleError('状态流转不允许：%s → %s' % (state_before, target),
                             409, 'invalid_transition')
    snapshot = impact(conn, code)
    if dry_run:
        return _result(policy, action, state_before, target, True, snapshot, extra=extra)

    now = _now()
    sets = ['status=:status', 'updated_at=:now', 'updated_by=:operator']
    params = {'status': _WRITE_STATUS[target], 'now': now,
              'operator': str(operator)[:64], 'id': policy.get('policy_id')}
    policy_columns = _table_columns(conn, 'policy') or set()
    if 'deleted_at' in policy_columns:
        if target == 'active':
            sets.append('deleted_at=NULL')  # 恢复：清空软删时间，卡片/台账恢复正常
        elif target == 'deleted':
            sets.append('deleted_at=:deleted_at')  # 软删：保留数据，仅标记删除时间
            params['deleted_at'] = now
    conn.execute(text('UPDATE policy SET %s WHERE policy_id=:id' % ', '.join(sets)), params)

    audit = _audit(conn, _ACTION_CN[action], policy,
                   {'state_before': state_before, 'state_after': target, 'reason': reason},
                   operator)
    events = _enqueue_cascade(conn, policy.get('policy_code'), action, reason, operator,
                              version=(extra or {}).get('version') or '')
    return _result(policy, action, state_before, target, False, snapshot,
                   audit=audit, events=events, extra=extra)


def suspend(conn, code, reason, operator, dry_run=False):
    """停用：active → suspended（检索排除，数据/scope 保留）。"""
    return _transition(conn, code, 'suspended', 'suspend', reason, operator, dry_run=dry_run)


def archive(conn, code, reason, operator, dry_run=False):
    """归档：active/suspended → archived（管理列表默认隐藏、检索排除）。"""
    return _transition(conn, code, 'archived', 'archive', reason, operator, dry_run=dry_run)


def restore(conn, code, reason, operator, dry_run=False, confirm=False):
    """恢复：suspended/archived/deleted → active；恢复已删除政策需 confirm 二次确认。"""
    _validate_actor(reason, operator)
    ensure_schema(conn)
    policy = _policy(conn, code)
    state = policy.get('state')
    if state not in ('suspended', 'archived', 'deleted'):
        raise LifecycleError('当前状态不可恢复：%s' % state, 409, 'invalid_transition')
    if state == 'deleted' and not confirm:
        raise LifecycleError('恢复已删除政策需要二次确认（confirm=true）', 409,
                             'confirmation_required')
    return _transition(conn, code, 'active', 'restore', reason, operator, dry_run=dry_run)


def delete_policy(conn, code, reason, operator, dry_run=False, hard=False, auth_conn=None):
    """删除：默认软删（status=deleted + deleted_at，scope 保留、可恢复）。

    硬删只允许"从未发布（无版本记录）且无问答引用"的草稿；任何一项无法确认都拒绝，
    宁可留数据也不误删（与 FP=0 同一保守原则）。
    """
    _validate_actor(reason, operator)
    ensure_schema(conn)
    if not hard:
        return _transition(conn, code, 'deleted', 'delete', reason, operator, dry_run=dry_run)

    policy = _policy(conn, code, for_update=not dry_run)
    if policy.get('state') != 'draft':
        raise LifecycleError('硬删除只允许从未发布的草稿', 409, 'hard_delete_requires_draft')
    snapshot = impact(conn, code, auth_conn=auth_conn)
    if (snapshot.get('versions') or 0) > 0:
        raise LifecycleError('草稿已有版本记录，不能硬删除', 409, 'hard_delete_has_versions')
    if snapshot.get('qa_log') is None:
        raise LifecycleError('无法确认问答引用，拒绝硬删除', 409, 'hard_delete_reference_unknown')
    if snapshot.get('qa_log') > 0:
        raise LifecycleError('草稿已被问答引用，不能硬删除', 409, 'hard_delete_has_qa_references')
    details = snapshot.get('details') or 0
    if dry_run:
        result = _result(policy, 'hard_delete', policy.get('state'), 'deleted', True, snapshot)
        result['would_delete_details'] = details
        return result

    if _table_columns(conn, 'policy_detail'):
        conn.execute(text('DELETE FROM policy_detail WHERE policy_id=:id'),
                     {'id': policy.get('policy_id')})
    if _table_columns(conn, 'policy_version'):
        conn.execute(text('DELETE FROM policy_version WHERE policy_id=:id'),
                     {'id': policy.get('policy_id')})
    conn.execute(text('DELETE FROM policy WHERE policy_id=:id'), {'id': policy.get('policy_id')})

    audit = _audit(conn, _ACTION_CN['hard_delete'], policy,
                   {'state_before': policy.get('state'), 'state_after': 'deleted',
                    'reason': reason}, operator)
    events = _enqueue_cascade(conn, policy.get('policy_code'), 'hard_delete', reason, operator)
    result = _result(policy, 'hard_delete', policy.get('state'), 'deleted', False, snapshot,
                     audit=audit, events=events)
    result['deleted_details'] = details
    return result


# ---------------- 版本发布 / 回滚 ----------------

def _require_version(conn, policy, version_no):
    columns = _table_columns(conn, 'policy_version')
    if columns is None or 'version_no' not in columns:
        raise LifecycleError('policy_version 表不可用', 500, 'version_table_missing')
    row = conn.execute(
        text('SELECT version_id FROM policy_version WHERE policy_id=:id AND version_no=:version'),
        {'id': policy.get('policy_id'), 'version': version_no}).first()
    if row is None:
        raise LifecycleError('版本不存在：%s' % version_no, 404, 'version_not_found')
    return True


def publish(conn, code, version_no, operator, reason='', dry_run=False):
    """发布：把 cur_version 指向指定版本；draft 首次发布同时置 active。

    历史问答不追溯改写（设计文档 §4.2）；侧效应只投递 outbox 事件。
    """
    _validate_actor(reason, operator, require_reason=False)
    ensure_schema(conn)
    version_no = str(version_no or '').strip()
    if not version_no:
        raise LifecycleError('缺少版本号', 400, 'missing_version')
    policy = _policy(conn, code, for_update=not dry_run)
    state = policy.get('state')
    if state not in ('draft', 'active', 'suspended'):
        raise LifecycleError('当前状态不可发布：%s' % state, 409, 'invalid_transition')
    _require_version(conn, policy, version_no)
    if str(policy.get('cur_version') or '') == version_no:
        raise LifecycleError('目标版本已是当前版本', 409, 'version_already_current')

    snapshot = impact(conn, code)
    target_state = 'active' if state == 'draft' else state
    extra = {'cur_version_before': policy.get('cur_version'),
             'cur_version_after': version_no, 'version': version_no}
    if dry_run:
        return _result(policy, 'publish', state, target_state, True, snapshot, extra=extra)

    now = _now()
    sets = ['cur_version=:version', 'updated_at=:now', 'updated_by=:operator']
    params = {'version': version_no, 'now': now,
              'operator': str(operator)[:64], 'id': policy.get('policy_id')}
    if state == 'draft':
        sets.append('status=:status')
        params['status'] = _WRITE_STATUS['active']
    conn.execute(text('UPDATE policy SET %s WHERE policy_id=:id' % ', '.join(sets)), params)

    audit = _audit(conn, _ACTION_CN['publish'], policy,
                   {'version_before': policy.get('cur_version'), 'version_after': version_no,
                    'reason': reason}, operator)
    events = _enqueue_cascade(conn, policy.get('policy_code'), 'publish', reason, operator,
                              version=version_no)
    return _result(policy, 'publish', state, target_state, False, snapshot,
                   audit=audit, events=events, extra=extra)


def rollback(conn, code, version_no, operator, reason='', dry_run=False):
    """回滚：把 cur_version 指回历史版本（状态不变；deleted/archived/draft 不允许）。"""
    _validate_actor(reason, operator, require_reason=False)
    ensure_schema(conn)
    version_no = str(version_no or '').strip()
    if not version_no:
        raise LifecycleError('缺少版本号', 400, 'missing_version')
    policy = _policy(conn, code, for_update=not dry_run)
    state = policy.get('state')
    if state not in ('active', 'suspended'):
        raise LifecycleError('当前状态不可回滚：%s' % state, 409, 'invalid_transition')
    _require_version(conn, policy, version_no)
    if str(policy.get('cur_version') or '') == version_no:
        raise LifecycleError('目标版本已是当前版本', 409, 'version_already_current')

    snapshot = impact(conn, code)
    extra = {'cur_version_before': policy.get('cur_version'),
             'cur_version_after': version_no, 'version': version_no}
    if dry_run:
        return _result(policy, 'rollback', state, state, True, snapshot, extra=extra)

    conn.execute(
        text('UPDATE policy SET cur_version=:version, updated_at=:now, updated_by=:operator '
             'WHERE policy_id=:id'),
        {'version': version_no, 'now': _now(), 'operator': str(operator)[:64],
         'id': policy.get('policy_id')})
    audit = _audit(conn, _ACTION_CN['rollback'], policy,
                   {'version_before': policy.get('cur_version'), 'version_after': version_no,
                    'reason': reason}, operator)
    events = _enqueue_cascade(conn, policy.get('policy_code'), 'rollback', reason, operator,
                              version=version_no)
    return _result(policy, 'rollback', state, state, False, snapshot,
                   audit=audit, events=events, extra=extra)


__all__ = [
    'STATES', 'TRANSITIONS', 'LifecycleError',
    'enabled', 'state_of', 'ensure_schema', 'impact', 'versions',
    'suspend', 'archive', 'restore', 'delete_policy', 'publish', 'rollback',
]
