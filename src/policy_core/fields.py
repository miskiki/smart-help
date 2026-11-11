"""Semantic field dictionary (data), shared by extraction, rules and questions."""
import json
from pathlib import Path

def dictionary():
    return json.loads((Path(__file__).parent/'fields.json').read_text('utf8'))

def model_fields(fields,direction=False,inference=False):
    keys={'scope','type','label','meaning','unit','values','min','max','recognized_identity','household_evidence_terms'}
    if direction:keys.update(('direction_allowed','direction_values','direction_min','source_terms_all'))
    if inference:keys.update(('inference_notes','value_evidence_patterns'))
    return {name:{k:v for k,v in definition.items() if k in keys} for name,definition in fields.items()}

def direction_value(value,definition):
    """Dictionary polarity, with bool/number kept distinct (False is not 0)."""
    if value is None or definition.get('direction_allowed') is False:return False
    if 'direction_values' in definition:
        return any(type(value) is type(v) and value==v for v in definition['direction_values'])
    if type(value) is bool:return value
    if type(value) in (int,float):return value>=definition['direction_min'] if 'direction_min' in definition else value>0
    return isinstance(value,str) and value.strip() not in ('','无','未知','不清楚')

def semantic_id(definition):
    import hashlib
    keys={'scope','type','meaning','unit','values','min','max','relative_to'}
    payload={k:v for k,v in definition.items() if k in keys}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode('utf8')).hexdigest()

def valid_value(value, definition):
    if value is None:return True
    kind=definition['type']
    if kind=='boolean':return type(value) is bool
    if kind=='number':
        import math
        return type(value) in (int,float) and math.isfinite(value) and definition.get('min',0)<=value<=definition.get('max',1e12)
    if not isinstance(value,str) or not value.strip() or len(value)>500:return False
    return value in definition['values'] if definition.get('values') else True

def merge_definitions(base, additions):
    import re
    out=dict(base)
    for name,d in additions.items():
        if name in out:
            if d!=out[name]:raise ValueError('field_definition_conflict:'+name)
            continue
        if set(d)-{'scope','type','label','meaning','question','unit','values','min','max'}:raise ValueError('unsupported_automatic_field_metadata')
        if not re.fullmatch(r'[a-z][a-z0-9_]{2,60}',name):raise ValueError('invalid_field_name')
        if d.get('scope') not in ('member','household') or d.get('type') not in ('boolean','number','string'):raise ValueError('invalid_field_definition')
        if d.get('implies_true'):raise ValueError('automatic_fields_cannot_add_unverified_inference')
        if not all(isinstance(d.get(k),str) and d[k].strip() for k in ('label','meaning','question')):raise ValueError('missing_field_semantics')
        if len(json.dumps(d,ensure_ascii=False))>2000:raise ValueError('field_definition_too_large')
        out[name]=d
    return out
