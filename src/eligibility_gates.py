"""Independent necessary-condition checks. A known contradiction dominates missing facts.

These gates can reject or require clarification; they never prove all eligibility.
Explicit alternative subjects are checked separately so one rejected branch cannot
remove another possible branch. Unparsed source text remains with the main verifier.
"""
import re
from household_facts import canonical_fact, is_unknown, reconcile_member

STAGES = {
    '学前': {'幼儿园','学前'}, '小学': {'小学','义务教育'}, '初中': {'初中','义务教育'},
    '高中': {'高中','普通高中'}, '中职': {'中职','中等职业学校','中等职业教育','中等职业'},
    '技工': {'技工','技校'}, '大专': {'大专','专科','高职','高等职业教育','高等职业'},
    '本科': {'本科','本专科'}, '研究生': {'研究生','硕士','博士'},
}
STUDENT = r'学生|在校|在籍|在读|就读|学籍|助学|学杂费|幼儿园|学前教育|职业教育者|雨露计划|本专科生|研究生'


def alternatives(text):
    """Only split explicit alternatives with independent subjects; never split grades."""
    text=str(text or '').replace('中、高职业教育','中等职业教育或高等职业教育')
    parts=re.split(r'或者|或',text)
    if len(parts)>1 and all(re.search(r'人员|对象|老人|老年人|学生|生$|残疾人|孕妇|孕产妇|家庭|农户|务工|就业|教育',p) for p in parts):
        return parts
    # A subject list is OR; a list of modifiers on one subject is not.
    parts=re.split(r'[、,，]',text)
    if len(parts)>1 and all(re.search(r'(?:人员|对象|老年人|老人|学生|残疾人|孕妇|孕产妇|农户|家庭)\s*$',p) for p in parts):
        return parts
    return [text]


def combine(results, mode):
    if not results: return True,'无对应必要条件'
    if mode=='any':
        if any(ok is True for ok,_ in results): return True,'存在未被该维度排除的独立对象分支'
        if any(ok=='unknown' for ok,_ in results):
            return 'unknown','；'.join(dict.fromkeys(n for ok,n in results if ok=='unknown'))
        return False,'所有独立对象分支均不满足：'+'；'.join(dict.fromkeys(n for _,n in results))
    if any(ok is False for ok,_ in results):
        return False,'；'.join(dict.fromkeys(n for ok,n in results if ok is False))
    if any(ok=='unknown' for ok,_ in results):
        return 'unknown','；'.join(dict.fromkeys(n for ok,n in results if ok=='unknown'))
    return True,'已核验该维度明确条件'


def stage_requirement(text):
    text=str(text).replace('本、专科','本科、专科').replace('本专科','本科、专科')
    text=text.replace('中、高职业教育','中等职业教育、高等职业教育')
    found=set()
    for stage,terms in STAGES.items():
        if any(t in text for t in terms): found.add(stage)
    # Broad education group names include their sub-stages only when used alone.
    if '高等教育' in text and not found: found.update(('大专','本科','研究生'))
    if '高中阶段' in text: found.update(('高中','中职','技工'))
    return found


def education(text, mf):
    # A degree required for a job does not require current enrollment.
    if re.search(r'学历|文化程度|毕业生',text) and not re.search(r'在读|在校|就读|学籍|助学',text):
        return True,'学历或毕业生身份另行核验，不推断当前在读'
    required=stage_requirement(text)
    if not re.search(STUDENT,text) and not (required and re.search(r'教育|学校|生',text)):
        return True,'无在读必要条件'
    enrolled=canonical_fact('enrolled',mf.get('enrolled'))
    stage=str(mf.get('stage') or '').strip()
    stage={'幼儿园':'学前','高职':'大专','专科':'大专','硕士':'研究生','博士':'研究生','大学':'本科','技校':'技工'}.get(stage,stage)
    # Known stage conflicts are checked before enrollment/full-time unknowns.
    if required and stage in STAGES and stage not in required:
        return False,'成员当前学段为%s，与政策要求的%s不符'%(stage,'、'.join(sorted(required)))
    if enrolled is False: return False,'成员明确不在读，不符合学生类政策必要条件'
    if enrolled is not True: return 'unknown','成员是否在读尚未确认'
    if required and stage not in STAGES: return 'unknown','成员当前在读学段尚未明确'
    if '全日制' in text and mf.get('study_mode')!='全日制':
        if mf.get('study_mode')=='非全日制': return False,'政策要求全日制，成员明确为非全日制'
        return 'unknown','全日制就读方式尚未确认'
    return True,'学生身份与已知学段未发现冲突'


def disability(text, mf):
    if '残疾' not in text: return True,'无残疾身份必要条件'
    from smart_matcher_kb import member_identity_tags
    f=reconcile_member(dict(mf))
    if '残疾' in f.get('negative_identity_tags',[]): return False,'成员已明确无残疾'
    raw=str(f.get('disability') or '')+' '+str(f.get('disability_grade') or '')
    if '残疾' not in member_identity_tags(f): return 'unknown','成员残疾身份尚未确认'
    digits={'一':'1','二':'2','三':'3','四':'4'}
    normalized=text.translate(str.maketrans(digits))
    grades=set()
    for a,b in re.findall(r'([1234])级?\s*(?:至|到|[-—])\s*([1234])级',normalized):
        grades.update(range(min(int(a),int(b)),max(int(a),int(b))+1))
    for group in re.findall(r'([1234](?:[、和及][1234])*)级',normalized):
        grades.update(int(g) for g in re.findall(r'[1234]',group))
    actual=re.search(r'([一二三四1234])级',raw)
    if grades:
        if re.search(r'级(?:及)?(?:以上|以下|以外)|除[^。；]*[1234]级',normalized):
            return 'unknown','残疾等级范围或排除表达需核对具体政策口径'
        if not actual: return 'unknown','政策要求的残疾等级尚未确认'
        value=int({'一':'1','二':'2','三':'3','四':'4'}.get(actual[1],actual[1]))
        if value not in grades: return False,'成员为%s级残疾，政策明确限定%s级'%(value,'、'.join(map(str,sorted(grades))))
    elif '重度残疾' in text:
        # Do not invent a numeric definition where the policy has not stated one.
        if '重度' not in raw: return 'unknown','政策限定重度残疾，具体等级适用口径需核对政策原文'
    return True,'残疾身份与原文明确等级未发现冲突'


def employment(text, mf):
    if '劳动力' in text and mf.get('labor') in ('无劳动力','丧失劳动力'):
        return False,'政策要求具备劳动能力，成员明确无劳动力或丧失劳动力'
    if not re.search(r'务工|就业|培训|创业|职业技能',text): return True,'无当前就业活动必要条件'
    if re.search(r'用人单位|吸纳|招用|企业|经营主体',text):
        return True,'经营主体资格另行核验'
    if mf.get('job_status')=='已退休' and canonical_fact('work_willing',mf.get('work_willing'))=='0':
        return False,'成员明确已退休且不再就业，不符合该就业活动分支'
    if re.search(r'省外.{0,8}务工|务工.{0,8}省外',text):
        if mf.get('work_location')=='江西省内': return False,'政策要求在江西省外务工，成员明确在省内务工'
        if is_unknown(mf.get('work_location')): return 'unknown','是否已在江西省外务工尚未确认'
    if re.search(r'(?:须|已|正在|稳定).{0,6}(?:务工|就业)|就业满|务工就业',text):
        if mf.get('job_status')=='未就业／正在求职': return False,'该分支要求已经务工就业，成员明确正在求职'
        if is_unknown(mf.get('job_status')): return 'unknown','当前实际就业情况尚未确认'
    return True,'就业分支未发现明确冲突'


def necessary_checks(policy, detail, mf):
    """AND independent necessary sources, OR only explicit independent branches."""
    sources=[str(detail.get('conditions') or policy.get('conditions') or ''),
             str(detail.get('target') or policy.get('target') or '')]
    parent=str(policy.get('conditions') or '')
    if parent and parent not in sources: sources.append(parent)
    # A narrowly named student benefit still requires a student when source tags are broad.
    title=str(policy.get('name') or '')
    if stage_requirement(title) and re.search(r'助学|学费|教育|学生|寄宿|教科书',title):
        sources.append(title+'在读')
    if re.search(r'学生|助学|学杂费|雨露计划',title) and not any(re.search(STUDENT,s) for s in sources):
        sources.append(title)
    checks={}
    from special_relevance import pregnancy_match
    from special_relevance import PREGNANCY
    if re.search(PREGNANCY,title) and not any(re.search(PREGNANCY,s) for s in sources):sources.append(title)
    for key,fn in [('education_required',education),('disability_required',disability),('employment_required',employment),('pregnancy_required',pregnancy_match)]:
        checks[key]=combine([combine([fn(p,mf) for p in alternatives(s)],'any') for s in sources if s],'all')
    # Preserve branch correlation: one cannot borrow education from branch A and
    # disability from branch B to satisfy an OR expression.
    checks['subject_branches']=combine([
        combine([combine([fn(p,mf) for fn in (education,disability,employment,pregnancy_match)],'all')
                 for p in alternatives(s)],'any') for s in sources if s], 'all')
    return checks
