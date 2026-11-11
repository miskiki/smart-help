# -*- coding: utf-8 -*-
"""政策预览数据层：组装单条政策（主表 + 明细 + 原文摘录）的只读数据。

由 `GET /api/policy/preview` 调用，供前端政策卡片右侧预览抽屉渲染。
只读：不写库、不改会话状态。

数据来源：
- `policy`：主表，经 ORM 读取（`models.Policy`）；
- `policy_detail`：地区明细，经 `db.session.execute(text(...))` 读取；
- `policy_vector_qwen3`：原文切片，按 `id` 升序拼接为“原文摘录”。
"""
from sqlalchemy import text

from config import DEFAULT_POLICY_REGIONS
from models import db, Policy

# 原文摘录上限（字符数）：与设计稿“≤3000 字”一致
EXCERPT_LIMIT = 3000
# details 列表最多返回的明细条数
DETAILS_LIMIT = 20
# 省级/全省明细优先标记
_PROVINCE_MARKS = ('全省', '省级')


def _text(value):
    """DB 可空文本 → 展示用字符串（None → ''，保留原始内容不 strip）。"""
    return '' if value is None else str(value)


def _load_detail_rows(policy_id):
    """按 detail_id 升序读取一条政策的全部地区明细。"""
    rows = db.session.execute(
        text('SELECT detail_id, region, benefit, conditions, process, contact, phone, extra '
             'FROM policy_detail WHERE policy_id = :pid ORDER BY detail_id'),
        {'pid': policy_id},
    ).mappings().all()
    return rows


def _region_matches_county(region, county):
    """region 是否属于该 county（支持 '莲花县' 命中 '江西省莲花县' 这类前缀差异）。"""
    region = _text(region).strip()
    if not region or not county:
        return False
    return county in region or region in county


def _pick_detail(rows, detail_id=None, county=None):
    """选择要展示的明细。

    优先级：显式 detail_id（属于该政策）> 与 county 匹配的第一条 > 全省/省级 > 第一条。
    """
    if not rows:
        return None
    if detail_id is not None:
        for row in rows:
            try:
                if int(row['detail_id']) == int(detail_id):
                    return row
            except (TypeError, ValueError):
                break
    county = _text(county).strip()
    if county:
        for row in rows:
            if _region_matches_county(row.get('region'), county):
                return row
    for row in rows:
        region = _text(row.get('region'))
        if any(mark in region for mark in _PROVINCE_MARKS):
            return row
    return rows[0]


def _detail_payload(row):
    return {
        'detail_id': int(row['detail_id']),
        'region': _text(row.get('region')).strip(),
        'benefit': _text(row.get('benefit')),
        'conditions': _text(row.get('conditions')),
        'process': _text(row.get('process')),
        'contact': _text(row.get('contact')),
        'phone': _text(row.get('phone')),
        'extra': _text(row.get('extra')),
    }


def _policy_payload(policy):
    level = _text(policy.level)
    regions = _text(policy.regions).strip() or DEFAULT_POLICY_REGIONS.get(level, '')
    return {
        'code': _text(policy.policy_code),
        'name': _text(policy.policy_name),
        'level': level,
        'regions': regions,
        'category': _text(policy.category1),
        'target': _text(policy.target),
        'conditions': _text(policy.conditions),
        'benefit': _text(policy.benefit),
        'process': _text(policy.process),
        'contact': _text(policy.contact),
        'effect_date': _text(policy.effect_date),
        'status': _text(policy.status),
        'cur_version': _text(policy.cur_version),
        'source': _text(policy.source),
    }


def _fetch_chunks(policy_code, detail_id):
    """读取一组切片的 doc_text 并拼接；detail_id=None 表示 policy 级切片。"""
    if detail_id is None:
        sql = text('SELECT doc_text FROM policy_vector_qwen3 '
                   'WHERE policy_code = :code AND detail_id IS NULL AND doc_text IS NOT NULL '
                   'ORDER BY id')
        params = {'code': policy_code}
    else:
        sql = text('SELECT doc_text FROM policy_vector_qwen3 '
                   'WHERE policy_code = :code AND detail_id = :did AND doc_text IS NOT NULL '
                   'ORDER BY id')
        params = {'code': policy_code, 'did': detail_id}
    rows = db.session.execute(sql, params).mappings().all()
    return '\n'.join(chunk for chunk in (_text(r.get('doc_text')) for r in rows) if chunk)


def _load_excerpt(policy_code, detail_id):
    """原文摘录：优先所选明细的切片，取不到时诚实回落到 policy 级切片。

    同编号下切片可能属于其他县区，按 policy_code 全量拼会把别县原文当成本次核验
    依据，因此明细级严格按 detail_id 过滤；该明细无切片时只回落到
    `detail_id IS NULL` 的政策级切片（不是全 code 拼接），两者都没有才返回空串。

    :return: (excerpt, excerpt_truncated, excerpt_scope)
             excerpt_scope ∈ {'detail', 'policy', ''}
    """
    joined = ''
    scope = ''
    if detail_id is not None:
        joined = _fetch_chunks(policy_code, detail_id)
        if joined:
            scope = 'detail'
    if not joined:
        joined = _fetch_chunks(policy_code, None)
        if joined:
            scope = 'policy'
    if not joined:
        return '', False, ''
    if len(joined) <= EXCERPT_LIMIT:
        return joined, False, scope
    return joined[:EXCERPT_LIMIT], True, scope


def build_preview(policy_code, detail_id=None, county=None):
    """组装单条政策的预览数据。

    :param policy_code: 政策编号（如 'S-ZB-090'）
    :param detail_id: 可选明细编号；属于该政策则用它，否则按 county/省级/第一条回落
    :param county: 可选县区名（如 '莲花县'）；用于挑选该政策下对应属地的明细
    :raises LookupError: 编号不存在（路由映射为 404 {"error": "policy_not_found"}）
    :rtype: dict
    """
    code = (policy_code or '').strip()
    policy = Policy.query.filter_by(policy_code=code).first()
    if policy is None:
        raise LookupError('policy_not_found')

    rows = _load_detail_rows(policy.policy_id)
    chosen = _pick_detail(rows, detail_id, county)
    selected_detail_id = int(chosen['detail_id']) if chosen is not None else None
    excerpt, excerpt_truncated, excerpt_scope = _load_excerpt(code, selected_detail_id)

    return {
        'policy': _policy_payload(policy),
        'detail': _detail_payload(chosen) if chosen is not None else None,
        'details': [{'detail_id': int(row['detail_id']),
                     'region': _text(row.get('region')).strip()}
                    for row in rows[:DETAILS_LIMIT]],
        'excerpt': excerpt,
        'excerpt_truncated': excerpt_truncated,
        'excerpt_scope': excerpt_scope,
    }
