# -*- coding: utf-8 -*-
"""管理后台 API：登录 / 台账 / 上传 / 草稿确认 / 修改记录 / 问答记录"""
import os
import re
import json
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, session, jsonify, send_file, current_app
from werkzeug.security import check_password_hash
from models import db, AdminUser, Policy, PolicyVersion, PolicyDraft, QaLog, AuditLog, SurveyResult
from config import UPLOAD_DIR, EXPORT_PATH
from recognize import recognize_pdf, extract_pdf_text
import kb_rag

from authentication import current_user as _login_user, login_required, admin_required, ApiError, require_user
from conversations import owned

bp = Blueprint('api', __name__, url_prefix='/api')

# 一级分类 → 拼音首码（命名规则.md）
CATEGORY_CODE = {
    '综合保障': 'ZB', '产业帮扶': 'CY', '健康帮扶': 'JK', '就业帮扶': 'JY',
    '其他教育帮扶': 'QJ', '义务教育保障': 'YW', '因灾救助': 'YZ', '公益岗位帮扶': 'GY',
    '住房安全保障': 'ZF', '金融帮扶': 'JR', '生产生活条件改善': 'SC',
    '饮水安全保障': 'YS', '社会帮扶': 'SH', '基础设施建设': 'JC', '搬迁': 'BQ',
}

# 台账展示列（不含管理字段的问答侧口径见文末说明）
LEDGER_COLS = ['政策编号', '政策名称', '层级', '一级分类', '地区数', '地区(前5)',
               '文号数', '文号(前60)', '状态', '版本', '来源', '层级说明',
               '更新时间', '操作人']

# 四级体系层级编码 → 中文（C=国家 / S=省级 / U=市级 / X=县级）
LEVEL_CN = {'C': '国家', 'S': '省级', 'U': '市级', 'X': '县级'}
LEVEL_CN_REV = {v: k for k, v in LEVEL_CN.items()}


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')



# ---------------- 统计辅助（台账导出/展示口径） ----------------
_DOC_PAT = re.compile(r'[〔\[(（【][0-9]{4}[〕\]）)】]\s*\d+\s*号')


def stat_regions(regions):
    """地区数 + 地区(前5) 展示串，口径：顿号/逗号/分号/空白分隔去重计数"""
    parts = [p.strip() for p in re.split(r'[、，,;；\s]+', regions or '') if p.strip()]
    total = len(parts)
    if total == 0:
        return 0, ''
    head = '、'.join(parts[:5])
    return total, (head if total <= 5 else '%s…(%d区)' % (head, total))


def stat_doc_ref(doc_ref):
    """文号数 + 文号(前60) 展示串，口径：〔/（/[ /【 年份】+序号+号 计数"""
    count = len(_DOC_PAT.findall(doc_ref or ''))
    return count, (doc_ref or '')[:60]


def ledger_row(p):
    from config import DEFAULT_POLICY_REGIONS
    region_count, region_head = stat_regions((p.regions or '').strip() or DEFAULT_POLICY_REGIONS.get(p.level, ''))
    doc_count, doc_head = stat_doc_ref(p.doc_ref)
    return {
        'policy_code': p.policy_code, 'policy_name': p.policy_name,
        'level': p.level, 'level_cn': LEVEL_CN.get(p.level, p.level or ''),
        'category1': p.category1,
        'region_count': region_count, 'region_head': region_head,
        'doc_count': doc_count, 'doc_head': doc_head,
        'status': p.status, 'cur_version': p.cur_version,
        'source': p.source, 'level_note': p.level_note,
        'updated_at': p.updated_at, 'updated_by': p.updated_by,   # 仅管理侧返回
    }


def gen_policy_code(level, category1):
    """新增编号：层级码 + 分类拼音首码 + 序号（命名规则.md）"""
    if db.session.get_bind().dialect.name == 'postgresql':
        from sqlalchemy import text
        db.session.execute(text('SELECT pg_advisory_xact_lock(2026101202)'))
    prefix = '%s-%s-' % (str(level).upper(), CATEGORY_CODE.get(category1, 'ZZ'))
    mx = 0
    for p in Policy.query.filter(Policy.policy_code.like(prefix + '%')).all():
        m = re.match(re.escape(prefix) + r'(\d+)', p.policy_code or '')
        if m:
            mx = max(mx, int(m.group(1)))
    return '%s%03d' % (prefix, mx + 1)


# ---------------- 认证 ----------------










@bp.get('/stats')
@admin_required
def stats():
    total = Policy.query.count()
    in_use = Policy.query.filter_by(status='在用').count()
    drafts = PolicyDraft.query.filter_by(confirm_status='待确认').count()
    qa_today = QaLog.query.filter(QaLog.ask_time.like(datetime.now().strftime('%Y-%m-%d') + '%')).count()
    return jsonify({'ok': True, 'stats': {
        'total': total, 'in_use': in_use, 'drafts': drafts, 'qa_today': qa_today}})


# ---------------- 台账 ----------------
@bp.get('/policies')
@admin_required
def policies():
    page = max(1, int(request.args.get('page', 1)))
    size = min(50, max(1, int(request.args.get('size', 20))))
    kw = (request.args.get('kw') or '').strip()
    q = Policy.query
    if kw:
        like = '%%%s%%' % kw
        q = q.filter(db.or_(Policy.policy_name.like(like),
                            Policy.policy_code.like(like),
                            Policy.doc_ref.like(like)))
    # 四级体系多选组合筛选（逗号分隔多值；region 为顿号分隔文本，逐值模糊匹配）
    levels = (request.args.get('level') or '').strip()
    cats = (request.args.get('category1') or '').strip()
    statuses = (request.args.get('status') or '').strip()
    versions = (request.args.get('version') or '').strip()
    regions = (request.args.get('region') or '').strip()
    if levels:
        vals = [v for v in levels.split(',') if v]
        q = q.filter(Policy.level.in_(vals))
    if cats:
        vals = [v for v in cats.split(',') if v]
        q = q.filter(Policy.category1.in_(vals))
    if statuses:
        vals = [v for v in statuses.split(',') if v]
        q = q.filter(Policy.status.in_(vals))
    if versions:
        vals = [v for v in versions.split(',') if v]
        q = q.filter(Policy.cur_version.in_(vals))
    if regions:
        from config import DEFAULT_POLICY_REGIONS
        for reg in [v for v in regions.split(',') if v]:
            default_levels = [level for level, scope in DEFAULT_POLICY_REGIONS.items() if reg in scope]
            q = q.filter(db.or_(Policy.regions.like('%%%s%%' % reg),
                               db.and_(db.func.trim(db.func.coalesce(Policy.regions, '')) == '', Policy.level.in_(default_levels))))
    total = q.count()
    items = [ledger_row(p) for p in q.order_by(Policy.policy_code).offset((page - 1) * size).limit(size).all()]
    return jsonify({'ok': True, 'total': total, 'page': page, 'size': size, 'items': items})


@bp.get('/policies/filters')
@admin_required
def policy_filters():
    """台账筛选选项：层级（四级中文）/ 帮扶类型 / 行政区域 / 状态 / 版本"""
    levels = []
    for (lv,) in db.session.query(Policy.level).distinct().all():
        if lv:
            levels.append({'value': lv, 'label': LEVEL_CN.get(lv, lv)})
    order = list(LEVEL_CN.keys())
    levels.sort(key=lambda x: order.index(x['value']) if x['value'] in order else 99)
    cats = [{'value': c, 'label': c} for (c,) in db.session.query(Policy.category1).distinct().all() if c]
    cats.sort(key=lambda x: x['value'])
    regs = set()
    from config import DEFAULT_POLICY_REGIONS
    for (regions, level) in db.session.query(Policy.regions, Policy.level).all():
        regions = (regions or '').strip() or DEFAULT_POLICY_REGIONS.get(level, '')
        if not regions:
            continue
        for part in re.split(r'[、，,;；\s]+', regions):
            if part.strip():
                regs.add(part.strip())
    region_opts = [{'value': r, 'label': r} for r in sorted(regs)]
    statuses = [{'value': s, 'label': s} for (s,) in db.session.query(Policy.status).distinct().all() if s]
    versions = [{'value': v, 'label': v} for (v,) in db.session.query(Policy.cur_version).distinct().all() if v]
    return jsonify({'ok': True, 'filters': {
        'levels': levels, 'category1': cats, 'regions': region_opts,
        'status': statuses, 'version': versions}})


@bp.get('/policies/export')
@admin_required
def export_policies():
    """由 db 导出台账 Excel（db 为唯一权威源）。原文件存在则先备份。"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    if os.path.exists(EXPORT_PATH):
        bak = EXPORT_PATH.replace('.xlsx', '_备份_%s.xlsx' % datetime.now().strftime('%Y%m%d_%H%M%S'))
        os.replace(EXPORT_PATH, bak)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '编号台账'
    ws.append(LEDGER_COLS)
    head_font = Font(bold=True, color='FFFFFF')
    head_fill = PatternFill('solid', fgColor='4472C4')
    for c in ws[1]:
        c.font = head_font
        c.fill = head_fill
        c.alignment = Alignment(horizontal='center', vertical='center')

    for p in Policy.query.order_by(Policy.policy_code).all():
        from config import DEFAULT_POLICY_REGIONS
        rc, rh = stat_regions((p.regions or '').strip() or DEFAULT_POLICY_REGIONS.get(p.level, ''))
        dc, dh = stat_doc_ref(p.doc_ref)
        ws.append([p.policy_code, p.policy_name, p.level, p.category1, rc, rh,
                   dc, dh, p.status, p.cur_version, p.source, p.level_note,
                   p.updated_at, p.updated_by])

    widths = [12, 36, 6, 14, 8, 44, 8, 50, 8, 8, 26, 34, 20, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A2'
    wb.save(EXPORT_PATH)
    return send_file(EXPORT_PATH, as_attachment=True,
                     download_name='政策编号台账.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ---------------- 文档上传 ----------------
def _apply_draft_to_ledger(d, user, via='上传'):
    """草稿/识别结果 → 台账落库（停用/更新/新增三分支，含版本留档与知识库同步）"""
    requested_code = ((d.draft_data or {}).get('policy_code') or '').strip().upper()
    if requested_code:
        matches = Policy.query.filter_by(policy_code=requested_code).all()
        if len(matches) != 1:
            return {'ok': False, 'msg': '政策编号%s不存在或不唯一，未更新台账' % requested_code}
        if d.match_policy_id != matches[0].policy_id:
            return {'ok': False, 'msg': '明确政策编号只能更新对应记录，不能改为新增或覆盖其他编号'}
    is_stop = (d.status == '停用' and d.match_policy_id)
    if not d.policy_name or d.policy_name.startswith('（待识别）'):
        return {'ok': False, 'msg': '政策名称为待识别占位，无法入库'}
    if d.match_policy_id and not db.session.get(Policy, d.match_policy_id):
        return {'ok': False, 'msg': '更新对象不存在，请核对政策编号'}
    if not is_stop and (not d.level or not d.category1):
        return {'ok': False, 'msg': '层级 / 一级分类缺失，无法入库'}

    if not is_stop:
        from policy_ingest import prepare
        try:
            prepare(d)
        except Exception as exc:
            return {'ok': False, 'msg': str(exc)}
    if is_stop:
        p = Policy.query.filter_by(policy_id=d.match_policy_id).populate_existing().with_for_update().one()
        p.status = '停用'
        p.updated_at = _now()
        p.updated_by = user.username
        from graph_update_state import mark_pending
        mark_pending(db.session,p,'政策停用')
        db.session.add(AuditLog(action='停用政策', target_code=p.policy_code,
                               target_name=p.policy_name,
                               detail='%s：依据文档识别为废止/失效' % via,
                               operator=user.username, created_at=_now()))
        d.policy_code = p.policy_code
        d.version_no = p.cur_version
        db.session.commit()
        try:
            from kg_retrieval import invalidate_cache
            from kg_source_update import request_update
            from smart_matcher_kb import invalidate_db_cache
            invalidate_cache()
            invalidate_db_cache()
            request_update()
            kb_r = kb_rag.remove_policy(p.policy_code)
            kb_msg = kb_r.get('msg') or ''
        except Exception as e:
            kb_r, kb_msg = {'ok': False}, '%s: %s' % (type(e).__name__, e)
        from policy_ingest import save_sync
        save_sync(d, kb_r)
        return {'ok': True, 'result': '停用', 'policy_code': p.policy_code,
                'kb_sync': {'ok': kb_r.get('ok', False), 'msg': kb_msg}}

    if d.match_policy_id:
        p = Policy.query.filter_by(policy_id=d.match_policy_id).populate_existing().with_for_update().one()
        old = {c.name: getattr(p, c.name) for c in Policy.__table__.columns}
        from sqlalchemy import text
        old['details'] = [dict(row) for row in db.session.execute(text('SELECT * FROM policy_detail WHERE policy_id=:id'), {'id':p.policy_id}).mappings()]
        ver = p.cur_version or 'v1'
        m = re.match(r'v(\d+)', ver)
        nv = 'v%d' % (int(m.group(1)) + 1 if m else 1)
        db.session.add(PolicyVersion(
            policy_id=p.policy_id, version_no=ver,
            content_snapshot=json.dumps(old, ensure_ascii=False, default=str),
            effect_date=p.effect_date, expire_date=p.expire_date,
            updated_by=user.username, updated_at=_now(), note='%s更新 %s→%s' % (via, ver, nv)))
        p.policy_name = d.policy_name
        p.level = d.level
        p.category1 = d.category1
        if d.doc_ref:
            p.doc_ref = d.doc_ref
        if d.regions:
            p.regions = d.regions
        p.status = d.status or '在用'
        p.cur_version = nv
        p.updated_at = _now()
        p.updated_by = user.username
        if d.match_reason:
            p.level_note = d.match_reason
        d.policy_code = p.policy_code
        d.version_no = nv
        db.session.add(AuditLog(action='更新政策', target_code=p.policy_code,
                               target_name=p.policy_name,
                               detail='%s：%s → %s' % (via, ver, nv),
                               operator=user.username, created_at=_now()))
        from policy_ingest import write_details
        write_details(p, d)
        db.session.commit()
        kb_r = _kb_sync_upsert(p, d)
        return {'ok': True, 'result': '更新', 'policy_code': p.policy_code,
                'old_version': ver, 'new_version': nv, 'kb_sync': kb_r}

    code = gen_policy_code(d.level, d.category1)
    p = Policy(policy_code=code, policy_name=d.policy_name, level=d.level,
               category1=d.category1, doc_ref=d.doc_ref, regions=d.regions,
               status=d.status or '在用', cur_version='v1', source=d.source,
               level_note=d.match_reason or '管理后台新增，待补充说明',
               updated_at=_now(), updated_by=user.username)
    db.session.add(p)
    db.session.flush()
    db.session.add(PolicyVersion(policy_id=p.policy_id, version_no='v1',
                                 content_snapshot='', effect_date='', expire_date='',
                                 updated_by=user.username, updated_at=_now(),
                                 note='%s新增入库' % via))
    d.policy_code = code
    d.version_no = 'v1'
    db.session.add(AuditLog(action='新增政策', target_code=code,
                           target_name=d.policy_name,
                           detail='%s 新增入库' % via,
                           operator=user.username, created_at=_now()))
    from policy_ingest import write_details
    write_details(p, d)
    db.session.commit()
    kb_r = _kb_sync_upsert(p, d)
    return {'ok': True, 'result': '新增', 'policy_code': code, 'kb_sync': kb_r}


@bp.post('/upload')
@admin_required
def upload():
    """读取政策文件；明确对象直接入库，存在歧义时保留草稿。"""
    user = _login_user()
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'ok': False, 'msg': '未选择文件'})
    results = []
    for f in files:
        if not f or not f.filename:
            continue
        if not f.filename.lower().endswith(('.pdf', '.xlsx', '.csv', '.docx')):
            results.append({'filename': f.filename, 'ok': False, 'msg': '支持 PDF、XLSX、CSV、DOCX 文件'})
            continue
        safe = re.sub(r'[\\/:*?"<>|]', '_', f.filename)
        from uuid import uuid4
        path = os.path.join(UPLOAD_DIR, '%s_%s_%s' % (_now().replace(' ', '_').replace(':', ''), uuid4().hex[:12], safe))
        f.save(path)
        try:
            if f.filename.lower().endswith(('.xlsx', '.csv')):
                from table_import import recognize_many
                records = recognize_many(path)
            else:
                records = [recognize_pdf(path)]
        except Exception as e:
            db.session.add(AuditLog(action='上传文档', target_code='', target_name=f.filename,
                                   detail='识别失败，未更新台账：%s' % e,
                                   operator=user.username, created_at=_now()))
            db.session.commit()
            results.append({'filename': f.filename, 'ok': False, 'msg': '识别失败（%s），未更新台账' % e})
            continue
        for rec in records:
            d = PolicyDraft(
                policy_name=rec['policy_name'] or '（待补充）%s' % os.path.splitext(f.filename)[0],
                level=rec['level'] or '', category1=rec['category1'] or '',
                doc_ref=rec.get('doc_ref', '') or '', regions=rec.get('regions', '') or '',
                status=rec['status'], match_type=rec['match_type'],
                confidence=rec.get('confidence'), match_policy_id=rec.get('match_policy_id'),
                match_reason=rec.get('match_reason', ''), source=f.filename, pdf_path=path,
                created_by=user.username, created_at=_now(),
                confirm_status='待确认', draft_data=rec.get('draft_data', {}))
            db.session.add(d)
            db.session.flush()
            if d.match_type == '待人工确认':
                r = {'ok': True, 'result': '待人工确认', 'msg': '更新对象不确定，请在待确认草稿中核实'}
            else:
                r = _apply_draft_to_ledger(d, user, via='上传%s' % f.filename)
                if r.get('ok'):
                    d.confirm_status = '已确认'
                    d.confirmed_by = user.username
                    d.confirmed_at = _now()
            db.session.add(AuditLog(action='上传文档', target_code=d.policy_code or '',
                                   target_name=f.filename,
                                   detail='上传 PDF 直接更新台账：%s（%s）' % (f.filename, r.get('result') or r.get('msg', '')),
                                   operator=user.username, created_at=_now()))
            db.session.commit()
            results.append({'filename': f.filename + ' / ' + d.policy_name, 'ok': r.get('ok', False),
                            'msg': (r.get('msg') if r.get('result') == '待人工确认' else '台账：%s；索引：%s' % (r.get('result') or r.get('msg', '失败'), '可用' if (r.get('kb_sync') or {}).get('ok') else '未完成（可在草稿记录重试）')),
                            'result': r.get('result'),
                            'draft_id': d.draft_id,
                            'policy_code': r.get('policy_code', ''), 'kb_sync': r.get('kb_sync')})
    return jsonify({'ok': True, 'results': results})


# ---------------- 草稿 ----------------
def draft_view(d):
    data = dict(d.draft_data or {})
    if d.match_type == '待人工确认' and not data.get('match_candidates') and d.match_policy_id:
        p = db.session.get(Policy, d.match_policy_id)
        if p:
            data['match_candidates'] = [{k:getattr(p,k) or '' for k in ('policy_id','policy_code','policy_name','level','regions','doc_ref','target','conditions','benefit')}]
    return {
        'draft_id': d.draft_id, 'policy_code': d.policy_code, 'policy_name': d.policy_name,
        'level': d.level, 'category1': d.category1, 'doc_ref': d.doc_ref,
        'regions': d.regions, 'status': d.status, 'version_no': d.version_no,
        'source': d.source, 'match_type': d.match_type, 'confidence': d.confidence,
        'match_policy_id': d.match_policy_id, 'match_reason': d.match_reason,
        'draft_data': data, 'created_by': d.created_by, 'created_at': d.created_at,
        'confirm_status': d.confirm_status, 'confirmed_by': d.confirmed_by,
        'confirmed_at': d.confirmed_at, 'graph_sync': _graph_sync_status(d.policy_code),
    }


@bp.get('/drafts')
@admin_required
def drafts():
    status = request.args.get('status', '待确认')
    q = PolicyDraft.query
    if status:
        q = q.filter_by(confirm_status=status)
    items = [draft_view(d) for d in q.order_by(PolicyDraft.draft_id.desc()).limit(200).all()]
    return jsonify({'ok': True, 'items': items})


@bp.get('/drafts/<int:did>')
@admin_required
def draft_get(did):
    d = PolicyDraft.query.get_or_404(did)
    return jsonify({'ok': True, 'item': draft_view(d)})


@bp.post('/drafts/<int:did>/update')
@admin_required
def draft_update(did):
    d = PolicyDraft.query.get_or_404(did)
    if d.confirm_status != '待确认':
        return jsonify({'ok': False, 'msg': '草稿已处理，不可修改'})
    data = request.get_json(force=True) or {}
    fields = data.get('fields') or {}
    editable = {'policy_name', 'level', 'category1', 'doc_ref', 'regions',
                'status', 'match_policy_id', 'match_reason', 'confidence', 'details', 'target', 'conditions', 'benefit', 'process', 'contact', 'effect_date', 'expire_date', 'match_decision'}
    decision = fields.get('match_decision')
    if decision is not None and decision not in ('update', 'new'):
        return jsonify({'ok': False, 'msg': '请选择更新已有政策或新增政策'}), 400
    if decision == 'new':
        fields['match_policy_id'] = None
    for k, v in fields.items():
        if k in editable:
            if k == 'match_policy_id':
                v = int(v) if v else None
            if hasattr(d, k):
                setattr(d, k, v)
    if fields:
        d.draft_data = {**(d.draft_data or {}), **fields}
    db.session.commit()
    return jsonify({'ok': True, 'item': draft_view(d)})


@bp.post('/drafts/<int:did>/confirm')
@admin_required
def draft_confirm(did):
    d = PolicyDraft.query.get_or_404(did)
    user = _login_user()
    if d.confirm_status != '待确认':
        return jsonify({'ok': False, 'msg': '草稿已处理（%s）' % d.confirm_status})
    data = request.get_json(force=True) or {}
    action = data.get('action', 'approve')

    if action == 'reject':
        d.confirm_status = '已驳回'
        d.confirmed_by = user.username
        d.confirmed_at = _now()
        db.session.add(AuditLog(action='驳回草稿', target_code=d.policy_code or '',
                               target_name=d.policy_name or '', detail='驳回草稿 #%s' % did,
                               operator=user.username, created_at=_now()))
        db.session.commit()
        return jsonify({'ok': True, 'result': 'reject'})

    # ---- 确认前校验（停用分支只改状态，不要求层级/分类） ----
    if d.match_type == '待人工确认':
        decision = (d.draft_data or {}).get('match_decision')
        if decision not in ('update','new'):
            return jsonify({'ok': False, 'msg': '请先选择更新已有政策或新增政策'}), 400
        if decision == 'update' and not d.match_policy_id:
            return jsonify({'ok': False, 'msg': '请选择要更新的已有政策'}), 400
        if decision == 'new' and d.match_policy_id:
            return jsonify({'ok': False, 'msg': '新增政策不能同时指定更新对象'}), 400
    is_stop = (d.status == '停用' and d.match_policy_id)
    if not d.policy_name or d.policy_name.startswith('（待识别）'):
        return jsonify({'ok': False, 'msg': '政策名称为待识别占位，请先修改后再确认'})
    if not is_stop and (not d.level or not d.category1):
        return jsonify({'ok': False, 'msg': '层级 / 一级分类缺失，请先修改后再确认'})

    # 落库（停用/更新/新增）→ 复用公共逻辑
    r = _apply_draft_to_ledger(d, user, via='确认草稿 #%s' % did)
    if not r.get('ok'):
        return jsonify({'ok': False, 'msg': r.get('msg', '入库失败')}), 400
    d.confirm_status = '已确认'
    d.confirmed_by = user.username
    d.confirmed_at = _now()
    db.session.commit()
    return jsonify({'ok': True, 'result': r.get('result'), 'policy_code': r.get('policy_code'),
                    'old_version': r.get('old_version'), 'new_version': r.get('new_version'),
                    'kb_sync': r.get('kb_sync')})



def _kb_sync_upsert(p, d):
    """新增/更新确认后同步知识库：用草稿 PDF 全文覆盖/写入该政策文档"""
    from kg_source_update import request_update
    from kg_retrieval import invalidate_cache
    from smart_matcher_kb import invalidate_db_cache
    invalidate_cache()
    invalidate_db_cache()
    request_update()
    try:
        txt = (d.draft_data or {}).get('document_text') or ''
        if not txt and d.pdf_path and os.path.exists(d.pdf_path):
            from policy_ingest import document_text
            txt = document_text(d.pdf_path) or ''
        if not txt.strip():
            return {'ok': False, 'msg': '缺少 PDF 全文，知识库未同步（可稍后手动重试）'}
        r = kb_rag.sync_policy(p.policy_code, p.policy_name, p.doc_ref,
                               p.status, p.cur_version, txt)
        from policy_ingest import save_sync
        return save_sync(d, {'ok': r.get('ok', False), 'msg': r.get('msg', ''), 'chunks': r.get('chunks', 0)})
    except Exception as e:
        from policy_ingest import save_sync
        return save_sync(d, {'ok': False, 'msg': '索引同步失败（%s），台账已保留，可重试' % type(e).__name__})


# ---------------- 政策问答 ----------------











# ---------------- 农户发展意愿问卷 ----------------
_QUES_Q1 = {'在家务农': 3, '外出打工': 2, '创业': 4, '都不想': 0}


def _survey_score(answers):
    """按问卷计分规则：总分=题1+题3/4+题5/7/8，满分11；≥8高/5-7中/≤4低"""
    a = answers or {}
    q1 = str(a.get('q1') or '')
    score = _QUES_Q1.get(q1, 0)
    if q1 == '在家务农':
        if a.get('q3') or a.get('q4'):
            score += 3
        if a.get('q5'):
            score += 3 if str(a.get('q5')) == '需要' else 1
    elif q1 == '创业':
        if a.get('q5'):
            score += 3 if str(a.get('q5')) == '需要' else 1
    elif q1 == '外出打工':
        if a.get('q7') == '想学':
            score += 2
    elif q1 == '都不想':
        if a.get('q8') == '愿意':
            score += 2
    level = '高' if score >= 8 else ('中' if score >= 5 else '低')
    return score, level


def _survey_tags(a):
    """问卷答案 → 意愿标签（与 rule_mapping「农户意愿问卷/特殊规则」对象描述对齐）"""
    tags = []
    q1 = a.get('q1')
    if q1 == '在家务农':
        t = '在家务农'
        if a.get('q2') == '种植':
            t += '-种植'
            if a.get('q3'):
                t += '-' + a['q3']
        elif a.get('q2') == '养殖':
            t += '-养殖'
            if a.get('q4'):
                t += '-' + a['q4']
        tags.append(t)
        if a.get('q5') == '需要':
            tags.append('需要启动资金')
    elif q1 == '创业':
        tags.append('想创业')
        if a.get('q5') == '需要':
            tags.append('需要启动资金')
    elif q1 == '外出打工':
        q6 = a.get('q6') or ''
        if '外省' in q6:
            tags.append('外出打工-外省')
        elif '省内' in q6 or '本省' in q6:
            tags.append('外出打工-省内')
        elif '村里' in q6 or '乡镇' in q6 or '就近' in q6:
            tags.append('外出打工-就近')
        else:
            tags.append('外出打工')
        if a.get('q7') == '想学':
            tags.append('外出打工-想学技能')
    elif q1 == '都不想':
        tags.append('都不想')
        if a.get('q8') == '愿意':
            tags.append('愿意公益性岗位')
    return tags


def _q8_eligible(member_labor, member_age):
    """第8题（公益岗）人群条件：弱劳/半劳 或 60周岁以上"""
    lab = str(member_labor or '')
    weak = ('弱劳' in lab) or ('半劳' in lab)
    try:
        age = int(member_age or 0)
    except (TypeError, ValueError):
        age = 0
    return weak or age >= 60


def _survey_speech(member_no, member_name, answers, score, level):
    """问卷答案 → 平台端话术（人类可读 + 【意愿标签】机器行）"""
    a = answers or {}
    q1 = a.get('q1')
    name = member_name or ('成员%d' % member_no)
    lines = []
    if q1 == '在家务农':
        lines.append('%s更希望通过在家务农的方式增加家庭收入。' % name)
        if a.get('q2') == '种植':
            lines.append('%s更倾向于开展种植项目。' % name)
            if a.get('q3'):
                lines.append('针对种植需求，%s计划具体种植%s。' % (name, a['q3']))
        elif a.get('q2') == '养殖':
            lines.append('%s更倾向于开展养殖项目。' % name)
            if a.get('q4'):
                lines.append('针对养殖需求，%s计划具体养殖%s。' % (name, a['q4']))
        if a.get('q5'):
            if str(a.get('q5')) == '需要':
                lines.append('%s需要启动资金，愿意申请5万以内免息小额贷款。' % name)
            else:
                lines.append('%s不需要启动资金，自有资金充足。' % name)
    elif q1 == '创业':
        lines.append('%s更希望通过创业的方式增加家庭收入。' % name)
        if a.get('q5'):
            if str(a.get('q5')) == '需要':
                lines.append('%s需要启动资金，愿意申请5万以内免息小额贷款。' % name)
            else:
                lines.append('%s不需要启动资金，自有资金充足。' % name)
    elif q1 == '外出打工':
        lines.append('%s更希望通过外出打工的方式增加家庭收入。' % name)
        lines.append('%s选择外出打工，倾向于在%s地区务工。' % (name, a.get('q6') or '本省内'))
        if a.get('q7') == '想学':
            lines.append('%s想学技能（%s）。' % (name, a.get('q7_skill') or '烹饪/电焊等'))
        else:
            lines.append('%s暂不想学技能。' % name)
    elif q1 == '都不想':
        lines.append('%s不想通过上述方式增加家庭收入。' % name)
        if a.get('q8') == '愿意':
            lines.append('%s愿意从事村里的公益性岗位。' % name)
        else:
            lines.append('%s不愿意从事村里的公益性岗位。' % name)
    lines.append('意愿评分：%d分（%s意愿）。' % (score, level))
    tags = _survey_tags(a)
    if level == '低':
        tags.append('整户低意愿')
    body = '\n'.join(lines)
    return '【问卷结果】成员%d（%s）发展意愿问卷\n%s\n【意愿标签】%s' % (
        member_no, name, body, ';'.join(tags))


@bp.post('/survey/submit')
@login_required
def survey_submit():
    """提交成员问卷：算分/等级 → 生成话术 → 落库 survey_result（含户标识）。
    请求：{session_id, household_id, household_head, member_no, member_name, member_labor,
          member_age, answers:{q1..q8}}；返回 {speech, score, level, survey_id}"""
    data = request.get_json(force=True) or {}
    answers = dict(data.get('answers') or {})
    if not answers or not answers.get('q1'):
        return jsonify({'ok': False, 'msg': '缺少问卷答案（至少需回答第1题）'})
    member_no = int(data.get('member_no') or 1)
    member_name = (data.get('member_name') or '').strip()
    household_id = (data.get('household_id') or '').strip()
    household_head = (data.get('household_head') or '').strip()
    if data.get('session_id'):
        owned(data['session_id'], write=True, create=True)
    if data.get('session_id'):
        from household_state import latest
        hh = latest(data['session_id'], require_user().id)
        if hh:
            member = next((m for m in hh['members'] if m['member_no'] == member_no), None)
            if not member:
                raise ApiError('该成员不在当前家庭中，请核对成员序号')
            household_id = hh.get('household_id') or ('session-' + data['session_id'])
            household_head = hh.get('household_head') or ''
            member_name = member.get('name') or member_name
            data['member_labor'] = member['fields'].get('labor')
            data['member_age'] = member['fields'].get('age')
        else:
            household_id = household_id or ('session-' + data['session_id'])
    # 第8题人群校验：提供了劳动能力/年龄且不满足弱劳/半劳/60+ → 忽略第8题（不计分不加标签）
    if answers.get('q8') and not _q8_eligible(data.get('member_labor'), data.get('member_age')):
        answers['q8'] = None
    score, level = _survey_score(answers)
    speech = _survey_speech(member_no, member_name, answers, score, level)
    user = _login_user()
    row = SurveyResult(member_no=member_no, member_name=member_name or None,
                       household_id=household_id or None,
                       household_head=household_head or None,
                       answers=json.dumps(answers, ensure_ascii=False),
                       score=score, will_level=level, speech=speech,
                       session_id=(data.get('session_id') or '').strip() or None,
                       user_id=user.id if user else None,
                       updated_by=user.username if user else None)
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'speech': speech, 'score': score, 'level': level,
                    'survey_id': row.survey_id})


@bp.get('/survey/list')
@admin_required
def survey_list():
    """问卷结果记录（管理员可见，最新倒序前 100 条）"""
    rows = SurveyResult.query.order_by(SurveyResult.survey_id.desc()).limit(100).all()
    items = [{'survey_id': r.survey_id, 'member_no': r.member_no, 'member_name': r.member_name,
              'score': r.score, 'will_level': r.will_level, 'speech': r.speech,
              'session_id': r.session_id, 'updated_by': r.updated_by, 'created_at': r.created_at}
             for r in rows]
    return jsonify({'ok': True, 'items': items, 'total': len(items)})


@bp.get('/survey/households')
@admin_required
def survey_households():
    """农户意愿档案（按户聚合）：搜索/分页。
    参数：keyword(户ID/户主模糊)、page、size；返回整户评分(平均)与成员数。"""
    from sqlalchemy import text
    kw = (request.args.get('keyword') or '').strip()
    page = max(1, int(request.args.get('page', 1)))
    size = min(50, max(1, int(request.args.get('size', 10))))
    where = "WHERE (household_id IS NOT NULL OR household_head IS NOT NULL)"
    params = {}
    if kw:
        where += " AND (household_id LIKE :kw OR household_head LIKE :kw)"
        params['kw'] = '%' + kw + '%'
    sql = ("SELECT COALESCE(household_id,'') AS hid, COALESCE(household_head,'') AS head, "
           "COUNT(*) AS members, ROUND(AVG(score)) AS avg_score, MAX(survey_id) AS last_id "
           "FROM survey_result %s GROUP BY COALESCE(household_id,''), COALESCE(household_head,'') "
           "ORDER BY last_id DESC" % where)
    from sqlalchemy import create_engine
    import config
    eng = db.engines['auth']
    with eng.connect() as c:
        total = c.execute(text(
            "SELECT COUNT(*) FROM (SELECT 1 FROM survey_result %s GROUP BY COALESCE(household_id,''), COALESCE(household_head,'')) t" % where),
            params).scalar()
        rows = c.execute(text(sql + " LIMIT :lim OFFSET :off"),
                         dict(params, lim=size, off=(page - 1) * size)).fetchall()
    items = []
    for hid, head, members, avg_score, last_id in rows:
        level = None
        if avg_score is not None:
            avg_score = int(avg_score)
            level = '高' if avg_score >= 8 else ('中' if avg_score >= 5 else '低')
        items.append({'household_id': hid, 'household_head': head, 'member_count': members,
                      'avg_score': avg_score, 'will_level': level, 'last_id': last_id})
    eng.dispose()
    return jsonify({'ok': True, 'items': items, 'total': total, 'page': page, 'size': size})


@bp.get('/survey/detail')
@admin_required
def survey_detail():
    """某户意愿档案明细（成员级问卷记录）"""
    hid = (request.args.get('household_id') or '').strip()
    head = (request.args.get('head') or '').strip()
    if not hid and not head:
        return jsonify({'ok': False, 'msg': '缺少 household_id 或 head'})
    rows = SurveyResult.query.filter(
        db.or_(SurveyResult.household_id == (hid or '__none__'),
               SurveyResult.household_head == (head or '__none__'))
    ).order_by(SurveyResult.survey_id.asc()).all()
    items = [{'survey_id': r.survey_id, 'member_no': r.member_no, 'member_name': r.member_name,
              'household_id': r.household_id, 'household_head': r.household_head,
              'score': r.score, 'will_level': r.will_level, 'answers': r.answers,
              'speech': r.speech, 'updated_by': r.updated_by, 'created_at': r.created_at}
             for r in rows]
    return jsonify({'ok': True, 'items': items, 'total': len(items)})


# ---------------- 修改记录 ----------------
@bp.get('/changelog')
@admin_required
def changelog():
    limit = min(100, max(1, int(request.args.get('limit', 30))))
    items = [{'log_id': l.log_id, 'action': l.action, 'target_code': l.target_code,
              'target_name': l.target_name, 'detail': l.detail,
              'operator': l.operator, 'created_at': l.created_at}
             for l in AuditLog.query.order_by(AuditLog.log_id.desc()).limit(limit).all()]
    return jsonify({'ok': True, 'items': items})


# ---------------- 问答记录 ----------------




# ---------------- 用户管理（管理员建普通用户；普通用户登录仅进问答页） ----------------




# ---------------- 户匹配结果导出（对齐台账导出样式） ----------------
@bp.post('/qa/export')
@login_required
def qa_export():
    from result_snapshot import export
    data = request.get_json(silent=True) or {}
    if not data.get('qa_id'):
        raise ApiError('请选择已保存的回答后导出，缺少问答编号')
    last = db.session.get(QaLog, data['qa_id'])
    if not last:
        raise ApiError('问答记录不存在', 404)
    owned(last.session_id)
    if last.status != 'complete':
        raise ApiError('该回答尚未完成，不能导出')
    try:
        snapshot = json.loads(last.match_json or 'null')
        if isinstance(snapshot, dict) and snapshot.get('judge_status') == 'unavailable':
            raise ApiError('该回答的 AI 判定未完成，不能导出，请重新提问。', 409)
        bio = export(snapshot, last.answer, last.qa_id)
    except (ValueError, TypeError, KeyError) as exc:
        raise ApiError(str(exc), 409)
    return send_file(bio, as_attachment=True, download_name='帮扶匹配结果_%s.xlsx' % last.qa_id,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.post('/drafts/<int:did>/retry-index')
@admin_required
def retry_index(did):
    d = db.session.get(PolicyDraft, did)
    if not d or d.confirm_status != '已确认' or not d.policy_code:
        raise ApiError('没有已确认政策可重试', 404)
    p = Policy.query.filter_by(policy_code=d.policy_code).first()
    if not p or p.cur_version != d.version_no:
        raise ApiError('该草稿不是当前版本，不能覆盖新索引', 409)
    r = kb_rag.remove_policy(p.policy_code) if p.status == '停用' else _kb_sync_upsert(p, d)
    from policy_ingest import save_sync
    save_sync(d, r)
    return jsonify(ok=r.get('ok', False), kb_sync=r)


@bp.get('/policy-health')
@admin_required
def policy_health():
    from sqlalchemy import text
    from config import DEFAULT_POLICY_REGIONS
    rows = db.session.execute(text("SELECT p.policy_code,p.policy_name,p.level,p.regions,p.status, (SELECT count(*) FROM policy_detail d WHERE d.policy_id=p.policy_id) AS details, (SELECT count(*) FROM policy_vector v WHERE v.policy_code=p.policy_code AND v.status='在用') AS vectors FROM policy p WHERE p.status='在用'")).mappings().all()
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    current_vectors = {}
    if 'policy_version' in {col['name'] for col in inspector.get_columns('policy_vector')}:
        current_vectors = dict(db.session.execute(text("SELECT v.policy_code,count(*) FROM policy_vector v JOIN policy p ON p.policy_code=v.policy_code WHERE v.status='在用' AND v.policy_version=p.cur_version GROUP BY v.policy_code")).all())
    else:
        current_vectors = {r['policy_code']:r['vectors'] for r in rows}
    graph_codes = set()
    graph_available = inspector.has_table('kg_node')
    if graph_available:
        graph_codes = {r[0][7:] for r in db.session.execute(text("SELECT id FROM kg_node WHERE node_type='POLICY'"))}
    from graph_update_state import pending
    graph_pending = {r['policy_code'] for r in pending(db.session.connection())}
    items = []
    for row in rows:
        item = dict(row)
        item['regions'] = (item['regions'] or '').strip() or DEFAULT_POLICY_REGIONS.get(item['level'], '')
        item['current_vectors'] = current_vectors.get(item['policy_code'],0)
        item['graph_status'] = '待重建' if item['policy_code'] in graph_pending else ('已构建' if item['policy_code'] in graph_codes else '未构建')
        item['issues'] = []
        if not item['regions']: item['issues'].append('地区待补充')
        if not item['details']: item['issues'].append('政策明细待确认')
        if not item['current_vectors']: item['issues'].append('当前版本索引未就绪')
        if graph_available and item['graph_status'] != '已构建': item['issues'].append('图谱'+item['graph_status'])
        if item['issues']:
            items.append(item)
    return jsonify(ok=True, items=items, total=len(items))


# ---------------- 知识图谱展示（只读） ----------------

def _graph_sync_status(code):
    from graph_update_state import status
    return status(db.session.connection(), code)


@bp.get('/kg/update-status')
@admin_required
def graph_update_status():
    return jsonify(ok=True, graph_sync=_graph_sync_status(request.args.get('code', '')))


@bp.get('/kg/edge-evidence')
@admin_required
def graph_edge_evidence():
    from sqlalchemy import text
    from kg_source_update import schema_ready
    c=db.session.connection()
    if not schema_ready(c):
        return jsonify(ok=False,msg='图谱尚未启用明细溯源'),409
    rows=c.execute(text('SELECT source_field,evidence,provenance FROM kg_edge WHERE src_id=:src AND dst_id=:dst AND edge_type=:et'),
        {'src':request.args.get('src'),'dst':request.args.get('dst'),'et':request.args.get('type')}).mappings().all()
    return jsonify(ok=True,items=[dict(r) for r in rows])


@bp.post('/kg/retry-update')
@admin_required
def graph_retry_update():
    from graph_update_state import mark_pending
    from kg_source_update import request_update
    from kg_retrieval import invalidate_cache
    code=(request.get_json(silent=True) or {}).get('code')
    p=Policy.query.filter_by(policy_code=code).first()
    if not p:
        return jsonify(ok=False,msg='政策不存在'),404
    mark_pending(db.session,p,'管理员重试图谱更新')
    db.session.commit()
    invalidate_cache()
    request_update()
    return jsonify(ok=True,graph_sync=_graph_sync_status(code))


@bp.get('/kg/stats')
@admin_required
def kg_stats():
    """图谱规模统计：节点/边类型分布、SAME_DOC/SIM_ATTR 数量、相似度分布、孤立政策数"""
    import kg_graph
    try:
        return jsonify(ok=True, stats=kg_graph.graph_stats())
    except Exception as e:  # 图谱未构建/加载失败时返回可读错误
        return jsonify(ok=False, msg='图谱数据不可用：%s' % e), 500


@bp.get('/kg/policies')
@admin_required
def kg_policies():
    """图谱起点搜索：政策编号/名称/文号模糊，返回前 20 条供前端选择起点"""
    import kg_graph
    return jsonify(ok=True, items=kg_graph.search_policies(request.args.get('kw') or '', 20))


@bp.get('/kg/graph')
@admin_required
def kg_graph_view():
    """政策子图导出：?node=POLICY:S-ZB-090&hops=2&limit=300
    node 支持 POLICY:/RISK:/REGION:/CATEGORY:/POPULATION:/CONDITION:/FILE: 前缀节点 id"""
    import kg_graph
    node = (request.args.get('node') or '').strip()
    if not node:
        return jsonify(ok=False, msg='缺少 node 参数'), 400
    try:
        hops = int(request.args.get('hops', 2))
        limit = min(500, max(50, int(request.args.get('limit', 300))))
    except ValueError:
        hops, limit = 2, 300
    g = kg_graph.build_subgraph(node, hops=hops, limit=limit)
    if g is None:
        return jsonify(ok=False, msg='节点不存在：%s（图谱可能未构建或编号错误）' % node), 404
    return jsonify(ok=True, seed=node, hops=hops,
                   nodes=g['nodes'], edges=g['edges'],
                   total_nodes=len(g['nodes']), total_edges=len(g['edges']),
                   truncated=g.get('truncated',False), omitted_edges=g.get('omitted_edges',0),
                   omitted_nodes=g.get('omitted_nodes',0))


@bp.get('/kg/nodes')
@admin_required
def kg_nodes():
    """按节点类型浏览起点（?type=RISK|CATEGORY|POPULATION|REGION|CONDITION&kw=），带政策关联数"""
    import kg_graph
    ntype = (request.args.get('type') or '').strip() or None
    kw = (request.args.get('kw') or '').strip() or None
    return jsonify(ok=True, items=kg_graph.list_nodes(node_type=ntype, kw=kw, limit=100))


# ---------------- 政策预览（只读，前端卡片抽屉） ----------------

@bp.get('/policy/preview')
@login_required
def policy_preview():
    """政策卡片预览：主表 + 明细 + 原文摘录（只读，不落库不改会话）。

    参数：code 必填；detail_id 可选（需属于该政策）；county 可选（县区名，用于挑选属地明细）。
    明细优先级：detail_id > county 匹配 > 全省/省级 > 第一条；编号不存在 → 404 {"error": "policy_not_found"}。
    响应顶层 6 键：policy / detail / details / excerpt / excerpt_truncated / excerpt_scope。
    """
    code = (request.args.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'missing_code'}), 400
    raw_detail_id = (request.args.get('detail_id') or '').strip()
    detail_id = None
    if raw_detail_id:
        try:
            detail_id = int(raw_detail_id)
        except ValueError:
            return jsonify({'error': 'invalid_detail_id'}), 400
    county = (request.args.get('county') or '').strip() or None
    from policy_preview import build_preview
    try:
        return jsonify(build_preview(code, detail_id, county))
    except LookupError:
        return jsonify({'error': 'policy_not_found'}), 404
    except Exception:
        current_app.logger.exception('政策预览失败 code=%s detail_id=%s county=%s', code, detail_id, county)
        return jsonify({'error': 'preview_failed'}), 500


# ---------------- 政策生命周期（T18；默认关闭，POLICY_LIFECYCLE=1 才启用） ----------------
# 只追加新路由，不改既有路由逻辑与返回；关闭时统一 404，现网行为与本次改动前一致。
_LIFECYCLE_DISABLED = {
    'ok': False,
    'code': 'policy_lifecycle_disabled',
    'msg': '政策生命周期接口未启用（POLICY_LIFECYCLE=0）',
}


def _policy_lifecycle_enabled():
    from policy_core import lifecycle
    return lifecycle.enabled()


def _policy_lifecycle_disabled():
    return jsonify(_LIFECYCLE_DISABLED), 404


def _policy_lifecycle_body():
    """写操作请求体：reason 必填 >=4；operator 缺省取当前登录管理员。"""
    data = request.get_json(silent=True) or {}
    reason = str(data.get('reason') or '').strip()
    if len(reason) < 4:
        raise ApiError('reason 必填且不少于 4 个字符', 400, 'invalid_reason')
    user = _login_user()
    operator = str(data.get('operator') or '').strip() or str(getattr(user, 'username', '') or '')
    if not operator:
        raise ApiError('无法确定操作人', 400, 'missing_operator')
    return data, reason, operator


def _policy_lifecycle_run(call):
    """生命周期写操作：同一事务内 ensure_schema + 业务写入，成功 commit，异常 rollback。"""
    from policy_core import lifecycle
    try:
        conn = db.session.connection()
        lifecycle.ensure_schema(conn)
        result = call(conn)
        db.session.commit()
        return jsonify(result)
    except lifecycle.LifecycleError as exc:
        db.session.rollback()
        return jsonify(ok=False, msg=exc.message, code=exc.code), exc.status
    except Exception:
        db.session.rollback()
        current_app.logger.exception('政策生命周期操作失败')
        return jsonify(ok=False, msg='政策生命周期操作失败，未确认完成',
                       code='lifecycle_failed'), 500


def _policy_lifecycle_auth_conn():
    """qa_log 在认证库；取不到 auth bind 时返回 None，impact/硬删会按不可确认保守处理。"""
    try:
        return db.session.get_bind(bind_key='auth').connect()
    except Exception:
        return None


def _policy_lifecycle_close(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


@bp.post('/policies/<code>/suspend')
@admin_required
def policy_lifecycle_suspend(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    data, reason, operator = _policy_lifecycle_body()
    from policy_core import lifecycle
    return _policy_lifecycle_run(
        lambda conn: lifecycle.suspend(conn, code, reason, operator,
                                       dry_run=bool(data.get('dry_run'))))


@bp.post('/policies/<code>/archive')
@admin_required
def policy_lifecycle_archive(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    data, reason, operator = _policy_lifecycle_body()
    from policy_core import lifecycle
    return _policy_lifecycle_run(
        lambda conn: lifecycle.archive(conn, code, reason, operator,
                                       dry_run=bool(data.get('dry_run'))))


@bp.post('/policies/<code>/restore')
@admin_required
def policy_lifecycle_restore(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    data, reason, operator = _policy_lifecycle_body()
    from policy_core import lifecycle
    return _policy_lifecycle_run(
        lambda conn: lifecycle.restore(conn, code, reason, operator,
                                       dry_run=bool(data.get('dry_run')),
                                       confirm=bool(data.get('confirm'))))


@bp.post('/policies/<code>/delete')
@admin_required
def policy_lifecycle_delete(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    data, reason, operator = _policy_lifecycle_body()
    from policy_core import lifecycle

    def call(conn):
        auth_conn = _policy_lifecycle_auth_conn()
        try:
            return lifecycle.delete_policy(conn, code, reason, operator,
                                           dry_run=bool(data.get('dry_run')),
                                           hard=bool(data.get('hard')),
                                           auth_conn=auth_conn)
        finally:
            _policy_lifecycle_close(auth_conn)

    return _policy_lifecycle_run(call)


@bp.get('/policies/<code>/impact')
@admin_required
def policy_lifecycle_impact(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    from policy_core import lifecycle
    auth_conn = _policy_lifecycle_auth_conn()
    try:
        result = lifecycle.impact(db.session.connection(), code, auth_conn=auth_conn)
        return jsonify(ok=True, **result)
    except lifecycle.LifecycleError as exc:
        return jsonify(ok=False, msg=exc.message, code=exc.code), exc.status
    except Exception:
        current_app.logger.exception('政策影响预检失败 code=%s', code)
        return jsonify(ok=False, msg='政策影响预检失败', code='lifecycle_failed'), 500
    finally:
        _policy_lifecycle_close(auth_conn)


@bp.get('/policies/<code>/versions')
@admin_required
def policy_lifecycle_versions(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    from policy_core import lifecycle
    try:
        items = lifecycle.versions(db.session.connection(), code)
        return jsonify(ok=True, policy_code=code, items=items)
    except lifecycle.LifecycleError as exc:
        return jsonify(ok=False, msg=exc.message, code=exc.code), exc.status
    except Exception:
        current_app.logger.exception('政策版本链读取失败 code=%s', code)
        return jsonify(ok=False, msg='政策版本链读取失败', code='lifecycle_failed'), 500


@bp.post('/policies/<code>/publish')
@admin_required
def policy_lifecycle_publish(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    data, reason, operator = _policy_lifecycle_body()
    version_no = str(data.get('version_no') or '').strip()
    if not version_no:
        raise ApiError('version_no 必填', 400, 'missing_version')
    from policy_core import lifecycle
    return _policy_lifecycle_run(
        lambda conn: lifecycle.publish(conn, code, version_no, operator, reason=reason,
                                       dry_run=bool(data.get('dry_run'))))


@bp.post('/policies/<code>/rollback')
@admin_required
def policy_lifecycle_rollback(code):
    if not _policy_lifecycle_enabled():
        return _policy_lifecycle_disabled()
    data, reason, operator = _policy_lifecycle_body()
    version_no = str(data.get('version_no') or '').strip()
    if not version_no:
        raise ApiError('version_no 必填', 400, 'missing_version')
    from policy_core import lifecycle
    return _policy_lifecycle_run(
        lambda conn: lifecycle.rollback(conn, code, version_no, operator, reason=reason,
                                        dry_run=bool(data.get('dry_run'))))

# ---------------- 政策批量导入（T22；默认关闭，POLICY_IMPORT=1 才启用） ----------------
# 纯追加：关闭时统一 404，现网行为与改动前一致。提交/回滚都在同一条数据库事务内完成，
# 任何异常都会 rollback，不留半批数据；预览只读，绝不写 import_batch 或政策表。
_IMPORT_DISABLED = {
    'ok': False,
    'code': 'policy_import_disabled',
    'msg': '政策批量导入接口未启用（POLICY_IMPORT=0）',
}

_IMPORT_FILE_SUFFIXES = ('.csv', '.xlsx', '.json')
_IMPORT_NOTE_ACTION = '导入操作备注'


def _policy_import_enabled():
    from policy_core import policy_import
    return policy_import.enabled()


def _policy_import_disabled():
    return jsonify(_IMPORT_DISABLED), 404


def _import_failure(exc, action):
    """路由内异常统一转 JSON：ApiError/ValueError 保留语义，其余 500 并回滚。"""
    db.session.rollback()
    if isinstance(exc, ApiError):
        return jsonify(ok=False, msg=exc.message, code=exc.code), exc.status
    if isinstance(exc, ValueError):
        return jsonify(ok=False, msg=str(exc), code='import_invalid'), 400
    current_app.logger.exception('政策导入%s失败', action)
    return jsonify(ok=False, msg='政策导入%s失败，未确认完成' % action,
                   code='import_failed'), 500


def _import_operator_from(payload):
    """操作人：请求字段优先，缺省取当前登录管理员（与生命周期路由一致）。"""
    payload = payload if isinstance(payload, dict) else {}
    user = _login_user()
    operator = str(payload.get('operator') or '').strip() or str(getattr(user, 'username', '') or '')
    if not operator:
        raise ApiError('无法确定操作人', 400, 'missing_operator')
    return operator


def _import_reason_from(payload):
    """可选原因/备注；截断到 500 字写审计，不参与导入判定。"""
    payload = payload if isinstance(payload, dict) else {}
    return str(payload.get('reason') or payload.get('note') or '').strip()[:500]


def _import_flag(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _import_request_rows():
    """解析导入请求：multipart 文件（csv/xlsx/json）或 JSON 行数组/{"rows": [...]}。

    返回 (rows, source, file_name, operator_field, reason, dry_run)；上传临时目录在返回前清理。
    JSON 行数组与文件二选一，避免同一次请求出现两份来源不一致的数据。
    """
    from policy_core import policy_import
    if request.files:
        upload = request.files.get('file') or next(iter(request.files.values()))
        file_name = str(upload.filename or '').strip()
        suffix = os.path.splitext(file_name)[1].lower()
        if suffix not in _IMPORT_FILE_SUFFIXES:
            raise ApiError('仅支持 CSV / XLSX / JSON 文件', 400, 'invalid_file_type')
        import shutil
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix='policy-import-api-')
        try:
            path = os.path.join(tmpdir, 'upload' + suffix)
            upload.save(path)
            rows = policy_import.load_rows(path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        form = request.form
        return (rows, str(form.get('source') or '').strip(), file_name,
                str(form.get('operator') or '').strip(),
                _import_reason_from({'reason': form.get('reason'),
                                     'note': form.get('note')}),
                _import_flag(form.get('dry_run')))
    data = request.get_json(silent=True)
    if isinstance(data, list):
        payload, rows = {}, data
    elif isinstance(data, dict):
        payload, rows = data, data.get('rows')
    else:
        raise ApiError('请求体需为行数组或 {"rows": [...]}', 400, 'invalid_rows')
    if not isinstance(rows, list) or not rows:
        raise ApiError('rows 不能为空，且每条必须是对象', 400, 'invalid_rows')
    if not all(isinstance(row, dict) for row in rows):
        raise ApiError('rows 每条必须是对象', 400, 'invalid_rows')
    return (rows, str(payload.get('source') or '').strip(),
            str(payload.get('file_name') or '').strip(),
            str(payload.get('operator') or '').strip(),
            _import_reason_from(payload),
            _import_flag(payload.get('dry_run')))


def _import_json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _import_batch_row(conn, batch_id):
    """只读批次头；不存在返回 None（调用方需先 ensure_schema）。"""
    from sqlalchemy import text as _text
    row = conn.execute(_text('SELECT * FROM "import_batch" WHERE "id" = :id'),
                       {'id': batch_id}).mappings().first()
    return dict(row) if row else None


def _import_batch_not_found():
    return jsonify(ok=False, code='batch_not_found', msg='导入批次不存在'), 404


def _import_table_exists(conn, table):
    """只读探测表是否存在，避免 PG 事务被错误语句打断后只能整事务回滚。"""
    from sqlalchemy import text as _text
    try:
        if conn.dialect.name == 'sqlite':
            return conn.execute(
                _text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                {'name': table}).scalar() is not None
        return conn.execute(
            _text('SELECT 1 FROM information_schema.tables '
                  'WHERE table_schema=current_schema() AND table_name=:name'),
            {'name': table}).scalar() is not None
    except Exception:
        return False


def _import_write_note(conn, batch_id, operator, reason):
    """可选原因/备注写 audit_log（表存在时）；失败只记日志，不影响批次结果。"""
    if not reason or not _import_table_exists(conn, 'audit_log'):
        return False
    from sqlalchemy import text as _text
    savepoint = None
    try:
        # 用 savepoint 包住备注审计：即使审计表结构异常，也不会拖垮导入主事务。
        savepoint = conn.begin_nested()
        conn.execute(_text('INSERT INTO "audit_log" ("action","target_code","target_name",'
                           '"detail","operator","created_at") VALUES '
                           '(:action,:code,:name,:detail,:operator,:at)'),
                     {'action': _IMPORT_NOTE_ACTION, 'code': batch_id, 'name': '',
                      'detail': reason, 'operator': operator, 'at': _now()})
        savepoint.commit()
        return True
    except Exception:
        try:
            if savepoint is not None:
                savepoint.rollback()
        except Exception:
            pass
        current_app.logger.exception('政策导入备注写审计失败 batch_id=%s', batch_id)
        return False


@bp.post('/policies/import/preview')
@admin_required
def policy_import_preview():
    if not _policy_import_enabled():
        return _policy_import_disabled()
    from policy_core import policy_import
    try:
        rows, source, file_name, _operator, _reason, _dry_run = _import_request_rows()
        report = policy_import.preview(db.session.connection(), rows,
                                       source=source, file_name=file_name)
        return jsonify(ok=True, **report)
    except Exception as exc:
        return _import_failure(exc, '预览')
    finally:
        # 预览只读：释放隐式读事务，绝不 commit 任何东西。
        try:
            db.session.rollback()
        except Exception:
            pass


@bp.post('/policies/import')
@admin_required
def policy_import_create():
    if not _policy_import_enabled():
        return _policy_import_disabled()
    from policy_core import policy_import
    try:
        rows, source, file_name, operator_field, reason, dry_run = _import_request_rows()
        operator = _import_operator_from({'operator': operator_field})
        conn = db.session.connection()
        policy_import.ensure_schema(conn)
        created = policy_import.create_batch(conn, rows, source=source, file_name=file_name,
                                             operator=operator, dry_run=dry_run)
        report = policy_import.validate_batch(conn, created['batch_id'])
        note_audited = _import_write_note(conn, created['batch_id'], operator, reason)
        db.session.commit()
        return jsonify(ok=True, note_audited=note_audited, **report)
    except Exception as exc:
        return _import_failure(exc, '创建批次')


@bp.post('/policies/import/<batch_id>/commit')
@admin_required
def policy_import_commit(batch_id):
    if not _policy_import_enabled():
        return _policy_import_disabled()
    from policy_core import policy_import
    try:
        body = _import_json_body()
        operator = _import_operator_from(body)
        reason = _import_reason_from(body)
        conn = db.session.connection()
        policy_import.ensure_schema(conn)
        if not _import_batch_row(conn, batch_id):
            db.session.rollback()
            return _import_batch_not_found()
        report = policy_import.commit(conn, batch_id, operator=operator)
        note_audited = _import_write_note(conn, batch_id, operator, reason)
        db.session.commit()
        return jsonify(ok=True, note_audited=note_audited, **report)
    except Exception as exc:
        return _import_failure(exc, '提交')


@bp.post('/policies/import/<batch_id>/rollback')
@admin_required
def policy_import_rollback(batch_id):
    if not _policy_import_enabled():
        return _policy_import_disabled()
    from policy_core import policy_import
    try:
        body = _import_json_body()
        operator = _import_operator_from(body)
        reason = _import_reason_from(body)
        conn = db.session.connection()
        policy_import.ensure_schema(conn)
        if not _import_batch_row(conn, batch_id):
            db.session.rollback()
            return _import_batch_not_found()
        result = policy_import.rollback(conn, batch_id, operator=operator)
        note_audited = _import_write_note(conn, batch_id, operator, reason)
        db.session.commit()
        return jsonify(ok=True, note_audited=note_audited, **result)
    except Exception as exc:
        return _import_failure(exc, '回滚')


@bp.get('/policies/import/<batch_id>')
@admin_required
def policy_import_report(batch_id):
    if not _policy_import_enabled():
        return _policy_import_disabled()
    from policy_core import policy_import
    try:
        conn = db.session.connection()
        policy_import.ensure_schema(conn)
        if not _import_batch_row(conn, batch_id):
            db.session.rollback()
            return _import_batch_not_found()
        report = policy_import.batch_report(conn, batch_id)
        return jsonify(ok=True, **report)
    except Exception as exc:
        return _import_failure(exc, '读取报告')
    finally:
        # 读接口：ensure_schema 的 IF NOT EXISTS 也随事务回滚，不产生任何持久写入。
        try:
            db.session.rollback()
        except Exception:
            pass

