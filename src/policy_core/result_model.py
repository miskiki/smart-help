"""One immutable view for cards, history, text and spreadsheet."""
import copy
from .full_settings import MODE,VERSION,limits

# 正文组织原则（去模板化）：固定小节标题与每轮重复的套话不再出现在正文里；
# 免责句只在会话首轮出现一次；已答“不清楚/暂时无法提供”的字段不再原样重述。
BODY_LIMIT=400
DISCLAIMER='部分满足不等于完整资格；结论以经办机构正式核定为准。'

def mark_disclaimer(st,coverage):
    """本轮是否展示免责句：同一会话只在首轮展示，并把轮次写进 coverage['round']。
    幂等：同一轮里规则路径与全库路径都可能走到这里，coverage 已判定过就直接复用，
    不重复推进轮次——否则同轮第二次调用会把首轮误判成“非首轮”。"""
    if isinstance(coverage,dict) and 'disclaimer' in coverage:
        return bool(coverage['disclaimer'])
    if isinstance(st,dict):
        first=not st.get('disclaimer_shown')
        st['disclaimer_shown']=True
        st['advice_turn']=int(st.get('advice_turn') or 0)+1
        turn=st['advice_turn']
    else:
        first,turn=True,1
    if isinstance(coverage,dict):
        coverage['disclaimer']=bool(first)
        coverage['round']=turn
    return bool(first)

def unclear_fact_map(st,fields):
    """已答“不清楚/暂时无法提供”的字段 → 缺项展示文本（“家庭：标签”/“成员N：标签”）到标签的映射。
    这些字段不再以“需确认 X / 仍需核对：X”原样重述，改由 unclear_note 说明一次。"""
    out={}
    for key,entry in (st.get('asked') or {}).items():
        if not isinstance(key,str) or ':' not in key:
            continue
        answer=entry.get('answer') if isinstance(entry,dict) else entry
        if not (answer is None or '不清楚' in str(answer)):
            continue
        entity,name=key.split(':',1)
        definition=fields.get(name) or {}
        label=str(definition.get('label') or name)
        who='家庭' if entity=='household' else '成员'+entity
        out[who+'：'+label]=label
    return out

def unclear_note(rows,coverage):
    """本轮卡片缺项里仍挂着“不清楚”的字段：一句话说明此前已表示不清楚，不再当作新的补充要求。"""
    unclear=coverage.get('unclear_facts') or {}
    if not unclear:
        return ''
    shown=[]
    for row in (rows or []):
        for text in (row.get('missing') or []):
            label=unclear.get(text)
            if label and label not in shown:
                shown.append(label)
    if not shown:
        return ''
    return '你此前表示'+'、'.join('“'+x+'”' for x in shown[:3])+'暂不清楚，这条暂不能判定。'

def _row_weight(row):
    """同一政策多名候选时，保留证据更完整、结论更靠前的那条。"""
    return (len(row.get('satisfied') or []), -len(row.get('missing') or []),
            1 if (row.get('basis') or '').strip() else 0,
            1 if str(row.get('code') or '').startswith('X') else 0)

def dedupe_rows(rows):
    """同一政策名称 + 同一成员的重复来源只保留一张卡片。
    政策库存在大量跨层级/跨批次重名条目（如"重度残疾人护理补贴"同时有 S-ZB-088 与
    S-ZB-091，两条都在用），此前同一成员会出两张同名卡片，用户看到"反复的政策"。
    规则：同名称同成员、编号不同 → 合并保留证据更完整的一条；同编号不同细分条款
    （detail_id 不同）属于政策的不同适用分支，保留。"""
    out, index = [], {}
    for row in (rows or []):
        name = (row.get('name') or '').strip()
        if not name:
            out.append(row)
            continue
        key = (name, row.get('member') or '')
        if key in index:
            cur = out[index[key]]
            if (cur.get('code') or '') != (row.get('code') or ''):
                if _row_weight(row) > _row_weight(cur):
                    out[index[key]] = row
                continue
        else:
            index[key] = len(out)
        out.append(row)
    return out

def table(rows):
    return [{'成员':r['member'],'政策编号':r['code'],'政策名称':r['name'],'政策层级':r.get('level',''),'政策版本':r['version'],
        '结果类型':r['decision'],'匹配理由':r['reason'],'已满足条件':'；'.join(r['satisfied']),
        '政策依据（文件文号/条款）':r['basis'],'尚缺条件':'；'.join(r['missing']),
        '缺家庭信息':'；'.join(r['missing']),'缺政策依据':'；'.join(r['source_gaps']),
        '下一步核实事项':'；'.join(r['verify']),'补贴标准':r['benefit'],'申领流程':r['process']} for r in rows]

def metadata(hh,rows,questions,coverage,audit,diagnostics,advice_audit):
    from household_state import simple
    snapshot=dict(mode=MODE,flow_version=3,engine_version=VERSION,hh=simple(hh),table_recs=copy.deepcopy(rows),
        questions=copy.deepcopy(questions),coverage=coverage,decision_audit=audit,diagnostics=diagnostics,advice_audit=advice_audit,
        source_versions=[dict(code=r['code'],version=r['version'],detail_id=r['detail_id'],hash=r['source_hash']) for r in rows],judge_status='full_model_evidence_bound')
    return dict(match_json=snapshot,table_rows=table(rows),cards=rows,questions=questions,
        sources=[dict(policy_code=r['code'],policy_name=r['name'],excerpt=r['basis']) for r in rows],
        progress=dict(round=hh['policy_core']['round'],max_rounds=limits()['max_rounds']),judge_status='full_model_evidence_bound')

# P0-5：错误码/状态 → 用户可读的中文原因（只解释"为什么没出卡片"，不推断资格）
REJECT_REASON_ZH={
    'core_audience_unconfirmed':'政策核心对象未能与家庭事实建立证据关联',
    'unbound_primary_phrase':'政策核心对象的引文未能回链到来源原文',
    'invalid_scope':'对象范围（个人/整户）未通过格式校验',
    'threshold_not_in_source':'所引用的数值门槛未在来源原文中出现',
    'unbound_conflict_logic_atom':'冲突判断引用了证据之外的字段',
    'unreachable_route_graph_node':'资格路径存在不可达节点',
    'audit_id_mismatch':'审核返回与请求不一致（协议错误，非政策问题）',
    'title_or_parent_matching_metadata_not_condition_proof':'以标题/分类代替了原文条件证明',
    'unknown_source_evidence_id':'引用了来源中不存在的证据编号',
    'unconfirmed':'对象或事实未能确认（家庭信息不足，不等于不符合）',
    'category_scope_mismatch':'成员已知属性与引文类目矛盾（如年龄/身份范围）',
    'exclude':'与另一条款存在冲突，本次按规则暂缓出卡',
    'rejected':'证据校验未通过',
}
# P2 分档：D 档（不适用/政策无效）不出卡，但原因要如实告诉用户。
WHY_NOT_ZH={
    'region_explicit_other':'政策条目只覆盖其他地区，未按本户属地核验',
    'condition_violated':'家庭已确认情况与政策明确要求不符',
    'policy_invalid':'政策来源或时效校验未通过',
    'safety_wording':'政策口径涉及资格承诺，系统不据此出卡',
    'no_conditions':'政策条件未能从原文确认',
    'evidence_binding_rejected':'所引条件无法回链到政策原文',
    'policy_source_gaps':'政策原文有未写明的口径，无法完整核验',
}
def why_not_lines(coverage):
    """coverage['why_not'] → 用户可读的"为什么没有这张卡"清单（只陈述机制原因）。"""
    items=coverage.get('why_not') or []
    if not items:return []
    lines=['以下政策本次没有列出卡片（说明原因，不代表当地没有这项政策）：']
    for item in items[:6]:
        who='（'+item['member']+'）' if item.get('member') else ''
        lines.append('- '+str(item.get('name') or item.get('code') or '')+who+'：'+
                     WHY_NOT_ZH.get(item.get('reason'), '条件与本户情况不适用'))
    return lines

def _reject_label(item):
    return REJECT_REASON_ZH.get(item.get('error')) or REJECT_REASON_ZH.get(item.get('status')) or '证据校验未通过'

def _empty_search_notes(coverage):
    """rows 为空时如实说明本次检索与筛选情况：查了什么、为什么没出卡片、还缺什么。
    只陈述本轮已发生的事实（检索统计、地区过滤名单、未确认保留数），不推断资格。"""
    if coverage.get('scope_blocked'):return []  # 地区不在服务范围，正文开头已有专门说明
    if coverage.get('region_gate'):return []  # 已进入地区追问门：advice 已说明未核验，不再列空检索事实
    if coverage.get('no_policy_data'):return []  # 只读全国模式：advice 已说明政策库未收录，不再列空检索事实
    notes=[]
    if coverage.get('region_status')=='unknown':
        notes.append('家庭所在县区尚未确认，本次还未开始逐条核对属地政策；确认县区后即可列出当地可用政策。')
        return notes
    if coverage.get('intent_count') is not None:
        notes.append('本次按已知事实与您提出的需求检索了%s个方向，得到%s条候选来源。'%(
            coverage.get('intent_count'),coverage.get('retrieved_chunks') if coverage.get('retrieved_chunks') is not None else coverage.get('unique_candidates')))
    region_dropped=coverage.get('region_dropped') or []
    if region_dropped:
        # 同名家族过滤已在 evidence_loader.load 完成（与已加载政策同族的外地条目不再列出）。
        # 措辞必须让用户看懂"是本地库只收了别的县区的条目"，而不是"系统把这项政策排除了"。
        notes.append('以下政策在本地政策库里的条目只覆盖其他县区，本次没能按属地核验'
                     '（不代表您所在地区没有这项政策，具体以当地经办口径为准）：'+'；'.join(
            d['name']+'（现有条目属地：'+str(d.get('regions') or '来源未明确地区')+'）' for d in region_dropped[:3])+'。')
    kept=(coverage.get('dropped') or {}).get('region_unconfirmed_kept')
    if kept:notes.append('另有%s项候选因政策地区与所在地尚不能比对，保留在核验范围内待确认。'%kept)
    return notes

def claims_on():
    """P3-B 开关：回答里补上"逐字引用的政策原文"，只能引用已绑定证据。"""
    import os as _os
    return _os.environ.get('ANSWER_CLAIMS','0').strip()=='1'

def claims_block(rows):
    """每张卡片取首条已成立条件对应的原文引文，逐条展示，便于用户自己核对。"""
    if not claims_on():return []
    lines=[]
    seen=set()
    for r in (rows or [])[:6]:
        quote=''
        for c in (r.get('claims') or []):
            if c.get('kind')!='satisfied':continue
            for e in (c.get('evidence') or []):
                q=(e or {}).get('quote') or ''
                if q:quote=q;break
            if quote:break
        if not quote or quote in seen:continue
        seen.add(quote)
        lines.append('- **'+r['name']+'**：'+quote)
    return lines

def agent_lines(coverage):
    """P3：对"看过但未出卡"的政策给出机制级核验说明（含可逐字回链的引文）。"""
    agent=coverage.get('agent') or {}
    findings=[f for f in (agent.get('findings') or []) if isinstance(f,dict)]
    if not findings:return []
    lines=['以下政策本次看过但没有形成卡片，核验说明如下：']
    for f in findings[:4]:
        who='（'+f['member']+'）' if f.get('member') else ''
        line='- '+str(f.get('name') or f.get('code') or '')+who+'：'+str(f.get('reason') or '')
        quote=f.get('quote') or ''
        if quote and f.get('quote_ok'):
            line+=' 原文：“'+quote+'”'
        lines.append(line)
    return lines

def _row_line(r,unclear_texts=()):
    """卡片对应的正文行：结论 + 已确认 + 仍需核对（用户最关心的三件事，先说结论）。
    已答“不清楚”的字段不是新的补充要求，从“仍需核对”里移出，由 unclear_note 说明一次。"""
    parts=['- **'+r['name']+'**（'+r['member']+'，'+r['code']+'）：'+r['decision']+'。']
    sat=[x for x in (r.get('satisfied') or []) if x]
    miss=[x for x in (r.get('missing') or []) if x and x not in (unclear_texts or ())]
    if sat:parts.append('已确认：'+'；'.join(sat[:3])+'。')
    if miss:parts.append('仍需核对：'+'；'.join(miss[:3])+'。')
    return ''.join(parts)

def render(advice,rows,questions,coverage):
    """正文只保留有内容的部分：不再有“核验到的政策线索”“本次检索情况”这类固定小节标题，
    有卡片就直接给政策名行，没卡片就只陈述检索事实。免责句只在会话首轮出现一次。
    P3-B：有卡片且 ANSWER_STYLE=claims 时，整段改用 claim 驱动三段式（不含模型散文）。"""
    from . import claim_answer
    if claim_answer.enabled() and rows:
        return claim_answer.answer(rows,questions,coverage)
    text=advice
    unclear=coverage.get('unclear_facts') or {}
    if rows:
        text+='\n\n'+'\n'.join(_row_line(r,unclear) for r in rows)
        _claims=claims_block(rows)
        if _claims:text+='\n\n原文依据（逐字引用政策原文）：\n'+'\n'.join(_claims)
    else:
        notes=_empty_search_notes(coverage)
        if notes:text+='\n\n'+'\n'.join('- '+n for n in notes)
        elif not coverage.get('region_gate') and not coverage.get('scope_blocked'):
            text+='\n\n本次还没有得到可以核对的政策条目。'
    note=unclear_note(rows,coverage)
    if note:text+='\n\n'+note
    rejected=coverage.get('rejected_policies') or []
    if rejected:
        text+='\n\n以下政策本次看过，但未形成可核验的卡片：'
        for r in rejected:
            who='（'+r['member']+'）' if r.get('member') else ''
            text+='\n- '+r['name']+who+'：'+_reject_label(r)
    _agent=agent_lines(coverage)
    if _agent:text+='\n\n'+'\n'.join(_agent)
    why=why_not_lines(coverage)
    if why:text+='\n\n'+'\n'.join(why)
    if questions:text+='\n\n还需要补充下面几项；不确定就直接选“不清楚”，系统不会替您猜。'
    pending=coverage.get('interpretation_pending',0);failed=coverage.get('interpretation_failed',0)
    if pending:text+='\n\n仍有%s项检索来源未完成分析；本次未将它们判为符合或不符合。可继续分析，系统会复用已核对来源。'%pending
    if failed:text+='\n\n%s项候选来源分析未通过校验，暂未给出结论；这与政策缺条款、家庭不符合是不同情况。'%failed
    if coverage.get('candidate_budget_truncated'):text+='\n\n另有部分相关来源尚未逐条核验完毕，以上结果可能不完整。'
    if coverage.get('disclaimer') and not coverage.get('scope_blocked'):text+='\n\n'+DISCLAIMER
    return text


def result_signature(rows,questions):
    """本轮结论指纹：卡片（政策×明细×成员）+ 追问键。用于识别"追问但结论没变"的重复轮次。"""
    import hashlib as _h,json as _j
    payload=dict(rows=[[r.get('code'),r.get('detail_id'),r.get('member'),r.get('decision')] for r in (rows or [])],
                 questions=[q.get('key') for q in (questions or [])])
    return _h.sha256(_j.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def render_repeat(rows,questions,coverage):
    """结论与上一轮相同的轮次：只回结论摘要 + 待补信息，不再重复整篇核验明细。
    重复轮次一定不是会话首轮，因此不再重复免责句。"""
    text='本轮补充的信息还没有改变判定结论，和上一轮一致。'
    unclear=coverage.get('unclear_facts') or {}
    if rows:
        text+='\n\n'+'\n'.join(_row_line(r,unclear) for r in rows)
        note=unclear_note(rows,coverage)
        if note:text+='\n\n'+note
    elif coverage.get('rejected_policies'):
        text+='\n\n本轮仍没有可以出结论的政策条目。'
    if questions:
        labels=[str(q.get('label') or q.get('question') or q.get('key') or '').strip(' 。.？?！!；;') for q in questions if q]
        picked=[x for x in labels if x][:3]
        if picked:text+='\n\n补充'+'、'.join(picked)+'中的任意一项，结论就会更新。'
    if coverage.get('disclaimer') and not coverage.get('scope_blocked'):text+='\n\n'+DISCLAIMER
    return text
