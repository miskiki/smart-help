"""Explain exclusions from the current interpreter proof, never legacy judgments."""
import json
import re
from .publishing import parse_json

def requested(question):
    if not re.search(r'为什么|为何|未匹配|没匹配|不推荐|未推荐|不适用',question):return []
    return sorted(set(re.findall(r'(?<![A-Z0-9_-])[CSUX]-[A-Z]+-\d+(?![A-Z0-9_-])',question.upper())))

def explain(codes,assessed,audit,catalog,fields,llm):
    context=[];summaries=[]
    labels={'matched':'按当前自述初步匹配','facts_missing':'相关但缺少家庭事实','policy_incomplete':'资格依据尚未通过完整核验','direction_unconfirmed':'关联方向尚未确认','not_applicable':'当前必要条件不满足'}
    for code in codes:
        instances=[i for i in catalog['instances'] if i['policy']['code']==code]
        if not instances:
            summaries.append(code+'：当前有效目录中没有该政策，未用旧版本补作资格结论。');continue
        if not any(i['pilot'] for i in instances):
            summaries.append(code+'：尚未接入新资格核验链路，目前只提供政策查询。');continue
        decisions=[dict(instance_id=r['instance']['id'],member=r['member_no'],result=r['result']) for r in assessed if r['instance']['policy']['code']==code]
        decisions += [r for r in audit if r.get('result') and r['instance_id'] in {i['id'] for i in instances}]
        context.append(dict(code=code,versions=sorted({str(i['policy'].get('version') or '') for i in instances}),decisions=decisions))
        if not decisions:
            summaries.append(code+'：当前来源尚未通过自动发布校验，或不在本户已确认地区及有效日期范围内；本次没有据此判定家庭不符合。');continue
        for d in decisions:
            r=d['result'];member='家庭' if d['member'] is None else '成员'+str(d['member'])
            line=code+'（'+member+'）：'+labels[r['status']]
            if r['status']=='not_applicable':
                def false_leaves(node):
                    if node.get('value') is not False:return []
                    if node.get('field'):return [node]
                    return [x for c in node.get('children',[]) for x in false_leaves(c)]
                conditions=false_leaves(r['proof'])
                line+='。核验记录：'+'；'.join(fields.get(c['field'],{}).get('label',c['field'])+'，自述值='+str(c.get('actual'))+'，条款比较='+c.get('op','')+' '+str(c.get('expected')) for c in conditions)
            elif r['status'] in ('facts_missing','direction_unconfirmed'):
                missing=r.get('missing') or r.get('direction_missing',[])
                if missing:line+='。尚缺：'+'、'.join(('家庭' if k.startswith('household:') else '成员'+k.split(':',1)[0])+'的'+fields.get(k.split(':',1)[1],{}).get('label',k) for k in missing)
            summaries.append(line+'。')
    fallback='本次根据当前家庭事实和当前有效政策版本重新核验：\n\n'+'\n\n'.join(summaries)
    try:
        draft=parse_json(llm('解释指定政策为何未推荐或处于未决状态',json.dumps(dict(context=context,verified_summary=summaries),ensure_ascii=False),system='根据核验proof和verified_summary动态解释给用户。不能改变状态、串用成员、添加资格或把尚未通过发布等同不符合；不要要求上传证明或让群众解释法规。输出JSON {"markdown":"中文Markdown解释"}。',temperature=.2,purpose='core_explanation'))
        body=draft.get('markdown')
        if not isinstance(body,str) or not body.strip() or len(body)>8000:raise ValueError('invalid_explanation')
        if not set(re.findall(r'(?<![A-Z0-9_-])[CSUX]-[A-Z]+-\d+(?![A-Z0-9_-])',body))<=set(codes):raise ValueError('unexpected_policy')
        audit=parse_json(llm('独立核对解释，没有证据不得确认资格',json.dumps(dict(context=context,verified_summary=summaries,candidate=body),ensure_ascii=False),system='检查candidate逐句是否忠实核验记录、成员、未知条件、政策版本和资格状态；不允许把条件失败解释为满足，或把依据未通过当不适用，不得新增金额或要求上传证明。输出JSON {"supported":true或false,"violations":[原因]}。',temperature=0,purpose='core_explanation_audit'))
        if audit.get('supported') is not True:raise ValueError('unsupported_explanation')
        return body,dict(status='accepted')
    except Exception as exc:return fallback,dict(status='proof_summary',reason=type(exc).__name__)
