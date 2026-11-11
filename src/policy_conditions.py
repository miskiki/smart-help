"""Guards on explicit policy conditions; broad catalog tags cannot prove eligibility."""
import re


def condition_identity_groups(text):
    """AND qualifications, OR only explicit alternatives or bare subject lists.

    In particular, "本县户籍残疾人，纳入低保的残疾人" is not an OR list.
    "低保对象、特困人员、残疾人" is a list of independent subjects.
    "一级、二级残疾人" is a grade list and must stay in one branch.
    """
    import smart_matcher_kb as s
    clauses=re.split(r'[;；。\n]',str(text or ''))
    groups=[]
    bare=r'(?:低保(?:对象|户|人员)?|特困(?:人员|户|对象)?|(?:重度)?残疾人|脱贫(?:人口|户)|防止返贫致贫对象|监测对象)'
    for clause in clauses:
        if not clause.strip(): continue
        parts=re.split(r'或者|或',clause)
        if len(parts)>1 and all(s.identities_for_text(p) for p in parts):
            groups.append(parts);continue
        # Fully named subjects linked by an enumeration remain alternatives even
        # with an introductory verb ("分类资助农村特困人员、农村低保对象…").
        # Grade lists and a shared suffix such as "低保、特困残疾人" do not fit.
        parts=re.split(r'、|(?<=对象)和(?=符合条件|防止返贫)',clause)
        if len(parts)>1 and all(s.identities_for_text(p) and re.search(r'(?:人员|对象|残疾人|农户)(?:等[^。]*)?$',p.strip()) for p in parts):
            groups.append(parts);continue
        listing=re.sub(r'等[^，,；;。]*$', '',clause).strip()
        parts=re.split(r'[、，,]',listing)
        if len(parts)>1 and all(re.fullmatch(bare,p.strip()) for p in parts):
            groups.append(parts)
        else: groups.append([clause])
    return groups


def required_object_checks(text, hh, mf):
    """No single broad identity can prove all qualifications in one branch."""
    import smart_matcher_kb as s
    from eligibility_gates import combine
    from household_facts import canonical_fact, reconcile_member
    mf=reconcile_member(dict(mf))
    labels=s.household_identity_tags(hh) | s.member_identity_tags(mf)
    negatives=set(hh.get('negative_tags') or []) | set(mf.get('negative_identity_tags') or [])
    def branch(part):
        checks=[]
        for tag in sorted(s.identities_for_text(part)):
            if tag in negatives: checks.append((False,'已明确不具备政策要求的身份：'+tag))
            elif tag not in labels: checks.append(('unknown','政策必要身份尚未确认：'+tag))
        if re.search(r'(?:本县|当地|本地)户籍',part):
            value=local_hukou_state(hh,mf)
            if value=='0': checks.append((False,'成员明确不是本县户籍'))
            elif value!='1': checks.append(('unknown','成员本县户籍尚未确认，居住地不能代替户籍'))
        return combine(checks,'all')
    return combine([combine([branch(p) for p in group],'any') for group in condition_identity_groups(text)],'all')


def local_hukou_state(hh, mf):
    from household_facts import canonical_fact
    value=canonical_fact('local_hukou',mf.get('local_hukou'))
    if value in ('0','1'): return value
    registered=str(mf.get('hukou_region') or '')
    region=str(hh.get('region') or '')
    if registered and region:
        county=lambda x: re.split(r'[省市]',x)[-1]
        return '1' if county(registered)==county(region) else '0'
    return '未知'


def insurance_duration_key(text):
    if '城乡居民' in text and ('医疗' in text or '医保' in text): return 'insurance_months_med_rural'
    if '职工' in text and ('医疗' in text or '医保' in text): return 'insurance_months_med_urban'
    if '失业保险' in text: return 'insurance_months_unemployment'
    if '医疗保险' in text or '医保' in text: return 'insurance_months_med_basic'
    return None


def object_guard(condition, hh, mf, original, note):
    if original is False:
        return original,note
    import smart_matcher_kb as s
    text=str(condition or '')
    required=s.identities_for_text(text)
    labels=s.household_identity_tags(hh) | s.member_identity_tags(mf)
    negatives=set(hh.get('negative_tags') or []) | set(mf.get('negative_identity_tags') or [])
    extra_ok,extra_note=required_object_checks(text,hh,mf)
    if extra_ok is not True: return extra_ok,extra_note
    if required and not required & labels:
        if required <= negatives:
            return False,'已明确不具备政策要求的身份：'+'、'.join(sorted(required))
        return 'unknown','政策具体条件要求的身份尚未确认：'+'、'.join(sorted(required))
    # A list of alternatives and a conjunction require different interpretation.
    if len(required)>1 and ('且' in text or '同时' in text) and not required <= labels and '或' not in text:
        return 'unknown','政策包含多个身份的组合条件，需要分别确认'
    if re.search(r'稳定就业|务工|就业满',text) and mf.get('job_status')=='未就业／正在求职':
        if '或' in text: return 'unknown','政策包含不同就业对象分支，需分别确认'
        return False,'该分支要求已经就业或务工，成员目前明确未就业／正在求职'
    threshold=re.search(r'(?:稳定就业|务工|就业).*?满\s*(\d+)\s*(?:个)?月',text)
    if threshold:
        try:
            months=float(mf.get('employment_months'))
        except (TypeError,ValueError):
            return 'unknown','连续稳定就业月数尚未确认'
        if months<int(threshold[1]): return False,'稳定就业月数未达到政策要求'
    if '就业困难' in text and mf.get('employment_certified')!='是':
        return (False,'未认定为就业困难人员') if mf.get('employment_certified')=='否' else ('unknown','就业困难人员正式认定情况待确认')
    if '灵活就业' in text and mf.get('flexible_employment')!='是':
        return (False,'已明确不是灵活就业') if mf.get('flexible_employment')=='否' else ('unknown','灵活就业情况待确认')
    # Matching a broad person category is not evidence that all extra qualifications hold.
    if '符合条件' in text or '符合规定' in text:
        return 'unknown','政策另有限定资格，需向经办部门确认具体适用口径'
    return original,note


def pension_status_for(mf):
    from household_facts import is_unknown
    status=mf.get('pension_status')
    if not is_unknown(status): return str(status)+' '+str(mf.get('pension_type') or '')
    if mf.get('pension_receiving')=='1': return '已领取养老金 '+str(mf.get('pension_type') or '')
    if str(mf.get('pen_rural'))=='1': return '已参加城乡居民养老保险'
    if str(mf.get('pen_urban_emp'))=='1': return '已参加职工养老保险'
    return status


def pension_match(text,mf):
    status=pension_status_for(mf)
    if status in (None,'','未知','不清楚','暂时无法提供'):
        return 'unknown','养老保险参保或领取情况尚未明确'
    if re.search(r'未参加.*养老保险|未参保.*养老',text):
        return ('未参保' in status),'按用户明确填写核验未参保对象条件'
    if '未参保' in status:
        return False,'政策要求养老保险参保，成员明确未参保'
    if '居民' in text and '职工' not in text and '职工' in status:
        return False,'养老保险类型与政策要求不符'
    if '职工' in text and '居民' not in text and '居民' in status:
        return False,'养老保险类型与政策要求不符'
    if mf.get('pension_receiving')=='0' and re.search(r'已(?:按规定)?(?:领取|发放).*养老',text) and not re.search(r'代缴|或',text):
        return False,'政策分支要求已领取养老金，成员明确尚未领取'
    # A self-reported pension type does not by itself prove subsidy eligibility.
    if any(x in text for x in ('代缴','困难','补助','补贴','符合条件','领取')):
        return 'unknown','养老保险具体补助对象或领取条件需按政策分支确认'
    if ('居民' in text or '职工' in text) and not any(x in status for x in ('居民','职工')):
        return 'unknown','养老保险具体险种尚未确认'
    return True,'已根据用户填写核验养老保险参保类型'
