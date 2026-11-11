"""Explicit household facts. Unknown values and personal scope survive every turn."""
import copy
import re

UNKNOWN = ('未知', '不清楚', '暂时无法提供', '不知道', '未确认')
STATE_KEYS = ('declared_member_count', 'tags', 'negative_tags', 'annual_income', 'income_level', 'house_level',
              'drink_water', 'big_disease_cnt', 'chronic_cnt', 'out_of_pocket',
              'out_of_pocket_year', 'out_of_pocket_month', 'disaster_loss',
              'med_insured_cnt', 'basic_review', 'clarification', 'fact_conflicts',
              'fact_sources', 'previous_decisions', 'supplementary_items')
STATE_KEYS += ('medical_expense_member', 'medical_expense_basis', 'annual_income_basis')
STATE_KEYS += ('policy_core',)
PERSONAL = {'残疾', '就业困难人员', '高校毕业生', '返乡农民工', '独生子女', '烈士子女'}


def is_unknown(value):
    return value is None or value == '' or str(value).strip() in UNKNOWN


def identities(text, aliases):
    """Longest non-overlapping aliases, with local negation (no substring guessing)."""
    # Labels are questions, never affirmative evidence ("是否享受低保政策：否").
    text = str(text or '')
    text = re.sub(r'(?:是否享受低保政策|是否享受特困供养|残疾情况|残疾等级)\s*[:：]\s*', '', text)
    occupied, yes, no = set(), set(), set()
    all_aliases = '|'.join(re.escape(a) for _, aa in aliases for a in aa)
    pairs = sorted(((a, norm) for norm, aa in aliases for a in aa), key=lambda x: -len(x[0]))
    for alias, norm in pairs:
        for m in re.finditer(re.escape(alias), str(text or '')):
            span = set(range(m.start(), m.end()))
            if span & occupied:
                continue
            occupied.update(span)
            prefix = re.split(r'[,，;；。\n：:]|但是|但', text[:m.start()])[-1]
            negative = bool(re.search(r'(?:不是|不属于|非|未|无|没有|不享受|未认定为|尚未认定为)\s*$', prefix))
            # Negation covers a coordinated identity list, not the next affirmative clause.
            negative = negative or bool(re.fullmatch(
                r'.*?(?:不是|不属于|没有|无|非)(?:(?:' + all_aliases + r')|[、及或和与\s])*', prefix))
            suffix = text[m.end():]
            # A coordinated list followed by "不清楚" is not an affirmative claim.
            tail = re.split(r'[;；。\n]', suffix)[0]
            if re.search(r'不清楚|不知道|未知|未确认', tail) and not re.search(r'是|已认定|已享受', tail):
                continue
            negative = negative or bool(re.match(r'\s*[:：]\s*(?:否|无|0)(?=$|[\s，,；;。])', suffix))
            (no if negative else yes).add(norm)
    return yes - no, no


def member_facts(text, fields):
    t = re.sub(r'\s+', '', str(text or ''))
    from special_relevance import extract_facts
    extract_facts(t,fields)
    fields.setdefault('fact_text', [])
    if t and t not in fields['fact_text']:
        fields['fact_text'].append(t)
    # Do not infer an insurance type, pension enrollment or pension receipt from vague wording.
    for label, key in [('城乡居民基本医疗保险', 'med_rural'), ('居民医保', 'med_rural'),
                       ('城镇职工基本医疗保险', 'med_urban'), ('职工医保', 'med_urban'),
                       ('城乡居民基本养老保险', 'pen_rural'), ('职工养老保险', 'pen_urban_emp')]:
        if label not in t:
            continue
        explicit=re.search(re.escape(label)+r'[:：](是|否|0|1)',t)
        neg = (explicit and explicit[1] in ('否','0')) or re.search(r'(?:未参加|不参加|没有参加|未缴纳|未参保|没有)' + re.escape(label), t)
        fields[key] = '0' if neg else '1'
        if key.startswith('med_'):
            if not neg: fields['med_insured'] = '1'
    if re.search(r'医保未参保|未参加(?:基本)?医疗保险|未参保医保', t):
        fields['med_insured'] = '0'
    elif re.search(r'医保参保|参加医保|参保医保|医疗保险参保', t):
        fields['med_insured'] = '1'
    if 'med_insured' in fields:
        fields.pop('med_insured_source', None)
    if '享受养老保险' in t:
        fields['pension_description'] = '享受养老保险（参保或领取含义待明确）'
    if re.search(r'(?:已经|正在|已)?领取(?:基本)?养老金|领取退休金', t):
        fields['pension_receiving'] = '1'
    if re.search(r'未领取(?:基本)?养老金', t):
        fields['pension_receiving'] = '0'
    import smart_matcher_kb as s
    # Keep explicit keyed facts even when the caller only supplied a free-text block.
    for label,key in [('是否享受低保政策','dibao'),('是否享受特困供养','tegang'),('是否在校','enrolled')]:
        hit=re.search(re.escape(label)+r'[:：](是|否|0|1)', t)
        if hit: fields[key] = ('1' if hit[1] in ('是','1') else '0')
    dis=re.search(r'(?:残疾情况|残疾等级)[:：](无残疾|无|否|未知|不清楚|[一二三四1234]级[^，,；;：:]*?)(?=是否|已|居民医保|职工医保|$|[，,；;])', t)
    if dis: fields['disability']=dis[1]
    grade = re.search(r'([一二三四1234])级(?:肢体|视力|听力|言语|智力|精神|多重)?残疾', t)
    if grade and not re.search(r'无残疾|未认定|不清楚|未知', t):
        fields['disability_grade'] = {'1':'一','2':'二','3':'三','4':'四'}.get(grade[1],grade[1])+'级'
        fields['disability'] = grade[0]
        # "非健康" is broad, not a contradiction of the explicitly stated disability.
        if fields.get('health') in (None,'','非健康','未知'): fields['health']='残疾'
    disability_type=re.search(r'(肢体|视力|听力|言语|智力|精神|多重)残疾',t)
    if disability_type and grade:
        fields['disability_type']=disability_type[1]+'残疾'
        fields['disability']=fields['disability_grade']+fields['disability_type']
    if re.search(r'(?:非|不是|没有|无)本县户籍',t): fields['local_hukou']='否'
    elif re.search(r'本县户籍',t): fields['local_hukou']='是'
    registered=re.search(r'(?:户籍(?:地)?[:：]([\u4e00-\u9fff]+(?:县|区))|([\u4e00-\u9fff]+(?:县|区))户籍)',t)
    if registered:
        value=registered[1] or registered[2]
        fields['hukou_region']=re.split(r'[，,；;。]',value)[-1]
    yes, no = identities(t, s.IDENTITY_ALIASES)
    yes = yes & PERSONAL
    # Member-level low-income status is never automatically assigned to other members.
    for norm in ('低保', '特困'):
        y, n = identities(t, s.IDENTITY_ALIASES)
        if norm in y:
            yes.add(norm)
    fields['identity_tags'] = sorted(yes | set(fields.get('identity_tags') or []))
    fields['identity_affirmations'] = sorted(yes | set(fields.get('identity_affirmations') or []))
    fields['negative_identity_tags'] = sorted(no | set(fields.get('negative_identity_tags') or []))
    fields['identity_tags'] = [x for x in fields['identity_tags'] if x not in fields['negative_identity_tags']]
    if '就业困难人员' in yes:
        fields['member_obj'] = '就业困难人员'
        if re.search(r'已(?:经)?(?:正式)?认定|认定为就业困难人员', t):
            fields['employment_certified'] = '是'
    if '就业困难人员' in no:
        fields.pop('member_obj', None)
    stage=re.search(r'(小学|初中|高中|中职|大专|本科|研究生|学前|幼儿园)(?:在读|在校)',t)
    if not stage:
        stage=re.search(r'(?:在读|就读)(小学|初中|高中|中职|大专|本科|研究生|学前|幼儿园)',t)
    if stage:
        fields['stage']=stage[1];fields['enrolled']=True
    if re.search(r'不在校|不在读|已毕业|没有在读|无在读',t): fields['enrolled']=False
    if re.search(r'不再就业|无就业意愿|不愿就业|没有就业意愿',t): fields['work_willing']='0'
    if '已退休' in t: fields['job_status']='已退休'
    if re.search(r'未就业|尚未就业|正在求职|目前求职|待业',t): fields['job_status']='未就业／正在求职'
    elif re.search(r'(?<!未)(?<!非)灵活就业',t): fields['job_status']='灵活就业'
    elif re.search(r'正在创业|自主创业|个体经营',t): fields['job_status']='自主创业'
    elif re.search(r'单位就业|已在.*公司工作|企业就业',t): fields['job_status']='单位就业'
    months=re.search(r'(?:连续|稳定)?就业(?:已|满)?(\d+)(个?月|年)',t)
    if months: fields['employment_months']=int(months[1])*(12 if months[2]=='年' else 1)
    if re.search(r'省外务工|在江西省外务工',t): fields['work_location']='江西省外'
    elif re.search(r'省内务工|在江西省内务工',t): fields['work_location']='江西省内'
    elif re.search(r'(?:本县|本地|本市).{0,6}(?:务工|就业|工作)',t): fields['work_location']='江西省内'


def canonical_fact(key, value):
    """Normalize representations before detecting a disagreement between turns."""
    if key == 'enrolled':
        if value is True or str(value).strip() in ('是','1','True','在校','在读'): return True
        if value is False or str(value).strip() in ('否','0','False','不在校','不在读','无','已毕业'): return False
    if key in ('dibao','tegang','med_insured','med_rural','med_urban','pen_rural','work_willing','local_hukou'):
        if value is True or str(value).strip() in ('是','1','True','有'): return '1'
        if value is False or str(value).strip() in ('否','0','False','无'): return '0'
    return value


def reconcile_member(fields):
    """Explicit negative values veto derived identity tags, including legacy snapshots."""
    for key in ('enrolled','dibao','tegang','med_insured','med_rural','med_urban','pen_rural','work_willing'):
        if key in fields: fields[key]=canonical_fact(key,fields[key])
    negatives=set(fields.get('negative_identity_tags') or [])
    for key,tag in [('dibao','低保'),('tegang','特困')]:
        if fields.get(key)=='0': negatives.add(tag)
    if str(fields.get('disability') or '').strip() in ('无','否','0','无残疾','非残疾','不是残疾人','没有残疾'):
        negatives.add('残疾')
    fields['negative_identity_tags']=sorted(negatives)
    fields['identity_tags']=sorted(set(fields.get('identity_tags') or [])-negatives)
    for tag in ('残疾','低保','特困'):
        key='identity_claim_'+tag
        if key in fields:
            if fields[key]=='是':
                fields['negative_identity_tags']=[x for x in fields['negative_identity_tags'] if x!=tag]
                fields['identity_tags']=sorted(set(fields['identity_tags'])|{tag})
            elif fields[key]=='否':
                fields['negative_identity_tags']=sorted(set(fields['negative_identity_tags'])|{tag})
                fields['identity_tags']=[x for x in fields['identity_tags'] if x!=tag]
            else:
                fields['negative_identity_tags']=[x for x in fields['negative_identity_tags'] if x!=tag]
                fields['identity_tags']=[x for x in fields['identity_tags'] if x!=tag]
            if tag=='残疾':
                if fields[key]=='否': fields['disability']='无';fields['health']='健康' if fields.get('health')=='残疾' else fields.get('health')
                elif fields[key]=='是' and str(fields.get('disability') or '') in ('无','否','未知',''):
                    fields['disability']='残疾（等级待确认）'
                elif is_unknown(fields[key]):
                    fields['disability']='未知'
                    if fields.get('health')=='残疾': fields['health']='未知'
            elif tag in ('低保','特困'):
                field='dibao' if tag=='低保' else 'tegang'
                fields[field]={'是':'1','否':'0'}.get(fields[key],'未知')
    return fields


def register_identity_conflicts(hh, member):
    """Only contradictory affirmative facts trigger a question, never field labels."""
    f=member['fields']
    positive=set(f.get('identity_affirmations') or [])
    if f.get('health')=='残疾': positive.add('残疾')
    negatives=set(f.get('negative_identity_tags') or [])
    for tag in positive & negatives:
        key='identity_claim_'+tag
        if key in f: continue
        conflicts=hh.setdefault('fact_conflicts',[])
        conflicts.append(dict(id='confirm_'+str(len(conflicts)),field=key,member_no=member['member_no'],old='否',new='是'))
        f[key]='未知'
    reconcile_member(f)


def persist(hh, data):
    for key in STATE_KEYS:
        if key in hh:
            data[key] = copy.deepcopy(hh[key])
    return data


def restore(data, hh):
    for key in STATE_KEYS:
        if key in data:
            hh[key] = copy.deepcopy(data[key])
    # Old snapshots may have leaked personal identities into household tags.
    hh['tags'] = [x for x in (hh.get('tags') or []) if x not in PERSONAL]
    # Legacy parsers inferred resident insurance and pension enrollment from vague text.
    # Preserve the legacy claim as unconfirmed; never silently trust it in a new decision.
    if data.get('_v', 0) < 3 and data.get('supplementary_info'):
        import smart_matcher_kb as s
        parsed = s.parse_household(data['supplementary_info'])
        for member in hh.get('members', []):
            fresh=next((m for m in parsed.get('members',[]) if m['member_no']==member['member_no']),None)
            if not fresh: continue
            f,n=member['fields'],fresh['fields']
            for key in ('med_rural','pen_rural'):
                if key in f and key not in n:
                    f.setdefault('legacy_unconfirmed',{})[key]=f.pop(key)
                elif key in n:
                    f[key]=n[key]
            for key in ('med_insured','pension_description','identity_tags','negative_identity_tags'):
                if key in n: f[key]=copy.deepcopy(n[key])
            if member.get('name') and any(x in member['name'] for x in ('住房','饮水','医保')):
                member['name']=None
    for member in hh.get('members',[]):
        reconcile_member(member['fields'])
        # Existing conversations retain member-owned raw facts. Backfill only
        # newly introduced fields; never overwrite an explicit answer or conflict.
        from special_relevance import extract_facts
        hints={}
        extract_facts('；'.join(member['fields'].get('fact_text') or []),hints)
        for key,value in hints.items():member['fields'].setdefault(key,value)
    if data.get('_v',0)<4 and data.get('supplementary_info'):
        import smart_matcher_kb as s
        fresh=s.parse_household(data['supplementary_info'])
        for key in ('disaster_loss','declared_member_count','med_insured_cnt'):
            if key not in hh and key in fresh: hh[key]=fresh[key]
        for member in hh.get('members',[]):
            parsed=next((m for m in fresh['members'] if m['member_no']==member['member_no']),None)
            if parsed:
                for key in ('disability','disability_grade','disability_type','health','med_insured','local_hukou','hukou_region'):
                    if is_unknown(member['fields'].get(key)) and key in parsed['fields']:
                        member['fields'][key]=parsed['fields'][key]
    if hh.get('region'): hh['region']=hh['region'].strip().rstrip('。；;，, ')
    reconcile_insurance(hh)
    return hh


def reconcile_insurance(hh):
    """Deduce only uniquely determined member states from an explicit household count."""
    members=hh.get('members',[])
    for m in members:
        f=m['fields']
        if f.pop('med_insured_source',None)=='household_count': f.pop('med_insured',None)
    count=hh.get('med_insured_cnt')
    declared=hh.get('declared_member_count')
    if not isinstance(count,int) or not declared or len(members)!=declared: return hh
    if any(not c.get('resolved') and c.get('field') in ('med_insured','med_insured_cnt') for c in hh.get('fact_conflicts',[])): return hh
    yes=[m for m in members if canonical_fact('med_insured',m['fields'].get('med_insured'))=='1']
    no=[m for m in members if canonical_fact('med_insured',m['fields'].get('med_insured'))=='0']
    if count < len(yes) or count > declared-len(no):
        conflicts=hh.setdefault('fact_conflicts',[])
        conflicts.append(dict(id='confirm_'+str(len(conflicts)),field='med_insured_cnt',member_no=0,
                              old=count,new=len(yes) if count<len(yes) else declared-len(no)))
        hh['med_insured_cnt']='未知'
        return hh
    unknown=[m for m in members if canonical_fact('med_insured',m['fields'].get('med_insured')) not in ('0','1')]
    remaining=count-len(yes)
    if remaining in (0,len(unknown)):
        for m in unknown:
            m['fields']['med_insured']='1' if remaining else '0'
            m['fields']['med_insured_source']='household_count'
    return hh


def merge(old, new, correcting=False):
    """Keep conflicting claims unresolved unless the user explicitly corrects prior input."""
    for key in STATE_KEYS:
        if key in ('clarification', 'previous_decisions', 'fact_conflicts') or key not in new:
            continue
        old[key] = copy.deepcopy(new[key])
    return old
