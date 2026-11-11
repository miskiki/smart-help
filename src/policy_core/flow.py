"""Pilot controller: no calls to legacy eligibility, relevance or question branches."""
import collections
import copy
import datetime
import hashlib
import json
from chain_trace import traced
from . import store
from .catalog import refresh,applicable_region,digest
from .engine import decide,atoms
from .fields import dictionary,merge_definitions,semantic_id
from .facts import state,ingest,apply,synchronize_definitions
from .questions import plan
from .advice import generate
from .publishing import prioritize,start
from .settings import ENGINE_VERSION,settings

STATUS={'matched':'初步匹配','facts_missing':'待补充家庭事实','policy_incomplete':'政策依据待完善'}

def date_status(instance,today):
    for name,lower in [('effect_date',True),('expire_date',False)]:
        value=instance['detail'].get(name) or instance['policy'].get(name)
        if not value:continue
        try:date=datetime.date.fromisoformat(str(value)[:10])
        except ValueError:return None
        if (lower and today<date) or (not lower and today>date):return False
    return True

def assess(hh,catalog,publications,fields):
    from kg_retrieval import _load,_cache,match_region_nodes
    nodes=_load()[0][0];index=_cache['region_idx'];region=hh.get('region')
    hh_nodes=match_region_nodes(region or '',nodes,index)[0]
    facts=state(hh)['facts'];members=state(hh)['members']
    rows=[];audit=[];counts=collections.Counter();regional_pending=set();unmigrated=collections.Counter()
    base_names=set(dictionary())
    today=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
    for instance in catalog['instances']:
        region_ok=applicable_region(instance,region,nodes,index,hh_nodes)
        if region_ok is False:continue
        active=date_status(instance,today)
        if active is False:continue
        if not instance['pilot']:
            unmigrated[instance['policy'].get('cat1') or '未分类']+=1;continue
        record=publications.get(instance['digest'],{})
        counts[record.get('status','queued')]+=1
        if not record.get('spec') or record.get('status') not in ('published','incomplete'):
            regional_pending.add(instance['digest']);continue
        spec=record['spec']
        try:defs=merge_definitions(fields,{k:v for k,v in record['fields'].items() if k not in base_names})
        except ValueError:
            counts['field_conflict']+=1;regional_pending.add(instance['digest']);continue
        subjects=[None] if spec['subject']=='household' else members
        for member in subjects:
            result=decide(spec,facts,defs,member)
            if result['status'] in ('matched','facts_missing','policy_incomplete') and region_ok is None and facts.get('household:region',{}).get('state')!='known':
                result['status']='direction_unconfirmed';result['direction_missing']=['household:region']
            elif result['status'] in ('matched','facts_missing','policy_incomplete') and (region_ok is None or active is None):
                result['status']='policy_incomplete';result['gaps']=result['gaps']+['政策地区或有效日期范围尚待确认']
            p,d=instance['policy'],instance['detail']
            item=dict(instance=instance,result=result,fields=defs,spec=spec,name=spec['benefit_name'],member_no=member)
            if result['status'] in STATUS or result['status']=='direction_unconfirmed':rows.append(item)
            else:audit.append(dict(instance_id=instance['id'],member=member,result=result))
            if result['status']=='direction_unconfirmed' or result.get('visible') is False:audit.append(dict(instance_id=instance['id'],member=member,status=result['status'],visible=result.get('visible',True),missing=result['direction_missing'],publication_digest=instance['digest']))
    coverage=dict(pilot=['医疗','残疾'],catalog_version=catalog['revision'],counts=dict(counts),pending_source_groups=len(regional_pending),unmigrated_categories=sorted(unmigrated),inventory=catalog['inventory'])
    prioritize(regional_pending)
    return rows,audit,coverage

def build_rows(assessments):
    rows=[]
    for item in assessments:
        result=item['result']
        if result['status'] not in STATUS or result.get('visible') is False:continue
        p=item['instance']['policy'];d=item['instance']['detail'];defs=item['fields']
        labels=lambda keys:[('家庭' if k.split(':',1)[0]=='household' else '成员'+k.split(':',1)[0])+'：'+defs[k.split(':',1)[1]]['label'] for k in keys]
        missing=labels(result['missing']) if result['status']=='facts_missing' else ['当前条款尚未通过完整资格核验，需要补齐政策依据；不要求你解释法规或上传证明'] if result['status']=='policy_incomplete' else []
        association=result['association']
        def witnesses(node):
            if node.get('value') is not True:return []
            if node.get('field'):return [node]
            return [w for child in node.get('children',[]) for w in witnesses(child)]
        evidence=[('家庭' if w['entity']=='household' else '成员'+w['entity'])+'已说明'+defs[w['field']]['label']+'：'+str(w.get('actual')) for w in witnesses(association)]
        citations=[cite for expression in ('association','eligibility') for atom in atoms(item['spec'][expression]) for cite in atom.get('evidence',[])]
        basis='；'.join(dict.fromkeys(x for x in [p.get('doc'),d.get('conditions') or p.get('conditions'),d.get('target') or p.get('target')]+[c['quote'] for c in citations] if x))
        member='家庭' if item['member_no'] is None else '成员'+str(item['member_no'])
        row=dict(
            code=p['code'], name=item['name'], member=member,
            member_no=item['member_no'] or 0,
            detail_id=d.get('detail_id'), detail_region=d.get('region'),
            level=p.get('level') or '', version=p.get('version') or '',
            cat1=p.get('cat1') or '',
            conditions=d.get('conditions') or p.get('conditions') or '',
            target=d.get('target') or p.get('target') or '',
            decision=STATUS[result['status']], missing=missing,
            relevance_evidence=evidence, relevance='related',
            reason='关联依据：'+'；'.join(evidence)+'；'+(
                '按当前自述满足已发布分支的必要条件'
                if result['status']=='matched' else '；'.join(missing)),
            verify=(['最终资格和金额由经办部门审核'] if result['status']=='matched'
                    else ['补充具体家庭事实'] if result['status']=='facts_missing'
                    else ['待补齐政策依据，不要求群众解释法规']),
            benefit=d.get('benefit') or p.get('benefit') or '原文未明确标准',
            process=d.get('process') or p.get('process') or '',
            contact=d.get('contact') or p.get('contact') or '',
            basis=basis, source='policy_core',
            row_id=item['instance']['id']+':'+str(item['member_no']),
            proof=result,
            sources=[dict(policy_code=p['code'],version=p.get('version'),
                          detail_id=d.get('detail_id'),citations=citations)],
            publication_digest=item['instance']['digest'],
            benefit_family=item['spec']['benefit_family'],
        )
        rows.append(row)
    # Merge only verifiably identical execution, location, period, standard and name.
    def semantic(node):
        if isinstance(node,list):return [semantic(value) for value in node]
        if not isinstance(node,dict):return node
        return {k:semantic(v) for k,v in node.items() if k not in ('evidence','sources','fact_source')}
    merged={}
    for row in rows:
        key=digest([row['benefit_family'],row['name'],row['member'],row['detail_region'],row['version'],row['benefit'],row['decision'],semantic(row['proof']),row['conditions']])
        if key in merged:
            merged[key]['sources']+=row['sources']
            merged[key].setdefault('also_codes',[]).append(row['code'])
        else:merged[key]=row
    return sorted(merged.values(),key=lambda r:list(STATUS.values()).index(r['decision']))

def metadata(hh,rows,questions,coverage,audit,advice_audit=None):
    from household_state import simple
    import smart_matcher_kb as sm
    mj=dict(flow_version=2,hh=simple(hh),table_recs=rows,recs=rows,questions=questions,coverage=coverage,engine_version=ENGINE_VERSION,decision_audit=audit,advice_audit=advice_audit or {},judge_status='core_verified')
    return dict(match_json=mj,sources=[dict(policy_code=r['code'],policy_name=r['name'],excerpt=r['basis']) for r in rows],cards=rows,questions=questions,progress=dict(round=state(hh)['round'],max_rounds=3),table_rows=sm.table_rows_from_recs(rows),analysis_md='',judge_status='core_verified')

@traced('policy_core.run')
def run(hh,question,session_id,user_id,llm,answers=None):
    # Read one transaction-consistent source snapshot per analysis. Never execute
    # stale publications while waiting for a background scan to notice an edit.
    catalog=refresh(force=True);start();publications=store.publications(i['digest'] for i in catalog['instances'] if i['pilot'])
    fields=dictionary();base_names=set(fields)
    for pub in publications.values():
        if pub.get('status') in ('published','incomplete') and pub.get('fields'):
            try:fields=merge_definitions(fields,{k:v for k,v in pub['fields'].items() if k not in base_names})
            except ValueError:continue
    st=state(hh)
    synchronize_definitions(st,fields)
    if answers is None and st.get('pending') and question.strip(' 。！!') in ('不清楚','不知道','都不清楚','全部不清楚','暂时无法提供'):
        answers={q['key']:'不清楚' for q in st['pending']}
    if answers is not None:
        for q in st.get('pending',[]):
            name=q['fact_key'].split(':',1)[1]
            if name in fields and semantic_id(fields[name])!=semantic_id(q['definition']):raise ValueError('追问字段含义已更新，请重新分析后再补充')
            fields.setdefault(name,q['definition'])
        apply(hh,answers,fields)
    else:
        text=question
        if not st['text_hashes']:
            text=hh.get('supplementary_info') or question
            old_answers=(hh.get('clarification') or {}).get('answered',{})
            if old_answers:text+='\n此前用户明确补充：'+json.dumps(old_answers,ensure_ascii=False)
        ingest(hh,text,fields,llm)
    from kg_retrieval import service_scope_check
    scope,reason=service_scope_check(hh)
    if scope=='blocked':return reason,[],metadata(hh,[],[],{},[])
    assessed,audit,coverage=assess(hh,catalog,publications,fields)
    from .explanations import requested,explain
    codes=requested(question)
    if codes:
        body,explanation_audit=explain(codes,assessed,audit,catalog,fields,llm)
        return body,[],metadata(hh,[],[],coverage,audit,explanation_audit)
    # Pending definitions are frozen with their question and fact snapshot.
    for row in assessed:
        for key,definition in row['fields'].items():fields.setdefault(key,definition)
    qs=plan(st,assessed,fields,llm)
    if qs:
        body='**请补充以下家庭信息**\n\n以下问题根据已知家庭情况和相关政策缺项生成；已说明的信息会保留，不清楚的可以直接说明。\n\n'
        body+='本次重点：'+'、'.join(dict.fromkeys(q['definition']['label'] for q in qs))+'。请在下方逐项补充。'
        body+='\n\n本次资格核验覆盖医疗、残疾；其他方向先记录线索，尚不作资格结论。'
        confirmed=build_rows([r for r in assessed if r['result']['status']=='matched'])
        if confirmed:body+='\n\n现有信息已形成%s条初步匹配，可在下方查看；补充或更正信息后会重新核验，最终由经办部门审核。'%len(confirmed)
        result=metadata(hh,confirmed,qs,coverage,audit);result['session_id']=session_id
        return body,sorted({r['code'] for r in confirmed}),result
    rows=build_rows(assessed)
    body,advice_audit=generate(hh,st,rows,coverage,fields,llm)
    if coverage['pending_source_groups']:body+='\n\n**覆盖说明**：本地区还有%s组试点条款尚未通过自动发布校验，系统将补查或等待来源更新；目前不能对这些条款作完整资格结论，不代表家庭不符合。'%coverage['pending_source_groups']
    body+='\n\n本次核验覆盖医疗、残疾业务；其他业务保留政策查询和线索，尚未接入新资格核验。'
    result=metadata(hh,rows,[],coverage,audit,advice_audit);result['session_id']=session_id
    st['last_analysis']=dict(catalog_version=catalog['revision'],engine_version=ENGINE_VERSION,rows=[dict(row_id=r['row_id'],decision=r['decision']) for r in rows])
    result['match_json']['hh']=__import__('household_state').simple(hh)
    from chain_trace import _emit,_current
    _emit(_current.get(),'policy_core.result',engine_version=ENGINE_VERSION,retained=len(rows),statuses=dict(collections.Counter(r['decision'] for r in rows)),coverage=coverage['counts'],advice_status=advice_audit['status'])
    return body,sorted({r['code'] for r in rows}),result
