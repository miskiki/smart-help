"""Fact/condition terminology only, not policy-specific eligibility rules."""
import copy

def canonical_value(field,value):
    if not isinstance(value,str):return value
    text=value.strip()
    if field=='study_stage':
        # 先检查具体学段，保留精度
        for aliases,label in [(('博士','硕士','研究生'),'研究生'),(('本科',),'本科'),(('大专','专科'),'专科'),
                              (('中等职业','中职','中专','职业高中'),'中职'),(('普通高中','高中'),'高中'),(('初中',),'初中'),(('小学',),'小学'),(('幼儿园','学前'),'学前')]:
            if any(a in text for a in aliases):return label
        # 仅当没有具体学段时，才保留义务教育宽范围
        if '义务教育' in text:return '义务教育'
    if field=='labor':
        if text in ('全劳动力','正常劳动力','普通劳动力'):return '普通劳动力'
    if field=='med_type':
        if text in ('居民医保','城乡居民基本医疗保险','城乡居民医保'):return '城乡居民医保'
        if text in ('职工基本医疗保险','城镇职工医保','职工医保'):return '职工医保'
    return text

def normalize_expression(expr):
    node=copy.deepcopy(expr)
    if node.get('field'):
        field=node['field'];value=node.get('value')
        node['value']=[canonical_value(field,v) for v in value] if isinstance(value,list) else canonical_value(field,value)
    for op in ('all','any'):
        if op in node:node[op]=[normalize_expression(c) for c in node[op]]
    if 'not' in node:node['not']=normalize_expression(node['not'])
    return node

def verify_consistency(expr):
    """Disjoint conjunctive equalities are a source/model conflict, not user failure."""
    if 'all' in expr:
        possible={}
        def conjuncts(node):
            return [a for c in node['all'] for a in conjuncts(c)] if 'all' in node else [node]
        for a in conjuncts(expr):
            if a.get('field') and a.get('op') in ('eq','in'):
                values=a['value'] if a['op']=='in' else [a['value']]
                try:current=set(values)
                except TypeError:continue
                name=a['field'];possible[name]=possible.get(name,current)&current
                if not possible[name]:raise ValueError('source_condition_conflict:'+name+': explicitly acknowledge source disagreement with unresolved; do not require incompatible facts')
    for key in ('all','any'):
        for child in expr.get(key,[]):verify_consistency(child)
    if 'not' in expr:verify_consistency(expr['not'])
