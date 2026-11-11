"""Split policy tables before extraction so a policy's index cannot contain other policies."""
import csv,io
from pathlib import Path


def table_documents(path):
    suffix = Path(path).suffix.lower()
    sheets = []
    if suffix == '.xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in wb:
                sheets.append(list(sheet.iter_rows(values_only=True)))
        finally:
            wb.close()
    elif suffix == '.csv':
        raw = Path(path).read_bytes()
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = raw.decode('gb18030')
        sheets = [list(csv.reader(io.StringIO(content)))]
    else:
        raise ValueError('不是政策表格文件')
    groups = {}
    identities = {}
    for rows in sheets:
        headers = None
        for row in rows:
            values = [str(v).strip() if v is not None else '' for v in row]
            if headers is None:
                if '政策名称' in values:
                    headers = values
                continue
            data = {key:value for key,value in zip(headers,values) if key and value}
            name = data.get('政策名称')
            if not name:
                continue
            code = (data.get('政策编号') or '').strip().upper()
            if code and code in identities and identities[code] != name:
                raise ValueError('同一政策编号%s对应不同名称，请核对表格' % code)
            if code:
                identities[code] = name
            key = (name, data.get('文号') or data.get('文件文号') or '', data.get('政策编号') or '')
            groups.setdefault(key, []).append(data)
            if sum(len(g) for g in groups.values()) > 500:
                raise ValueError('表格超过500条明细，请拆分上传')
    if not groups:
        raise ValueError('表格需包含“政策名称”列，建议同时提供层级、一级分类、适用地区、申请条件、补贴标准和办理流程')
    if len(groups) > 50:
        raise ValueError('一次最多识别50项政策，请拆分上传')
    return [(key[0], '\n\n'.join('政策明细%d\n%s' % (i+1,'\n'.join('%s：%s' % (k,v) for k,v in row.items())) for i,row in enumerate(rows))) for key,rows in groups.items()]


def recognize_many(path):
    from recognize import recognize_pdf
    import re
    records = []
    for name, body in table_documents(path):
        # Table identity is supplied by the operator, never rewritten by the model.
        fields = {'policy_name': name, 'doc_ref': ''}
        for label, key in [('政策编号','policy_code'),('层级','level'),('一级分类','category1'),('适用地区','regions'),('状态','status'),('文号','doc_ref'),('文件文号','doc_ref')]:
            values = set(re.findall(r'^' + label + r'：([^\n]+)', body, re.M))
            if len(values) > 1:
                raise ValueError('同一政策的%s存在冲突，请拆分或核对表格' % label)
            if values:
                fields[key] = values.pop()
        records.append(recognize_pdf(path, document=body, input_fields=fields))
    return records
