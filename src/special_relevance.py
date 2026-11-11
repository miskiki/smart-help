"""Member-owned evidence for narrow policy subjects, independent of eligibility."""
import re

PREGNANCY = r'孕妇|孕产妇|妊娠|孕期|产前筛查|产前检查'
FAMILY = r'计划生育|计生特殊|独生子女父母|失独'
NEWBORN = r'新生儿|新生婴儿'


def extract_facts(text, fields):
    t=str(text or '')
    if re.search(NEWBORN,t) and not re.search(r'不是新生儿|无新生儿|新生儿.*(?:不清楚|未知)',t):
        fields['newborn_fact']='是'
    days=re.search(r'(?:出生|日龄[:：]?)(\d+)天?',t)
    if days:fields['age_days']=int(days[1])
    # A spouse's pregnancy is not evidence that this member is pregnant.
    other=bool(re.search(r'(?:妻子|配偶|女儿|母亲|家人).{0,8}(?:怀孕|妊娠|孕妇)',t))
    if other:
        fields['pregnancy_unscoped']=True
    else:
        if re.search(r'未怀孕|没有怀孕|并未怀孕|不是孕妇|未妊娠|是否怀孕[:：](?:否|0)',t):
            fields['pregnancy_status']='否'
        elif not re.search(r'(?:怀孕|孕妇|妊娠).{0,5}(?:不清楚|未知|未确认)|是否怀孕[:：](?:不清楚|未知)|(?:计划|准备|打算|希望|想要).{0,6}怀孕',t):
            if re.search(r'(?:已|正在|目前)?怀孕(?:了|\d|[，,；;。]|$)|妊娠\d+周|孕期\d+周|(?:是|为)孕妇|是否怀孕[:：](?:是|1)',t):
                fields['pregnancy_status']='是'
            weeks=re.search(r'(?:怀孕|妊娠|孕期|孕周[:：]?)(\d+)周?',t)
            if weeks:
                fields['pregnancy_weeks']=int(weeks[1]);fields['pregnancy_status']='是'
        if re.search(r'(?:需要|希望|想了解|咨询).{0,8}(?:产检|产前筛查|孕产)',t) and not re.search(r'无需|不需要|没有.*需求',t):
            fields['pregnancy_interest']='是'
    if re.search(r'(?:未|没有|不曾)(?:进行过|做过|进行)?地中海贫血基因检测',t):
        fields['prenatal_genetic_tested']='否'
    elif re.search(r'(?:已|已经)(?:进行过|做过|进行)?地中海贫血基因检测',t):
        fields['prenatal_genetic_tested']='是'
    family=re.search(r'独生子女父母|独生子女家庭|计划生育家庭|计生特殊家庭|失独家庭',t)
    if family:
        prefix=t[max(0,family.start()-6):family.start()]
        suffix=t[family.end():family.end()+6]
        if re.search(r'非|不是|不属于|没有',prefix):fields['family_planning_status']='否'
        elif not re.search(r'不清楚|未知|未确认',suffix):
            fields['family_planning_status']='是';fields['family_planning_type']=family[0]
    if re.search(r'(?:希望|想了解|咨询).{0,8}(?:计划生育|计生特殊)',t) and not re.search(r'不需要|无需',t):
        fields['family_planning_interest']='是'


def pregnancy_match(text, fields):
    """Pregnancy and source-stated week ranges; no age-based pregnancy inference."""
    if not re.search(PREGNANCY,str(text)): return True,'无孕产必要条件'
    if fields.get('sex')=='男' or fields.get('pregnancy_status')=='否':
        return False,'成员明确不符合该孕产对象分支'
    if fields.get('pregnancy_status')!='是':return 'unknown','成员是否怀孕尚未确认'
    ranges=[(int(a),int(b)) for a,b in re.findall(r'(\d+)\s*[-—至]\s*(\d+)\s*周',str(text))]
    upper=re.search(r'(\d+)\s*周(?:以内|以下)',str(text))
    if upper:ranges.append((0,int(upper[1])))
    if ranges:
        weeks=fields.get('pregnancy_weeks')
        if not isinstance(weeks,(int,float)):return 'unknown','政策限定孕周，成员当前孕周尚未确认'
        if not any(low<=weeks<=high for low,high in ranges):return False,'成员当前孕周不在该明细限定范围'
    if re.search(r'未进行过地中海贫血基因检测',str(text)):
        tested=fields.get('prenatal_genetic_tested')
        if tested=='是':return False,'该分支要求未做过检测，成员已明确做过'
        if tested!='否':return 'unknown','是否做过地中海贫血基因检测尚未确认'
    return True,'已按成员自述核验孕产身份及原文明确孕周条件'


def assess_special(row, fields):
    """None means no narrow subject; a rejection only affects user-facing relevance."""
    detail=str(row.get('conditions') or '')+' '+str(row.get('target') or '')
    text=detail+' '+str(row.get('name') or '')
    if not re.search(PREGNANCY+'|'+FAMILY+'|'+NEWBORN,text):
        text+=' '+' '.join(str(row.get(k) or '') for k in ('cat2','cat3'))
    has_pregnancy=bool(re.search(PREGNANCY,text));has_family=bool(re.search(FAMILY,text));has_newborn=bool(re.search(NEWBORN,text))
    if not has_pregnancy and not has_family and not has_newborn:return None
    # Preserve explicit independent subject branches; common qualifiers are not OR.
    from eligibility_gates import alternatives
    branches=alternatives(str(row.get('conditions') or row.get('target') or ''))
    if len(branches)>1:
        import smart_matcher_kb as s
        for branch in branches:
            if not re.search(PREGNANCY+'|'+FAMILY,branch) and s.identities_for_text(branch)&s.member_identity_tags(fields):
                return dict(related=True,code='independent_subject_evidence',evidence=['该成员已提供另一独立对象分支的身份事实'])
    evidence=[]
    if has_newborn:
        if fields.get('newborn_fact')=='是':evidence.append('该成员已明确描述为新生儿')
        elif fields.get('age')==0:evidence.append('该成员未满一周岁，出生天数及新生儿身份仍需确认')
        else:return dict(related=False,code='newborn_no_evidence',evidence=[])
    if has_pregnancy:
        if fields.get('pregnancy_status')=='否' or fields.get('sex')=='男':
            return dict(related=False,code='pregnancy_denied',evidence=[])
        if fields.get('pregnancy_status')=='是':evidence.append('该成员已明确描述怀孕情况')
        elif fields.get('pregnancy_interest')=='是':evidence.append('该成员明确提出孕产检查需求，孕产身份仍需确认')
        else:return dict(related=False,code='pregnancy_no_evidence',evidence=[])
    if has_family:
        if fields.get('family_planning_status')=='否':return dict(related=False,code='family_planning_denied',evidence=[])
        if fields.get('family_planning_status')=='是':evidence.append('该成员已描述'+str(fields.get('family_planning_type') or '计划生育家庭情况'))
        elif fields.get('family_planning_interest')=='是':evidence.append('该成员明确提出计划生育政策咨询需求，具体家庭情况仍需确认')
        else:return dict(related=False,code='family_planning_no_evidence',evidence=[])
    return dict(related=True,code='special_subject_evidence',evidence=evidence)
