"""T5 冲突口径程序化裁决：纯函数分类、路线构造与等价冲突归并。

设计目标：把“已验证的明确冲突”从大模型路线重建中拆出来，用最小可判定原子
直接求值，减少 core_full_conflict_scope 的调用与重试；任何不确定情形都交回
原模型路径，绝不新增放行。

安全边界（FP=0）：
- 只有全部冲突原子都能明确求值（事实 known、字段已定义、比较结果严格为 False）
  或可证明“事实不弱于原文要求”的等价变体时才判 program；
- 等价归并只在事实值不弱于政策要求值时成立（更具体/同 canon），事实更弱一律
  交回模型，避免用模糊地名误放行；
- build_route 只复刻 _scope_value/_validate_logic_structure 已支持的结构，
  不引入新的逻辑语义；本模块不做 IO、不读环境变量、不产生任何判定副作用。
"""
import copy

from .facts import _canon_value,_equivalent_values,_more_specific
from .fields import dictionary

PROGRAM='program'
SOURCE_MISSING='source_missing'
SEMANTIC='semantic'

_ALLOWED_OPS=('eq','in','gte','lte','gt','lt')
# 与 validate_decisions:261 的元数据引文口径一致：这些字段只是标题/父级标签，
# 不能作为“必要条件已被违反”的证明。
_METADATA_SOURCES=('title','original_title','original_category3','parent_target','parent_conditions')


class _Unsupported(Exception):
    """单个冲突原子无法程序化：由调用方回落到模型路径，不构成判定。"""

    def __init__(self,reason):
        super().__init__(reason)
        self.reason=reason


def _definition(fields,name):
    fields=fields or {}
    definition=fields.get(name)
    if isinstance(definition,dict):return definition
    return dictionary().get(name,{})


def _claim_evidence(claim):
    """返回可交给 _bind 的最小证据引用；无法确定时返回 None。

    已验证冲突的 claim 一定带 citation（validate_decisions 写入 source/segment_id），
    优先用它而不是 evidence_id：只有 source 明示引用段名，才能识别标题/父级等
    元数据引文并归入 source_missing。
    """
    if not isinstance(claim,dict):return None
    citation=claim.get('citation') if isinstance(claim.get('citation'),dict) else claim
    source=citation.get('source')
    if isinstance(source,str) and source.strip() and 'segment_id' in citation:
        return {'source':source,'segment_id':citation['segment_id']}
    evidence_id=claim.get('evidence_id')
    if isinstance(evidence_id,str) and evidence_id.strip():return {'evidence_id':evidence_id}
    return None


def _is_metadata_evidence(evidence):
    source=str((evidence or {}).get('source') or '')
    return source in _METADATA_SOURCES or source.startswith('scope_category')


def _resolve_fact_key(decision,test,st,fields):
    """按 _scope_value 的补全口径解析完整事实键。

    生产数据里的冲突原子可能只写短字段名；_scope_value 会在短名属于字段字典时
    补 entity 前缀。程序路线直接写完整键，避免依赖该补全分支，同时与求值一致。
    """
    facts=(st or {}).get('facts') or {}
    key=test.get('fact_key')
    if not isinstance(key,str) or not key.strip():raise _Unsupported('invalid_fact_key')
    if key in facts:return key
    if ':' not in key and key in (fields or {}):
        definition=fields[key]
        entity='household' if definition.get('scope')=='household' else str(decision.get('member'))
        candidate=entity+':'+key
        if candidate in facts:return candidate
    raise _Unsupported('unknown_fact_key')


def _canonical_for_field(name,value):
    from .terms import canonical_value
    if isinstance(value,list):return [canonical_value(name,v) for v in value]
    return canonical_value(name,value)


def _equivalence_adjustment(definition,name,actual,expected,op):
    """判断 strict 比较为 False 的原子是否只是行政区/标点等价变体。

    返回调整后的政策值（令比较结果变 True）；事实更弱或无法证明时返回 None。
    仅处理字符串 eq/in，数值与枚举不做任何等价放宽。
    """
    if not isinstance(actual,str):return None
    if op=='eq':
        candidates=[expected]
    elif op=='in' and isinstance(expected,list):
        candidates=list(expected)
    else:
        return None
    for candidate in candidates:
        if not isinstance(candidate,str):continue
        if _canon_value(definition,actual)==_canon_value(definition,candidate):
            return actual
        if _equivalent_values(definition,actual,candidate,field=name):
            # 只有“事实不弱于政策要求”才算满足：更具体或同 canon。反方向
            # （政策要求比事实更细）无法确认，交回模型。
            if _more_specific(definition,actual,candidate,field=name)==actual:
                return actual
    return None


def _atom(decision,claim,st,fields):
    """把一条已验证冲突还原为可求值原子。

    只接受能被 reasoning._scope_value 复核的最小叶子；任何结构/事实/等价方向
    的不确定都 raise _Unsupported，由上层回退模型，不做猜测。
    """
    if not isinstance(claim,dict):raise _Unsupported('invalid_conflict_claim')
    test=claim.get('test')
    if not isinstance(test,dict):raise _Unsupported('missing_conflict_test')
    op=test.get('op')
    if op not in _ALLOWED_OPS:raise _Unsupported('unsupported_test_operator')
    if 'value' not in test:raise _Unsupported('missing_test_value')
    key=_resolve_fact_key(decision,test,st,fields)
    entity,name=key.split(':',1)
    definition=_definition(fields,name)
    if not definition or 'type' not in definition:raise _Unsupported('undefined_field')
    facts=st['facts'];fact=facts.get(key) or {}
    if fact.get('state')!='known':raise _Unsupported('unknown_fact')
    actual=fact.get('value')
    expected=_canonical_for_field(name,test['value'])
    from .engine import evaluate,validate
    try:
        expression=dict(field=name,op=op,value=expected)
        validate(expression,fields)
        evaluated=evaluate(expression,facts,fields,None if entity=='household' else entity)
    except (ValueError,KeyError,TypeError,AttributeError):
        raise _Unsupported('invalid_test_expression')
    value=evaluated.get('value')
    if value is None:raise _Unsupported('unknown_fact_value')
    if value is True:
        # validate_decisions 的 conflicts 组要求比较必须为 False；出现 True 说明
        # 上游口径已变化或数据被改写，程序不得据此排除，直接交回模型。
        raise _Unsupported('conflict_atom_not_false')
    evidence=_claim_evidence(claim)
    if evidence is None:raise _Unsupported('claim_without_bound_evidence')
    if _is_metadata_evidence(evidence):raise _Unsupported('conflict_cites_metadata')
    adjusted=_equivalence_adjustment(definition,name,actual,test['value'],op)
    satisfied=False
    required=_canonical_for_field(name,test['value'])
    route_value=required
    if adjusted is not None:
        # 等价变体：用调整后的“事实值”做原子值，_scope_value 会严格比较为 True。
        route_value=adjusted if op=='eq' else [adjusted]
        try:
            check=dict(field=name,op=op,value=route_value)
            validate(check,fields)
            checked=evaluate(check,facts,fields,None if entity=='household' else entity)['value']
        except (ValueError,KeyError,TypeError,AttributeError):
            raise _Unsupported('equivalence_adjustment_invalid')
        if checked is not True:
            raise _Unsupported('equivalence_adjustment_not_satisfied')
        satisfied=True
    return dict(index=getattr(claim,'index',None),fact_key=key,entity=entity,field=name,op=op,
                actual=actual,required=required,route_value=route_value,
                equivalent=adjusted is not None,satisfied=satisfied,
                evidence=copy.deepcopy(evidence))


def _route_leaf(atom):
    leaf=dict(atom['evidence'])
    leaf['test']=dict(fact_key=atom['fact_key'],op=atom['op'],value=copy.deepcopy(atom['route_value']))
    return leaf


def classify(decision,pack,st,fields):
    """三分类：program / source_missing / semantic，附理由与原子明细。

    返回 dict(category,reason,claims)，不修改输入；任何异常由调用方视为
    semantic（最保守的模型回退）。
    """
    claims=list((decision or {}).get('conflicts') or [])
    if not claims:
        # 仅 related（无已验证冲突）的决策需要模型重建完整适用路线，scope
        # 审核与 primary gate 都依赖那份路线；程序无法只凭正向原子替代。
        return dict(category=SEMANTIC,reason='no_verified_conflicts',claims=[])
    if decision.get('source_gaps'):
        # 政策资料本身缺失/矛盾时，冲突原子不一定是完整必要条件；保留冲突
        # 但不排除（保守），也不调用模型。
        return dict(category=SOURCE_MISSING,reason='decision_has_source_gaps',claims=[])
    atoms=[]
    for claim in claims:
        try:
            atoms.append(_atom(decision,claim,st,fields))
        except _Unsupported as exc:
            category=SOURCE_MISSING if exc.reason=='conflict_cites_metadata' else SEMANTIC
            return dict(category=category,reason=exc.reason,claims=atoms)
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            return dict(category=SEMANTIC,reason='classify_error:'+type(exc).__name__,claims=atoms)
    return dict(category=PROGRAM,reason='all_conflict_atoms_programmatic',claims=atoms)


def build_route(decision,pack,st,fields):
    """构造与模型同 schema 的 route logic。

    单原子为叶子（evidence_id 或 source+segment_id + test），多原子用 all 并列：
    所有已验证冲突都是“已知值违反原文明示条件”的明确必要条件，全部为 False
    时无论 AND/OR 层级都只能得到 False，因此 all 是安全上界。
    """
    claims=list((decision or {}).get('conflicts') or [])
    if not claims:raise ValueError('no_verified_conflict_claims')
    leaves=[]
    for claim in claims:
        atom=_atom(decision,claim,st,fields)
        leaves.append(_route_leaf(atom))
    return leaves[0] if len(leaves)==1 else {'all':leaves}


def resolve_equivalent(decision,st):
    """报告冲突原子中的等价变体归并结果，供 related 处理与审计使用。

    返回列表项：fact_key/field/actual/required/adjusted/satisfied/reason。
    satisfied=False 表示“存在等价关系但事实更弱/方向不明”，必须交回模型。
    """
    fields=dictionary()
    out=[]
    for claim in (decision or {}).get('conflicts') or []:
        try:
            atom=_atom(decision,claim,st,fields)
        except _Unsupported as exc:
            if exc.reason=='equivalence_adjustment_invalid':continue
            out.append(dict(fact_key=(claim.get('test') or {}).get('fact_key'),satisfied=False,reason=exc.reason))
            continue
        if atom['equivalent']:
            out.append(dict(fact_key=atom['fact_key'],field=atom['field'],actual=atom['actual'],
                            required=atom['required'],adjusted=atom['route_value'],
                            satisfied=atom['satisfied'],reason='fact_not_weaker_than_requirement'))
    return out


def apply_program_result(decision,route,value):
    """按原模型路径的非异常分支写回决策，返回结果类型。

    只有 _scope_value 严格返回 False 才 exclude；True 只做 related/unconfirmed；
    三态 None 不写回并返回 fallback（调用方把该条重新并入模型批次）。
    """
    if value is False:
        decision['conflict_scope']=route
        decision['conflict_scope_value']=False
        decision['status']='exclude'
        return 'exclude'
    if value is True:
        decision['conflict_scope']=route
        decision['conflict_scope_value']=True
        decision['branch_conflicts']=decision.get('conflicts',[])
        decision['conflicts']=[]
        decision['status']='related' if decision.get('object_matches') else 'unconfirmed'
        return decision['status']
    return 'fallback'
