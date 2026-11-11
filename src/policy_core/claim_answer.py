# -*- coding: utf-8 -*-
"""P3-B：claim 驱动三段式回答。

结构固定为三段，段间空行（SSE 逐段流式即按空行切分）：
  1. 结论边界：核对了多少项、多少项已有已核实条件支持、不等于资格认定；
  2. 逐条依据：每张卡片已核实的条件 + 已绑定的政策原文/政策条目文字；
  3. 待补齐：缺什么、追问、看过但没出卡的原因、未完成分析、免责句。

约束：只渲染已核验的卡片与已绑定证据；不调用模型、不新增/删除卡片、不改档位、
不产生资格结论。整体可回退：ANSWER_STYLE=classic（或 0/off）。
"""
import os

VERSION = 'claims-1'
MAX_ROWS = 6
MAX_QUOTE = 80
_OFF = ('classic', '0', 'off', 'false', 'no')


def enabled():
    """开关：默认开（claims）；ANSWER_STYLE=classic/0 回退旧版式。"""
    return os.environ.get('ANSWER_STYLE', 'claims').strip().lower() not in _OFF


def _clean(text, limit=None):
    t = ' '.join(str(text or '').split())
    if limit and len(t) > limit:
        t = t[:max(1, limit - 1)] + '…'
    return t


def _satisfied(row):
    out = []
    for x in (row.get('satisfied') or []):
        text = _clean(x, 60)
        if text and text not in out:
            out.append(text)
    return out


_MISSING_NOISE = ('等待补充家庭事实后继续核验', '等待补充家庭事实后继续核验。')


def _missing(row):
    out = []
    for x in (row.get('missing') or []):
        text = _clean(x, 40)
        if text and text not in out and text not in _MISSING_NOISE:
            out.append(text)
    return out


def _quote(row):
    """只取已成立（satisfied）claim 上已绑定回链的逐字引文；没有就返回空。"""
    for c in (row.get('claims') or []):
        if not isinstance(c, dict) or c.get('kind') != 'satisfied':
            continue
        for e in (c.get('evidence') or []):
            q = _clean((e or {}).get('quote'), MAX_QUOTE)
            if q:
                return q
    return ''


def _clause(row):
    return _clean(row.get('clause_quote'), MAX_QUOTE)


def _counts(rows, coverage):
    """优先按展示行自身的档位统计，保证“支持/待补充”与正文逐条一致；
    行上没有档位时才回退 coverage.match_core 的计数。"""
    tiers = [str(r.get('tier') or '').upper() for r in rows]
    if any(t in ('A', 'B', 'C', 'D') for t in tiers):
        support = sum(1 for r, t in zip(rows, tiers)
                      if t in ('A', 'B') or (not t and _satisfied(r)))
        pending = sum(1 for r, t in zip(rows, tiers) if t == 'C')
        return support, pending
    counts = ((coverage or {}).get('match_core') or {}).get('counts') or {}
    def _int(key):
        try:
            return int(counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0
    support = _int('A') + _int('B')
    if not counts:
        support = sum(1 for r in rows if _satisfied(r))
    return support, _int('C')


def _boundary(rows, coverage):
    support, pending = _counts(rows, coverage)
    n = len(rows)
    if support:
        lines = ['本次逐条核对了 %d 项与您家庭情况相关的政策，其中 %d 项已有已核实条件支持；'
                 '下面逐条列出已核实的条件和政策依据。' % (n, support)]
    else:
        lines = ['本次逐条核对了 %d 项与您家庭情况相关的政策，暂无可确认的已满足条件；'
                 '下面说明核验到哪一步、还缺什么。' % n]
    if (coverage or {}).get('region_status') == 'unknown' or (coverage or {}).get('region_unconfirmed'):
        # 地区没确认就不能让用户以为卡片是"当地适用"：结论边界必须先说清。
        lines.append('家庭所在地区本次未确认，以上卡片尚未按属地逐条核验，不能视为当地适用政策；'
                     '补充所在市／县（区）全称后可重新核对。')
    if pending:
        lines.append('另有 %d 项需要补充家庭信息后才能判断，见末尾待补充项。' % pending)
    lines.append('本次结论口径统一为“已满足部分条件、政策依据待补齐”，只是已核实条件的汇总，'
                 '不等于资格认定。')
    return '\n'.join(lines)


def _evidence(rows):
    lines = ['已核实的条件与政策依据：']
    for r in rows[:MAX_ROWS]:
        name = r.get('name') or r.get('code') or ''
        member = r.get('member') or ''
        sat = _satisfied(r)
        head = '- **%s**（%s）：' % (name, member)
        head += ('已核实条件：' + '；'.join(sat[:2]) + '。') if sat else '暂无可确认的已满足条件。'
        lines.append(head)
        quote = _quote(r)
        if quote:
            lines.append('  - 原文：“%s”' % quote)
        elif _clause(r):
            lines.append('  - 政策条目载明：“%s”' % _clause(r))
        gaps = [_clean(x, 40) for x in (r.get('source_gaps') or []) if _clean(x)]
        if gaps:
            lines.append('  - 政策原文未明确的口径：' + '；'.join(gaps[:3]))
    if len(rows) > MAX_ROWS:
        lines.append('- 其余 %d 项同类卡片见页面卡片列表。' % (len(rows) - MAX_ROWS))
    return '\n'.join(lines)


def _needs(rows, questions, coverage):
    from . import result_model as rm
    parts = []
    miss = []
    for r in rows:
        for m in _missing(r):
            if m not in miss:
                miss.append(m)
    if miss:
        parts.append('待补充的家庭信息：' + '；'.join(miss[:6]) + '。')
    labels = []
    for q in (questions or []):
        if not isinstance(q, dict):
            continue
        label = _clean(q.get('label') or q.get('question') or q.get('key'), 30)
        if label and label not in labels:
            labels.append(label)
    if labels:
        parts.append('补充下面任意一项，结论就会更新：' + '、'.join(labels[:4]) +
                     '（不确定可直接选“不清楚”）。')
    note = rm.unclear_note(rows, coverage)
    if note:
        parts.append(note)
    why = rm.why_not_lines(coverage)
    if why:
        parts.append('\n'.join(why))
    rejected = coverage.get('rejected_policies') or []
    if rejected:
        lines = ['以下政策本次看过，但未形成可核验的卡片：']
        for item in rejected[:4]:
            if not isinstance(item, dict):
                continue
            who = '（' + item['member'] + '）' if item.get('member') else ''
            lines.append('- ' + str(item.get('name') or item.get('code') or '') + who +
                         '：' + rm._reject_label(item))
        parts.append('\n'.join(lines))
    agent = rm.agent_lines(coverage)
    if agent:
        parts.append('\n'.join(agent))
    pending = coverage.get('interpretation_pending', 0)
    failed = coverage.get('interpretation_failed', 0)
    if pending:
        parts.append('仍有%s项检索来源未完成分析；本次未将它们判为符合或不符合。'
                     '可继续分析，系统会复用已核对来源。' % pending)
    if failed:
        parts.append('%s项候选来源分析未通过校验，暂未给出结论；'
                     '这与政策缺条款、家庭不符合是不同情况。' % failed)
    if coverage.get('candidate_budget_truncated'):
        parts.append('另有部分相关来源尚未逐条核验完毕，以上结果可能不完整。')
    if coverage.get('disclaimer') and not coverage.get('scope_blocked'):
        parts.append(rm.DISCLAIMER)
    if not parts:
        return '本次没有新的待补充项；补充其他家庭情况（成员、收入、残疾等级等）可以继续核验更多政策。'
    return '还需要补充与核对的事项：\n' + '\n'.join(parts)


def answer(rows, questions, coverage, intro=None):
    """三段式正文。rows 为空或开关关闭时返回空串，由调用方走旧版式。"""
    if not enabled() or not rows:
        return ''
    coverage = coverage or {}
    blocks = []
    if intro:
        head = '\n'.join(str(x) for x in intro if str(x or '').strip())
        if head:
            blocks.append(head)
    blocks.append(_boundary(rows, coverage))
    blocks.append(_evidence(rows))
    blocks.append(_needs(rows, questions, coverage))
    return '\n\n'.join(blocks)
