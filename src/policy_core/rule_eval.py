"""确定性规则求值器（C2）。

输入：compiled_rules.db 里的已编译规则 + 一个家庭的 facts（hh['policy_core']['facts']）
输出：与现有 build_rows 卡片语义对齐的判定结果（哪些政策对哪个成员部分成立/冲突/缺项）

不调用任何模型。engine.evaluate 是三值（True/False/None），None 表示家庭事实未知。
"""
import json
from .engine import evaluate

DICT_CACHE = {}


# 原子-政策同域词表：已证条件必须与政策文本谈的是同一件事。
# 否则会出现"因为家里有残疾人，就给免学杂费/学前教育/计划生育出卡"这类跨域噪声。
_DOMAIN_OF = {
    'disabled': ('残疾',), 'disability_grade': ('残疾',), 'disability_type': ('残疾',),
    'disability_certificate': ('残疾',),
    'labor': ('劳动力', '就业', '岗位', '务工', '培训'),
    'job_status': ('就业', '岗位', '务工', '失业'),
    'age': ('周岁', '年龄'),
    'study_stage': ('学', '教育', '学校', '学生'), 'enrolled': ('学', '教育', '学校', '学生'),
    'full_time_study': ('学', '教育', '学校', '学生'),
    'student_difficulty_recognized': ('学', '教育', '学校', '学生', '困难'),
    'med_insured': ('医保', '参保', '医疗', '保险'), 'med_type': ('医保', '参保', '医疗', '保险'),
    'medical_cost_12m': ('医保', '医疗', '费用'), 'medical_cost_year': ('医保', '医疗', '费用'),
    'medical_cost_month': ('医保', '医疗', '费用'),
    'chronic_disease': ('病', '疾病', '慢病'), 'major_disease': ('病', '疾病', '大病'),
    'housing_safe': ('住房', '危房', '房屋'), 'water_safe': ('饮水', '供水', '水'),
    'poverty_exit': ('脱贫', '监测', '帮扶'), 'monitoring': ('脱贫', '监测', '帮扶'),
    'member_poverty_exit': ('脱贫', '监测', '帮扶'), 'member_monitoring': ('脱贫', '监测', '帮扶'),
    'dibao': ('低保', '特困', '供养'), 'member_dibao': ('低保', '特困', '供养'),
    'tekun': ('特困', '供养'),
    'local_hukou': ('户籍', '本地', '本县'),
}


def domain_gate(satisfied_atoms, policy_text):
    """至少一个已证原子与政策文本同域；没有可映射词表的字段按放行处理（不误杀）。"""
    unknown = []
    for atom in satisfied_atoms:
        words = _DOMAIN_OF.get(atom.get('field'))
        if not words:
            unknown.append(atom)
            continue
        if any(w in policy_text for w in words):
            return True, ''
    if unknown:
        return True, ''
    return False, 'cross_domain_atom'


# 身份类事实：属于"该户/该人是否属于某类对象"，可以靠一次追问补齐。
# 实质类事实（年龄/学段/参保/病情/残疾等级等）决定政策是否真的适用，未确认时不宜出卡。
_IDENTITY_FIELDS = {
    'dibao', 'member_dibao', 'tekun', 'member_tekun', 'poverty_exit', 'monitoring',
    'member_poverty_exit', 'member_monitoring', 'local_hukou', 'hukou_region', 'region',
}


def only_identity_missing(missing_keys):
    """资格侧未确认的条件是否全部属于身份类（可由追问补齐）。"""
    for key in missing_keys:
        field = key.split(':', 1)[-1]
        if field not in _IDENTITY_FIELDS:
            return False
    return True


# 必要条件完整性门（模型无关）：政策类型决定了它必须有哪些谓词。
# 缺了就说明这条规则编译得比原文宽松，不能拿它出卡——转成追问线索即可。
# 依据：8 案例裁定中，免学杂费出给 62 岁成员、公益岗出给非脱贫户，全部属于这一类。
_EDU_WORDS = ('学杂费', '助学金', '学前教育', '免学费', '助学', '教科书', '寄宿', '就读', '幼儿园', '中职', '本专科')
_EDU_FIELDS = {'enrolled', 'study_stage', 'full_time_study', 'student_difficulty_recognized'}
_JOB_WORDS = ('公益性岗位', '岗位', '就业', '培训', '务工', '职业')
_NUM_OPS = ('gte', 'gt', 'lte', 'lt', 'between')


def _age_bounds(expr):
    """收集表达式里的年龄下界/上界。between 视为同时给出双边界。"""
    from .engine import atoms
    lower = upper = False
    for atom in atoms(expr):
        if atom.get('field') != 'age':
            continue
        op = atom.get('op')
        if op in ('gte', 'gt'):
            lower = True
        elif op in ('lte', 'lt'):
            upper = True
        elif op == 'between':
            lower = upper = True
    return lower, upper


def completeness_gate(row, rule):
    """政策类型 ↔ 规则谓词完整性检查。返回 (ok, reason)。"""
    text = ' '.join(str(row.get(k) or '') for k in
                    ('name', 'target', 'conditions', 'detail_target', 'detail_conditions'))
    from .engine import atoms
    fields = {a.get('field') for a in atoms(rule['eligibility'])}
    lower, upper = _age_bounds(rule['eligibility'])
    if any(w in text for w in _EDU_WORDS):
        if not (fields & _EDU_FIELDS):
            return False, 'education_without_school_field'
        if 'age' in fields and not upper:
            return False, 'education_without_age_upper'
    if any(w in text for w in _JOB_WORDS):
        if 'age' not in fields:
            return False, 'job_without_age_window'
        if not (lower and upper):
            return False, 'job_without_age_range'
    return True, ''

_SPECIAL = ('轮椅', '机动车', '农机', '器械', '辅具', '器具', '设备', '车辆', '船舶', '渔船', '机械', '工具')
_DOMAIN = ('养老保险', '医疗保险', '社保', '医保', '失业保险', '工伤保险', '生育保险')


def _fact_text(facts, fields):
    """已知且为真的事实（键+标签+真值）拼成的文本，用于 grounding。schema 标签不算家庭事实。"""
    out = []
    for key, fact in facts.items():
        if fact.get('state') != 'known' or fact.get('value') in (False, 'false', None, ''):
            continue
        definition = fields.get(key.split(':', 1)[-1]) or {}
        out.append(str(key) + str(definition.get('label') or '') + str(fact.get('value') or ''))
    return ' '.join(out)


def structural_gate(row, fact_text):
    """模型无关的结构门（与 reasoning.build_rows 同源）。

    返回 (ok, reason)。approve 必须在"政策对象要求的特指物项/险种"被家庭事实证实时才放行，
    否则给卡就是误放行——S-ZB-034（机动轮椅车）就是靠这道门拦下的。
    """
    name_txt = str(row.get('name') or '')
    target_txt = ' '.join(str(row.get(k) or '') for k in ('name', 'target', 'conditions', 'detail_target', 'detail_conditions'))
    hits = [t for t in _SPECIAL if t in target_txt and t not in fact_text]
    if hits:
        return False, 'special_object_missing:' + ','.join(hits)
    name_target = name_txt + str(row.get('target') or '') + str(row.get('detail_target') or '')
    for dom in _DOMAIN:
        if dom in name_target and dom not in fact_text and dom[:2] not in fact_text:
            return False, 'insurance_domain_unproven:' + dom
    return True, ''



def _fields():
    global DICT_CACHE
    if not DICT_CACHE:
        from .fields import dictionary
        DICT_CACHE = dictionary()
    return DICT_CACHE


def _members(hh):
    st = hh.get('policy_core') or {}
    out = []
    for m in st.get('members') or []:
        no = m.get('member_no') if isinstance(m, dict) else m
        if no is not None:
            out.append(str(no))
    return out or ['1']


# 户级身份事实 → 成员视角：脱贫户/监测对象是整户认定，同一户的每个成员共享该身份。
# 只补成员侧缺失、且户级已知的事实；户级未知不推断，成员自身事实优先。
_HOUSEHOLD_IDENTITY_ALIAS = {
    'member_poverty_exit': 'household:poverty_exit',
    'member_monitoring': 'household:monitoring',
}


def fact_view(hh):
    """把 hh 的 facts 转成 engine 需要的 {key: {state, value}}。

    region 归一：用户口语说"莲花县"，录入可能是"江西省萍乡市莲花县"，
    规则原子是 eq "莲花县"。全称不归一会让地区原子恒 False（漏卡），
    短名路径又会让纯 region 规则误出卡——统一归到县名。
    """
    st = hh.get('policy_core') or {}
    out = {}
    for key, fact in (st.get('facts') or {}).items():
        if not isinstance(fact, dict):
            continue
        value = fact.get('value')
        if key == 'household:region' and isinstance(value, str):
            try:
                from .geo import normalize_address
                county = (normalize_address(value) or {}).get('county')
                if county:
                    value = county
            except Exception:
                pass
        out[key] = dict(state=fact.get('state'), value=value, schema_id=fact.get('schema_id'))
    if st.get('members'):
        for member_no in _members(hh):
            for member_field, household_key in _HOUSEHOLD_IDENTITY_ALIAS.items():
                alias_key = member_no + ':' + member_field
                if alias_key in out:
                    continue
                household_fact = out.get(household_key)
                if not household_fact or household_fact.get('state') != 'known':
                    continue
                out[alias_key] = dict(state='known', value=household_fact.get('value'),
                                      schema_id=household_fact.get('schema_id'))
    return out


def _atoms_with_state(expr, facts, fields, member):
    """展平表达式，给每个原子附上三值结果，用于生成"已满足/冲突/缺项"。"""
    from .engine import atoms
    out = []
    for atom in atoms(expr):
        ev = evaluate(atom, facts, fields, member)
        out.append(dict(field=atom.get('field'), op=atom.get('op'), value=atom.get('value'),
                        evidence=atom.get('evidence') or [], result=ev.get('value'),
                        actual=ev.get('actual'), expected=ev.get('expected'),
                        missing=(ev.get('missing') or [])))
    return out


def evaluate_rule(rule, facts, fields, member):
    """单条规则对单成员/单户的三值求值。"""
    assoc = evaluate(rule['association'], facts, fields, member)
    elig = evaluate(rule['eligibility'], facts, fields, member)
    assoc_atoms = _atoms_with_state(rule['association'], facts, fields, member)
    elig_atoms = _atoms_with_state(rule['eligibility'], facts, fields, member)
    conflicts = [a for a in assoc_atoms + elig_atoms if a['result'] is False]
    satisfied = [a for a in assoc_atoms + elig_atoms if a['result'] is True]
    missing = sorted({k for a in assoc_atoms + elig_atoms for k in a['missing']})
    return dict(association=assoc.get('value'), eligibility=elig.get('value'),
                assoc_atoms=assoc_atoms, elig_atoms=elig_atoms,
                conflicts=conflicts, satisfied=satisfied, missing=missing)


def decide(rule, facts, fields, member):
    """把三值结果映射成卡片语义（与 build_rows 的"方向成立 → 才有条件可谈"口径一致）。

    card          : 方向成立（association True，或原文无对象限定）+ 至少一个资格条件成立 + 无明确冲突
    conflict      : 存在与已知事实明确冲突的必要条件 → 不出卡
    direction     : 方向条件未成立或未确定 → 不出卡（这正是 S-ZB-034 轮椅车类误放行的拦截点）
    no_satisfied  : 方向成立但资格条件一个都没确认 → 不出卡，只进追问
    eligible_full : 方向与全部资格条件均为 True
    """
    r = evaluate_rule(rule, facts, fields, member)
    if r['conflicts']:
        return dict(card=False, reason='conflict', **r)
    # 方向"未知"不等于"不适用"：家庭只是还没被问到该身份项。与 build_rows 一致，
    # 这类情况标为对方待确认，是否出卡交给结构门（特指物项/险种域）裁决。
    direction_ok = r['association'] is not False
    elig_satisfied = [a for a in r['elig_atoms'] if a['result'] is True]
    if not elig_satisfied:
        return dict(card=False, reason='no_satisfied_atom', **r)
    # 置信分层：方向成立只需 1 个已证条件即可出卡；方向未知（没追问过该身份项）时
    # 要求至少 2 个已证条件，避免"泛词命中一张卡"式的噪声。
    # 方向未知时：2 个以上已证条件，或只差身份认定（可通过追问补齐）才出卡；
    # 若还差实质条件（年龄/学段/参保/病情），先不进卡片，只做追问线索。
    unknown_fields = sorted({k for a in r['elig_atoms'] if a['result'] is None for k in a['missing']})
    strong = len(elig_satisfied) >= 2 or only_identity_missing(unknown_fields)
    tier = 'A' if r['association'] is True else ('B' if strong else None)
    if tier is None:
        return dict(card=False, reason='substantive_condition_missing', **r)
    status = 'eligible_full' if r['eligibility'] is True else 'partial_supported'
    return dict(card=True, reason=status, tier=tier, **r)



# 待遇合并：省级与县级常常是同一条待遇的两个版本（S-ZB-090 困难残疾人生活补贴 /
# X-ZB-301 困难残疾人补贴）。不合并的话用户会看到同一待遇的两张卡。
_MERGE_STRIP = ('生活补贴', '护理补贴', '补贴', '补助', '奖励', '资助', '标准', '政策', '办法', '项目')


def _benefit_key(name):
    text = str(name or '')
    for word in _MERGE_STRIP:
        text = text.replace(word, '')
    return text.strip()


def _merge_cards(cards):
    """同名/近名待遇只保留一张：保留已证条件最多、其次层级最高的。"""
    from difflib import SequenceMatcher
    kept = []
    for card in sorted(cards, key=lambda c: (-len(c['satisfied']), str(c['code']))):
        key = _benefit_key(card['name'])
        dup = None
        for other in kept:
            if other['member_no'] != card['member_no']:
                continue
            okey = _benefit_key(other['name'])
            if not key or not okey:
                continue
            if key in okey or okey in key or SequenceMatcher(None, key, okey).ratio() >= 0.82:
                dup = other
                break
        if dup:
            dup.setdefault('merged_codes', []).append(card['code'])
            continue
        kept.append(card)
    return kept


# 对象身份门：target 点名的资格身份必须落到字典字段上。
# 背景：X-CY-301（产业奖补）eligibility 只剩 region 原子，而 target 写明
# "脱贫人口,防止返贫致贫对象"——不加此门，任何地区符合的家庭都会误出卡。
_AUDIENCE_MAP = (
    ('脱贫人口', ('poverty_exit', 'member_poverty_exit')),
    ('脱贫户', ('poverty_exit', 'member_poverty_exit')),
    ('建档立卡', ('poverty_exit', 'member_poverty_exit')),
    ('防止返贫致贫', ('monitoring', 'member_monitoring')),
    ('监测对象', ('monitoring', 'member_monitoring')),
    ('返贫致贫', ('monitoring', 'member_monitoring')),
    ('特困', ('tekun', 'member_tekun')),
    ('低保', ('dibao', 'member_dibao')),
    ('最低生活保障', ('dibao', 'member_dibao')),
    ('残疾人', (None, 'disabled')),
    ('孤儿', (None, 'member_orphan')),
    ('水库移民', ('reservoir_migrant', 'reservoir_migrant')),
    ('农业户口', (None, 'rural_resident')),
    ('农村居民', (None, 'rural_resident')),
    ('学生', (None, 'enrolled')),
    ('在读', (None, 'enrolled')),
)


def audience_gate(row, rule, facts, member):
    """target 点名的身份：事实为 False → 拒卡；未知 → 补进缺项。返回 (missing, conflict)。"""
    text = ' '.join(str(row.get(k) or '') for k in ('target', 'detail_target'))
    if not text.strip():
        return [], []
    subject = 'household' if rule.get('subject') == 'household' else member
    required = []
    for keyword, (household_field, member_field) in _AUDIENCE_MAP:
        if keyword in text:
            field = household_field if subject == 'household' else member_field
            if field and (field, subject) not in required:
                required.append((field, subject))
    missing, conflict = [], []
    for field, who in required:
        key = ('household' if who == 'household' else str(who)) + ':' + field
        fact = facts.get(key)
        if not fact or fact.get('state') != 'known':
            missing.append(key)
        elif fact.get('value') is False:
            conflict.append(key)
    return missing, conflict


def evaluate_family(rules, hh, fields=None):
    """对一个家庭求值全部规则。返回卡片列表与全部判定明细。"""
    fields = fields or _fields()
    facts = fact_view(hh)
    members = _members(hh)
    fact_text = _fact_text(facts, fields)
    cards = []
    details = []
    for row in rules:
        rule = row['rule']
        if not rule:
            continue
        targets = ['household'] if rule.get('subject') == 'household' else members
        for member in targets:
            decision = decide(rule, facts, fields, member)
            gate_ok, gate_reason = structural_gate(row, fact_text)
            if decision['card'] and not gate_ok:
                decision = dict(decision, card=False, reason=gate_reason)
            if decision['card']:
                ok_complete, complete_reason = completeness_gate(row, rule)
                if not ok_complete:
                    decision = dict(decision, card=False, reason=complete_reason)
            if decision['card']:
                aud_missing, aud_conflict = audience_gate(row, rule, facts, member)
                if aud_conflict:
                    decision = dict(decision, card=False, reason='audience_conflict')
                elif aud_missing:
                    decision = dict(decision, card=decision['card'],
                                    missing=list(dict.fromkeys(list(decision['missing']) + aud_missing)))
            if decision['card']:
                policy_text = ' '.join(str(row.get(k) or '') for k in
                                       ('name', 'target', 'conditions', 'detail_target', 'detail_conditions'))
                ok_domain, domain_reason = domain_gate(
                    [a for a in decision['elig_atoms'] if a['result'] is True], policy_text)
                if not ok_domain:
                    decision = dict(decision, card=False, reason=domain_reason)
            item = dict(code=row['policy_code'], detail_id=row.get('detail_id'), name=row.get('name') or '',
                        member=('家庭' if member == 'household' else '成员' + member),
                        member_no=(0 if member == 'household' else member),
                        status=decision['reason'], unresolved=rule.get('unresolved') or [],
                        missing=decision['missing'],
                        satisfied=[dict(field=a['field'], op=a['op'], value=a['value'],
                                        actual=a['actual'], evidence=a['evidence'])
                                   for a in decision['satisfied']],
                        conflicts=[dict(field=a['field'], value=a['value'], actual=a['actual'])
                                   for a in decision['conflicts']],
                        weak=rule.get('weak', False), tier=decision.get('tier'))
            details.append(item)
            if decision['card']:
                cards.append(item)
    # 同 (code, member) 去重，保留已满足条件最多的那条
    best = {}
    for card in cards:
        key = (card['code'], card['member_no'])
        if key not in best or len(card['satisfied']) > len(best[key]['satisfied']):
            best[key] = card
    return _merge_cards(list(best.values())), details


def _filter_inactive_policies(rows):
    """T21：按所属政策状态过滤规则；状态无法确认时原样返回（保持旧行为）。

    为什么放在这里：规则路径唯一出卡入口是本函数的 load_rules（rule_flow.try_rule_run 直接
    使用它的返回值）；金仓 store 层的 load_rules 不过滤，编译 worker 仍能看到全部规则。
    """
    if not rows:
        return rows
    from . import rule_store
    if not rule_store.status_filter_enabled():
        return rows
    statuses = rule_store.policy_status_map({r.get('policy_code') for r in rows})
    if statuses is None:
        return rows
    kept = [r for r in rows if rule_store.is_active_policy_status(statuses.get(r.get('policy_code')))]
    if len(kept) != len(rows):
        try:
            from chain_trace import event as _event
            _event('rule.status_filtered', rules=len(rows), kept=len(kept),
                   dropped=len(rows) - len(kept))
        except Exception:  # 可观测性失败绝不影响判定
            pass
    return kept


def load_rules(county=None):
    """读规则（county 同时命中该县专属与省级/全国 '*' 规则），并补上政策名称。

    优先金仓 policy_rule；金仓不可用或该县无规则时回退到原型 SQLite，
    这样规则路径可以在迁移未完成时继续工作。
    """
    rows = []
    store_available = False
    try:
        from . import rule_store
        rows = rule_store.load_rules('*' if county is None else county)
        store_available = True
    except Exception:
        rows = []
    if not rows:
        from .compiler import load_rules as _load
        rows = _load('*' if county is None else county)
    # T21：停用/归档/软删/草稿政策不得再经规则路径出卡。只在金仓库可用时过滤——
    # store 不可用说明已回落 sqlite 离线编译路径（可能没有 policy 表），跳过过滤保持旧行为。
    if rows and store_available:
        rows = _filter_inactive_policies(rows)
    codes = sorted({r['policy_code'] for r in rows})
    # 必须先初始化：金仓查询失败时下面的 r.update(meta...) 不能引用未定义变量。
    # （实测缺陷：金仓异常 → UnboundLocalError: meta referenced before assignment，
    #   规则路径整体崩到全模型链路。）
    names, meta, dmeta = {}, {}, {}
    if codes:
        try:
            from kb_rag import _db
            from sqlalchemy import text, bindparam
            engine = _db()
            try:
                with engine.connect() as conn:
                    conn.execute(text('SET TRANSACTION READ ONLY'))
                    stmt = text('SELECT policy_code, policy_name FROM policy WHERE policy_code IN :codes'
                                ).bindparams(bindparam('codes', expanding=True))
                    names = {r['policy_code']: r['policy_name'] for r in conn.execute(stmt, dict(codes=codes)).mappings()}
                    # 结构门需要政策/明细原文；一次查回来，避免求值时再访问数据库
                    stmt2 = text('SELECT policy_code, target, conditions FROM policy WHERE policy_code IN :codes'
                                 ).bindparams(bindparam('codes', expanding=True))
                    meta = {r['policy_code']: dict(target=r['target'], conditions=r['conditions'])
                            for r in conn.execute(stmt2, dict(codes=codes)).mappings()}
                    stmt3 = text('SELECT detail_id, target, conditions, region FROM policy_detail WHERE detail_id IN :ids'
                                 ).bindparams(bindparam('ids', expanding=True))
                    ids = [int(r['detail_id']) for r in rows if str(r.get('detail_id') or '').isdigit()]
                    dmeta = {}
                    if ids:
                        dmeta = {str(r['detail_id']): dict(detail_target=r['target'], detail_conditions=r['conditions'],
                                                          detail_region=r['region'])
                                 for r in conn.execute(stmt3, dict(ids=ids)).mappings()}
            finally:
                engine.dispose()
        except Exception:
            names, meta, dmeta = {}, {}, {}
    for r in rows:
        r['name'] = names.get(r['policy_code'], '')
        r.update(meta.get(r['policy_code'], {}))
        r.update(dmeta.get(str(r.get('detail_id')), {}))
    return rows
