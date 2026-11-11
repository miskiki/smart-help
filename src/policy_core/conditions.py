# -*- coding: utf-8 -*-
"""P2 条件原子层：政策要求与家庭事实之间唯一可求值的中间表示。

契约见 docs/redesign/10-p2-match-core-contract.md §3–§5。四条硬约束：

1. 只有本模块写 state（satisfied / violated / unknown / not_applicable）；
   规则编译、模型输出、检索都只能产出"未求值原子"。
2. 三值语义：unknown 不是 violated，永不被当作"不符合"去拦候选。
3. 标准算子的比较语义复用 policy_core/engine.py（同一套类型校验与成员作用域绑定），
   本模块只补 contains / implies / count_gte / nin 与证据绑定。
4. 证据绑定：llm_proposal 原子必须带 clause 引文，且引文（折叠空白后）是来源原文的
   逐字子串；否则 binding=rejected，原子降为 unknown，候选最高 B 档。

不调用模型、不读数据库、不写文件：纯函数，可在单测里穷举真值表。
"""
import re

from .engine import evaluate as _engine_evaluate, OPS as _ENGINE_OPS

VERSION = 'cond-1'
EVALUATED_BY = 'conditions.' + VERSION

SATISFIED = 'satisfied'
VIOLATED = 'violated'
UNKNOWN = 'unknown'
NOT_APPLICABLE = 'not_applicable'

STATES = (SATISFIED, VIOLATED, UNKNOWN, NOT_APPLICABLE)
SOURCES = ('rule', 'structured', 'llm_proposal', 'user')
SUBJECTS = ('member', 'household', 'policy')
EXTRA_OPS = ('nin', 'contains', 'implies', 'count_gte')
OPS = tuple(_ENGINE_OPS) + EXTRA_OPS

# 参与"实质条件"的判定：not_applicable 不进入分母，unknown 才影响档位。
_SUBSTANTIVE = (SATISFIED, VIOLATED)


def _expected_valid(atom, definition):
    """政策侧期望值是否合法（与字段字典同构）。不合法时求值只能是 unknown。"""
    from .fields import valid_value
    op, value = atom.get('op'), atom.get('value')
    if op == 'exists' or op == 'count_gte':
        return True
    if op in ('in', 'nin', 'between'):
        if not isinstance(value, (list, tuple)):
            return False
        if op == 'between' and len(value) != 2:
            return False
        return all(valid_value(v, definition) for v in value)
    return valid_value(value, definition)


def _text(value):
    return re.sub(r'\s+', '', str(value if value is not None else ''))


def normalize_text(value):
    """只折叠空白，不做同义改写、不做标点替换：绑定校验必须逐字可回链。"""
    return _text(value)


def make_atom(field, op='eq', value=True, subject='member', source='rule',
              condition_id=None, evidence=None, unit=None, period=None,
              confidence=None, negate=False, reason=''):
    """构造未求值原子。绝不写 state：只有 evaluate_atom / evaluate_all 能写。"""
    atom = dict(subject=subject if subject in SUBJECTS else 'member', field=str(field or ''),
                op=op, value=value, source=source,
                unit=unit, period=period, negate=bool(negate), reason=reason or '')
    if condition_id:
        atom['condition_id'] = condition_id
    if evidence:
        atom['evidence'] = dict(evidence) if isinstance(evidence, dict) else {'quote': str(evidence)}
    if confidence is not None:
        atom['confidence'] = confidence
    return atom


def validate(atom):
    """返回 (ok, reason)。只校验结构，不求值。"""
    if not isinstance(atom, dict):
        return False, 'not_a_dict'
    if atom.get('op') not in OPS:
        return False, 'unsupported_op'
    if atom.get('source') not in SOURCES:
        return False, 'unsupported_source'
    if atom.get('subject') not in SUBJECTS:
        return False, 'unsupported_subject'
    if not str(atom.get('field') or '').strip():
        return False, 'missing_field'
    op = atom.get('op')
    if op in ('in', 'nin', 'between'):
        value = atom.get('value')
        if not isinstance(value, (list, tuple)) or not value:
            return False, 'invalid_range'
        if op == 'between' and (len(value) != 2 or value[0] > value[1]):
            return False, 'invalid_range'
    if op in ('gte', 'gt', 'lte', 'lt', 'between'):
        value = atom.get('value')
        if op != 'between' and not isinstance(value, (int, float)):
            return False, 'comparison_type'
    if op == 'count_gte':
        value = atom.get('value')
        if not isinstance(value, int) or value < 0:
            return False, 'invalid_count'
    if atom.get('source') == 'llm_proposal':
        quote = (atom.get('evidence') or {}).get('quote')
        if not isinstance(quote, str) or not normalize_text(quote):
            return False, 'missing_evidence_quote'
    return True, ''


def bind_evidence(atom, clause_text):
    """把原子与来源原文做逐字绑定；返回新原子（不改原对象）。

    binding 取值：
      ok             引文逐字可回链（折叠空白后子串命中）
      rejected       llm_proposal 引文对不上原文 → 求值降 unknown（不得用于 A 档）
      binding_gap    规则原子来自离线编译条款，本轮没有引文 → 允许求值，但 A 档需要人工核对时降 B
      not_required   结构化/用户原子，不需要引文
    """
    out = dict(atom)
    source = atom.get('source')
    quote = (atom.get('evidence') or {}).get('quote')
    if source == 'llm_proposal':
        haystack = normalize_text(clause_text)
        needle = normalize_text(quote)
        bound = bool(haystack) and bool(needle) and needle in haystack
        out['evidence_bound'] = bound
        out['binding'] = 'ok' if bound else 'rejected'
        if not bound:
            out['state'] = UNKNOWN
            out['reason'] = 'evidence_binding_rejected'
        return out
    if source == 'rule':
        bound = bool(normalize_text(quote)) and normalize_text(quote) in normalize_text(clause_text)
        out['evidence_bound'] = bound
        out['binding'] = 'ok' if bound else 'binding_gap'
        return out
    out['evidence_bound'] = True
    out['binding'] = 'not_required'
    return out


def _field_def(fields, field):
    return (fields or {}).get(field)


def _fact_for(atom, facts, fields, member):
    field = atom.get('field')
    definition = _field_def(fields, field)
    if not definition:
        return None, None, 'undefined_field'
    scope = definition.get('scope')
    entity = 'household' if (atom.get('subject') == 'household' or scope == 'household') else str(member or '1')
    key = entity + ':' + field
    fact = (facts or {}).get(key) or {}
    if fact.get('state') != 'known':
        return None, key, 'fact_unknown'
    actual = fact.get('value')
    if atom.get('op') == 'contains':
        # contains 是子串/包含语义，允许"中职在读"这类比字段枚举更具体的值。
        return (actual, key, '') if isinstance(actual, str) and actual.strip() else (None, key, 'fact_value_invalid')
    if not _value_valid(actual, definition):
        return None, key, 'fact_value_invalid'
    return actual, key, ''


def _value_valid(value, definition):
    """与字段字典同构的最小校验：不让错误类型的比较产生假真值。"""
    if value is None:
        return False
    kind = definition.get('type')
    if kind == 'boolean':
        return isinstance(value, bool)
    if kind == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == 'text':
        values = definition.get('values')
        if values:
            return str(value) in [str(v) for v in values]
        return isinstance(value, str) and bool(value.strip())
    return True


def _compare(atom, actual, fields, entity):
    """返回 True / False / None。标准算子走 engine（同一套类型校验与作用域），扩展算子本模块补齐。"""
    op = atom.get('op')
    expected = atom.get('value')
    if op == 'nin':
        if isinstance(expected, (list, tuple, set)):
            return actual not in expected
        return actual != expected
    if op == 'contains':
        if isinstance(actual, (list, tuple, set)):
            return expected in actual
        return str(expected) in str(actual)
    if op == 'implies':
        return bool(actual) == bool(expected)
    if op == 'count_gte':
        count = len(actual) if isinstance(actual, (list, tuple, set)) else (1 if actual not in (None, '', False) else 0)
        return count >= int(expected)
    key = entity + ':' + str(atom.get('field'))
    try:
        result = _engine_evaluate({'field': atom.get('field'), 'op': op, 'value': expected},
                                  {key: {'state': 'known', 'value': actual}}, fields, member=entity)
        return result.get('value')
    except Exception:
        return None


def evaluate_atom(atom, facts, fields, member=None):
    """三值求值单个原子；返回带 state 的新结果 dict（输入原子不被修改）。"""
    out = dict(atom)
    out['evaluated_by'] = EVALUATED_BY
    ok, reason = validate(atom)
    if not ok:
        out.update(state=UNKNOWN, reason=reason, actual=None, missing=[])
        return out
    if out.get('binding') == 'rejected':
        out.update(state=UNKNOWN, reason='evidence_binding_rejected')
        return out
    definition = _field_def(fields, out.get('field'))
    if definition and not _expected_valid(out, definition):
        # 政策侧的值本身不合法（如"五级"不在该字段枚举里）不是"家庭不符合"，只能是 unknown。
        out.update(state=UNKNOWN, reason='invalid_expected_value', actual=None, missing=[])
        return out
    actual, key, why = _fact_for(atom, facts, fields, member)
    if key is None:
        out.update(state=UNKNOWN, reason=why, actual=None, missing=[])
        return out
    if actual is None:
        out.update(state=UNKNOWN, reason=why, actual=None, missing=[key])
        return out
    entity = key.split(':', 1)[0]
    value = _compare(atom, actual, fields, entity)
    if out.get('negate'):
        value = None if value is None else not value
    out['actual'] = actual
    out['missing'] = []
    if value is None:
        out.update(state=UNKNOWN, reason='comparison_inconclusive')
    elif value:
        out.update(state=SATISFIED, reason='')
    else:
        out.update(state=VIOLATED, reason='')
    return out


def evaluate_all(atoms, facts, fields, member=None):
    """批量求值，保持输入顺序与 condition_id。"""
    return [evaluate_atom(atom, facts, fields, member) for atom in (atoms or [])]


def aggregate(results, op='and'):
    """三值聚合：AND（violated 优先）/ OR（satisfied 优先）/ NOT（对偶，unknown 保持）。"""
    states = [r.get('state') for r in (results or []) if r.get('state') in STATES]
    if not states:
        return NOT_APPLICABLE
    if op == 'and':
        if VIOLATED in states:
            return VIOLATED
        if all(s == SATISFIED for s in states):
            return SATISFIED
        if all(s == NOT_APPLICABLE for s in states):
            return NOT_APPLICABLE
        return UNKNOWN
    if op == 'or':
        if SATISFIED in states:
            return SATISFIED
        if all(s == VIOLATED for s in states):
            return VIOLATED
        if all(s == NOT_APPLICABLE for s in states):
            return NOT_APPLICABLE
        return UNKNOWN
    if op == 'not':
        state = states[0]
        if state == SATISFIED:
            return VIOLATED
        if state == VIOLATED:
            return SATISFIED
        return state
    raise ValueError('unsupported_aggregate_op')


def missing_facts(results):
    """汇总所有 unknown 原子缺的事实键（去重、保持顺序）。"""
    seen, out = set(), []
    for result in results or []:
        if result.get('state') != UNKNOWN:
            continue
        for key in result.get('missing') or []:
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def substantive(results):
    """实质条件结果：排除 not_applicable（它不参与分母）。"""
    return [r for r in (results or []) if r.get('state') in _SUBSTANTIVE + (UNKNOWN,)]


def atoms_from_expression(expr, subject='member', source='rule'):
    """把规则 AST 展开成未求值原子列表（AND/OR 结构压平，只保留叶子）。"""
    if not isinstance(expr, dict) or 'unresolved' in expr:
        return []
    out = []

    def walk(node, negate=False):
        if not isinstance(node, dict):
            return
        if 'unresolved' in node:
            return
        if 'all' in node or 'any' in node:
            for child in node.get('all', node.get('any', [])):
                walk(child, negate)
            return
        if 'not' in node:
            walk(node['not'], not negate)
            return
        atom = make_atom(field=node.get('field'), op=node.get('op'), value=node.get('value'),
                         subject=subject, source=source, condition_id='c%d' % (len(out) + 1),
                         evidence=node.get('evidence') if isinstance(node.get('evidence'), dict) else None,
                         negate=negate)
        out.append(atom)

    walk(expr)
    return out
