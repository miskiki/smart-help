"""会话级家庭状况描述——通过多轮表述，补充家庭信息进行匹配."""
"""Conversation-owned household state, persisted with each answer snapshot."""
import copy
import json
import logging
import re

logger = logging.getLogger(__name__)

#  供 supplementary_items / simple 结构版本追踪
SCHEMA_VERSION = 4

#  region 正则误匹配排除词
_REGION_BAD = ('小区', '地区', '市区', '园区', '社区', '学区', '开发区', '风景区')
#  enjoyed 否定词
_ENJOYED_NEG = re.compile(r'没有|无|未享受|没享受|不享受')


def _find_member(hh, member):
    """优先用姓名匹配，编号兜底，避免编号漂移导致重复加人。"""
    if member.get('name'):
        for m in hh['members']:
            if m.get('name') and m['name'] == member['name']:
                return m
    for m in hh['members']:
        if m['member_no'] == member['member_no']:
            return m
    return None


def _append_supplementary(hh, question):
    """结构化 + 去重 + 滑动窗口，避免切头丢早期信息。"""
    items = hh.setdefault('supplementary_items', [])
    q = (question or '').strip()
    if q and q not in items:
        items.append(q)
    total = sum(len(x) for x in items)
    while total > 16000 and len(items) > 1:
        total -= len(items.pop(0))
    hh['supplementary_info'] = '\n'.join(items)[-16000:]


def latest(session_id, user_id):
    from models import QaLog
    # Query household snapshots directly: a long sequence of content-only QA
    # must not push the family's structured memory out of a fixed turn window.
    rows = (QaLog.query
            .filter_by(session_id=session_id, user_id=user_id, status='complete')
            .filter(QaLog.match_json.like('%"hh":%'))
            .order_by(QaLog.qa_id.desc())
            .yield_per(50))
    for row in rows:
        try:
            data = json.loads(row.match_json or '{}')
            if isinstance(data, dict) and data.get('hh'):
                import smart_matcher_kb as smk
                return smk.hh_from_simple(data['hh'])
        except (ValueError, TypeError) as e:
            #  不再静默吞异常
            logger.warning('bad match_json qa_id=%s: %s', row.qa_id, e)
            continue
    return None


def resolve(question, session_id, user_id):
    import smart_matcher_kb as smk

    #  输入类型校验
    if not isinstance(question, str):
        question = str(question or '')
    if not session_id or not user_id:
        raise ValueError('session_id/user_id required')

    old = latest(session_id, user_id)
    new = smk.parse_household(question)

    # 显式新户标识 → 另起一户
    if old and ((new.get('household_id') and new['household_id'] != old.get('household_id')) or
                (re.search(r'户主\s*[:：]',question) and new.get('household_head') and old.get('household_head') and
                 new['household_head'] != old['household_head'])):
        old = None

    hh = copy.deepcopy(old or new)

    if old:
        merge_facts(hh, new, question)
        if question.strip(' 。！!') in ('不清楚','不知道','都不清楚','全部不清楚','暂时无法提供'):
            from household_flow import apply
            pending=(hh.get('clarification') or {}).get('pending',[])
            apply(hh,{key:'不清楚' for key in pending})

    if not hh['members']:
        hh['members'] = [dict(member_no=1, name=None, fields={}, raw=[])]

    #  household_id 兜底带上 user_id，跨 session 更稳定
    hh['household_id'] = hh.get('household_id') or ('u%s-%s' % (user_id, session_id))
    hh['household_head'] = hh.get('household_head') or hh['members'][0].get('name')
    hh['enjoyed_known'] = bool(hh.get('enjoyed_known') or re.search(r'已享受|享受帮扶', question))

    if '【问卷结果】' not in question:
        # [FIX] 结构化累积
        _append_supplementary(hh, question)

    # 地区兜底（加排除词）
    if not hh.get('region'):
        match = re.search(r'([\u4e00-\u9fff]{2,8}(?:县|区))', question)
        if match and match.group(1) not in _REGION_BAD:
            hh['region'] = match.group(1)

    attach_surveys(hh, session_id, user_id)
    return hh


def attach_surveys(hh, session_id, user_id):
    from models import SurveyResult
    from api import _survey_tags

    rows = (SurveyResult.query
            .filter_by(session_id=session_id, user_id=user_id)
            .order_by(SurveyResult.survey_id.desc())
            .all())

    found, tags, scores = set(), [], []
    for row in rows:
        if row.member_no in found or (row.household_id and row.household_id != hh.get('household_id')):
            continue
        member = next((m for m in hh['members'] if m['member_no'] == row.member_no), None)
        if not member:
            continue

        #  json 解析容错
        try:
            answers = json.loads(row.answers or '{}')
        except (ValueError, TypeError) as e:
            logger.warning('bad survey answers survey_id=%s: %s', row.survey_id, e)
            continue

        found.add(row.member_no)
        tags.extend(_survey_tags(answers))
        scores.append(row.score or 0)

        if answers.get('q1') in ('都不想', '外出打工'):
            member['fields']['work_willing'] = '0' if answers.get('q1') == '都不想' else '1'
        member['survey_complete'] = True

    if scores:
        #  合并而非覆盖 wills；明确问卷优先于自然语言
        hh['wills'] = list(dict.fromkeys((hh.get('wills') or []) + tags))
        hh['will_score'] = round(sum(scores) / len(scores))
        hh['will_level'] = '高' if hh['will_score'] >= 8 else ('中' if hh['will_score'] >= 5 else '低')
        hh['_survey_profile'] = True


def simple(hh):
    #  加版本号，便于 hh_from_simple 校验/迁移
    #  P0-1：'tags'（家庭级规范身份）纳入持久化白名单——此前落库丢失导致
    #  判定层 family_labels 为空（"脱贫户"→"脱贫人口"身份在追问重建后失效）。
    keys = ('region', 'risk', 'enjoyed', 'enjoyed_known', 'wills', 'will_score',
            'will_level', 'household_id', 'household_head', 'supplementary_info', 'tags')
    data = {k: hh.get(k) for k in keys}
    data['members'] = [
        dict(no=m['member_no'], name=m.get('name'), fields=m['fields'],
             survey_complete=m.get('survey_complete', False))
        for m in hh['members']
    ]
    data['_v'] = SCHEMA_VERSION
    from household_facts import persist
    return persist(hh, data)


def missing(hh, ask_all=False, *, include_known=None):
    from household_flow import basics
    return basics(hh)


def merge_facts(hh, new, question):
    from household_facts import is_unknown, canonical_fact, reconcile_member, register_identity_conflicts
    for member in hh.get('members',[]):
        fields=member['fields']
        if fields.pop('med_insured_source',None)=='household_count': fields.pop('med_insured',None)
    correcting = bool(re.search(r'更正|纠正|填错|实际|改为|以.*为准', question))
    def put(target, key, value, no=0):
        prior = canonical_fact(key, target.get(key))
        value = canonical_fact(key, value)
        if key=='disability' and not is_unknown(prior) and not is_unknown(value):
            grades=[re.search(r'([一二三四1234])级',str(x)) for x in (prior,value)]
            types=[re.search(r'(肢体|视力|听力|言语|智力|精神|多重)残疾',str(x)) for x in (prior,value)]
            if all(grades) and grades[0][1].translate(str.maketrans('1234','一二三四'))==grades[1][1].translate(str.maketrans('1234','一二三四')) and (not all(types) or types[0][1]==types[1][1]):
                target[key]=max((prior,value),key=lambda x:len(str(x)))
                return
        if not is_unknown(prior) and prior != value and not correcting and not isinstance(value,(list,dict)):
            conflicts=hh.setdefault('fact_conflicts',[])
            if not any(c['field']==key and c['member_no']==no and not c.get('resolved') for c in conflicts):
                conflicts.append(dict(id='confirm_'+str(len(conflicts)),field=key,member_no=no,old=prior,new=value))
            target[key]='未知'
        else:
            target[key]=copy.deepcopy(value)
            if correcting:
                for c in hh.get('fact_conflicts',[]):
                    if c['field']==key and c['member_no']==no: c['resolved']=True
    for key in ('region','household_id','household_head','annual_income','income_level','house_level','drink_water',
                'big_disease_cnt','chronic_cnt','out_of_pocket','out_of_pocket_year','out_of_pocket_month','disaster_loss','med_insured_cnt'):
        if key == 'household_head' and not re.search(r'户主\s*[:：]',question): continue
        if key in new and new[key] is not None: put(hh,key,new[key])
    for key in ('tags','negative_tags','risk','wills'):
        if new.get(key): hh[key]=list(dict.fromkeys((hh.get(key) or [])+new[key]))
    hh['tags']=[t for t in hh.get('tags',[]) if t not in hh.get('negative_tags',[])]
    if new.get('enjoyed_known'): hh['enjoyed']=new.get('enjoyed',[]);hh['enjoyed_known']=True
    for member in new.get('members',[]):
        prior=_find_member(hh,member)
        if prior is None: hh['members'].append(copy.deepcopy(member));continue
        for key,value in member.get('fields',{}).items():
            if key in ('identity_tags','negative_identity_tags','identity_affirmations','fact_text'):
                prior['fields'][key]=list(dict.fromkeys((prior['fields'].get(key) or [])+value))
            else:
                put(prior['fields'],key,value,member['member_no'])
        f=prior['fields']
        if correcting:
            # An explicit correction supersedes old denials of the same identity.
            positive=set(member['fields'].get('identity_tags') or [])
            for key,tag in [('dibao','低保'),('tegang','特困')]:
                if member['fields'].get(key)=='1': positive.add(tag)
            if member['fields'].get('disability') not in (None,'','无','否','未知','不清楚'):
                positive.add('残疾')
            f['negative_identity_tags']=sorted(set(f.get('negative_identity_tags') or [])-positive)
        reconcile_member(f)
        register_identity_conflicts(hh,prior)
        if '就业困难人员' in f.get('negative_identity_tags',[]): f.pop('member_obj',None)
        if member.get('name'): prior['name']=member['name']
    from household_facts import reconcile_insurance
    reconcile_insurance(hh)
    st=hh.get('clarification') or {}
    for key in list(st.get('pending',[])):
        field,no=key.rsplit(':',1)
        target=hh if no=='0' else next((m['fields'] for m in hh['members'] if str(m['member_no'])==no),{})
        if field in target and not is_unknown(target[field]): st.setdefault('answered',{})[key]=str(target[field])
