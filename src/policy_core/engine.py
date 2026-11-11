"""Typed three-valued interpreter. No policy IDs, names or household templates."""
from .fields import valid_value

OPS={'eq','ne','in','gte','gt','lte','lt','between','exists'}

def canonical(expr,fields):
    """Normalize equivalent Boolean syntax without adding or removing evidence."""
    import copy
    node=copy.deepcopy(expr)
    if 'all' in node or 'any' in node:
        key='all' if 'all' in node else 'any';node[key]=[canonical(c,fields) for c in node[key]]
    elif 'not' in node:
        child=canonical(node['not'],fields)
        if 'not' in child:return child['not']
        if child.get('field') in fields and fields[child['field']]['type']=='boolean' and child.get('op')=='eq' and type(child.get('value')) is bool:
            child['value']=not child['value'];return child
        node['not']=child
    elif node.get('field') in fields and fields[node['field']]['type']=='boolean' and node.get('op')=='ne' and type(node.get('value')) is bool:
        node.update(op='eq',value=not node['value'])
    return node

def validate(expr, fields, depth=0):
    if depth>12 or not isinstance(expr,dict):raise ValueError('invalid_expression')
    if 'unresolved' in expr:
        if set(expr)!={'unresolved'} or not isinstance(expr['unresolved'],str) or not expr['unresolved'].strip():raise ValueError('invalid_unresolved')
    elif 'all' in expr or 'any' in expr:
        key='all' if 'all' in expr else 'any'
        if set(expr)-{key,'evidence'} or not isinstance(expr[key],list) or not 1<=len(expr[key])<=60:raise ValueError('invalid_logic')
        for child in expr[key]:validate(child,fields,depth+1)
    elif 'not' in expr:
        if set(expr)-{'not','evidence'}:raise ValueError('invalid_not')
        validate(expr['not'],fields,depth+1)
    else:
        if set(expr)-{'field','op','value','evidence'}:raise ValueError('unsupported_atom_property')
        if expr.get('field') not in fields or expr.get('op') not in OPS:raise ValueError('invalid_atom')
        d=fields[expr['field']];op=expr['op'];value=expr.get('value')
        if op in ('gte','gt','lte','lt','between') and d['type']!='number':raise ValueError('comparison_type')
        if op in ('in','between'):
            if not isinstance(value,list) or not value or (op=='between' and (len(value)!=2 or value[0]>value[1])):raise ValueError('invalid_range')
            if not all(valid_value(v,d) for v in value):raise ValueError('invalid_value')
        elif op!='exists' and not valid_value(value,d):raise ValueError('invalid_value')
        if op!='exists' and value is None:raise ValueError('unknown_is_not_a_rule_value')
    return True

def atoms(expr):
    if 'unresolved' in expr:return []
    if 'all' in expr or 'any' in expr:
        return [a for child in expr.get('all',expr.get('any',[])) for a in atoms(child)]
    if 'not' in expr:return atoms(expr['not'])
    return [expr]

def evaluate(expr, facts, fields, member=None):
    """AND: false dominates unknown. OR: true dominates unknown; branches stay bound."""
    if 'unresolved' in expr:return dict(value=None,missing=[],source_gap=expr['unresolved'])
    if 'all' in expr or 'any' in expr:
        key='all' if 'all' in expr else 'any'
        children=[evaluate(c,facts,fields,member) for c in expr[key]]
        values=[c['value'] for c in children]
        value=(False if False in values else None if None in values else True) if key=='all' else (True if True in values else None if None in values else False)
        relevant=[c for c in children if c['value'] is None] if value is None else []
        return dict(value=value,op=key,children=children,missing=sorted({m for c in relevant for m in c['missing']}))
    if 'not' in expr:
        child=evaluate(expr['not'],facts,fields,member)
        return dict(value=None if child['value'] is None else not child['value'],op='not',children=[child],missing=child['missing'])
    field=expr['field'];scope=fields[field]['scope'];entity='household' if scope=='household' else str(member)
    key=entity+':'+field;fact=facts.get(key,{})
    actual=fact.get('value');known=fact.get('state')=='known' and valid_value(actual,fields[field])
    if not known:return dict(value=None,field=field,entity=entity,missing=[key],evidence=expr.get('evidence',[]))
    op=expr['op'];expected=expr.get('value')
    # study_stage范围匹配：按集合运算三态比较
    if field=='study_stage':
        # 学段值对应的可能范围集合
        def stage_range(s):
            if s=='义务教育':return {'小学','初中'}
            return {s}
        # 获取事实和政策允许的范围
        actual_range = stage_range(actual) if isinstance(actual,str) else set()
        # 合并政策允许范围（支持单个值或列表）
        if isinstance(expected,str):
            expected_range = stage_range(expected)
        elif isinstance(expected,list):
            expected_range = set()
            for e in expected:
                if isinstance(e,str):expected_range |= stage_range(e)
        else:
            expected_range = set()
        # 集合运算三态比较
        if actual_range and expected_range:
            if actual_range.issubset(expected_range):
                # 事实范围全部包含于政策范围 → True
                value = True if op in ('eq','in') else False if op=='ne' else None
                if value is not None:
                    return dict(value=value,field=field,entity=entity,actual=actual,expected=expected,op=op,missing=[],fact_source=fact.get('sources',[]),evidence=expr.get('evidence',[]))
            elif actual_range.isdisjoint(expected_range):
                # 完全不相交 → False
                value = False if op in ('eq','in') else True if op=='ne' else None
                if value is not None:
                    return dict(value=value,field=field,entity=entity,actual=actual,expected=expected,op=op,missing=[],fact_source=fact.get('sources',[]),evidence=expr.get('evidence',[]))
            else:
                # 部分重叠 → Unknown
                return dict(value=None,field=field,entity=entity,actual=actual,expected=expected,op=op,missing=[key],evidence=expr.get('evidence',[]))
    if op=='eq':value=actual==expected
    elif op=='ne':value=actual!=expected
    elif op=='in':value=actual in expected
    elif op=='gte':value=actual>=expected
    elif op=='gt':value=actual>expected
    elif op=='lte':value=actual<=expected
    elif op=='lt':value=actual<expected
    elif op=='between':value=expected[0]<=actual<=expected[1]
    else:value=actual is not None
    return dict(value=value,field=field,entity=entity,actual=actual,expected=expected,op=op,missing=[],fact_source=fact.get('sources',[]),evidence=expr.get('evidence',[]))

def decide(spec,facts,fields,member):
    relation=evaluate(spec['association'],facts,fields,member)
    proof=evaluate(spec['eligibility'],facts,fields,member)
    incomplete=not spec.get('eligibility_complete') or bool(spec.get('gaps'))
    if incomplete:status='policy_incomplete' if relation['value'] is True else 'direction_unconfirmed'
    elif proof['value'] is False:status='not_applicable'
    elif relation['value'] is not True:status='direction_unconfirmed'
    elif proof['value'] is True:status='matched'
    else:status='facts_missing'
    return dict(status=status,association=relation,proof=proof,missing=proof['missing'] if status=='facts_missing' else [],direction_missing=relation['missing'] if status=='direction_unconfirmed' and (incomplete or proof['value'] is not False) else [],gaps=spec.get('gaps',[]),visible=not(incomplete and proof['value'] is False))

def counterexamples(spec,fields):
    """Truth-table algebra checks, not an oracle for source semantics."""
    validate(spec['eligibility'],fields);validate(spec['association'],fields)
    expr={'all':[{'field':'x','op':'eq','value':True},{'field':'y','op':'eq','value':True}]}
    defs={k:dict(type='boolean',scope='member') for k in ('x','y')}
    assert evaluate(expr,{'1:x':dict(state='known',value=False)},defs,1)['value'] is False
    assert evaluate({'any':expr['all']},{'1:x':dict(state='known',value=True)},defs,1)['value'] is True
    # Cross-check each actual expression against a separate truth-table evaluator.
    import random
    rng=random.Random(17)
    count=0
    for expression in (spec['association'],spec['eligibility']):
        leaves=atoms(expression);domains={}
        for atom in leaves:
            field=atom['field'];definition=fields[field]
            values=[None]
            if definition['type']=='boolean':values+=[True,False]
            elif definition['type']=='number':
                raw=atom.get('value');raw=raw if isinstance(raw,list) else [raw] if isinstance(raw,(int,float)) else [0,1]
                values+=[v+delta for v in raw for delta in (-1,0,1) if valid_value(v+delta,definition)]
            else:
                value=atom.get('value');values+=list(definition.get('values',[]))+(value if isinstance(value,list) else [value] if isinstance(value,str) else ['任意值'])+['不相同的值']
            domains.setdefault(field,[]).extend(v for v in values if v is None or valid_value(v,definition))
        def reference(node,assignment):
            if 'unresolved' in node:return None
            if 'all' in node or 'any' in node:
                key='all' if 'all' in node else 'any';vals=[reference(x,assignment) for x in node[key]]
                return (False if False in vals else None if None in vals else True) if key=='all' else (True if True in vals else None if None in vals else False)
            if 'not' in node:
                v=reference(node['not'],assignment);return None if v is None else not v
            actual=assignment[node['field']]
            if actual is None:return None
            expected=node.get('value');op=node['op']
            import operator
            if op=='in':return actual in expected
            if op=='between':return expected[0]<=actual<=expected[1]
            if op=='exists':return True
            return {'eq':operator.eq,'ne':operator.ne,'gt':operator.gt,'gte':operator.ge,'lt':operator.lt,'lte':operator.le}[op](actual,expected)
        for _ in range(64):
            assignment={k:rng.choice(v) for k,v in domains.items()}
            facts={('household' if fields[k]['scope']=='household' else '1')+':'+k:dict(state='known' if v is not None else 'unknown',value=v) for k,v in assignment.items()}
            assert evaluate(expression,facts,fields,'1')['value'] is reference(expression,assignment),'expression_truth_table_mismatch'
            # Other member's facts must never fulfill a member-bound expression.
            member_facts={k.replace('1:','2:',1):v for k,v in facts.items() if k.startswith('1:')}
            if member_facts:assert evaluate(expression,member_facts,fields,'1')['value'] is None,'member_scope_leak'
            count+=1
    return dict(structure=True,three_state=True,scope_binding=True,expression_cases=count)
