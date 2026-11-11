"""Evaluate only independently reviewed policy interpretations, never old verdicts."""
import copy
from .engine import evaluate
from .fields import merge_definitions,dictionary
from .facts import state
from .full_runtime import event

def leaves(node):
    if node.get('field') or 'source_gap' in node:return [node]
    return [leaf for child in node.get('children',[]) for leaf in leaves(child)]

def witnesses(node):
    if node.get('value') is not True:return []
    if node.get('field'):return [node]
    return [leaf for child in node.get('children',[]) for leaf in witnesses(child)]

def pretty(value):return '是' if value is True else '否' if value is False else str(value)

def assess(hh,packs,interpretations):
    st=state(hh);rows=[];audit=[];assessments=[];definitions=dictionary()
    definitions.update(st.get('full_fields',{}));facts=copy.deepcopy(st['facts'])
    for pack in packs:
        analysis=interpretations.get(pack['id'],dict(status='pending'))
        if not analysis['status'].startswith('ready_'):
            audit.append(dict(id=pack['id'],status=analysis['status'],error=analysis.get('error'),reason=analysis.get('reason')));continue
        spec=analysis['interpretation']['spec']
        try:
            fields=merge_definitions(definitions,analysis['interpretation'].get('fields',{}))
        except ValueError:
            audit.append(dict(id=pack['id'],status='field_conflict'));continue
        subjects=st['members'] if spec['subject']=='member' else [None]
        for member in subjects:
            relation=evaluate(spec['association'],facts,fields,member)
            proof=evaluate(spec['eligibility'],facts,fields,member)
            item=dict(name=spec['benefit_name'],result=dict(status='direction_unconfirmed',direction_missing=relation['missing'],missing=[]),fields=fields,spec=spec,member_no=member)
            if proof['value'] is False:
                audit.append(dict(id=pack['id'],member=member,status='contradicted',proof=proof));continue
            partial_witnesses=[n for n in leaves(relation) if n.get('field') and n.get('value') is True and fields[n['field']].get('direction_allowed') is not False]
            if relation['value'] is False or not partial_witnesses:
                if relation['value'] is None:assessments.append(item)
                audit.append(dict(id=pack['id'],member=member,status='direction_unconfirmed',proof=relation));continue
            evidence=witnesses(relation) if relation['value'] is True else partial_witnesses
            if not evidence:
                audit.append(dict(id=pack['id'],member=member,status='no_positive_condition'));continue
            missing=proof['missing']
            source_gaps=list(dict.fromkeys(spec.get('gaps',[])+[n['source_gap'] for n in leaves(proof) if n.get('source_gap')]))
            if pack.get('date_status') is None:source_gaps.append('来源有效日期格式不明确，现行性待核')
            complete=analysis['status']=='ready_full' and not source_gaps
            status='matched' if complete and proof['value'] is True else 'facts_missing' if complete else 'policy_incomplete'
            item['result'].update(status='facts_missing' if missing else status,missing=missing,direction_missing=[])
            assessments.append(item)
            label='家庭' if member is None else '成员'+str(member)
            labels=lambda keys:[('家庭' if k.startswith('household:') else '成员'+k.split(':')[0])+'：'+fields[k.split(':')[1]]['label'] for k in keys]
            satisfied=[('家庭' if w['entity']=='household' else '成员'+w['entity'])+'：'+fields[w['field']]['label']+'='+pretty(w['actual']) for w in witnesses(proof)]
            relation_text=[('家庭' if w['entity']=='household' else '成员'+w['entity'])+'：'+fields[w['field']]['label']+'='+pretty(w['actual']) for w in evidence]
            satisfied=list(dict.fromkeys(relation_text+satisfied))
            decision={'matched':'按现有依据初步匹配','facts_missing':'已满足部分条件，需补充家庭信息','policy_incomplete':'已满足部分条件，政策依据待补齐'}[status]
            p,d=pack['policy'],pack['detail']
            basis='；'.join(dict.fromkeys(x for x in (p.get('doc'),d.get('target') or p.get('target'),d.get('conditions') or p.get('conditions')) if x))
            if not basis:basis='对象依据：'+'；'.join(c['quote'] for w in evidence for c in w.get('evidence',[]))
            benefit=d.get('benefit') or ('本库未收录本分支补贴标准' if d else p.get('benefit')) or '本库未收录补贴标准'
            row=dict(code=p['code'],name=spec['benefit_name'],level=p.get('level',''),version=p.get('version',''),member=label,member_no=member or 0,
                row_id=pack['id']+':'+str(member or 'household'),detail_id=d.get('detail_id'),detail_region=d.get('region') or p.get('regions'),
                decision=decision,analysis_status=status,source='full_model',reason='；'.join(relation_text),relevance_evidence=relation_text,satisfied=satisfied,
                missing=labels(missing),missing_keys=missing,source_gaps=source_gaps or ([] if complete else ['现有来源尚不足以确认完整资格']),
                verify=['按下方问题补充明确事实；政策资料缺项由系统继续核对，不要求上传证明'],
                basis=basis,benefit=str(benefit)+'（原资料记载，现行标准及实际待遇需核对）' if d.get('benefit') or (not d and p.get('benefit')) else str(benefit),
                process=d.get('process') or '本库未收录本分支办理流程',contact='',proof=proof,association=relation,
                source_hash=pack['source_hash'],qualification_published=complete,condition_progress='已核对'+str(len(satisfied))+'项相关事实；'+('已列条件满足' if proof['value'] is True else '仍有未决条件'),
                recall_sources=['vector'],retrieval_refs=[dict(hash=c['hash'],score=c['score']) for c in pack['retrieval']['chunks']])
            rows.append(row)
    st['full_fields']={k:v for row in assessments for k,v in row['fields'].items() if k not in dictionary()}
    event('assessment',rows=len(rows),excluded=len(audit))
    return rows,assessments,audit
