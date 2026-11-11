"""Document parsing and ledger details. No eligibility facts are fabricated."""
import json
from pathlib import Path
from sqlalchemy import text


def document_text(path):
    suffix = Path(path).suffix.lower()
    if suffix == '.pdf':
        from recognize import extract_pdf_text
        content = extract_pdf_text(path)
        if len(content.strip()) < 30:
            raise ValueError('PDF 无可读取文字，可能为扫描件；请先 OCR 转为带文字层 PDF 再上传')
        return content
    if suffix == '.xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            lines = []
            for ws in wb:
                lines.append('工作表：' + ws.title)
                for row in ws.iter_rows(values_only=True):
                    if any(v is not None for v in row):
                        lines.append(' | '.join(str(v or '') for v in row))
                    if len(lines) > 20000:
                        raise ValueError('表格超过 20000 行，请拆分后上传')
            return '\n'.join(lines)
        finally:
            wb.close()
    if suffix == '.csv':
        import csv
        raw = Path(path).read_bytes()
        try:
            value = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            value = raw.decode('gb18030')
        return '\n'.join(' | '.join(row) for row in csv.reader(value.splitlines()))
    if suffix == '.docx':
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET
        with ZipFile(path) as z:
            root = ET.fromstring(z.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return '\n'.join(''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in root.findall('.//w:p', ns))
    raise ValueError('支持 PDF、XLSX、CSV、DOCX；旧版 XLS/DOC 请另存为新格式')


def prepare(draft):
    if str(draft.policy_name or '').strip() in ('无','措施/项目','政策名称'):
        raise ValueError('政策名称是模板或占位值，请根据政策原件确认名称后入库')
    from recognize import llm_extract, load_model_cfg
    fields = dict(draft.draft_data or {})
    if not fields.get('details'):
        fields.update(llm_extract(fields.get('document_text') or document_text(draft.pdf_path), load_model_cfg()))
    details = fields.get('details')
    if not isinstance(details, list) or not details:
        raise ValueError('未识别出政策条件明细，请在草稿中补充 details 后确认')
    from kg_source_update import is_placeholder
    candidate = dict(fields, policy_name=draft.policy_name, doc_ref=draft.doc_ref)
    if is_placeholder(candidate, details):
        raise ValueError('政策包含名称、文号、条件、流程模板占位，请根据原件确认后入库')
    for row in details:
        if not isinstance(row, dict) or not (row.get('conditions') or row.get('target')):
            raise ValueError('政策明细缺少适用对象或申请条件，不能作为可匹配政策入库')
    if not (draft.regions or '').strip():
        import config
        from models import db, Policy
        existing = db.session.get(Policy, draft.match_policy_id) if draft.match_policy_id else None
        draft.regions = ((existing.regions or '').strip() if existing else '') or config.DEFAULT_POLICY_REGIONS.get(draft.level, '')
    if not draft.regions:
        raise ValueError('适用地区缺失，请先补充后入库')
    from datetime import date
    for key in ('effect_date', 'expire_date'):
        if fields.get(key):
            date.fromisoformat(str(fields[key]))
    if fields.get('effect_date') and fields.get('expire_date') and fields['expire_date'] < fields['effect_date']:
        raise ValueError('失效日期不能早于生效日期')
    draft.draft_data = fields
    return fields


def write_details(policy, draft):
    from models import db
    fields = draft.draft_data or {}
    from graph_update_state import mark_pending
    mark_pending(db.session, policy, '政策或明细更新，等待从确认来源重建图谱')
    for key in ('target', 'conditions', 'benefit', 'process', 'contact', 'effect_date', 'expire_date'):
        if key in fields:
            setattr(policy, key, str(fields.get(key) or ''))
    # Confirmed extraction replaces previous details in the same ledger transaction.
    db.session.execute(text('DELETE FROM policy_detail WHERE policy_id=:pid'), {'pid': policy.policy_id})
    # Preserve old vectors until replacement succeeds; version checks hide stale evidence.
    for row in fields['details']:
        extra = {k: row.get(k) or '' for k in ('lab', 'health', 'age', 'pov')}
        extra.update(doc=draft.doc_ref or '', conditions=row.get('conditions') or '', target=row.get('target') or '')
        if row.get('unit') or row.get('cycle') or row.get('currency'):
            extra['benefit_source']={k:row.get(k) or '' for k in ('unit','cycle','currency')}
        db.session.execute(text('INSERT INTO policy_detail (policy_id,region,benefit,process,contact,phone,target,conditions,extra) VALUES (:pid,:region,:benefit,:process,:contact,:phone,:target,:conditions,:extra)'),
            dict(pid=policy.policy_id, region=row.get('region') or draft.regions,
                 benefit=row.get('benefit') or fields.get('benefit') or '',
                 process=row.get('process') or fields.get('process') or '',
                 contact=row.get('contact') or fields.get('contact') or '', phone=row.get('phone') or '',
                 target=row.get('target') or '', conditions=row.get('conditions') or '', extra=json.dumps(extra, ensure_ascii=False)))


def save_sync(draft, result):
    from models import db
    data = dict(draft.draft_data or {})
    data['index_status'] = 'ready' if result.get('ok') else 'failed'
    data['index_message'] = result.get('msg') or ''
    draft.draft_data = data
    db.session.commit()
    return result
