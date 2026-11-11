# -*- coding: utf-8 -*-
"""P3 只读调查工具：agent 循环只能读、不能写、不能出卡、不能改档。

契约见 docs/redesign/11-p3-agent-contract.md。
- 每个工具是纯函数 + 只读上下文（packs/hh/decisions/fields）；无网络、无数据库写入。
- 观测结构固定：{ok, tool, ...}；任何异常返回 {ok: False, tool, error}，绝不抛给调用方。
- 引文（quote）只从来源原文中截取，调用方必须再用 verbatim_ok 校验后才能展示。
"""
import os

from . import conditions as cond
from .grader import clause_text as _clause_text, region_status as _region_status

VERSION = 'tools-1'
MAX_TEXT = 420

# 工具清单（供规划提示词列出可用动作；顺序固定，便于日志对照）。
TOOL_SPECS = [
    dict(name='read_clause', args=['code', 'field?'],
         desc='读取政策原文片段。field 可选 title/target/conditions/parent_target/parent_conditions/benefit/process/reference；不给则返回全部。'),
    dict(name='region_check', args=['code'],
         desc='判断该政策条目与家庭所在地的适用关系（applicable/parent_applicable/unknown/reference/explicit_other）。'),
    dict(name='member_facts', args=['member_no?'],
         desc='列出某成员（缺省全部成员）与户级已确认为已知的事实字段。'),
    dict(name='evaluate_condition', args=['code', 'field', 'value', 'op?', 'member_no?', 'subject?'],
         desc='对单个条件做三值求值，返回 satisfied/violated/unknown 与实际值。'),
    dict(name='search_policy', args=['query'],
         desc='在本次候选里按关键词查找政策（名称/对象/条件），返回最多 3 条 code。'),
    dict(name='prior_judgement', args=['code'],
         desc='读取该政策本次已有的核验结论（状态、原文引用、未确认字段、来源缺口）。'),
]

_FIELDS = ('title', 'target', 'conditions', 'parent_target', 'parent_conditions',
           'benefit', 'process', 'reference')


def enabled():
    """AGENT_LOOP：默认开启；=0 完全关闭调查循环（不影响主链路）。"""
    return os.environ.get('AGENT_LOOP', '1').strip() != '0'


def _clip(text, limit=MAX_TEXT):
    text = ' '.join(str(text if text is not None else '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def verbatim_ok(quote, source_text):
    """引文能否逐字回链来源原文（折叠空白后子串命中，与 conditions 同一口径）。"""
    quote = cond.normalize_text(quote)
    body = cond.normalize_text(source_text)
    return bool(quote) and quote in body


def _packs(packs):
    return [p for p in (packs or []) if isinstance(p, dict) and p.get('policy')]


def find_pack(packs, code):
    code = str(code or '').strip()
    if not code:
        return None
    for pack in _packs(packs):
        if str(pack['policy'].get('code') or '') == code:
            return pack
    return None


def clause_of(pack):
    return _clause_text((pack or {}).get('source') or {})


def _decision_for(ctx, code):
    packs = ctx.get('packs') or []
    pack = find_pack(packs, code)
    if not pack:
        return None
    for decision in (ctx.get('decisions') or []):
        if isinstance(decision, dict) and decision.get('source_id') == pack.get('id'):
            return decision
    return None


def _err(tool, error):
    return dict(ok=False, tool=tool, error=str(error)[:120])


def read_clause(ctx, code=None, field=None):
    tool = 'read_clause'
    try:
        pack = find_pack(ctx.get('packs'), code)
        if not pack:
            return _err(tool, 'unknown_policy')
        source = pack.get('source') or {}
        field = str(field or '').strip()
        if field:
            if field not in _FIELDS:
                return _err(tool, 'unsupported_field')
            return dict(ok=True, tool=tool, code=pack['policy'].get('code'),
                        field=field, text=_clip(source.get(field) or ''))
        return dict(ok=True, tool=tool, code=pack['policy'].get('code'),
                    text=_clip(clause_of(pack)), fields=list(_FIELDS))
    except Exception as exc:
        return _err(tool, exc)


def region_check(ctx, code=None):
    tool = 'region_check'
    try:
        pack = find_pack(ctx.get('packs'), code)
        if not pack:
            return _err(tool, 'unknown_policy')
        info = _region_status(pack.get('policy'), pack.get('detail'),
                               str(ctx.get('region') or ''))
        return dict(ok=True, tool=tool, code=pack['policy'].get('code'),
                    status=info.get('status'), verdict=info.get('verdict'),
                    note=info.get('note') or '')
    except Exception as exc:
        return _err(tool, exc)


def _fact_rows(facts, prefix, fields):
    out = []
    for key, fact in (facts or {}).items():
        if not isinstance(fact, dict) or not key.startswith(prefix + ':'):
            continue
        if fact.get('state') != 'known':
            continue
        name = key.split(':', 1)[1]
        definition = (fields or {}).get(name) or {}
        out.append(dict(field=name, label=definition.get('label') or name,
                        value=fact.get('value')))
    return out[:24]


def member_facts(ctx, member_no=None):
    tool = 'member_facts'
    try:
        facts = (ctx.get('hh') or {}).get('facts') or {}
        fields = ctx.get('fields') or {}
        if member_no is None:
            members = (ctx.get('hh') or {}).get('members') or {}
            rows = [dict(member='household', facts=_fact_rows(facts, 'household', fields))]
            for no in sorted(members):
                rows.append(dict(member=str(no),
                                 facts=_fact_rows(facts, str(no), fields)))
            return dict(ok=True, tool=tool, members=rows[:6])
        entity = 'household' if str(member_no) in ('0', 'household') else str(member_no)
        return dict(ok=True, tool=tool, member=entity,
                    facts=_fact_rows(facts, entity, fields))
    except Exception as exc:
        return _err(tool, exc)


def evaluate_condition(ctx, field=None, value=True, op='eq', member_no=None, subject=None, code=None):
    tool = 'evaluate_condition'
    try:
        field = str(field or '').strip()
        if not field:
            return _err(tool, 'missing_field')
        fields = ctx.get('fields') or {}
        if field not in fields:
            return _err(tool, 'undefined_field')
        atom = cond.make_atom(field, op or 'eq', value, subject=subject or 'member',
                              source='structured')
        result = cond.evaluate_atom(atom, (ctx.get('hh') or {}).get('facts') or {},
                                    fields, member=member_no)
        return dict(ok=True, tool=tool, code=str(code or '').strip() or None,
                    field=field, op=atom.get('op'),
                    expected=atom.get('value'), state=result.get('state'),
                    actual=result.get('actual'), missing=result.get('missing') or [],
                    reason=result.get('reason') or '')
    except Exception as exc:
        return _err(tool, exc)


def search_policy(ctx, query=None):
    tool = 'search_policy'
    try:
        query = cond.normalize_text(query)
        if not query:
            return _err(tool, 'empty_query')
        hits = []
        for pack in _packs(ctx.get('packs')):
            source = pack.get('source') or {}
            body = cond.normalize_text(' '.join(str(source.get(k) or '') for k in _FIELDS))
            score = body.count(query)
            if score:
                hits.append(dict(code=pack['policy'].get('code'),
                                 name=pack['policy'].get('name'), score=score))
        hits.sort(key=lambda x: -x['score'])
        return dict(ok=True, tool=tool, query=_clip(query, 40), hits=hits[:3])
    except Exception as exc:
        return _err(tool, exc)


def prior_judgement(ctx, code=None):
    tool = 'prior_judgement'
    try:
        pack = find_pack(ctx.get('packs'), code)
        if not pack:
            return _err(tool, 'unknown_policy')
        decision = _decision_for(ctx, pack['policy'].get('code'))
        if not decision:
            return dict(ok=True, tool=tool, code=pack['policy'].get('code'),
                        status='no_decision')
        matches = []
        for item in list(decision.get('object_matches') or []) + list(decision.get('matches') or []):
            if not isinstance(item, dict):
                continue
            quote = str((item.get('citation') or {}).get('quote') or '').strip()
            matches.append(dict(meaning=_clip(item.get('meaning'), 60),
                                quote=_clip(quote, 80),
                                quote_ok=verbatim_ok(quote, clause_of(pack))))
        unknowns = []
        for question in decision.get('unknowns') or []:
            if isinstance(question, dict):
                unknowns.append(dict(fact_key=str(question.get('fact_key') or '')[:40],
                                     requirement=_clip(question.get('requirement'), 50)))
        return dict(ok=True, tool=tool, code=pack['policy'].get('code'),
                    status=str(decision.get('status') or ''),
                    matches=matches[:4], unknowns=unknowns[:4])
    except Exception as exc:
        return _err(tool, exc)


_DISPATCH = {
    'read_clause': read_clause,
    'region_check': region_check,
    'member_facts': member_facts,
    'evaluate_condition': evaluate_condition,
    'search_policy': search_policy,
    'prior_judgement': prior_judgement,
}


def run(name, args, ctx):
    """执行一个工具动作；未知工具/参数错误都返回观测而不是抛异常。"""
    fn = _DISPATCH.get(str(name or '').strip())
    if fn is None:
        return dict(ok=False, tool=str(name or ''), error='unknown_tool')
    args = args if isinstance(args, dict) else {}
    try:
        return fn(ctx, **{k: v for k, v in args.items() if k in _allowed(name)})
    except TypeError:
        return dict(ok=False, tool=str(name), error='invalid_arguments')
    except Exception as exc:
        return _err(str(name), exc)


def _allowed(name):
    return {'read_clause': ('code', 'field'),
            'region_check': ('code',),
            'member_facts': ('member_no',),
            'evaluate_condition': ('field', 'value', 'op', 'member_no', 'subject', 'code'),
            'search_policy': ('query',),
            'prior_judgement': ('code',)}.get(name, ())
