"""Transport capabilities, not business rules or a model-platform switch."""
from urllib.parse import urlparse


def request_options(cfg,purpose):
    if not (purpose or '').startswith(('core_','policy_','showcase_')):return {}
    host=urlparse(cfg.get('base_url','')).hostname or ''
    official=host=='dashscope.aliyuncs.com' or host.endswith('.dashscope.aliyuncs.com') or host.endswith('.maas.aliyuncs.com')
    supported=cfg.get('json_mode') is True or official and str(cfg.get('llm_model','')).startswith(('qwen-plus','qwen-turbo','qwen-max'))
    if cfg.get('json_mode') is False or not supported:return {}
    options=dict(response_format=dict(type='json_object'))
    # P0-3/P1-2：core_full_reason 输出上限 2200→6000。2200 时代批量组（2 来源/组）结论 JSON
    # 常被截断丢弃；P1-2 将批次扩到 4 来源后，4 组结论约需 3-4K tokens，6000 留足余量。
    if (purpose or '').startswith('core_full_reason'):options['max_tokens']=6000
    if purpose=='core_full_fact_audit':options['max_tokens']=1800
    return options


FACT_FLAGS=('object_supported','matches_supported','conflicts_supported','questions_supported','coverage_supported','source_gaps_supported')
FACT_WIRE=('o','m','c','q','v','g')

def fact_audit_schema(ids):
    properties=dict(id=dict(enum=ids),**{key:dict(type='boolean') for key in FACT_WIRE},
                    e=dict(type='array',items=dict(type='string')),b=dict(type='array',items=dict(type='object')))
    item=dict(type='object',properties=properties,required=list(properties),additionalProperties=False)
    return dict(type='object',properties=dict(items=dict(type='array',items=item,minItems=len(ids),maxItems=len(ids))),required=['items'],additionalProperties=False)

def decode_fact_audit(response):
    rows=[]
    for item in response.get('items',[]):
        if not isinstance(item,dict) or any(type(item.get(key)) is not bool for key in FACT_WIRE):raise ValueError('invalid_compact_fact_audit')
        if not isinstance(item.get('id'),str) or not isinstance(item.get('e'),list) or any(not isinstance(e,str) for e in item['e']) or not isinstance(item.get('b'),list) or any(not isinstance(b,dict) for b in item['b']):raise ValueError('invalid_compact_fact_ledger')
        rows.append(dict(id=item['id'],**{name:item[key] for name,key in zip(FACT_FLAGS,FACT_WIRE)},issues=item['e'],blocking_conflicts=item['b']))
    return dict(items=rows)
