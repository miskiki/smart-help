"""Bounded clarification and evidence-owned household answers (no database writes)."""
import copy
import json
import re
from household_facts import is_unknown, UNKNOWN

MAX_ROUNDS = 3
MAX_QUESTIONS = 8


def positive_topic(text, tokens):
    """A denied or unknown activity cannot establish a related policy direction."""
    for clause in re.split(r'[，,；;。\n]|但是|但|不过', str(text or '')):
        if re.search(r'无|没有|不需要|未从事|未开展|不清楚|不知道|暂无|不再|不愿',clause):
            continue
        if any(t in clause for t in tokens): return True
    return False


def question(key, label, options=None, reason='用于确认相关政策的适用条件'):
    opts = list(options or [])
    if opts:
        opts += [x for x in ('不清楚', '暂时无法提供') if x not in opts]
    return dict(key=key, label=label, kind='select' if opts else 'text',
                options=opts, value='', reason=reason)


def basics(hh):
    qs = []
    if any(m['fields'].get('pregnancy_unscoped') for m in hh.get('members',[])) and not any(m['fields'].get('pregnancy_status')=='是' for m in hh.get('members',[])):
        if 'pregnancy_member_review' not in hh.get('basic_review',{}):
            qs.append(question('pregnancy_member_review:0','你提到了家人怀孕，请说明对应的家庭成员编号及已知孕周。',reason='先确认事实属于谁，避免把家人的情况套到其他成员'))
    if is_unknown(hh.get('region')):
        qs.append(question('region:0', '家庭所在市／县（区）全称是？（当前仅支持江西省内政策）', reason='判断政策属地适用范围；不是户籍、务工或就学地'))
    # These facts affect medical assistance; zero disease counts do not negate costs.
    if any(isinstance(hh.get(k),(int,float)) and hh[k]>0 for k in ('out_of_pocket','out_of_pocket_year','out_of_pocket_month')):
        for key,label in [
            ('annual_income','家庭最近一年总收入约多少元？请填家庭总额，不清楚可填“不清楚”。'),
            ('medical_expense_member','上述医疗自付费用分别由哪些成员发生？请注明成员编号及对应金额。'),
            ('medical_expense_basis','上述自付金额是医保等报销后的个人负担，还是报销前支出？是否还有报销或救助未结算？'),
        ]:
            if is_unknown(hh.get(key)): qs.append(question(key+':0',label,reason='区分家庭负担和费用口径，为医疗救助方向核验提供依据；金额本身不能证明资格'))
    for m in hh.get('members', []):
        f, no = m['fields'], m['member_no']
        for key, label, opts in [('age', '年龄', None), ('health', '健康情况', ['健康', '残疾', '长期慢性病', '患有大病']),
                                 ('labor', '劳动能力', ['普通劳动力', '技能劳动力', '弱劳动力或半劳动力', '无劳动力'])]:
            if is_unknown(f.get(key)):
                from policy_core.member_labels import member_display
                qs.append(question('%s:%s' % (key,no), '%s的%s是？' % (member_display(no, m.get('name')), label), opts))
    review = hh.get('basic_review') or {}
    # Age gate: a schooling question is only useful when some member has an age
    # that makes study plausible; with unknown ages the age question comes first.
    from policy_core.member_labels import (age_gated_questions_enabled,
        MIN_STUDENT_AGE, MAX_STUDENT_AGE)
    student_ready = True
    if age_gated_questions_enabled():
        _known_ages = []
        for _m in hh.get('members', []):
            _age = (_m.get('fields') or {}).get('age')
            if isinstance(_age, (int, float)) and not isinstance(_age, bool):
                _known_ages.append(_age)
        student_ready = any(MIN_STUDENT_AGE <= a <= MAX_STUDENT_AGE for a in _known_ages)
    for key, label in [
        ('identity_review', '家庭及成员有哪些已认定身份？请注明成员，如低保、特困、监测对象、残疾、就业困难；没有请填“无”，已说明的无需重复。'),
        ('education_review', '是否有在校或在读成员？如有，请填写成员编号和学段；没有填“无”。'),
        ('activity_review', '目前有哪些就业、创业、种植养殖或改善住房饮水等需求／实际情况？请按成员或家庭说明，没有填“无”。'),
    ]:
        if key not in review and not (key == 'education_review' and not student_ready):
            qs.append(question(key+':0',label, reason='检查尚未涉及的政策方向，避免遗漏相关机会'))
    return qs


def answered(hh, key):
    return key in (hh.get('clarification') or {}).get('answered', {})


def schedule(hh, questions):
    st = hh.setdefault('clarification', dict(round=0, answered={}, asked=[]))
    unique = {}
    for q in questions:
        if not answered(hh,q['key']):
            if q['key'] in unique:
                unique[q['key']].setdefault('policies',[]).extend(q.get('policies',[]))
            else:
                unique[q['key']] = q
    if st['round'] >= MAX_ROUNDS or not unique:
        st['pending'] = []
        return []
    # Reuse an unanswered prompt without consuming another round on an empty follow-up.
    pending = st.get('pending') or []
    selected = list(unique.values())[:MAX_QUESTIONS]
    if not pending or {q['key'] for q in selected} != set(pending):
        st['round'] += 1
    for q in selected:
        q.update(round=st['round'], max_rounds=MAX_ROUNDS)
    st['pending'] = [q['key'] for q in selected]
    st['asked'] = list(dict.fromkeys(st.get('asked',[]) + st['pending']))
    return selected


def apply(hh, answers):
    """Only offered fields may be answered; unknown remains unknown, including booleans."""
    st = hh.setdefault('clarification', dict(round=0, answered={}, asked=[]))
    for key, value in answers.items():
        if value is None or value == '':
            continue
        if key not in st.get('pending',[]) and key not in st.get('asked',[]):
            raise ValueError('补充项不属于本会话已提出的问题，请通过家庭描述补充新情况')
        st.setdefault('answered',{})[key] = str(value).strip()
        field, no = key.rsplit(':',1)
        no = int(no)
        if field.endswith('_review'):
            hh.setdefault('basic_review',{})[field] = str(value)
            if not is_unknown(value) and str(value) != '无':
                import smart_matcher_kb as smk
                parsed = smk.parse_household(str(value))
                from household_state import merge_facts
                merge_facts(hh, parsed, str(value))
                # Apply a clearly universal statement to each existing member,
                # without inventing a new member for phrases such as "两人均".
                if re.search(r'两(?:名成员|人)|全体成员|所有成员|全家',str(value)) and re.search(r'均|都',str(value)):
                    from household_facts import member_facts, reconcile_member
                    facts={};member_facts(str(value),facts)
                    for member in hh['members']:
                        for fk in ('enrolled','job_status','work_willing','work_location'):
                            if fk in facts and is_unknown(member['fields'].get(fk)):
                                member['fields'][fk]=facts[fk]
                        reconcile_member(member['fields'])
            elif field == 'education_review' and str(value) == '无':
                for member in hh['members']:
                    member['fields']['enrolled'] = False
            if field == 'identity_review' and (str(value)=='无' or re.search(r'无其他身份|没有其他身份',str(value))):
                import smart_matcher_kb as smk
                have=smk.household_identity_tags(hh)
                family={'低保','特困','脱贫人口','防止返贫致贫对象'}
                hh['negative_tags']=sorted((set(hh.get('negative_tags') or [])|family)-have)
                for member in hh['members']:
                    f=member['fields'];known=smk.member_identity_tags(f)
                    f['negative_identity_tags']=sorted(set(f.get('negative_identity_tags') or [])|({'残疾','就业困难人员'}-known))
            continue
        if field == 'region':
            if not is_unknown(value): hh['region'] = str(value).strip().rstrip('。；;，, ')
            continue
        if field.startswith('confirm_'):
            conflict = next((c for c in hh.get('fact_conflicts',[]) if c.get('id') == field),None)
            if conflict and not is_unknown(value):
                target = hh if conflict['member_no'] == 0 else next(m['fields'] for m in hh['members'] if m['member_no']==conflict['member_no'])
                if str(value) in [str(conflict['old']),str(conflict['new'])]:
                    target[conflict['field']] = conflict['old'] if str(value)==str(conflict['old']) else conflict['new']
                    from household_facts import reconcile_member
                    if conflict['member_no']: reconcile_member(target)
                    conflict['resolved'] = True
            continue
        target = hh if no == 0 else next((m['fields'] for m in hh['members'] if m['member_no']==no),None)
        if target is None: raise ValueError('家庭成员不存在')
        target[field] = '未知' if is_unknown(value) else value
        if field=='annual_income' and not is_unknown(value):
            number=float(str(value).strip().removesuffix('元'))
            if number<0: raise ValueError('收入不能为负数')
            target[field]=number
            target['annual_income_basis']='家庭年总收入'
        if (field in ('age','employment_months') or field.startswith('insurance_months_')) and not is_unknown(value):
            number=int(value)
            if number < 0 or number > (120 if field=='age' else 1200):
                raise ValueError('年龄或月数超出合理范围')
            target[field] = number
        if field == 'med_type' and not is_unknown(value):
            target.pop('med_insured_source',None)
            target['med_insured'] = '0' if value=='未参保' else '1'
            target['med_rural'] = '1' if value=='城乡居民医保' else '0'
            target['med_urban'] = '1' if value=='职工医保' else '0'
        if field=='med_insured': target.pop('med_insured_source',None)
        if field == 'stage' and not is_unknown(value):
            target['enrolled'] = False if value == '无' else True
        if field == 'disability_grade' and not is_unknown(value):
            target['disability']=str(value)+'残疾'
        if field == 'employment_certified' and value == '是':
            target['member_obj'] = '就业困难人员'
        if field=='pregnancy_weeks' and not is_unknown(value):
            number=int(value)
            if number<0 or number>45:raise ValueError('孕周超出合理范围')
            target[field]=number
        if field=='age_days' and not is_unknown(value):
            number=int(value)
            if number<0 or number>3660:raise ValueError('出生天数超出合理范围')
            target[field]=number
        if field=='family_planning_type' and not is_unknown(value):
            from special_relevance import extract_facts
            extract_facts(str(value),target)
            if str(value).strip() in ('无','否','不是'):target['family_planning_status']='否'
    st['pending'] = []
    from household_facts import reconcile_insurance
    reconcile_insurance(hh)
    return hh


def conflict_questions(hh):
    return [question(c['id']+':0', '请确认%s的%s：此前为“%s”，本次为“%s”，以哪个为准？' %
                     ('家庭' if c['member_no']==0 else '成员%s'%c['member_no'],c['field'],c['old'],c['new']),
                     [str(c['old']),str(c['new'])], '两次信息矛盾，确认后才能可靠匹配')
            for c in hh.get('fact_conflicts',[]) if not c.get('resolved')]


def member_for(hh,r):
    no = str(r.get('member_no') or '')
    if not no:
        m = re.match(r'成员(\d+)',str(r.get('member') or ''));no=m[1] if m else ''
    return next((m for m in hh.get('members',[]) if str(m['member_no'])==no),None)


def _general_relevance(hh,r):
    """Positive relation guides display; it never changes eligibility."""
    import smart_matcher_kb as s
    m = member_for(hh,r); f = (m or {}).get('fields',{})
    from eligibility_gates import necessary_checks
    gates=necessary_checks({'name':r.get('name')},r,f)
    if any(ok is False for ok,_ in gates.values()): return False
    ptext = ' '.join(str(r.get(k) or '') for k in ('name','target','conditions','cat1'))
    ids = s.household_identity_tags(hh) | s.member_identity_tags(f)
    # A generic medical keyword cannot establish a specifically named identity.
    required=s.identities_for_text(r.get('conditions') or '')
    target=str(r.get('target') or '')
    if not required and not re.search(r'[,，、；;或]',target): required=s.identities_for_text(target)
    negatives=set(hh.get('negative_tags') or [])|set(f.get('negative_identity_tags') or [])
    if required and required <= negatives and not required & ids: return False
    activity = str(hh.get('basic_review',{}).get('activity_review',''))
    known = ' '.join([activity, *hh.get('risk',[]), *hh.get('wills',[]), *f.get('fact_text',[])])
    if re.search(r'孤儿|事实无人抚养|失去父母',ptext) and not (
            '孤儿' in ids or positive_topic(' '.join(f.get('fact_text',[])),('事实无人抚养','失去父母','找不到父母'))):
        return False
    if re.search(r'吸纳[^。；]{0,20}就业|用人单位|经营主体|车间奖补',ptext) and not positive_topic(known,('经营企业','开办企业','经营车间','用人单位')):
        return False
    # A broad "脱贫人口" catalog tag is not positive evidence for farming or renovation.
    # Education relates to this member, never just another member's school needs.
    from eligibility_gates import STUDENT
    if re.search(STUDENT,ptext):
        from household_facts import canonical_fact
        if canonical_fact('enrolled',f.get('enrolled')) is not True: return False
    # School enrollment plus family poverty alone is not a pension-enrollment fact.
    # Keep the internal candidate; show it if the member supplies a pension fact or need.
    if '养老保险' in ptext and f.get('enrolled') is True:
        from policy_conditions import pension_status_for
        if is_unknown(pension_status_for(f)) and not positive_topic(' '.join(f.get('fact_text',[])),('养老','养老金')):
            return False
    if ('因灾' in str(r.get('cat1') or '') or re.search(r'受灾|灾害|灾损',str(r.get('conditions') or ''))) and hh.get('disaster_loss')=='无':
        return False
    if re.search(r'就业|务工|培训|创业|职业技能',ptext) and f.get('labor') in ('无劳动力','丧失劳动力'):
        return False
    for tokens in [('产业','光伏'),('金融','贷款','信贷'),('创业',),('种植','稻谷','耕地','农机','油茶','果园'),('养殖','育肥猪','能繁母猪'),
                   ('户厕','改厕','化粪池'),('危房','住房改造')]:
        if any(t in ptext for t in tokens) and not (positive_topic(known,tokens) or any(t in str(f.get('stage','')) for t in tokens)):
            return False
    if any(n in s.identities_for_text(ptext) for n in ids): return True
    if any(s.identity_of(x) in ids for x in re.split(r'[,，、]',r.get('target') or '') if s.identity_of(x)):
        return True
    sources = ' '.join(str(x) for x in [hh.get('basic_review',{}).get('activity_review',''),*hh.get('risk',[]),*hh.get('wills',[]),*f.get('fact_text',[])])
    for group in [('就业','务工','求职'),('创业',),('种植','稻谷','耕地'),('养殖',),('住房','危房'),('饮水',),('在读','学生','教育'),('医疗','医保','大病','慢病')]:
        if any(x in ptext for x in group) and positive_topic(sources,group):
            if group[0]=='住房' and hh.get('house_level')=='无问题': continue
            if group[0]=='饮水' and hh.get('drink_water')=='无问题': continue
            return True
    # A real age-specific benefit is related to a known age; a broad catalog's
    # 0-110 age field does not establish pregnancy or any other special identity.
    if '高龄' in ptext and isinstance(f.get('age'),int):return True
    if target.strip() in ('所有农户','一般农户','所有居民','全体居民') and str(r.get('conditions') or '').strip() in ('','无','不限','无限制'):
        return True
    return False


def relevance_decision(hh,r):
    """A display decision with evidence, separate from eligibility and recall."""
    m=member_for(hh,r);f=(m or {}).get('fields',{})
    from eligibility_gates import necessary_checks
    gates=necessary_checks({'name':r.get('name')},r,f)
    if any(ok is False for ok,_ in gates.values()):
        return dict(related=False,code='necessary_condition_conflict',evidence=[])
    from special_relevance import assess_special
    special=assess_special(r,f)
    if special is not None:return special
    ok=_general_relevance(hh,r)
    evidence=[]
    if ok:
        import smart_matcher_kb as s
        text=' '.join(str(r.get(k) or '') for k in ('name','target','conditions','cat1'))
        identities=s.identities_for_text(text)&(s.household_identity_tags(hh)|s.member_identity_tags(f))
        if identities:evidence.append('已提供与该条款相关的身份：'+'、'.join(sorted(identities)))
        if re.search(r'医疗|医保|大病|慢病',text):
            owner=str(hh.get('medical_expense_member') or '')
            if re.search(r'(?:家庭)?成员\s*'+str((m or {}).get('member_no'))+r'(?!\d)',owner):evidence.append('已说明该成员发生医疗费用')
            elif f.get('med_insured')=='1':evidence.append('该成员已明确参加医保')
        if f.get('enrolled') is True and re.search(r'学生|在读|教育|助学',text):evidence.append('该成员已明确在读')
        if '高龄' in text:evidence.append('该成员已提供年龄，可核验高龄条件')
        if not evidence:evidence.append('已提供与该方向相关的家庭或成员事实，具体资格另行核验')
    return dict(related=ok,code='household_direction_evidence' if ok else 'no_related_evidence',evidence=evidence)


def relevance(hh,r):
    return relevance_decision(hh,r)['related']


def policy_questions(hh, rows):
    import smart_matcher_kb as s
    qs = []
    for r in rows:
        if ((r.get('forbid_v2') or {}).get('evidence') or {}).get('verdict')=='不适用': continue
        if not relevance(hh,r): continue
        ev = (r.get('forbid_v2') or {}).get('evidence') or {}
        if ev.get('verdict')!='待核实': continue
        m = member_for(hh,r)
        if not m: continue
        no,f = m['member_no'],m['fields']
        txt = (r.get('conditions') or '')+' '+(r.get('target') or '')
        def add(field,label,opts=None):
            if is_unknown(f.get(field)) and not answered(hh,'%s:%s'%(field,no)):
                q=question('%s:%s'%(field,no),'成员%s：'%no+label,opts)
                q['reason']='用于核验%s（%s）的具体条件，不清楚可保留待核实'%(r['name'],r['code'])
                q['policies']=[r['code']];qs.append(q)
        from special_relevance import PREGNANCY,FAMILY,NEWBORN
        special_text=' '.join(str(r.get(k) or '') for k in ('name','target','conditions','cat2','cat3'))
        if re.search(PREGNANCY,special_text) and not re.search(NEWBORN,txt):
            add('pregnancy_status','目前是否怀孕？',['是','否'])
            if f.get('pregnancy_status')=='是':
                if '周' in txt:add('pregnancy_weeks','目前孕周是多少？请填周数。')
                if '未进行过地中海贫血基因检测' in txt:
                    add('prenatal_genetic_tested','是否已经做过地中海贫血基因检测？',['是','否'])
        if re.search(FAMILY,special_text):
            add('family_planning_type','与本人相关的计划生育家庭情况是什么？如独生子女父母、计生特殊家庭等；没有请填“无”。')
        if re.search(NEWBORN,txt):
            add('age_days','目前出生多少天？请填天数。')
        # Ask about the missing conjunct, even if the broad identity review was
        # already answered. A known disability does not establish low-income status.
        condition=str(r.get('conditions') or '')
        from policy_conditions import condition_identity_groups
        for group in condition_identity_groups(condition):
            if len(group)!=1: continue  # alternative branches must not become AND questions
            required=s.identities_for_text(group[0])
            have=s.household_identity_tags(hh) | s.member_identity_tags(f)
            for tag,key,label in [('低保','dibao','是否已纳入城乡最低生活保障？'),('特困','tegang','是否已认定为特困供养人员？')]:
                if tag in required and tag not in have: add(key,label,['是','否'])
            if re.search(r'(?:本县|当地|本地)户籍',group[0]):
                from policy_conditions import local_hukou_state
                if local_hukou_state(hh,f) not in ('0','1'):
                    add('local_hukou','是否具有家庭所在县／区的本县户籍？居住地与户籍地可能不同。',['是','否'])
        # Identity gates precede insurance details: do not ask about an inapplicable subsidy.
        needed=s.identities_for_text(r.get('conditions') or '')
        have=s.household_identity_tags(hh) | s.member_identity_tags(f)
        if needed and not needed & have:
            if not answered(hh,'identity_review:0'):
                q=question('identity_review:0','是否还有已认定的低保、特困或防止返贫监测等身份？请按家庭或成员说明；没有填“无”。')
                q['policies']=[r['code']];qs.append(q)
            continue
        if '就业困难' in txt and '就业困难人员' in s.member_identity_tags(f):
            add('employment_certified','是否已经正式认定为就业困难人员？',['是','否'])
            if f.get('employment_certified')!='是': continue
        if re.search(r'稳定就业|务工|就业满|灵活就业|创业',txt):
            add('job_status','目前的就业情况是？',['未就业／正在求职','单位就业','灵活就业','自主创业'])
            if is_unknown(f.get('job_status')): continue
            if f.get('job_status')=='未就业／正在求职' and re.search(r'稳定就业|务工|就业满',txt): continue
        if re.search(r'省外.*务工|务工.*省外',txt):
            add('work_location','目前务工地点在省内还是省外？',['江西省内','江西省外'])
        if re.search(r'(?:稳定就业|务工|就业).*满\s*\d+\s*(?:个)?月',txt):
            add('employment_months','目前已经连续稳定就业多少个月？请填月数。')
        if '灵活就业' in txt:
            add('flexible_employment','目前是否灵活就业？',['是','否'])
            if f.get('flexible_employment')!='是': continue
        if re.search(r'居民.*(?:医保|医疗保险)|职工.*(?:医保|医疗保险)',txt):
            if f.get('med_insured') not in ('0','否',False) and not any(f.get(k) in ('1','是',True) for k in ('med_rural','med_urban')):
                add('med_type','参加的是哪类基本医保？',['城乡居民医保','职工医保','未参保'])
        if re.search(r'连续缴费|累计缴费|缴费满',txt):
            from policy_conditions import insurance_duration_key
            key=insurance_duration_key(txt)
            if key: add(key,'该项保险已连续缴费多少个月？请填月数（对应政策：%s）。'%r['name'])
        if '养老保险' in txt:
            from policy_conditions import pension_status_for
            status=pension_status_for(f)
            if is_unknown(status):
                add('pension_status','养老保险情况是？',['正在参加城乡居民养老保险','正在参加职工养老保险','已领取养老金','未参保'])
            elif ('居民' in txt or '职工' in txt) and not re.search(r'居民|职工',str(status)):
                add('pension_type','已说明养老保险状态，该养老保险具体属于哪类？',['城乡居民养老保险','职工养老保险'])
        if re.search(r'学生|在校|在读|学段|职业教育|研究生|助学',txt) and f.get('enrolled') is not False:
            add('stage','目前在读学段是？没有在读请填“无”',['小学','初中','高中','中职','大专','本科','研究生','无'])
        if '全日制' in txt:
            add('study_mode','目前是全日制还是非全日制就读？',['全日制','非全日制'])
        if re.search(r'重度残疾|[一二三四1234]级',txt) and '残疾' in s.member_identity_tags(f):
            if not re.search(r'[一二三四1234]级',str(f.get('disability') or '')):
                add('disability_grade','已认定的残疾等级是？',['一级','二级','三级','四级'])
        if '证书' in txt or '职业资格' in txt:
            add('credential','已取得哪些职业资格证书？没有请填“无”。')
        # Unparsed policy sentences are not questions for citizens to interpret.
    return qs


def clarification_result(hh,qs,session_id):
    from household_state import simple
    round_no=(hh.get('clarification') or {}).get('round',1)
    body='**补充家庭信息 · 第 %s 轮／最多 3 轮**\n\n'%round_no
    body+='已保留你提供的信息。以下问题会影响相关政策的匹配；不清楚的可以直接选择“不清楚”。\n\n'
    body+='\n'.join('- '+q['label'] for q in qs)
    return body,[],dict(session_id=session_id,sources=[],table_rows=[],cards=[],questions=qs,
        progress=dict(round=round_no,max_rounds=3),analysis_md='',
        match_json=dict(hh=simple(hh),recs=[],table_recs=[],questions=qs,flow_version=1))


def row_key(r):
    return '%s|%s|%s'%(r['code'],r.get('member_no',r.get('member')),r.get('detail_id'))


def render(hh,rows):
    """Four sections; exact member/branch status is never merged upward."""
    direct=[r for r in rows if r['decision']=='优先推荐']
    pending=[r for r in rows if r['decision']=='待核实']
    lines=['**家庭情况与帮扶重点**', '', '已记录 %s 名成员，家庭所在地区：%s。'%(len(hh['members']),hh.get('region') or '尚待确认')]
    jobs=[str(m['member_no']) for m in hh['members'] if '就业困难' in str(m['fields'].get('member_obj',''))]
    if jobs: lines.append('成员%s已描述就业困难情况，重点关注就业支持；具体补贴还需分别核对就业形式和认定等条件。'%'、'.join(jobs))
    if hh.get('house_level')=='无问题' and hh.get('drink_water')=='无问题':
        lines.append('住房和饮水已说明安全，当前不作为改善措施的重点。')
    if isinstance(hh.get('out_of_pocket'),(int,float)) and hh['out_of_pocket']>0:
        amount='、当月%s元'%format(hh['out_of_pocket_month'],'g') if isinstance(hh.get('out_of_pocket_month'),(int,float)) else ''
        lines.append('已记录近12个月医疗自付%s元%s。需结合家庭收入、费用所属成员及报销结算口径判断，不能仅凭支出金额确认医疗救助资格。'%(format(hh['out_of_pocket'],'g'),amount))
    uninsured=[str(m['member_no']) for m in hh['members'] if m['fields'].get('med_insured')=='0']
    if uninsured: lines.append('成员%s已记录为医保未参保，建议先了解当地参保及待遇衔接安排；这不代表已经符合缴费资助或医疗救助条件。'%'、'.join(uninsured))
    lines+=['','**建议优先办理的政策**','']
    if not direct: lines.append('目前尚无足够依据确认适用的政策；相关机会列在下方，不代表没有政策可用。')
    for r in direct[:6]:
        lines+=['- **%s**（%s，%s）：根据已填信息符合，建议申办。'%(r['name'],r['code'],r['member'])]
        if r.get('benefit'): lines.append('  - 政策标准：%s；具体适用档次以对应条件核验及经办审核为准。'%r['benefit'])
    if len(direct)>6: lines.append('其余 %s 条推荐详见下方“查看全部政策”。'%(len(direct)-6))
    lines+=['','**仍需确认的政策机会**','']
    if not pending: lines.append('暂无其他已发现的相关待确认政策。')
    for r in pending[:6]:
        lacks=r.get('missing') or ['政策适用条件需向经办部门确认']
        relation='关联依据：'+r['relevance_evidence'][0]+'；待确认：' if r.get('relevance_evidence') else ''
        lines.append('- **%s**（%s，%s）：%s%s。'%(r['name'],r['code'],r['member'],relation,'；'.join(lacks)))
    if len(pending)>6: lines.append('其余 %s 条相关机会已保留，可展开查看。'%(len(pending)-6))
    lines+=['','**下一步行动**','']
    if direct: lines.append('- 优先按已确认适用的政策联系经办部门办理；无需在本系统上传证明材料。')
    if pending: lines.append('- 对待确认政策，按列出的具体缺项补充信息；政策口径不清的，请向经办部门确认。')
    lines.append('- 可以继续补充或纠正家庭情况，系统会更新匹配；最终资格及金额由经办部门审核。')
    old=hh.get('previous_decisions') or {}
    changes=[r for r in rows if row_key(r) in old and old[row_key(r)]!=r['decision']]
    if changes: lines+=['','本次更新：'+'；'.join('%s（%s）由%s调整为%s'%(r['name'],r['member'],old[row_key(r)],r['decision']) for r in changes[:8])]
    removed=set(old)-{row_key(r) for r in rows}
    if removed:
        labels=[]
        for key in sorted(removed)[:8]:
            code,member,*_=key.split('|')
            labels.append('%s（%s）不再列入当前建议'%(code,member if member.startswith('成员') else '成员'+member))
        lines+=['','信息更新后：'+'；'.join(labels)+'。可询问具体政策名称或编号了解原因。']
    hh['previous_decisions']={row_key(r):r['decision'] for r in rows}
    return '\n'.join(lines)


def validate_advice(text,rows):
    """A conservative output gate: free text may explain priorities, not invent entitlements."""
    if not isinstance(text,str) or not text.strip(): return False
    if len(text)>180: return False
    if re.search(r'超出.*知识库|\d+(?:\.\d+)?\s*(?:元|万元)|自动(?:发放|落实|到账)|无需申请|免申即享|已经符合|已符合|保证|一定能|已认定|已确认为|必须提交|必须提供|拨打|请提供|请补充|请协助|证明|认证材料|[？?]',text): return False
    codes=set(re.findall(r'\b[CSUX]-[A-Z]+-\d+\b',text))
    if not codes <= {r['code'] for r in rows}: return False
    # No policy/member status can be upgraded by a generative paragraph.
    if re.search(r'符合|可领取|可享受|将获得|属于.*对象|可以申领|建议申办|申领|无需|即可|前提|额外身份|必须|认定为|已明确认定|新增政策',text): return False
    if re.search(r'(?:建议|可以|可为|为其|为孩子|为成员\d+)(?:优先|先|直接|为成员\d+|为孩子|为其|\s){0,3}(?:申请|办理)',text): return False
    # No new policy names or unverified agency names may enter through stylistic prose.
    names={str(r.get('name') or '') for r in rows}
    if any(x not in names for x in re.findall(r'[“「]([^”」]+)[”」]',text)): return False
    supported=' '.join(str(r.get(k) or '') for r in rows for k in ('name','process','contact'))
    for label in re.findall(r'[\u4e00-\u9fff]{2,20}(?:补贴|补助|津贴|助学金|救助|服务中心|人社局|民政局|残联)',text):
        if not any(label.endswith(n) or n.endswith(label) for n in names if n) and label not in supported:
            return False
    return True


def grounded_advice(hh,rows):
    """Evidence-owned narrative when a model adds unsupported eligibility claims."""
    direct=[r for r in rows if r['decision']=='优先推荐']
    parts=[]
    if hh.get('house_level')=='无问题' and hh.get('drink_water')=='无问题':
        parts.append('你已说明住房和饮水安全，可以把精力放在与当前家庭情况更相关的事项上。')
    if direct:
        parts.append('先了解已核验政策的办理安排，再逐项确认其他机会的缺失条件，会更有针对性。')
    elif rows:
        parts.append('目前的主要缺口是资格条件尚未确认，宜先核清具体缺项，再决定申请哪项政策，避免往返咨询。')
    else:
        parts.append('目前信息中还没有发现可确认的政策机会；家庭情况发生变化时，可以继续补充后重新分析。')
    return ''.join(parts)


def final_result(hh,recs,question_text,session_id,graph_meta,llm):
    import smart_matcher_kb as s
    from household_state import simple
    rows=[];audit=[]
    for r in recs:
        ev=(r.get('forbid_v2') or {}).get('evidence') or {}
        if ev.get('verdict')=='不适用': continue
        relation=relevance_decision(hh,r)
        if not relation['related']:
            audit_row=copy.deepcopy(r);audit_row['relevance_decision']=relation;audit.append(audit_row);continue
        row=copy.deepcopy(r)
        row['relevance_decision']=relation
        row['relevance_evidence']=relation['evidence']
        row['decision']='优先推荐' if ev.get('verdict')=='通过' else '待核实'
        gaps=ev.get('miss') or ([] if row['decision']=='优先推荐' else ['政策适用条件需向经办部门确认'])
        row['missing']=list(dict.fromkeys(str(g).replace('条件原文未覆盖：','政策另有要求，需向经办部门确认：') for g in gaps))
        row['verify']=['最终资格与金额由经办部门审核'] if row['decision']=='优先推荐' else ['补充事实或咨询经办部门确认具体条件']
        row['reason']=('根据已填信息符合，建议申办：' if row['decision']=='优先推荐' else '待确认：')+'关联依据：'+'；'.join(relation['evidence'])+'；按本成员对应政策分支核验；'+str(ev.get('note') or '具体条件待确认')
        row['relevance']='related';rows.append(row)
    # Merge only explicitly household-level units; never infer scope from currency unit.
    dedup={}
    for r in rows:
        if re.search(r'每户|以户为单位|按户(?:申领|申请|补助)',(r.get('conditions') or '')+' '+(r.get('target') or '')):
            r['member']='家庭';r['member_no']=0
        dedup.setdefault((r['code'],r['member'],r.get('detail_id'),r['decision']),r)
    household={};personal=[]
    for r in dedup.values():
        if r['member']!='家庭': personal.append(r);continue
        key=(r['code'],r.get('detail_id'))
        if key not in household: household[key]=r
        elif household[key]['decision']!=r['decision']:
            household[key]['decision']='待核实'
            household[key]['reason']='待确认：按户申领时成员条件的作用范围尚未确认'
            household[key]['missing']=list(dict.fromkeys(household[key]['missing']+r['missing']+['按户申请时成员条件的作用范围需确认']))
    rows=personal+list(household.values());rows.sort(key=lambda r:r['decision']!='优先推荐')
    body=render(hh,rows)
    selected=rows[:12]
    prompt=json.dumps({'家庭':{'region':hh.get('region'),'members':hh['members'],'risk':hh.get('risk')},
        '已有回答':(hh.get('clarification') or {}).get('answered',{}),
        '重点政策':[{'code':r['code'],'name':r['name'],'member':r['member'],'status':r['decision'],'missing':r['missing']} for r in selected]},ensure_ascii=False)
    gate='accepted'
    try:
        advice=llm(question_text,prompt,system='你是家庭政策顾问。只写一段150字以内的行动思路，说明核实事项的先后顺序及原因。不得判定身份资格、推断家庭事实、建议申请或申领、声称无需确认或即可办理，不提金额，不承诺发放。不得新增政策、就业服务或未经给出的机构名称；机构用“经办部门”“就读学校”等通称。不要再向用户提问题或索取证明材料，不重复已有回答。待确认不能说已符合。',purpose='advice',temperature=0.2)
        if not validate_advice(advice,selected): raise ValueError('advice_inconsistent')
        body+='\n\n'+advice.strip()
    except Exception:
        gate='grounded_fallback'
        body+='\n\n'+grounded_advice(hh,rows)
        # Matching remains usable if stylistic generation fails; no fabricated model success.
    from chain_trace import _emit, _current
    from collections import Counter
    relevance_filtered=dict(Counter(r['relevance_decision']['code'] for r in audit))
    try: _emit(_current.get(), 'household.result',retained=len(rows),related_pending=sum(r['decision']=='待核实' for r in rows),internal_candidates=len(audit),relevance_filtered=relevance_filtered,advice_gate=gate,flow_version=1)
    except (ImportError,TypeError,NameError): pass
    judgment={g:[] for g in ('direct','try','exclude','enjoyed')}
    for r in rows:
        judgment['direct' if r['decision']=='优先推荐' else 'try'].append(dict(code=r['code'],member=r['member'],detail_id=r.get('detail_id'),why=r['reason'],gap='；'.join(r['missing'])))
    graph_meta=dict(graph_meta or {}, model_calls_actual=1, llm_judged_rows=0,
                    final_recommended_rows=sum(r['decision']=='优先推荐' for r in rows),
                    final_pending_rows=sum(r['decision']=='待核实' for r in rows))
    mj=dict(hh=simple(hh),recs=recs,table_recs=rows,judgment=judgment,questions=[],flow_version=1,
            internal_candidates=audit,graph=graph_meta,advice_gate=gate)
    meta=dict(session_id=session_id,sources=[dict(policy_code=r['code'],policy_name=r['name'],excerpt=r['reason']) for r in rows],
        analysis_md='',table_rows=s.table_rows_from_recs(rows),cards=rows,questions=[],match_json=mj,
        progress=dict(round=(hh.get('clarification') or {}).get('round',0),max_rounds=3),judge_status='evidence_verified')
    return body,sorted({r['code'] for r in rows}),meta


def explain_policy(hh, question_text):
    if not re.search(r'为什么|为何|没匹配|未推荐|不推荐',question_text): return None
    import smart_matcher_kb as s
    from kg_forbid_v2 import verify_member_detail
    data=s.load_db()
    policies=[p for p in data['policies'] if p['code'] in question_text or p['name'] and p['name'] in question_text]
    if not policies: return '请说明想了解的政策名称或编号，我会结合当前家庭信息解释匹配依据。'
    lines=['**政策匹配依据说明**','']
    for p in policies:
        lines.append('**%s（%s）**'%(p['name'],p['code']))
        for m in hh['members']:
            results=[]
            for d in data.get('by_policy',{}).get(p['pid'],[]) or [dict(target=p.get('target'),conditions=p.get('conditions'))]:
                ev=verify_member_detail(p,d,m['member_no'],hh['members'],hh,hh.get('enjoyed',[]))
                results.append('%s：%s'%(ev['verdict'],'；'.join(ev.get('miss') or [ev.get('note','需确认')])) )
            lines.append('- 成员%s：%s'%(m['member_no'],'；'.join(dict.fromkeys(results))))
    lines+=['','以上依据当前家庭信息与政策对应分支核验；未说明的事实不会直接视为不符合。']
    return '\n'.join(lines)
