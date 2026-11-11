"""Full-library model reasoning. No legacy rule retrieval or eligibility verdicts."""
import copy
import hashlib
import os
import json
import re
import time
from .full_settings import MODE,VERSION,limits
from .full_runtime import Budget,event
from .fields import dictionary,merge_definitions
from .facts import state,apply,synchronize_definitions
from .full_facts import ingest,refresh_full_geo
from .retrieval import plan as retrieval_plan,retrieve,prioritize
from .evidence_loader import load as load_evidence
from .reasoning import reason,build_rows
from .full_questions import plan as question_plan
from .result_model import metadata,render

def region_ask_gate_enabled():
    """地区追问门开关：默认开；REGION_ASK_GATE=0 回到旧行为（忽略 service_scope_check 的 ask）。"""
    return os.environ.get('REGION_ASK_GATE', '1').strip().lower() not in ('0', 'off', 'false', 'no')

def _service_scope_national():
    """SERVICE_SCOPE=national：地区口径不再默认江西（文案与追问判定随之调整）。"""
    try:
        from .region import service_scope_mode
        return service_scope_mode() == 'national'
    except Exception:
        return False

def region_needs_ask(scope, region_known, value):
    """地区未解析到县区级时是否应走地区追问门（M1）。

    历史只在 service_scope_check 返回 ask 时追问；但家庭只写到"江西省"时旧判定返回 ok，
    追问门永不触发，等于默认按江西省检索。现在只要地区有内容却解析不出市/县（区）就追问；
    全国模式下省级/市级可解析属于正常口径（由 region.canonical_levels 全国区划层判定）。
    """
    if not region_ask_gate_enabled() or not region_known:
        return False
    if scope not in ('ask', 'ok'):
        return False
    return _region_unresolved(value)

def _region_unresolved(value):
    """地区文本是否"有内容但解析不出可核验的市/县区"。省名带矛盾市（江西省长沙市）
    解析到 province 层级，同样属于无法按属地对齐，需要追问。"""
    text = str(value or '').strip()
    if not text:
        return False
    try:
        from .region import canonical_levels
        level = (canonical_levels(text) or {}).get('level')
    except Exception:
        return True
    return level in ('unknown', 'province')

def region_ask_question(fields):
    """追问门要用户补的字段必须是 household:region 本身（可结构化回填），不能是自由文本讨论。"""
    definition = (fields or {}).get('region') or {}
    if not definition:
        return None
    return dict(key='core|household|region', fact_key='household:region',
                label='家庭：' + str(definition.get('question') or '家庭所在市／县（区）全称是？'),
                reason='用于补充家庭事实，尚未确认政策资格。', kind='text', options=[],
                value='', conflict=False, definition=definition, trigger_fact_keys=[])

def _region_gate_advice(reason):
    text = str(reason or '').strip().rstrip('。；;')
    if not text:
        text = ('家庭所在地区还不能确认属于服务范围' if _service_scope_national()
                else '家庭所在地区还不能确认属于江西省服务范围')
    return (text + '。本次没有在政策库中按属地逐条核验，也不会据此生成政策卡片；'
            '请补充家庭所在市／县（区）全称（例如“江西省赣州市章贡区”）。')

def national_readonly_enabled():
    """NATIONAL_READONLY：只读全国模式（用于单独灰度，默认关）。

    开启后，家庭所在地区在政策库里没有任何收录条目时如实说明"暂无收录"：
    既不再默认套用江西省政策，也不生成任何资格结论，更不出卡片。
    探测异常（covered=None）一律放行，绝不因探测失败误判"未收录"。
    """
    return os.environ.get('NATIONAL_READONLY', '0').strip().lower() in ('1', 'on', 'true', 'yes')

def _region_coverage(value):
    """数据驱动的地区收录探测；任何异常返回 None（调用方按"未确认"放行）。"""
    try:
        from .coverage_probe import coverage as _cov
        got = _cov(value)
        return got if isinstance(got, dict) else None
    except Exception:
        return None

def _readonly_no_data_advice(region_label, cov):
    """未收录地区的确定性文案：只说政策库收录事实，不作资格判断。"""
    label = str(region_label or '').strip()
    cov = cov if isinstance(cov, dict) else {}
    level = str(cov.get('level') or '')
    where = label or ('该' + {'province': '省', 'city': '市', 'county': '县区'}.get(level, '地区'))
    rows = cov.get('rows')
    text = ('当前政策库还没有收录%s的帮扶政策（已核对地区收录范围：%s条政策明细）。'
            '本次为只读模式，仅说明政策库收录情况，不生成政策卡片，也不作资格判断。' % (
                where, rows if rows is not None else 0))
    if level == 'county':
        text += '如县区名称有误，可补充所在市／县（区）全称后重新核对。'
    else:
        text += '请补充所在市／县（区）全称后重新核对。'
    text += '政策库收录该地区政策后，系统会按新数据重新核验。'
    return text

def _apply_scope_filter(packs, coverage, household_region):
    """POLICY_SCOPE 开启时按 policy_scope 结构化范围三态过滤候选（默认关闭）。

    只在证据候选层过滤：True 保留、False 丢弃（明确不适用）、None 保留待核、
    无 scope 行保留。任何异常都退回原候选集合，绝不让范围表问题导致丢政策。
    """
    if not packs:
        return packs
    try:
        from . import scope_filter
        if not scope_filter.enabled():
            return packs
        kept, report = scope_filter.filter_packs(packs, household_region or '')
    except Exception as exc:
        coverage['scope_filter'] = dict(state='error', error=type(exc).__name__ + ':' + str(exc)[:120])
        return packs
    summary = scope_filter.summary(report)
    coverage['scope_filter'] = summary
    try:
        event('scope.filter', kept=summary['kept'], dropped=summary['dropped'],
              unknown=summary['unknown'], input=summary['input'], output=summary['output'],
              version=summary['version'])
    except Exception:
        pass
    return kept


def model_identity():
    from kb_rag import _cfg
    from urllib.parse import urlparse
    cfg=_cfg()
    return json.dumps(dict(model=cfg.get('llm_model'),platform=cfg.get('platform'),host=urlparse(cfg.get('base_url','')).hostname),sort_keys=True)

def source_consultation(text,config,session_id):
    """No household qualification claims: quote current full-library records."""
    candidates,coverage=retrieve([dict(intent_id='C1',subject='household',direction='政策原文查询',query=text,fact_keys=[])],config)
    # Without a known county, consultations remain content-only and label geography.
    from kb_rag import _db
    from sqlalchemy import text as sql,bindparam
    engine=_db();records=[]
    try:
        with engine.connect() as conn:
            codes=list(dict.fromkeys(c['policy_code'] for c in candidates))[:6]
            if codes:
                query=sql('SELECT policy_code,policy_name,regions,target,conditions,benefit,cur_version FROM policy WHERE status=:status AND policy_code IN :codes').bindparams(bindparam('codes',expanding=True))
                records=[dict(r) for r in conn.execute(query,dict(status='在用',codes=codes)).mappings()]
    finally:engine.dispose()
    body='**全库政策资料查询**\n\n'
    for p in records:
        body+='**'+p['policy_name']+'（'+p['policy_code']+'）**\n\n地区：'+str(p['regions'] or '来源未明确')+'；版本：'+str(p['cur_version'])+'\n\n'+str(p['conditions'] or p['target'] or '本库尚未收录资格条款')+'\n\n'
    if not records:body+='本次检索未找到可引用的在用政策资料。\n\n'
    body+='以上是来源资料查询，不表示任何家庭符合资格；需结合具体地区、执行明细和家庭事实分析。'
    return body,[],dict(sources=[dict(policy_code=p['policy_code'],policy_name=p['policy_name'],excerpt=p['conditions'] or p['target'] or '') for p in records],
        cards=[],questions=[],table_rows=[],match_json=dict(mode=MODE,flow_version=3,kind='consultation',content_only=True,coverage=coverage),judge_status='content_only')


def _ingest_with_normalize(hh,text,fields,model,extract_only=None):
    """口语输入先规范化再走严格抽取（见 policy_core/normalize.py）。

    为什么需要：抽取校验要求引文逐字、成员写成"成员N"。真实用户说
    "我家在莲花县，两口人，我62岁肢体残疾二级"时两条都不满足，实测 7.8 秒
    返回 0 条事实。这里先重排成结构化文本，抽取照旧严格，审计仍拿用户原话核对。
    """
    from .normalize import needs_normalize,to_structured,verify_support,sanitize
    from .full_facts import ingest
    if extract_only or not needs_normalize(text):
        return ingest(hh,text,fields,model,extract_only=extract_only,source_text=text)
    structured=to_structured(text,model)
    if structured:
        structured,_dropped=sanitize(structured,text)
    if structured:
        ok,why=verify_support(structured,text,model)
        if ok:
            try:
                ingest(hh,structured,fields,model,source_text=text)
                if (state(hh).get('facts')):return
            except ValueError:
                pass
    return ingest(hh,text,fields,model,source_text=text)

_WHOLE_FORM_RE=re.compile(r'(户\s*ID|户\s*号|户主\s*[:：]|户内家庭成员|家庭基本情况|家庭成员\s*[1１]\s*[，,、]|^\s*一、\s*家庭)')

def _is_whole_form(text):
    t=text or ''
    return bool(0 < len(t) < 4000 and _WHOLE_FORM_RE.search(t))

def _new_household(hh,text):
    """整户重新申报识别：户ID/户主与上一户不同，或重新粘贴整户表单。
    语义：整户表单是新基线，上一户/上一轮会话的旧事实不能沿用——历史缺陷是
    换了一户人（35岁户主）后仍沿用上一户（62岁二级残疾）的事实，直接答错。"""
    if not hh or not hh.get('members'):return False
    t=text or ''
    m_id=re.search(r'户\s*ID\s*[：:]\s*([A-Za-z0-9_\-]{2,})',t)
    m_head=re.search(r'户主\s*[：:]\s*([^\s，,、；;：:]{1,8})',t)
    old_id=hh.get('household_id') or ''
    old_head=hh.get('household_head') or ''
    if m_id and old_id and m_id.group(1)!=old_id:return True
    if m_head and old_head and m_head.group(1)!=old_head:return True
    return _is_whole_form(t)

def _reset_household(hh,text):
    """清空上一户家庭事实，保留会话归属；本次表单作为新户的唯一事实来源。"""
    for key in ('region','risk','enjoyed','enjoyed_known','wills','will_score','will_level',
                'household_head','tags','supplementary_info'):
        hh.pop(key,None)
    hh['members']=[];hh['policy_core']={};hh['supplementary_info']=text or ''

def run(hh,text,session_id,user_id,llm,answers=None):
    config=limits();model=Budget(llm,config);model.session_id=session_id
    hh.setdefault('members',[])
    if answers is None and _new_household(hh,text):
        _reset_household(hh,text)
    st=state(hh)
    if answers is None and not st['facts'] and not re.search(r'成员|\d+\s*岁|我家|本户|家里|户内|家庭情况|我们家|我(?:是|有|能|想)',text) and re.search(r'原文|标准|条件|哪些|怎么办|怎么申请|政策',text):
        return source_consultation(text,config,session_id)
    if st.get('mode')!=MODE:
        original=hh.get('supplementary_info') or text
        prior_answers=(hh.get('clarification') or {}).get('answered',{})
        hh['policy_core']={};hh['members']=[];st=state(hh);st['mode']=MODE
        text=original if original==text else original+'\n'+text
        if prior_answers:text+='\n此前用户补充：'+json.dumps(prior_answers,ensure_ascii=False)
    fields=merge_definitions(dictionary(),st.get('full_fields',{}));synchronize_definitions(st,fields)
    # An explicit all-unknown reply consumes the current questions without inventing facts.
    if answers is None and st['pending'] and text.strip(' 。!！') in ('不清楚','不知道','都不清楚','全部不清楚','暂时无法提供'):
        answers={q['key']:'不清楚' for q in st['pending']}
    extraction=dict(status='structured_answers' if answers is not None else 'accepted')
    if answers is not None:
        apply(hh,answers,fields);refresh_full_geo(st,fields)
        # 即使有结构化回答，也处理补充文本中的新信息
        try:
            if text.strip() not in ('追问补充',''):
                _ingest_with_normalize(hh,text,fields,model,extract_only=None)
        except Exception as exc:
            from kb_rag import AnswerCancelled
            if isinstance(exc,AnswerCancelled):raise
            extraction=dict(status='partial',error=type(exc).__name__,reason=str(exc)[:300])
    else:
        try:
            if not (st['facts'] and text.strip(' 。!！') in ('继续','继续分析','继续匹配','重新分析','继续核验')):_ingest_with_normalize(hh,text,fields,model)
        except Exception as exc:
            from kb_rag import AnswerCancelled
            if isinstance(exc,AnswerCancelled):raise
            # 已有可靠家庭事实时，本轮"补充信息没读出东西"不能拖垮整轮：
            # 旧事实仍可用于检索核验，只是本轮不新增事实。历史缺陷是把这种情况判成
            # failed，随后跳过检索→0 卡，用户追问一句就得到"暂无线索"。
            extraction=dict(status='failed' if not st['facts'] else 'partial',
                            error=type(exc).__name__,reason=str(exc)[:300])
    st['mode']=MODE
    st['fact_revision']=hashlib.sha256(json.dumps({k:dict(value=f.get('value'),state=f.get('state'),schema_id=f.get('schema_id')) for k,f in st['facts'].items()},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    hh['supplementary_info']=((hh.get('supplementary_info') or '')+'\n'+text)[-24000:] if text not in (hh.get('supplementary_info') or '') else hh['supplementary_info']
    region=st['facts'].get('household:region',{})
    # T4 地区软过滤：开启时家庭地区未确认（空/冲突/无法解析）也继续检索与核验，
    # 结果按"地区未确认"标注；规则引擎仍只在 known 时启用（判定口径不放宽）。
    try:
        from .region import soft_filter_enabled
        region_soft=soft_filter_enabled()
    except Exception:
        region_soft=False
    region_known=region.get('state')=='known'
    # 服务范围/属地校验必须先于规则引擎与检索：service_scope_check 的 ask 表示
    # "地区有内容但无法确认属于江西"（外省地级市不带省字、街道名、省市矛盾）。
    # 历史缺陷是只处理 blocked、忽略 ask，继续在江西政策库里核验并可能出 B 档卡片，
    # 效果等于把家庭地区默认成江西。ask 时必须先追问，不能静默匹配。
    scope, scope_reason = '', ''
    if extraction['status'] != 'failed':
        try:
            from kg_retrieval import service_scope_check
            scope, scope_reason = service_scope_check(hh)
        except Exception:
            scope, scope_reason = '', ''
    region_ask = region_needs_ask(scope, region_known, region.get('value'))
    # M0 可观测性：规则快速路径（毫秒级求值、无检索/核验事件）也要让用户看到
    # "地区确认→事实抽取完成"两段真实进度，避免前端只有两三条模型事件。
    try:
        from chain_trace import event as _trace_event
        _trace_event('region.normalized', region_status=('known' if region_known else (region.get('state') or 'unknown')))
        _trace_event('extraction.completed', status=extraction.get('status'), model_calls=model.calls)
    except Exception:
        pass
    coverage=dict(engine=VERSION,scope='原全量政策库向量候选；不等于逐条资格核验',fact_extraction=extraction)
    if not region_known:coverage['region_status']='unknown'
    if region_soft and not region_known:coverage['region_filter']='soft_unknown'
    if region_ask:
        coverage.update(region_gate=True, region_unconfirmed=scope_reason, region_status='unknown')
        event('region.gate', status='ask', reason=scope_reason, region=str(region.get('value') or ''))
    # 只读全国模式（单独灰度）：数据驱动判断该地区是否被政策库收录。
    # 未收录时不再走规则引擎与检索（江西库里不可能有该地区政策），直接如实说明并 0 卡 0 问；
    # 只读语义=不生成资格结论。covered=None（表缺失/解析不明/查询异常）一律放行。
    if (national_readonly_enabled() and not region_ask and region_known
            and extraction['status'] != 'failed'):
        _cov = _region_coverage(region.get('value'))
        if _cov is not None and _cov.get('covered') is False:
            try:
                from .coverage_probe import summary as _cov_summary
                coverage['region_coverage'] = _cov_summary(_cov)
            except Exception:
                coverage['region_coverage'] = dict(_cov)
            coverage.update(readonly_national=True, no_policy_data=True,
                            scope='只读全国模式：政策库未收录该地区政策，本次不作资格判断')
            event('region.coverage', status='none', region=str(region.get('value') or ''),
                  policies=_cov.get('policies') or 0, rows=_cov.get('rows') or 0)
            _diag = dict(calls=model.calls, seconds=round(time.monotonic() - model.started, 2),
                         extraction=extraction, source_analysis=[],
                         question_plan=st.get('question_plan_status'))
            _result = metadata(hh, [], [], coverage, [], _diag,
                               dict(status='readonly_no_data', model_calls=0))
            _result['session_id'] = session_id
            event('result', rows=0, questions=0, seconds=_diag['seconds'],
                  advice_status='readonly_no_data')
            return _readonly_no_data_advice(region.get('value'), _cov), set(), _result
    # C4 灰度：规则引擎优先。规则未就绪/求值失败自动回退旧链路（coverage 标注原因）。
    if os.environ.get('RULE_ENGINE','on')=='on' and region_known and extraction['status']!='failed' and not region_ask:
        try:
            from .rule_flow import try_rule_run
            _rule_out=try_rule_run(hh,st,fields,coverage,model,session_id,text=text)
            if _rule_out is not None:
                event('result',rows=len(_rule_out[2].get('cards') or []),questions=len(_rule_out[2].get('questions') or []),seconds=_rule_out[2].get('diagnostics',{}).get('seconds'),advice_status='rule_engine')
                return _rule_out
        except Exception as exc:
            from kb_rag import AnswerCancelled
            if isinstance(exc,AnswerCancelled):raise
            coverage['rule_engine']=dict(state='error',error=type(exc).__name__+':'+str(exc)[:160])
    rows=[];assessments=[];audit=[];interpretations={};packs=[]
    if (region_known or region_soft) and extraction['status']!='failed' and not region_ask:
        if scope=='blocked':coverage.update(scope_blocked=scope_reason)
        else:
            try:
                from .retrieval import PROMPT as retrieval_prompt
                # plan_key 必须包含本轮用户原话：新增的明确需求（如"申请雨露计划"）不在事实里，
                # 不参与 fact_revision；漏掉会让旧的 0 方向计划跨轮复用。
                plan_key=hashlib.sha256((st['fact_revision']+retrieval_prompt+model_identity()+json.dumps(fields,sort_keys=True,ensure_ascii=False)+hashlib.sha256(text.encode()).hexdigest()).encode()).hexdigest()
                prior_plan=st.get('retrieval_plan',{})
                intents=prior_plan['intents'] if prior_plan.get('key')==plan_key else retrieval_plan(st,fields,model,config,text=text)
                st['retrieval_plan']=dict(key=plan_key,intents=intents)
                candidates,search_stats=retrieve(intents,config,timeout=max(1,min(20,model.remaining)))
                coverage.update(search_stats)
                # 地区软过滤未确认时传空串：证据层不再直接返回空，而是全量保留并标注 unknown。
                packs,source_stats=load_evidence(candidates,region.get('value') or '',config);coverage.update(source_stats)
                try:packs=prioritize(packs,st,model,config)
                except Exception as exc:
                    from kb_rag import AnswerCancelled
                    if isinstance(exc,AnswerCancelled):raise
                    coverage['rerank_status']=dict(status='unavailable',error=type(exc).__name__)
                coverage['candidate_budget_truncated']=max(0,source_stats.get('eligible_candidates',len(packs))-config['max_candidates'])
                packs=packs[:config['max_candidates']]
                packs=_apply_scope_filter(packs,coverage,region.get('value') or '')
                fields_before_reason=set(fields)
                decisions,fields,reason_audit=reason(hh,packs,fields,model)
                # Initially extracted dynamic facts already belong to this
                # family. Re-read only genuinely new source-introduced fields.
                new_fields={k:v for k,v in fields.items() if k not in fields_before_reason}
                if new_fields and answers is None and model.remaining>32:
                    # A source may introduce a field the initial extractor did
                    # not know. Read only that field, then revalidate judgments
                    # against the revised facts; never reuse old conclusions.
                    values=lambda:{k:dict(value=f.get('value'),state=f.get('state'),schema_id=f.get('schema_id')) for k,f in st['facts'].items()}
                    prior_facts=values()
                    _ingest_with_normalize(hh,text,fields,model,extract_only=new_fields)
                    st['extracted_full_fields']=dict(st.get('extracted_full_fields',{}),**new_fields)
                    if values()!=prior_facts:
                        decisions,fields,reason_audit=reason(hh,packs,fields,model)
                rows,assessments,audit=build_rows(hh,packs,decisions,fields)
                from .result_model import dedupe_rows
                rows=dedupe_rows(rows)  # 同名政策多条来源只留一张卡（政策库跨层级重名很多）
                # P2 匹配核心 v2：全模型路径同样走条件原子三值求值 + 引文逐字回链，给 A/B/C/D 档。
                # 这是排序/标注层，绝不改变"不出资格结论"口径；出错时退回原序并如实标注，
                # 不能因为分档异常把已经通过证据门禁的卡片全部丢掉。
                try:
                    from . import grader as _grader
                    _match_on=_grader.enabled()
                except Exception:
                    _match_on=False
                if _match_on and rows:
                    try:
                        _grades=[];_graded=[]
                        for row in rows:
                            pack=next((p for p in packs if str(row.get('row_id') or '').startswith(p['id']+':')),None)
                            if pack is None:
                                pack=next((p for p in packs if p['policy']['code']==row.get('code')),None)
                            grade=_grader.grade_full_row(row,pack,region.get('value') or '')
                            grade['member']=row.get('member')
                            _graded.append(_grader.annotate_row(row,grade,grade.get('region_info')))
                            _grades.append(grade)
                        _sorted=_grader.sort_rows(_graded)
                        _hidden=[r for r in _sorted if r.get('tier')==_grader.TIER_D]
                        rows=[r for r in _sorted if r.get('tier')!=_grader.TIER_D]  # D 档不出卡
                        _counts=_grader.summary(_grades)['counts']
                        coverage['match_core']=dict(_grader.summary(_grades), hidden_d=len(_hidden))
                        _wn,_wn_seen=[],set()
                        for r in _hidden:
                            _key=(r.get('code'),r.get('member'),r.get('grade_reason') or '')
                            if _key in _wn_seen:
                                continue
                            _wn_seen.add(_key)
                            _wn.append(dict(code=r.get('code'),name=r.get('name'),member=r.get('member'),
                                            tier=r.get('tier'),reason=r.get('grade_reason') or ''))
                        coverage['why_not']=_grader.why_not(_wn)[:8]
                        event('match.graded',cards=len(rows),a=_counts.get('A'),b=_counts.get('B'),
                              c=_counts.get('C'),d=_counts.get('D'),version=_grader.VERSION)
                    except Exception as exc:
                        coverage['match_core']=dict(state='error',error=type(exc).__name__+':'+str(exc)[:120])
                # P0-5：被拒/未确认政策透明化。此前这些来源只留下统计数字（pending/failed），
                # 用户完全看不到"哪些政策被看过、为什么没出卡片"。这里生成紧凑清单供渲染。
                rejected_summary=[];_seen=set()
                def _reject_entry(d,error):
                    if not isinstance(d,dict) or d.get('id') in _seen:return
                    pack=next((p for p in packs if p['id']==d.get('source_id')),None)
                    if not pack:return
                    _seen.add(d.get('id'))
                    rejected_summary.append(dict(code=pack['policy']['code'],name=pack['policy']['name'],
                        member='家庭' if d.get('member')=='household' else '成员'+str(d.get('member') or ''),
                        status=d.get('status') or 'rejected',error=str(error or '')[:90]))
                for d in decisions:
                    if d.get('status')!='related':
                        pr=d.get('primary_review') if isinstance(d.get('primary_review'),dict) else {}
                        qr=d.get('question_direction_review') if isinstance(d.get('question_direction_review'),dict) else {}
                        _reject_entry(d,pr.get('error') or qr.get('error'))
                for b in reason_audit.get('batches',[]):
                    for r in (b.get('rejected') or [])[:8]:
                        if isinstance(r,dict) and r.get('id') and r.get('error'):
                            pack=next((p for p in packs if str(r['id']).startswith(p['id']+':')),None)
                            if pack and r['id'] not in _seen:
                                _seen.add(r['id'])
                                rejected_summary.append(dict(code=pack['policy']['code'],name=pack['policy']['name'],
                                    member='',status='rejected',error=str(r.get('error'))[:90]))
                # P4-2：分析前置门的来源对用户透明化——列出"名称与来源原文不符、未逐条核验"。
                _prefiltered={x['id']:x for x in reason_audit.get('name_prefiltered',[])}
                for pid,x in _prefiltered.items():
                    pack=next((p for p in packs if p['id']==pid),None)
                    if pack and pid not in _seen:
                        _seen.add(pid)
                        rejected_summary.append(dict(code=x.get('code') or pack['policy']['code'],
                            name=x.get('name') or pack['policy']['name'],member='',status='name_gate_prefiltered',
                            error='政策名称判别片段未出现在来源原文中，未进入逐条核验'))
                # P1-9：同一政策多明细分支各产出一条决策，用户名单按（编号×成员）去重。
                _rs_seen=set();_rs_dedup=[]
                for r in rejected_summary:
                    k=(r.get('code'),r.get('member'),r.get('error'))
                    if k in _rs_seen:continue
                    _rs_seen.add(k);_rs_dedup.append(r)
                rejected_summary=_rs_dedup
                coverage['rejected_policies']=rejected_summary[:6]
                reviewed={d['source_id'] for d in decisions}
                coverage['reasoning_audit']=reason_audit
                failed={sid for b in reason_audit.get('batches',[]) if b['status'] in ('failed','rejected','partial') for sid in b.get('attempted_ids',[]) if sid not in reviewed}
                cached_ids=set(reason_audit.get('cached_source_ids',[]))
                interpretations={p['id']:dict(status='ready_name_gate_prefiltered',cache_hit=False) if p['id'] in _prefiltered else dict(status='ready_partial' if p['id'] in reviewed else 'failed' if p['id'] in failed else 'pending',cache_hit=p['id'] in cached_ids) for p in packs}
            except Exception as exc:
                from kb_rag import AnswerCancelled
                if isinstance(exc,AnswerCancelled):raise
                coverage['retrieval_or_analysis_error']=dict(type=type(exc).__name__,message=str(exc)[:250])
    coverage.update(interpretation_pending=sum(a['status']=='pending' for a in interpretations.values()),
        interpretation_failed=sum(a['status']=='failed' for a in interpretations.values()),
        reviewed_sources=sum(a['status'].startswith('ready_') for a in interpretations.values()),
        cache_hits=sum(a.get('cache_hit',False) for a in interpretations.values()),
        analyzed_policy_codes=[p['policy']['code'] for p in packs if interpretations.get(p['id'],{}).get('status','').startswith('ready_')])
    # P3 调查式 agent：对"看过但未出卡"的政策做限定轮次只读核验，产出机制级说明。
    # 只读、不增删卡片、不改档位；异常只写 coverage['agent']，绝不影响主链路。
    if coverage.get('rejected_policies') and not coverage.get('scope_blocked'):
        try:
            from . import agent as _agent
            coverage['agent']=_agent.investigate(hh,packs,decisions,reason_audit,fields,config,model,
                                                 session_id=session_id,text=text)
        except Exception as exc:
            coverage['agent']=dict(state='error',error=type(exc).__name__+':'+str(exc)[:120])
    questions=[] if coverage.get('scope_blocked') else question_plan(st,assessments,fields,model,config)
    if region_ask:
        # 地区未确认时先只问地区：其它家庭事实问了也无法属地核验，容易让用户误解已经核对过。
        _region_q=region_ask_question(fields)
        if _region_q:
            questions=[_region_q]
    from .full_advice import generate,prepare_claim_state,skip_model_advice
    if region_ask:
        # 追问门是确定性文案：不生成模型 advice（既不需要，也不能让它自由发挥属地结论）。
        advice=_region_gate_advice(scope_reason)
        advice_audit=dict(status='region_gate',model_calls=0,
                          disclaimer=prepare_claim_state(st,coverage,fields))
    elif skip_model_advice(rows,coverage):
        # claims 三段式会整体替换散文正文：不再生成一次用不上的模型 advice，
        # 但免责句/轮次/不清楚字段必须照常准备，否则首轮免责句与"此前已说不清楚"提示会丢。
        _disclaimer=prepare_claim_state(st,coverage,fields)
        advice,advice_audit='',dict(status='claims_render',model_calls=0,disclaimer=_disclaimer)
    else:
        advice,advice_audit=generate(hh,st,rows,questions,coverage,fields,model)
    # 重复轮次识别：用户只是追问、没有新增会影响判定的事实时，不再重复整篇核验明细。
    # 历史缺陷是"继续问"每次都原样重发同一份长报告，用户读起来像系统没听懂。
    from .result_model import result_signature,render_repeat
    _sig=result_signature(rows,questions)
    _prev=st.get('result_signature')
    st['result_signature']=_sig
    _repeat=bool(_prev and _prev==_sig and answers is None and (rows or questions)
                 and text.strip(' 。!！') not in ('继续','继续分析','继续匹配','重新分析','继续核验'))
    from kb_rag import _check_cancel
    _check_cancel(session_id)
    body=render_repeat(rows,questions,coverage) if _repeat else render(advice,rows,questions,coverage)
    coverage['repeat_turn']=_repeat
    if extraction['status']=='failed':body='本次家庭信息读取未完成；已保留可靠旧记录，未据此生成新的政策判断。\n\n'+body
    if coverage.get('scope_blocked'):body=coverage['scope_blocked']+'\n\n'+body
    if coverage.get('retrieval_or_analysis_error'):body+='\n\n本次检索或来源分析发生错误，不能据此认为家庭不符合政策；请稍后重试。'
    diagnostics=dict(calls=model.calls,seconds=round(time.monotonic()-model.started,2),extraction=extraction,
        source_analysis=[dict(id=p['id'],source_hash=p['source_hash'],status=interpretations.get(p['id'],{}).get('status'),error=interpretations.get(p['id'],{}).get('error')) for p in packs],
        question_plan=st.get('question_plan_status'))
    # C3 shadow：规则路径对同一次请求复算，差异只写 eval/shadow_log.jsonl，不影响用户输出。
    try:
        from .shadow import run_shadow
        _shadow=run_shadow(hh,rows,session_id,base_seconds=diagnostics['seconds'])
        if _shadow:coverage['shadow']=dict(state=_shadow.get('state'),county=_shadow.get('county'))
    except Exception:pass
    result=metadata(hh,rows,questions,coverage,audit,diagnostics,advice_audit);result['session_id']=session_id
    event('result',rows=len(rows),questions=len(questions),seconds=diagnostics['seconds'],advice_status=advice_audit['status'])
    return body,sorted({r['code'] for r in rows}),result
