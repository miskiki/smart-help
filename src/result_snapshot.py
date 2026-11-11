"""Immutable answer/export data. Never retrieve policies again during export."""
import io
import json
import os
from datetime import datetime


# 问答快照附加政策版本/状态（生命周期审计第 10 条）：默认开启，只读查询、纯附加。
# 关闭方式 POLICY_QA_SNAPSHOT=0/off/false/no；任何异常都不写该键，绝不影响回答落库。
_POLICY_SNAPSHOT_ENV = 'POLICY_QA_SNAPSHOT'
_POLICY_SNAPSHOT_OFF_VALUES = ('0', 'off', 'false', 'no')
_POLICY_SNAPSHOT_VERSION = 'qa-policy-snapshot-1'
_MAX_POLICY_CODES = 500


def _policy_snapshot_enabled():
    raw = os.environ.get(_POLICY_SNAPSHOT_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _POLICY_SNAPSHOT_OFF_VALUES


def _code_text(value):
    if value is None:
        return ''
    return str(value).strip()


def _dedupe_codes(codes):
    seen = set()
    out = []
    for code in codes:
        code = _code_text(code)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _codes_from_rows(rows):
    if not isinstance(rows, (list, tuple)):
        return []
    codes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get('政策编号') or row.get('policy_code') or row.get('code')
        codes.append(_code_text(code))
    return codes


def _codes_from_matched_list(items):
    if not isinstance(items, (list, tuple)):
        return []
    codes = []
    for item in items:
        if isinstance(item, dict):
            codes.append(_code_text(item.get('code') or item.get('policy_code')))
    return codes


def _codes_from_matched(matched):
    if isinstance(matched, str):
        return [matched]
    if isinstance(matched, dict):
        return [_code_text(matched.get('code') or matched.get('policy_code'))]
    if isinstance(matched, (list, tuple)):
        codes = []
        for item in matched:
            if isinstance(item, dict):
                codes.append(_code_text(item.get('code') or item.get('policy_code')))
            else:
                codes.append(_code_text(item))
        return codes
    return []


def _referenced_codes(meta):
    """本次回答引用到的政策编号，按 table_rows/export_rows → matched_list → matched 取第一个非空来源。"""
    if not isinstance(meta, dict):
        return []
    for key in ('table_rows', 'export_rows'):
        codes = _dedupe_codes(_codes_from_rows(meta.get(key)))
        if codes:
            return codes
    codes = _dedupe_codes(_codes_from_matched_list(meta.get('matched_list')))
    if codes:
        return codes
    return _dedupe_codes(_codes_from_matched(meta.get('matched')))


def _plain_value(value):
    """DATETIME 等非标量列转成 JSON 可序列化值；历史回答快照要能直接 dumps。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    isoformat = getattr(value, 'isoformat', None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _query_policy_rows(codes):
    """只读查询政策主表。优先 kb_rag._db()，不可用时回落 models.db。"""
    from sqlalchemy import text
    engine = None
    try:
        from kb_rag import _db
        engine = _db()
    except Exception:
        engine = None
    if engine is None:
        from models import db
        engine = db.engine
    params = {}
    placeholders = []
    for i, code in enumerate(codes):
        key = 'code_%d' % i
        params[key] = code
        placeholders.append(':' + key)
    # level 是 Kingbase 保留字，列名必须加引号（sqlite/PG 同样接受）。
    sql = ('SELECT policy_code, policy_name, "level", status, cur_version, updated_at '
           'FROM policy WHERE policy_code IN (%s)' % ', '.join(placeholders))
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _policy_snapshot(meta):
    """构建 policy_snapshot；开关关闭/无编号/查询失败一律返回 None：不写键、不抛异常。"""
    try:
        if not _policy_snapshot_enabled():
            return None
        codes = _referenced_codes(meta)[:_MAX_POLICY_CODES]
        if not codes:
            return None
        rows = _query_policy_rows(codes)
        found = {}
        for row in rows or []:
            values = tuple(row)
            if not values:
                continue
            code = _code_text(values[0])
            if not code or code in found:
                continue
            found[code] = {
                'code': code,
                'name': _plain_value(values[1] if len(values) > 1 else None),
                'level': _plain_value(values[2] if len(values) > 2 else None),
                'status': _plain_value(values[3] if len(values) > 3 else None),
                'cur_version': _plain_value(values[4] if len(values) > 4 else None),
                'updated_at': _plain_value(values[5] if len(values) > 5 else None),
            }
        return {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'version': _POLICY_SNAPSHOT_VERSION,
            'items': [found[code] for code in codes if code in found],
            'missing': [code for code in codes if code not in found],
        }
    except Exception:
        # 纯附加信息：历史回答、导出与判定不能因为这次只读查询失败而受影响。
        return None


def freeze(meta, answer):
    import smart_matcher_kb as smk
    existing = meta.get('match_json')
    snapshot = dict(existing) if isinstance(existing, dict) else {'kind': 'consultation', 'matched_list': meta.get('matched_list', [])}
    rows = meta.get('table_rows')
    if rows is None:
        rows = []
        for p in meta.get('matched_list', []):
            if p.get('applicable') is True:
                rows.append({'成员': '政策咨询', '政策编号': p['code'], '政策名称': p['name'],
                    '政策层级': '', '政策版本': p.get('version', ''), '匹配理由': p.get('reason', ''),
                    '政策依据（文件文号/条款）': p.get('doc', ''), '尚缺条件': p.get('missing', ''),
                    '下一步核实事项': p.get('next', ''), '补贴标准': '见本行政策依据原文', '申领流程': '见本行政策依据原文'})
    snapshot.update(schema_version=2, export_rows=rows, answer=answer,
                    questions=meta.get('questions', []), will_missing=meta.get('will_missing', False),
                    judge_status=meta.get('judge_status', 'complete'))
    # 既有键顺序与语义保持不变；policy_snapshot 只在成功时追加到最后，失败时不写。
    policy_snapshot = _policy_snapshot(meta)
    if policy_snapshot is not None:
        snapshot['policy_snapshot'] = policy_snapshot
    return snapshot


def export(snapshot, answer, qa_id):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    import smart_matcher_kb as smk
    if isinstance(snapshot, dict) and snapshot.get('schema_version') == 2:
        rows = snapshot['export_rows']
    elif isinstance(snapshot, dict) and 'table_recs' in snapshot:
        rows = smk.table_rows_from_recs(snapshot['table_recs'])
    elif isinstance(snapshot, list):
        rows = [{'政策编号': x['code'], '政策名称': x.get('name', ''), '匹配理由': x.get('reason', ''),
                 '政策依据（文件文号/条款）': x.get('doc') or x.get('excerpt', ''),
                 '尚缺条件': x.get('missing', ''), '下一步核实事项': x.get('next', '')}
                for x in snapshot if x.get('applicable') is True]
    else:
        raise ValueError('历史回答缺少完整结果快照，请重新提问后导出；系统不会重新匹配替换原回答')
    headers = ['成员', '政策编号', '政策名称', '政策层级', '政策版本', '结果类型', '匹配理由', '政策依据（文件文号/条款）', '尚缺条件', '下一步核实事项', '补贴标准', '申领流程']
    if isinstance(snapshot,dict) and snapshot.get('mode')=='full-model-v1':
        headers+=['已满足条件','缺家庭信息','缺政策依据']
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '匹配结果'
    ws.append(headers)
    for row in rows:
        ws.append([str(row.get(k) or '') for k in headers])
    notes = wb.create_sheet('原始回答')
    notes.append(['问答记录编号', str(qa_id)])
    notes.append(['推荐行数', len(rows)])
    if not rows:
        notes.append(['结果说明', '本次没有最终推荐政策，未回退候选政策'])
    # Excel has a 32767-character cell limit. Preserve the complete answer in multiple cells.
    for i in range(0, max(len(answer), 1), 30000):
        notes.append(['回答正文', answer[i:i+30000]])
    for sheet in wb:
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='176B4B')
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'  # Imported text must never become an Excel formula.
                cell.alignment = Alignment(vertical='top', wrap_text=True)
        for n in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(n)].width = 24 if n != 2 else 40
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio
