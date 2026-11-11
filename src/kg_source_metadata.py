"""Recover units/cycles from exact-matching original workbook rows, never infer them."""
import json
import re
from collections import defaultdict
from kg_normalization import normalize_label


def clean(v):
    s=re.sub(r'\s+','',str(v if v is not None else ''))
    return re.sub(r'^(\d+)\.0$',r'\1',s)


def describe_benefit(raw, extra):
    raw=str(raw or '').strip()
    metadata=extra.get('benefit_source') or {}
    if re.fullmatch(r'[\d\s.,，；;—\-]+',raw) and all(metadata.get(k) for k in ('currency','unit','cycle')):
        return raw+metadata['currency']+'；计量单位：'+metadata['unit']+'；周期：'+metadata['cycle']
    return raw


def match_key(detail, extra):
    return (clean(detail.get('region')),normalize_label('DOC',extra.get('doc')),
            normalize_label('POPULATION',detail.get('target')),
            *(clean(detail.get(k)) for k in ('conditions','benefit','process','contact','phone')),
            *(clean(extra.get(k)) for k in ('lab','health','age','pov')))


def recover(details, path):
    from openpyxl import load_workbook
    from kg_source_update import extra_of
    wb=load_workbook(path,read_only=True,data_only=True)
    index=defaultdict(list)
    mapping={'region':'政策适用范围','target':'政策适用对象','conditions':'政策适用条件',
        'benefit':'补贴标准/元','process':'申领流程','contact':'政策联系单位/联系人','phone':'联系电话'}
    emapping={'doc':'政策文件名称及文号','lab':'适用劳动能力类型','health':'适用健康状况',
        'age':'适用劳动力年龄','pov':'适用致贫返贫类型'}
    try:
        for sheet in wb:
            headers=None
            for number,row in enumerate(sheet.iter_rows(values_only=True),1):
                if headers is None:
                    if '补贴标准/元' in row and '政策适用条件' in row:
                        headers=[str(x or '') for x in row]
                    continue
                data=dict(zip(headers,row))
                d={k:data.get(v) for k,v in mapping.items()}
                ex={k:data.get(v) for k,v in emapping.items()}
                index[match_key(d,ex)].append({'currency':'元','unit':str(data.get('补贴单位') or '').strip(),
                    'cycle':str(data.get('补贴周期') or '').strip(),
                    'source_file':path.name,'source_sheet':sheet.title,'source_row':number})
    finally:
        wb.close()
    recovered={}
    ambiguous=[]
    for d in details:
        matches=index.get(match_key(d,extra_of(d)),[])
        if not matches:continue
        identity={(r['currency'],r['unit'],r['cycle']) for r in matches}
        if len(identity)!=1:
            ambiguous.append(d['detail_id']);continue
        recovered[d['detail_id']]=dict(matches[0],source_rows=[m['source_row'] for m in matches])
    return recovered,ambiguous
