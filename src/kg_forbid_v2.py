# -*- coding: utf-8 -*-
"""kg_forbid_v2.py — forbid 证据化策略 v2.4.1（线上版本，2026-10-12 替换 v2.2）
⚠️ config.FORBID_STRATEGY 默认值为 'v2'（config.py:82），线上即 v2 启用状态。
v2.4.1 相对 v2.4 的修复（本轮）：
  F1. B1 正则顺序 bug：lt 正则 (低于|少于|<) 会误匹配"不少于/不低于"中的
      "少于/低于"子串，导致 ge 条件被错误路由为 lt。修复：lt 加负向断言 (?<!不)。
  F2. B1 补全"不高于"（语义 = ≤，并入 le 正则）。
  F3. B2 夹词窗口 {0,6} 收紧为 {0,5}，避免"非义务教育阶段残疾学生"这类
      "非"跨 6 字否定"残疾"的误伤；常见夹词（任何=2、相关资格=4）仍可命中。
  F4. 异常候选 forbid_v2 补 evidence 占位（{'verdict':'待核实',...}），
      保证下游 kg_batch5_merge.py 6 处 row['forbid_v2']['evidence']['verdict']
      不会 KeyError。
  F5. A1 实际集合收敛为 {'残疾','孤儿'}（孤儿为有效成员独占身份；
      "在校生/学生"不在 OBJECT_TERMS 中，属无效条目，移除），注释与实现一致。
本版保留 v2.4 全部修复（A1/A2/B1/B2 + v2.3 的 1~10）。
"""
import re
import traceback
from datetime import date
# ---------- 1. 互斥模式白名单（线索级） ----------
MUTEX_EXCLUDE_PATTERNS = [
    r'不得同时享受', r'不能同时享受', r'不能重复享受', r'不得重复享受',
    r'不重复享受', r'不可重复享受', r'不予重复', r'不能叠加享受', r'不得叠加', r'不重复享受同类',
]
MUTEX_NEGATIVE = [r'可重复享受', r'可以重复享受', r'允许重复享受', r'可以重复申领', r'可重复申领']
MUTEX_CLUE_ONLY = [r'择一享受', r'就高标准执行', r'按照高标准执行']
def is_mutex_evidence(text):
    """返回 (kind, 命中片段)：kind='exclude'|'clue'|None。"""
    if not text:
        return None, None
    s = str(text)
    for clause in re.split(r'[；;。\n]|但是|但', s):
        if any(re.search(r'(?<!不)' + neg, clause) for neg in MUTEX_NEGATIVE):
            continue
        for pat in MUTEX_EXCLUDE_PATTERNS:
            m = re.search(pat, clause)
            if m:
                return 'exclude', m.group(0)
    for pat in MUTEX_CLUE_ONLY:
        m = re.search(pat, s)
        if m:
            return 'clue', m.group(0)
    return None, None
# ---------- 2. 来源不清 forbid 收集 ----------
def parse_forbid_parts(forbid_text):
    parts = [re.sub(r'^A\d+', '', f.strip()) for f in str(forbid_text or '').replace('□', ' ').split()]
    return [p for p in parts if p]
def risk_check(checks, risk):
    for c in checks:
        if risk and risk in (c.get('risk') or ''):
            return c
    return None
def collect_unverified_forbid(checks, risks):
    out = []
    for risk in risks or []:
        ck = risk_check(checks, risk)
        if ck and ck.get('forbid') and str(ck.get('forbid')).strip() != '无限制':
            out.append({'risk': risk,
                        'categories': parse_forbid_parts(ck.get('forbid')),
                        'forbid_raw': ck.get('forbid'),
                        'unverified': True})
    return out
# ---------- 3. 地区：复用 kg_retrieval.region_judge ----------
def _region_eval(policy, detail, hh_region):
    """复用已验证的图谱地区判断 → ('适用'|'冲突'|'不适'|'未知', note)。"""
    try:
        import kg_retrieval as kgr
    except ImportError as e:
        return '未知', f'kg_retrieval 模块不可用：{e}'
    try:
        nodes, edges, same_doc, sim = kgr._load()[0]
        region_idx = kgr._cache.get('region_idx') if hasattr(kgr, '_cache') else None
        if region_idx is None:
            return '未知', '地区索引未初始化（缓存未加载）'
        flag, note = kgr.region_judge(policy, detail, hh_region or '', nodes, None, region_idx)
        if '启发式' in note:
            return '未知', '地区仅启发式匹配，需确认：' + note
        if flag == '适用':
            return '适用', note
        if flag == '冲突':
            # R2：明细级限定地区不含家庭地区 = 该明细分支明确不适用（政策级适用但分支限地区）；
            # 仅"信息缺失/无法解析"类冲突保留待核（证据不足）。
            if '不含家庭地区' in note:
                return '不适', note
            return '未知', note
        if flag == '不适':
            return '不适', note
        return '未知', note
    except KeyError as e:
        return '未知', f'地区判断缓存字段缺失：{e}'
    except Exception as e:
        print(f'[kg_forbid_v2] 地区判断异常：{traceback.format_exc()}')
        return '未知', f'地区判断异常（{type(e).__name__}）：{e}'
# ---------- 4. 条件维度 ----------
STAGE_TERMS = [
    ('研究生', '研究生'), ('硕士', '研究生'), ('博士', '研究生'),
    ('本科', '本科'), ('高职', '高职'), ('大专', '大专'), ('专科', '大专'),
    ('中等职业学校', '中职'), ('中职', '中职'), ('技工院校', '技工'), ('技校', '技工'),
    ('高中', '高中'), ('义务教育', '义务教育'), ('小学', '义务教育'), ('初中', '义务教育'),
    ('幼儿园', '学前'), ('学前', '学前'),
]
# 学段层级（用于"及以上/及以下"比较）
STAGE_LEVEL = {
    '学前': 0,
    '义务教育': 1, '小学': 1, '初中': 1,
    '高中': 2, '中职': 2, '技工': 2,          # 高中阶段教育，同级
    '高职': 3, '大专': 3, '专科': 3,          # 专科教育，同级
    '本科': 4,
    '研究生': 5, '硕士': 5, '博士': 5,
}
# A2：学段分组（方案A，保守策略）
# 同一组内可比较，跨组一律返回"未知"，交人工核实
STAGE_GROUP = {
    '学前': 'preschool',
    '义务教育': 'compulsory', '小学': 'compulsory', '初中': 'compulsory',
    # 高中阶段教育：高中、中职、技工 视为同组（学历层级相同）
    '高中': 'high_school', '中职': 'high_school', '技工': 'high_school',
    '高职': 'vocational_high', '大专': 'vocational_high', '专科': 'vocational_high',
    '本科': 'undergraduate',
    '研究生': 'graduate', '硕士': 'graduate', '博士': 'graduate',
}
OBJECT_TERMS = ['脱贫人口', '防止返贫致贫对象', '监测对象', '监测户', '低保户', '低保', '特困',
                '残疾', '孤儿', '烈士子女', '低收入', '困难学生', '困难家庭', '困难儿童',
                '因病因灾', '刚性支出', '易返贫致贫户', '建档立卡', '原建档立卡', '五保', '独生子女']
# P0-1：条件内对象词（本体身份词，未列入 OBJECT_TERMS 以免扰动 target 表达式解析）。
# 用于 conditions 中"就业困难人员""高校毕业生""返乡农民工"等对象限定参与对象命中。
OBJ_IDENTITY_TERMS = ('就业困难人员', '高校毕业生', '返乡农民工', '脱贫劳动力',
                      '防止返贫致贫对象', '边缘易致贫户', '突发严重困难户')
OBJ_GENERIC = ['所有农户', '一般农户', '建档立卡户', '城乡居民', '本省户籍', '户籍', '农户', '家庭',
               '人员', '群众', '居民', '农民', '脱贫户', '防止返贫致贫户', '困难群众', '在校生', '学生']
# F5：成员独占身份（不共享家庭标签）——仅保留有效条目：
#   '残疾'（个人身体状况）、'孤儿'（个人血缘身份）均在 OBJECT_TERMS 中；
#   '在校生/学生' 不在 OBJECT_TERMS，对象命中不会走到此集合，已移除（无效条目）。
MEMBER_ONLY_TAGS = {
    '残疾',
    '孤儿',
}
# R1：对象类关键词（即使 target 原文无法完整解析，也能用关键词+成员已知信息判"明确不符"）
OBJ_STUDENT_KEYWORDS = ('学生', '在校生', '在校', '在籍', '在读', '学籍', '学前教育', '幼儿园',
                        '助学', '入学', '高考', '中职', '高中', '大学生', '高校', '本科',
                        '研究生', '学费', '助学金', '就学', '新生', '专科', '大学')
OBJ_DISABILITY_KEYWORDS = ('残疾',)
OBJ_PREGNANT_KEYWORDS = ('孕妇', '孕产', '妊娠', '孕期')
OBJ_NEWBORN_KEYWORDS = ('新生儿', '婴儿', '婴幼儿')
OBJ_CHILD_KEYWORDS = ('儿童', '孤儿', '未成年人', '事实无人抚养')
OBJ_CLASS_KEYWORDS = ('孕妇', '新生儿', '儿童', '孤儿', '在校生', '学生', '残疾', '婴幼儿',
                      '未成年人', '新生', '专科', '中职', '高中', '大学', '本科', '研究生', '学前', '助学')
def _is_obj_list(clause):
    """clause 是否多对象清单（或关系）：至少 2 个片段各含独立对象词才视为清单，
    避免"本、专科新生"（同一对象的两类学段）被误判为清单而跳过对象冲突判断。"""
    parts = [p for p in re.split(r'[,，、]|和|及|与', str(clause or '')) if p.strip()]
    if len(parts) < 2:
        return False
    obj_parts = [p for p in parts
                 if any(k in p for k in OBJ_CLASS_KEYWORDS + tuple(OBJECT_TERMS))]
    return len(obj_parts) >= 2
NON_STUDENT_OBJ_WORDS = ('低保', '特困', '残疾', '老年人', '孤儿', '脱贫', '监测', '低收入',
                         '儿童', '婴幼儿', '新生儿', '孕妇', '烈士', '五保', '独生子女',
                         '务工', '就业', '农户', '人员', '群众', '居民')
def _student_only_clause(clause):
    """clause 是否"纯学生"对象限定：拆分段全部含学生词、且不含其他对象类别词。
    用于多对象清单也全部是学生类时（如"农村学生、城市涉农专业学生、家庭经济困难学生"），
    成员明确超龄/非在校 → 判不适用，避免教育类政策对 58 岁成员残留。"""
    parts = [p for p in re.split(r'[,，、]|和|及|与|；', str(clause or '')) if p.strip()]
    if not parts:
        return False
    for p in parts:
        if not any(k in p for k in OBJ_STUDENT_KEYWORDS):
            return False
        if any(k in p for k in NON_STUDENT_OBJ_WORDS):
            return False
    return True
def _obj_known_conflict(text_parts, member_fields):
    """利用成员已知信息判断对象类是否明确不符。
    返回 (None|'unknown'|'conflict', note)：conflict=明确不适用（对象与成员已知信息冲突）；
    unknown=证据不足待核实；None=无冲突。禁止把"未知"升级为"适用"。
    """
    mf = member_fields or {}
    age = mf.get('age')
    sex = str(mf.get('sex') or '')
    enrolled = mf.get('enrolled')
    joined = ' '.join(str(x) for x in (text_parts or []) if x)
    if not joined:
        return None, ''
    has_student = any(k in joined for k in OBJ_STUDENT_KEYWORDS)
    has_disability = any(k in joined for k in OBJ_DISABILITY_KEYWORDS)
    has_pregnant = any(k in joined for k in OBJ_PREGNANT_KEYWORDS)
    has_newborn = any(k in joined for k in OBJ_NEWBORN_KEYWORDS)
    has_child = any(k in joined for k in OBJ_CHILD_KEYWORDS)
    # 学生类：明确不在校/超龄 → 不适用；18-24 无在校证据 → 待核实
    if has_student:
        if enrolled in (False, '0', '否', '不在校', '已毕业'):
            return 'conflict', '对象为在校学生，成员已明确不在校/已毕业'
        if enrolled not in (True, '1'):
            return 'unknown', '对象为在校学生，成员在校状态未提供，需核实'
    # 残疾类：三态——明确无残疾=不适用；无残疾证据=待核实（信息缺失≠明确不符，需追问）；
    # 有残疾证据=不适用排除在此不做（由下方 has_evidence 命中逻辑决定）。
    if has_disability:
        mf_tags = str(mf.get('tags') or mf.get('member_obj') or '')
        if mf.get('disability') and _negated(str(mf['disability']), '残疾'):
            return 'conflict', '成员已明确无残疾，对象要求残疾人，明确不符'
        has_evidence = bool(mf.get('disability') and not _negated(str(mf['disability']), '残疾')) or \
                       ('残疾' in mf_tags) or str(mf.get('health')) == '残疾'
        if not has_evidence:
            return 'unknown', '对象为残疾人（需残疾证），成员未说明残疾身份，需核实'
    # 孕妇/孕产类：性别明确不符 → 不适用
    if has_pregnant:
        from special_relevance import pregnancy_match
        ok,note=pregnancy_match(joined,mf)
        if ok is False:return 'conflict',note
        if ok=='unknown':return 'unknown',note
    # 新生儿/婴幼儿类：年龄明确超限 → 不适用
    if has_newborn:
        if isinstance(age, int) and age > 3:
            return 'conflict', '对象为新生儿/婴幼儿，成员已%d岁，明确不符' % age
        if isinstance(age, int):
            return 'unknown', '对象为新生儿/婴幼儿，成员%d岁需核实' % age
    # 儿童/孤儿/未成年人：成年 → 不适用
    if has_child:
        if isinstance(age, int) and age >= 18:
            return 'conflict', '对象为儿童/未成年人，成员已成年（%d岁），明确不符' % age
    return None, ''
CREDENTIAL_TERMS = [('成绩', '成绩'), ('优秀', '优秀'), ('技能表现', '技能表现'), ('证书', '证书'),
                    ('资格证', '资格证'), ('职业资格', '职业资格'), ('认定', '认定'), ('培训合格', '培训合格'),
                    ('合格', '合格'), ('通过考试', '通过考试')]
JOIN_ANY = ('或', '及以上', '以上')
JOIN_ALL = ('且', '和', '与')
# F3：否定检测支持中间夹词（窗口 0~5 个非标点字符）
_NEG_PREFIXES = ('非', '无', '未', '没有', '不曾', '尚未', '并未',
                 '未取得', '未持有', '未通过', '不符合')
def _negated(v, key):
    """否定检测：支持否定词和关键词之间夹词（0~5 个非标点字符）。
    '未取得证书' / '未取得任何证书' / '未持有相关资格证书' → True
    '未通过。持有证书' → False（跨句不算）
    '非义务教育阶段残疾学生' → False（"非"与"残疾"间隔 6 字 > 5，
       避免把"非义务教育阶段的残疾学生"误判为"非残疾"；如业务出现更长的
       否定夹词，请按实际语料调整窗口并复验）
    """
    s = str(v)
    if key not in s:
        return False
    # 允许中间夹 0~5 个非标点字符，避免跨句误匹配
    gap = r'[^，。；、,.;\n]{0,5}'
    for neg in _NEG_PREFIXES:
        if re.search(re.escape(neg) + gap + re.escape(key), s):
            return True
    return False
def _member_negates_requirement(member_value, requirement):
    """成员值是否明确否定明细要求（如'未取得'否定'证书'）"""
    return _negated(member_value, requirement)
def _any_all(words, member_value, label, expression=None):
    """Only an explicit connector determines OR/AND; ambiguous lists stay unknown."""
    if not words:
        return True, '无%s限制' % label
    values = [x.strip() for x in re.split(r'[,，、]', str(member_value or '')) if x.strip()]
    if not values:
        return 'unknown', '需核实成员%s' % label
    mode = _expression_mode(expression or '', len(words))
    if mode is None:
        return 'unknown', '%s条件组合逻辑待核实' % label
    hits = [any(v == w and not _negated(v, w) for v in values) for w in words]
    return (all(hits) if mode == 'all' else any(hits)), '%s条件核验：%s' % (label, expression or words)
def _expression_mode(expression, count):
    if count <= 1:
        return 'any'
    has_or = bool(re.search(r'或(?:者)?', expression))
    has_and = bool(re.search(r'且|并且|同时|和|与', expression))
    if has_or and has_and:
        return None
    if has_or:
        return 'any'
    if has_and:
        return 'all'
    return None
def _term_expression(expression, aliases):
    """Return terms only if every character belongs to a supported expression."""
    text = re.sub(r'\s+', '', expression)
    pattern = re.compile('|'.join(re.escape(k) for k in sorted(aliases, key=len, reverse=True)))
    terms = [aliases[m.group()] for m in pattern.finditer(text)]
    remainder = pattern.sub('', text)
    remainder = re.sub(r'或者|并且|同时|或|且|和|与|[,，、]', '', remainder)
    if remainder or not terms:
        return None
    return list(dict.fromkeys(terms))
def _risk_match(condition, risks):
    from risk_vocabulary import RISK_ALIASES, canonical_risk
    aliases = {x: x for x in ('因病', '因学', '因残', '缺劳动力', '因产业项目失败',
                              '因务工就业不稳', '因自然灾害', '因意外事故', '失学辍学', '未参保',
                              '饮水安全问题', '住房安全问题')}
    aliases.update(RISK_ALIASES)
    aliases.update({'因安全住房': '因安全住房', '因安全饮水': '因安全饮水'})
    aliases = {key: canonical_risk(value) for key, value in aliases.items()}
    terms = _term_expression(condition, aliases)
    if terms is None or _expression_mode(condition, len(terms)) is None:
        return 'unknown', '风险条件组合或含义待核实'
    values = {canonical_risk(value) for value in risks or []}
    hits = [x in values for x in terms]
    ok = all(hits) if _expression_mode(condition, len(terms)) == 'all' else any(hits)
    return ok, '家庭风险集合核验'
def _stage_match(cond_text, member_fields):
    from eligibility_gates import alternatives, combine
    branches=alternatives(cond_text)
    if len(branches)>1:
        return combine([_stage_match(b,member_fields) for b in branches],'any')
    text = str(cond_text or '').strip()
    if not any(kw in text for kw, _ in STAGE_TERMS):
        return True, '无学段限制'
    if any(x in text for x in ('在校', '在读', '就读')):
        enrolled = (member_fields or {}).get('enrolled')
        if enrolled in (False, '0', '否', '不在校', '已毕业'):
            return False, '政策要求在校/在读，成员明确不在校'
        if enrolled not in (True, '1'):
            return 'unknown', '当前在校/在读状态尚未确认'
    if '全日制' in text:
        mode = (member_fields or {}).get('study_mode')
        if mode not in ('全日制', '非全日制'):
            return 'unknown', '全日制就读方式尚未确认'
        if mode != '全日制':
            return False, '要求全日制，成员明确为非全日制'
        text = text.replace('全日制', '')
    is_degree = text.endswith(('学历', '文化程度'))
    text = re.sub(r'^(?:须|必须|需|应)?(?:为|属于)?', '', text)
    text = re.sub(r'(?:在校生|在校学生|在读学生|学生|在读|在校)$', '', text)
    text = re.sub(r'(?:学历|文化程度)$', '', text)
    value = str((member_fields or {}).get('education' if is_degree else 'stage') or '').strip()
    aliases = dict(STAGE_TERMS)
    if value not in aliases and value not in STAGE_LEVEL:
        return 'unknown', '成员当前学段未知或未规范化'
    value = aliases.get(value, value)
    m = re.fullmatch(r'(.+?)(及以上|及以下)', text)
    if m:
        base = aliases.get(m.group(1))
        if base is None:
            return 'unknown', '学段范围无法完整解析'
        # A2：方案A，跨组一律返回未知
        value_group = STAGE_GROUP.get(value)
        base_group = STAGE_GROUP.get(base)
        if value_group != base_group:
            return 'unknown', f'学段跨组比较（{value} vs {base}），需人工核实'
        a, b = STAGE_LEVEL[value], STAGE_LEVEL[base]
        return (a >= b if m.group(2) == '及以上' else a <= b), '学段层级条件核验'
    terms = _term_expression(text, aliases)
    if terms is None:
        return 'unknown', '学段原文含未核验条件：%s' % str(cond_text)[:80]
    return _any_all(terms, value, '学段', text)
def _obj_known_terms(expr):
    """提取表达式中命中的已知对象词（子串匹配）。
    档次保护：'低保边缘/低保支出型（困难家庭）' 不得因含'低保'子串误命中'低保'。
    P0-1：并入 OBJ_IDENTITY_TERMS，使"就业困难人员/高校毕业生"等条件内对象词可参与命中。"""
    e = str(expr or '')
    if re.search(r'低保(?:边缘|支出)', e):
        return [t for t in list(OBJECT_TERMS) + list(OBJ_IDENTITY_TERMS) if t in e and t != '低保']
    return [t for t in list(OBJECT_TERMS) + list(OBJ_IDENTITY_TERMS) if t in e]


_OBJ_LIST_HINTS = ('人员', '对象', '人口', '农户', '农民', '学生', '儿童', '孤儿')


def _looks_like_obj_list(expr):
    """分隔片段>=2 且至少 2 个片段含对象词提示 → 或关系多对象清单。
    词表外对象词（'就业困难人员''返乡农民工'）使 _is_obj_list 识别失败时，
    不因单一片段维度冲突（如'高校毕业生'超龄）整体排除其他对象分支。"""
    parts = [p for p in re.split(r'[,，、]|和|及|与', str(expr or '')) if p.strip()]
    if len(parts) < 2:
        return False
    hints = OBJ_CLASS_KEYWORDS + tuple(OBJECT_TERMS) + OBJ_IDENTITY_TERMS + _OBJ_LIST_HINTS
    hits = [p for p in parts if any(k in p for k in hints)]
    return len(hits) >= 2


def _obj_match(target_text, cond_text, hh, member_fields=None):
    from eligibility_gates import alternatives, combine
    branches=alternatives(target_text)
    if len(branches)>1:
        return combine([_obj_match(b,cond_text,hh,member_fields) for b in branches],'any')
    branches=alternatives(cond_text)
    if len(branches)>1:
        return combine([_obj_match(target_text,b,hh,member_fields) for b in branches],'any')
    from smart_matcher_kb import identity_of as _identity_of
    from smart_matcher_kb import household_identity_tags as _hh_identity_tags
    from smart_matcher_kb import member_identity_tags as _mem_identity_tags
    target = str(target_text or '').strip()
    mf = member_fields or {}
    expr, terms = '', []
    target_pending = None
    # 成员/家庭身份标签（P0-1：统一走身份本体投影——'脱贫户'→'脱贫人口'、
    # '监测对象'→'防止返贫致贫对象' 等别名/上下位归一；'低保边缘' 独立不并入'低保'）
    member_labels = _mem_identity_tags(mf)
    family_labels = _hh_identity_tags(hh)
    if target and target not in ('所有农户', '一般农户', '无限制', '不限', '无'):
        expr = re.sub(r'(?:人员|对象)$', '', target)
        terms = _term_expression(expr, {x: x for x in OBJECT_TERMS})
        if terms is None or _expression_mode(expr, len(terms)) is None:
            # R1：原文无法完整解析时，先用对象关键词+成员已知信息判"明确不符"，
            #     但不提前返回——target 未解析出限定词时继续检查 conditions 中的对象词。
            # 多对象清单（或关系）不做整句冲突：'高校毕业生、就业困难人员、返乡农民工'
            # 不因'高校毕业生'维度不符就整体排除，其他对象词可能适用（下方逐词判定）。
            if _is_obj_list(expr) or _looks_like_obj_list(expr):
                pass
            else:
                conflict, note = _obj_known_conflict((expr,), mf)
                if conflict:
                    return (False if conflict == 'conflict' else 'unknown'), note
            # 对象词（含表外词如"返贫致贫人口"、单对象如"农村低保对象"）：任一已解析对象词
            # 命中成员/家庭身份即可判定对象条件满足（或关系，不因表外词整体阻断）
            known = _obj_known_terms(expr)
            if not known:
                # P0-1：后缀剥离后无词表命中（如'防止返贫致贫'←'防止返贫致贫对象'）
                # → 本体投影补词，使监测户等身份能命中。
                _norm = _identity_of(expr)
                if _norm:
                    known.append(_norm)
            if known:
                if any((_identity_of(t) in member_labels or _identity_of(t) in family_labels) for t in known):
                    return True, '对象条件已知词命中成员/家庭身份：%s' % ','.join(known)
                # 主表已知词均未命中：不立即 unknown——对象条件可能在条件句（conditions）中
                # 更具体（如 target 为"防止返贫致贫对象,脱贫人口,一般农户"分类标签，
                # conditions 才含"低保对象"等申请对象）。继续核验条件句，由 clause 层判定。
                target_pending = '适用对象主表已知词均未命中成员/家庭身份：%s' % expr[:60]
                expr, terms = '', []
            else:
                target_pending = '适用对象原文尚未完整核验：%s' % target[:80]
                expr, terms = '', []
        else:
            # 多对象清单（或关系，如"低保对象、特困人员、重度残疾人等缴费困难群体"）：
            # 不因任一单对象词（如"残疾"）证据缺失而整体误杀——成员/家庭标签命中判定
            # 在下方统一处理；学生/孕妇/新生儿等正交维度的明确冲突仍由
            # condition_clauses 循环（R1/R1b）覆盖。
            if len(terms) > 1 or _is_obj_list(expr):
                pass
            else:
                conflict, note = _obj_known_conflict(terms, mf)
                if conflict:
                    return (False if conflict == 'conflict' else 'unknown'), note
    condition_clauses = [x.strip() for x in re.split(r'[；;。\n]|并且|且', str(cond_text or '')) if x.strip()]
    for clause in condition_clauses:
        # R1c：clause 含"在校/在籍/在读/学籍"（+ 学段/学生词）→ 对象整体为在校学生限定，
        #      即使清单含"脱贫户学生、残疾学生、低保家庭学生"等混合项也全部是在校学生
        if any(k in clause for k in ('在校', '在籍', '在读', '学籍')):
            conflict, note = _obj_known_conflict((clause,), mf)
            if conflict:
                return (False if conflict == 'conflict' else 'unknown'), note
        # R1b：纯学生对象清单（拆分段全部为学生限定）→ 成员明确超龄/非在校 → 不适用
        if _student_only_clause(clause):
            conflict, note = _obj_known_conflict((clause,), mf)
            if conflict:
                return (False if conflict == 'conflict' else 'unknown'), note
        # R1：仅单一对象限定（非多对象清单）用已知信息判"明确不符"
        if not _is_obj_list(clause) and any(x in clause for x in OBJ_CLASS_KEYWORDS):
            conflict, note = _obj_known_conflict((clause,), mf)
            if conflict:
                return (False if conflict == 'conflict' else 'unknown'), note
        if any(x in clause for x in ('孕妇', '新生儿', '户籍', '在校生', '学生', '农村', '城镇', '城市')):
            stage_ok, _ = _stage_match(clause, mf)
            if not (any(kw in clause for kw, _ in STAGE_TERMS) and stage_ok is True):
                return 'unknown', '对象或身份条件待核实：%s' % clause[:80]
        if any(x in clause for x in OBJECT_TERMS) or any(x in clause for x in OBJ_IDENTITY_TERMS):
            clean = re.sub(r'^(?:须|必须|需|应)?(?:为|属于)?', '', clause)
            clean = re.sub(r'(?:人员|对象)$', '', clean)
            extra = _term_expression(clean, {x: x for x in OBJECT_TERMS})
            if extra is None:
                # 对象清单（或关系）不做单项年龄/身份冲突判断，避免误伤；
                # 词表外对象词（'就业困难人员''返乡农民工'）用 _looks_like_obj_list 保护
                if not _is_obj_list(clean) and not _looks_like_obj_list(clean):
                    conflict, note = _obj_known_conflict((clean,), mf)
                    if conflict:
                        return (False if conflict == 'conflict' else 'unknown'), note
                # 对象词（或关系，含表外词如"返贫致贫人口"、单对象如"农村低保对象"）→
                # 任一已解析对象词命中成员/家庭身份即满足（与 target 层退化一致）
                known = _obj_known_terms(clean)
                if known:
                    if any((_identity_of(t) in member_labels or _identity_of(t) in family_labels) for t in known):
                        return True, '对象条件已知词命中成员/家庭身份：%s' % ','.join(known)
                return 'unknown', '对象条件原文尚未完整核验：%s' % clause[:80]
            if terms and clean != expr:
                return 'unknown', '对象条件跨语句逻辑需核实'
            expr, terms = clean, extra
    if not terms:
        if target_pending:
            return 'unknown', target_pending
        return True, '无对象限制' if not target else '明确无对象限制'
    mode = _expression_mode(expr, len(terms))
    if mode is None:
        return 'unknown', '对象组合含义待核实'
    # 修复5 + A1/F5：成员独占身份不共享家庭标签；P0-1：身份词统一投影本体后比较
    hits = []
    for t in terms:
        tn = _identity_of(t) or t
        if t in MEMBER_ONLY_TAGS:
            hits.append(tn in member_labels)
        else:
            hits.append(tn in member_labels or tn in family_labels)
    if (all(hits) if mode == 'all' else any(hits)):
        return True, '对象条件已按成员/家庭明确身份核验'
    return 'unknown', '对象身份缺少对应成员或家庭证据：%s' % expr
def _house_match(cond_text, hh):
    text = str(cond_text or '')
    if not any(x in text for x in ('住房', '危房', '无房')):
        return True, '无住房等级限制'
    terms = _term_expression(text, {x: x for x in ('C级危房', 'D级危房', '无房户', '危房')})
    if terms is None or _expression_mode(text, len(terms)) is None:
        return 'unknown', '住房条件原文尚未完整核验'
    value = str((hh or {}).get('house_level') or '').strip()
    value = {'C': 'C级危房', 'D': 'D级危房', '无房': '无房户'}.get(value, value)
    if not value or value == '待核实':
        return 'unknown', '需核实住房等级'
    hits = [value == t or (t == '危房' and value in ('C级危房', 'D级危房')) for t in terms]
    return (all(hits) if _expression_mode(text, len(terms)) == 'all' else any(hits)), '住房等级条件核验'
# B1+F1/F2：收入条件区分 < 和 <=，并补充反向条件；lt 加负向断言避免"不少于/不低于"误路由
def _income_match(cond_text, hh):
    text = str(cond_text or '')
    if not any(x in text for x in ('收入', '刚性支出', '困难家庭')):
        return True, '无收入/困难程度限制'
    actual = (hh or {}).get('annual_income')
    if (hh or {}).get('annual_income_basis')=='家庭年总收入':
        # A household's annual total cannot be compared directly with a person's
        # monthly income or a published regional income statistic.
        if re.search(r'公布|上年度.*(?:居民|地区).*收入|地区.*收入',text):
            return 'unknown','政策使用地区公布的收入参数，不能用本户收入替代'
        if '年' not in text or re.search(r'月收入|月人均',text):
            return 'unknown','已知家庭年总收入，政策收入统计周期仍需确认'
        if '人均' in text:
            count=(hh or {}).get('declared_member_count')
            if not isinstance(count,int) or count<=0 or count!=len((hh or {}).get('members',[])):
                return 'unknown','需确认家庭统计人数，不能将家庭总收入直接作为人均收入'
            try: actual=float(actual)/count
            except (TypeError,ValueError): return 'unknown','家庭年收入尚未确认'
    def _compare(threshold, op, op_symbol):
        if actual is None:
            return 'unknown', f'需核实家庭年收入（门槛 {op_symbol} {threshold:.0f} 元）'
        try:
            val = float(actual)
        except (ValueError, TypeError):
            return 'unknown', f'家庭年收入格式异常，需核实（门槛 {op_symbol} {threshold:.0f} 元）'
        if op == 'le':
            return (val <= threshold), f'收入核验：{val} ≤ {threshold:.0f}'
        if op == 'lt':
            return (val < threshold), f'收入核验：{val} < {threshold:.0f}'
        if op == 'ge':
            return (val >= threshold), f'收入核验：{val} ≥ {threshold:.0f}'
        if op == 'gt':
            return (val > threshold), f'收入核验：{val} > {threshold:.0f}'
        return 'unknown', '未知比较操作'
    # F2：le 含"不高于"（语义 = ≤）；F1：lt 用 (?<!不) 排除"不少于/不低于"中的子串
    m = re.search(r'(不超过|≤|<=|不高于)\s*(\d+(?:\.\d+)?)\s*(万元|元)', text)
    if m:
        threshold = float(m.group(2))
        if m.group(3) == '万元':
            threshold *= 10000
        return _compare(threshold, 'le', '≤')
    m = re.search(r'(?<!不)(低于|少于|<)\s*(\d+(?:\.\d+)?)\s*(万元|元)', text)
    if m:
        threshold = float(m.group(2))
        if m.group(3) == '万元':
            threshold *= 10000
        return _compare(threshold, 'lt', '<')
    m = re.search(r'(不少于|不低于|≥|>=)\s*(\d+(?:\.\d+)?)\s*(万元|元)', text)
    if m:
        threshold = float(m.group(2))
        if m.group(3) == '万元':
            threshold *= 10000
        return _compare(threshold, 'ge', '≥')
    m = re.search(r'(高于|超过|>)\s*(\d+(?:\.\d+)?)\s*(万元|元)', text)
    if m:
        threshold = float(m.group(2))
        if m.group(3) == '万元':
            threshold *= 10000
        return _compare(threshold, 'gt', '>')
    # 身份标签匹配
    if text in ('低收入', '低收入家庭', '困难家庭'):
        value = str((hh or {}).get('income_level') or '').strip()
        if value == text or (text == '低收入家庭' and value == '低收入'):
            return True, '收入身份明确匹配'
    return 'unknown', '收入金额/口径或困难身份需对应证据核实'
# ---------- 4.5 已享受/排除句匹配 ----------
# 保障名称规范化：让户画像"低保"能命中"最低生活保障"，但严格区分"低保"与"低保边缘"
_ENJOYED_NORM = [
    ('最低生活保障边缘家庭', '低保边缘'),
    ('最低生活保障边缘', '低保边缘'),
    ('最低生活保障', '低保'),
    ('低保户', '低保'),
    ('低保边缘家庭', '低保边缘'),
    ('特困人员救助供养', '特困供养'),
    ('特困供养', '特困供养'),
    ('特困人员', '特困'),
    ('计划生育家庭', '计划生育'),
    ('独生子女', '计划生育'),
]
def _norm_benefit(x):
    out = str(x or '')
    for s, d in _ENJOYED_NORM:
        if s in out:
            out = out.replace(s, d)
    return out
def _benefit_hit(k, item):
    """判断已享受保障 k 是否命中排除句子项 item（防"低保"误命中"低保边缘"）。"""
    kn = _norm_benefit(k)
    subs = [s.strip() for s in re.split(r'[、,，/;；]', str(item or '')) if s.strip()]
    for sn_raw in subs:
        sn = _norm_benefit(sn_raw)
        if not kn or not sn:
            continue
        if kn in sn or sn in kn:
            # 防误判："低保" ≠ "低保边缘"（用户享低保不代表是低保边缘家庭）
            if ('边缘' in sn) != ('边缘' in kn):
                continue
            return True
    return False
def _enjoyed_match(cond_text, enjoyed):
    cond = str(cond_text or '')
    # 排除句：未纳入低保/特困等 → 已享受对应保障则明确不适用
    m2 = re.search(r'未纳入([^，。；\n]{2,40}?)(?:的)?(?:生活困难家庭|困难家庭|家庭|人员|对象|人口)?', cond)
    if m2:
        item = m2.group(1)
        if not enjoyed:
            return 'unknown', '需确认家庭是否已纳入「%s」保障' % item
        for k in (enjoyed or []):
            if _benefit_hit(str(k), item):
                return False, '家庭已享受「%s」，不符合"未纳入%s"条件' % (k, item)
        return True, '已享受核验：家庭未纳入「%s」' % item
    m = re.search(r'未享受过(.{2,20}?)(政策|补助|保障|支持|资助|补贴|改造)', cond)
    if not m:
        return True, '无已享受限制'
    item = m.group(1)
    if not enjoyed:
        return 'unknown', '需确认是否曾享受过「%s」' % item
    if any(k and (k in item or item in k) for k in (enjoyed or [])):
        return False, '已享受过「%s」，不符合"未享受过%s"条件' % (item, item)
    return True, '已享受核验：未享受过「%s」' % item
# ---------- 4.6 参保核验（结构化规则扩展：政策要求参保/未参保 ↔ 成员参保状态） ----------
_INSURANCE_REQUIRED = ('参加城乡居民基本医疗保险', '参加城乡居民医保', '参加基本医疗保险',
                       '已参加基本医疗保险', '参加医保', '应当参加', '应参加', '须参加', '必须参加')
_INSURANCE_EXCLUDED = ('未参加基本医疗保险', '未参保人员', '尚未参加基本医疗保险',
                       '没有参加基本医疗保险', '未参保')
# 具体险种条件（非城乡居民医保口径，需按参保记录核验险种差异，不得判"无限制"）
_INSURANCE_TYPE_OTHER = ('城镇职工基本医疗', '职工基本医疗', '职工医保', '商业医疗', '商业保险',
                         '长期护理保险', '工伤保险', '生育保险', '失业保险')
# 缴费时长类条件（需缴费记录核验，不得判"无限制"）
_INSURANCE_DURATION = ('连续缴费', '缴费满', '缴费年限', '参保满', '累计缴费', '缴费时长', '缴费期')
_INSURANCE_NEG_PATTERNS = (
    r'未参加(?:城乡居民)?基本医疗保险',
    r'尚未参加(?:城乡居民)?基本医疗保险',
    r'没有参加(?:城乡居民)?基本医疗保险',
    r'未参保(?:人员|对象|人口|群众)?',
    r'不(?:应)?参加(?:城乡居民)?基本医疗保险',
)
def _insurance_match(cond_text, member_fields):
    """参保核验：政策明确要求参保 ↔ 成员参保状态。
    明确冲突→False（不适用）；状态未知→'unknown'（待核实）；无参保条件→True。
    覆盖四类易错点：
      否定句：'未参保对象' + 成员已参保 → False（明确冲突），未参保 → True；
      倡导句：'应当参加' 等非硬性表述 → 不因未参保硬排除（按 REQUIRED 状态判定，状态未知→unknown）；
      且/复合条件：'已参加且连续缴费满2年' → 缴费时长需记录核验 → unknown；
      险种差异：'参加城镇职工基本医疗保险' 与家庭成员居民医保口径不同 → unknown 待核验。
    """
    text = str(cond_text or '')
    if not text:
        return True, '无参保限制'
    # Pension eligibility is independent of medical insurance and household identity.
    # A current pension enrollment flag alone does not prove eligibility for this subsidy.
    if '养老保险' in text:
        from policy_conditions import pension_match
        return pension_match(text, member_fields or {})
    mf = member_fields or {}
    ins = mf.get('med_insured')
    if ins is None and any(mf.get(k) in ('1','是',True) for k in ('med_rural','med_urban')): ins = '1'
    if '城乡居民基本医疗' in text or '居民医保' in text:
        ins = mf.get('med_rural')
    elif '职工' in text and ('医保' in text or '医疗保险' in text):
        ins = mf.get('med_urban')
    # Explicitly uninsured implies no enrollment in either subtype; the converse
    # does not hold (an insured member's subtype can still be unknown).
    if mf.get('med_insured') in ('0','否',False): ins='0'
    # 0) 或关系：参保是"或"分支之一时，该分支不满足不得整体排除；其他分支未知→待核实
    if re.search(r'或', text) and any(k in text for k in _INSURANCE_REQUIRED):
        if ins in ('是', '1', True):
            # 含具体险种时（如"参加基本医疗保险或城镇职工医保"），已参保不足以判定满足，
            # 需核验成员实际险种；仅无条件"参保或…"才直接通过。
            hit_type_or = [k for k in _INSURANCE_TYPE_OTHER if k in text]
            if hit_type_or and '城乡居民基本医疗' not in text:
                return 'unknown', '政策为“参保或…”或条件且含具体险种（%s），需核验成员实际险种' % hit_type_or[0]
            return True, '参保分支满足（政策为“参保或…”或条件）'
        return 'unknown', '政策为“参保或…”或条件，参保分支不满足，其他分支（救助条件等）待核验'
    # 1) 缴费时长类条件优先：需缴费记录外部证据，不得当作"无参保限制"
    hit_dur = [k for k in _INSURANCE_DURATION if k in text]
    if hit_dur:
        from policy_conditions import insurance_duration_key
        period = re.search(r'(?:连续缴费|累计缴费|缴费满)(?:满)?\s*(\d+)\s*(年|个月|月)',text)
        try:
            actual_months = int(mf.get(insurance_duration_key(text)))
        except (ValueError,TypeError):
            actual_months = None
        if period is None or actual_months is None:
            return 'unknown', '政策含缴费时长要求，需明确对应险种的缴费月数'
        required_months = int(period[1]) * (12 if period[2]=='年' else 1)
        if actual_months < required_months:
            return False, '缴费月数%s个月，未达到政策要求%s个月' % (actual_months,required_months)
    # 2) 具体险种条件：与"参加城乡居民基本医疗保险"口径不同 → 待核验
    hit_type = [k for k in _INSURANCE_TYPE_OTHER if k in text]
    if hit_type and '城乡居民基本医疗' not in text and ins not in ('1','是',True):
        return 'unknown', '条件涉及具体险种（%s），与家庭成员参保记录需核验险种差异' % hit_type[0]
    # 2.4) 兼容性描述："未参保也可申请/因灾未参保也可…"——未参保者可申请，
    #      不代表排除已参保者；已参保/未参保均不因本句被排除。
    if re.search(r'(?:未参保|未参加(?:城乡居民)?基本医疗保险)(?:也|亦|仍|也能|也可|仍可)(?:申请|享受|纳入|救助|补贴|保障)', text):
        return True, '政策表述为“未参保也可申请”（兼容性描述），不构成参保门槛'
    # 2.5) 明确"无需/不用/不需参加"：非参保门槛，不得当作要求参保
    if re.search(r'(?:无需|不用|不需|不必)参加', text):
        return True, '政策明确无需参加基本医疗保险（非参保门槛）'
    # 2.51) 否定句优先于倡导句：对象限定为"未参保人员"时，成员已参保即对象不符。
    for _neg_pat in _INSURANCE_NEG_PATTERNS:
        if re.search(_neg_pat, text):
            if ins in ('是', '1', True):
                return False, '政策对象为未参保人员，成员已参加基本医疗保险'
            if ins in ('否', '0', False):
                return True, '成员未参保，符合未参保对象条件'
            return 'unknown', '政策对象为未参保人员，成员参保状态待确认'
    # 2.6) 软性倡导（鼓励/提倡/积极/引导参加）：非硬性门槛，未参保不得硬排除，
    #      已参保可直接通过。"应当/应参加"属硬门槛（_INSURANCE_REQUIRED → 分支4）。
    if re.search(r'(?:鼓励|提倡|积极|引导)[^，。；]{0,15}(?:应当)?参加', text):
        if ins in ('是', '1', True):
            return True, '成员已参保（政策为倡导性表述）'
        return 'unknown', '政策为倡导性参保表述，成员未参保或状态待确认，需核实是否自愿参保'
    # 2.7) 且关系独立条件："参加医保并持有有效居住证"——参保核验通过不得覆盖同句其他独立要求
    if re.search(r'(?:并|且|同时)', text) and any(k in text for k in _INSURANCE_REQUIRED):
        other = re.sub(r'^.*?(?:并|且|同时)', '', text).strip(' ，。；、')
        if other and any(k in other for k in ('居住证', '暂住证', '户口', '户籍', '就业', '收入',
                                               '社保', '缴费', '居住', '稳定住所', '在本地')):
            return 'unknown', '句子同时要求参保与其他独立条件（%s），需单独核验' % other[:30]
    # 3) 否定句：政策对象为"未参保人员"
    for pat in _INSURANCE_NEG_PATTERNS:
        if re.search(pat, text):
            if ins in ('是', '1', True):
                return False, '政策对象为未参保人员，成员已参加基本医疗保险'
            if ins in ('否', '0', False):
                return True, '成员未参保，符合未参保对象条件'
            return 'unknown', '政策对象为未参保人员，成员参保状态待确认'
    # 4) 要求参保（含倡导句"应当参加"）：状态未知→unknown，不硬排除
    if any(k in text for k in _INSURANCE_REQUIRED):
        if ins in ('是', '1', True):
            return True, '成员已参加城乡居民基本医疗保险'
        if ins in ('否', '0', False):
            return False, '政策要求参加基本医疗保险，成员未参保'
        return 'unknown', '成员参保状态尚未确认'
    return True, '无参保限制'


def _credential_match(cond_text, member_fields):
    text = str(cond_text or '').strip()
    if not any(term in text for term, _ in CREDENTIAL_TERMS):
        return True, '无成绩/证书/资格条件'
    requirement = _credential_requirement(text)
    if requirement is None:
        return 'unknown', '成绩或证书原文要求尚未完整解析'
    value = str((member_fields or {}).get('credential') or '').strip()
    if not value or value == '待核实':
        return 'unknown', '缺少对应成绩/证书证据：%s' % requirement
    # B2/F3：_negated 支持夹词（窗口 0~5），这里自动受益
    if requirement in value and _negated(value, requirement):
        return False, '明确未满足要求：%s' % requirement
    normalized = re.sub(r'^(?:已取得|取得|已持有|持有|已获得|获得)', '', value)
    if normalized == requirement:
        return True, '对应资格证据明确匹配：%s' % requirement
    return 'unknown', '已提供证据与具体要求未明确匹配：%s' % requirement
def _credential_requirement(text):
    m = re.fullmatch(r'(?:须|必须|需|应)?(?:取得|持有|获得)([\u4e00-\u9fffA-Za-z0-9]+(?:资格证书|资格证|技能等级证书|合格证书|合格证))', text)
    if m:
        return m.group(1)
    if text in ('成绩优秀', '技能表现优秀', '培训合格', '通过考试'):
        return text
    return None
# 非条件性文字（程序说明/描述性文字，不算资格条件；整句匹配才算非条件）
NON_CONDITION_PATTERNS = [
    r'^(?:需申请|无需申报|无需申请|自动享受|系统识别|按程序办理|主动申请|自行申请)$',
    r'^按(?:各地|县级|当地|国家|省|市|文件)?(?:规定|标准|办法|方案|要求|执行)',
    r'^原文未说明.*$', r'^原文未细化.*$',
    r'^(?:具体|标准|金额|范围|限额).*由(?:各地|县级|当地).*(?:确定|制定)',
    r'^(?:详见|参照|依据).*',
    r'^(?:办理流程|申报材料|申请材料|所需材料|提供材料|咨询电话|联系方式|联系电话).*',
    r'^(?:原则上|积极引导|鼓励|引导).*',
    r'^(?:多干多补|少干少补|以干定补|应补尽补).*',
    r'^[（(].*[）)]$',
    # 文件年份/发文日期类描述（"2024年3月印发"）：非资格条件。
    # 注意：不能匹配"2025年新建的柑橘苗木假植网棚…"这类以年份开头的真实条件句
    # （否则 X-CY-429 种植业补助会被当非条件句跳过 → 无未覆盖句 → 误判"通过"）。
    r'^\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?(?:起)?(?:印发|发布|施行|出台|实施|底)',
    r'^\d{1,2}月(?:\d{1,2}日)?(?:起|底)?(?:印发|发布|施行|出台|实施)',
]
def _is_condition_sentence(sent):
    """判断是否为真正的资格条件句（程序说明/描述性文字不算）。"""
    s = str(sent or '').strip()
    if not s:
        return False
    for pat in NON_CONDITION_PATTERNS:
        if re.search(pat, s):
            return False
    return True


def _insurance_atom(atom):
    """判断句子片段是否为参保条件（由 _insurance_match 独立核验，不重复记为"条件未覆盖"）。
    覆盖：要求参保、未参保对象、缴费时长、具体险种差异。"""
    if any(k in atom for k in _INSURANCE_REQUIRED) or any(k in atom for k in _INSURANCE_EXCLUDED):
        return True
    if any(k in atom for k in _INSURANCE_DURATION):
        return True
    if any(k in atom for k in _INSURANCE_TYPE_OTHER) and re.search(r'(?:参加|参保|缴纳|享受)?(?:城镇职工|职工|商业|长期护理|工伤|生育|失业)', atom):
        return True
    for pat in _INSURANCE_NEG_PATTERNS:
        if re.search(pat, atom):
            return True
    return False


def _sentence_coverage(sent, tri, dims, detail=None):
    """返回 (covered: bool, uncovered_atoms: list)。"""
    uncovered = []
    atoms = [x.strip() for x in re.split(r'并且|且', sent) if x.strip()]
    for atom in atoms:
        if atom in ('无', '不限', '无限制', '所有农户', '一般农户'):
            continue
        if _insurance_atom(atom) and not any(x in atom for x in ('符合条件','低保','特困','监测','返贫','资助','连续','持有','灵活就业')):
            continue
        if dims.get('insurance',(None,''))[0] is True and re.fullmatch(r'(?:连续缴费|累计缴费|缴费满)(?:满)?\s*\d+\s*(?:年|个月|月)',atom):
            continue
        stage_text = re.sub(r'^(?:须|必须|需|应)?(?:为|属于)?', '', atom)
        stage_text = stage_text.replace('全日制', '')
        stage_text = re.sub(r'(?:在校生|在校学生|在读学生|学生|在读|在校|学历|文化程度)$', '', stage_text)
        stage_alias = dict(STAGE_TERMS)
        stage_expr = re.sub(r'及以上|及以下', '', stage_text)
        if _term_expression(stage_expr, stage_alias) is not None:
            continue
        from smart_matcher_kb import IDENTITY_ALIASES
        literal = re.sub(r'^(?:须|必须|需|应)?(?:为|属于)?', '', atom).strip()
        if dims.get('obj', (None,''))[0] is True and any(literal in aliases for _,aliases in IDENTITY_ALIASES):
            continue
        if dims.get('age', (None,''))[0] is True and re.fullmatch(r'(?:年龄[:：]?)?(?:未满|年满|不超过|满)?\d{1,3}(?:周岁|岁)?(?:及以上|以上|及以下|以下|[-至到]\d{1,3}(?:周岁|岁))?', literal):
            continue
        obj_text = re.sub(r'^(?:须|必须|需|应)?(?:为|属于)?', '', atom)
        obj_text = re.sub(r'(?:人员|对象)$', '', obj_text)
        if _term_expression(obj_text, {x: x for x in OBJECT_TERMS}) is not None:
            continue
        # 对象清单（或关系，含表外词如"返贫致贫人口"）：由 _obj_match 清单核验判定
        # （任一已知词命中成员/家庭身份→对象通过；均未命中→对象维度待核实），
        # 句子覆盖层不再重复标记"未覆盖"，避免与对象核验结论冲突拉低判定。
        if _is_obj_list(atom) and any(t in atom for t in OBJECT_TERMS):
            continue
        if _credential_requirement(atom) is not None:
            continue
        if _term_expression(atom, {x: x for x in ('C级危房', 'D级危房', '无房户', '危房')}) is not None:
            continue
        m = re.fullmatch(r'(?:年龄[:：]?)?(\d+)\s*[-~—至]\s*(\d+)(?:周岁|岁)', atom)
        if m and re.fullmatch(r'\s*%s\s*[-~—]\s*%s\s*' % m.groups(), str((detail or {}).get('age') or '')):
            continue
        m = re.fullmatch(r'(劳动能力|健康状况|风险类型)[:：](.+)', atom)
        if m:
            dim = {'劳动能力': 'lab', '健康状况': 'health', '风险类型': 'pov'}[m[1]]
            if str((detail or {}).get(dim) or '').strip() == m[2].strip():
                continue
        uncovered.append(atom)
    return len(uncovered) == 0, uncovered
def _tri_vs_member(det, member_fields, hh_risk):
    """结构化四维三态（pov 用家庭画像 hh_risk；未知≠满足；否定值不判满足）。"""
    tri = {}
    for dim, mval in (('age', member_fields.get('age')), ('lab', member_fields.get('labor')),
                      ('health', member_fields.get('health'))):
        dval = str(det.get(dim) or '').strip()
        if not dval or dval in ('无限制', '不限', ''):
            tri[dim] = '无限制'
            continue
        if dim == 'age':
            m = re.match(r'^\s*(\d+)\s*[-~—]\s*(\d+)\s*$', dval)
            if m and isinstance(mval, int):
                tri[dim] = '满足' if int(m.group(1)) <= mval <= int(m.group(2)) else '不满足'
            else:
                tri[dim] = '未知'
        else:
            mvals = [x.strip() for x in str(mval or '').split(',') if x.strip()]
            if not mvals:
                tri[dim] = '未知'
            else:
                dvals = [x.strip() for x in dval.split(',') if x.strip()]
                # 修复3 + B2/F3：用封装函数，支持夹词
                neg = any(_member_negates_requirement(v, d) for v in mvals for d in dvals if len(d) >= 2)
                if neg:
                    tri[dim] = '不满足'
                else:
                    matched = any(v in dvals for v in mvals)
                    # 残疾证据（健康字段为'健康'但单独填写残疾等级）满足残疾类明细要求
                    if not matched and dim == 'health' and any('残疾' in d for d in dvals):
                        dis_ev = member_fields.get('disability')
                        if dis_ev and not _negated(str(dis_ev), '残疾'):
                            matched = True
                    tri[dim] = '满足' if matched else '不满足'
    d_pov = str(det.get('pov') or '').strip()
    if not d_pov or d_pov in ('无限制', ''):
        tri['pov'] = '无限制'
    elif hh_risk:
        ok, _ = _risk_match(d_pov, hh_risk)
        tri['pov'] = '未知' if ok == 'unknown' else ('满足' if ok else '不满足')
    else:
        tri['pov'] = '未知'
    return tri
# ---------- 6. 成员×明细 证据核验 ----------
# ---------- 6.5 原因分类：给每条核验结果加结构化原因码 ----------
_REASON_RANK = ['region_conflict', 'region_unknown', 'unparsed_policy_condition',
                'ambiguous_policy_source', 'missing_external_evidence', 'missing_household_field']


def _derive_reason_codes(checks, miss):
    """由 checks 状态派生可追溯原因码（按优先级截取前 3 个，不改变核验结论）。
    原因码含义：
      missing_household_field  家庭资料未提供（在校状态/劳动能力/住房/收入等字段缺失）
      unparsed_policy_condition 政策条件存在，但程序尚未解析（未覆盖句）
      ambiguous_policy_source  原文含义或依据不清楚（互斥范围未确认/无可用原文条件）
      region_unknown           地区信息不足
      region_conflict          来源之间的地区范围冲突（明确不适用也保留该码用于追溯）
      missing_external_evidence 缺少证件、缴费记录等外部证据（残疾证/参保记录/收入凭证等）
    """
    codes = []
    region = checks.get('region')
    if region == 'conflict' or region is False:
        codes.append('region_conflict')
    elif region == 'unknown':
        codes.append('region_unknown')
    if checks.get('unparsed_conditions'):
        codes.append('unparsed_policy_condition')
    if checks.get('mutex'):
        codes.append('ambiguous_policy_source')
    if checks.get('source') == 'ambiguous':
        codes.append('ambiguous_policy_source')
    if checks.get('credential') == 'unknown' or checks.get('insurance') == 'unknown' \
            or checks.get('income') == 'unknown' or checks.get('enjoyed') == 'unknown':
        codes.append('missing_external_evidence')
    if checks.get('obj') == 'unknown' or checks.get('stage') == 'unknown' \
            or checks.get('house') == 'unknown' or checks.get('member') == 'unknown':
        codes.append('missing_household_field')
    tri = checks.get('tri')
    if isinstance(tri, dict) and any(v == '未知' for v in tri.values()):
        codes.append('missing_household_field')
    if not codes:
        return []
    return [c for c in _REASON_RANK if c in codes][:3]


def _age_match(cond_text, member_fields):
    cond=str(cond_text or '')
    if not re.search(r'岁|年龄|年满|高龄|老年',cond) or '年龄不限' in cond:
        return True, '原文未涉及年龄门槛'
    if '或' in cond or '、' in cond and re.search(r'老年|残疾|儿童',cond):
        return 'unknown', '年龄涉及多个适用对象分支，需分别确认对象条件'
    try: age=int(member_fields.get('age'))
    except (TypeError,ValueError): return 'unknown','成员年龄未知'
    constraints=[]
    interval=re.search(r'(\d{1,3})\s*(?:周岁|岁)?\s*[-至到~—]\s*(\d{1,3})\s*(?:周岁|岁)',cond)
    if interval: constraints.extend([(int(interval[1]),True,'low'),(int(interval[2]),True,'high')])
    for pat,inclusive,kind in [
        (r'未满\s*(\d{1,3})\s*(?:周岁|岁)',False,'high'),
        (r'不超过\s*(\d{1,3})\s*(?:周岁|岁)',True,'high'),
        (r'(\d{1,3})\s*(?:周岁|岁)\s*及以下',True,'high'),
        (r'(\d{1,3})\s*(?:周岁|岁)\s*以下',False,'high'),
        (r'(?:年满|(?<!未)满|达)\s*(\d{1,3})\s*(?:周岁|岁)',True,'low'),
        (r'(\d{1,3})\s*(?:周岁|岁)\s*(?:及以上|以上)',True,'low')]:
        for m in re.finditer(pat,cond): constraints.append((int(m[1]),inclusive,kind))
    if not constraints: return 'unknown','年龄门槛原文需确认'
    for n,inc,kind in constraints:
        ok=(age>=n if inc else age>n) if kind=='low' else (age<=n if inc else age<n)
        if not ok: return False,'年龄%s岁不满足原文门槛：%s'%(age,cond[:100])
    return True,'年龄%s岁满足原文门槛：%s'%(age,cond[:100])


def verify_member_detail(policy, detail, member_no, members, hh, enjoyed):
    """确定性证据核验；未知事实保持待核实。"""
    checks, miss = {}, []
    if policy.get('status') and policy['status'] != '在用':
        return dict(verdict='不适用', checks={'valid':False}, miss=[], note='政策当前已停用', mutex=None, evidence_text='', reason_codes=[])
    member = next((m for m in (members or []) if str(m.get('member_no')) == str(member_no)), None)
    if member is None:
        return {'verdict': '待核实', 'miss': ['对应家庭成员不存在'], 'checks': {'member': 'unknown'},
                'note': '成员编号未匹配，不推断成员资格', 'mutex': None, 'evidence_text': '',
                'reason_codes': ['missing_household_field']}
    mf = dict((member or {}).get('fields') or {})
    if mf.get('enrolled') in ('是', '在校', '在读', '1'):
        mf['enrolled'] = True
    elif mf.get('enrolled') in ('否', '不在校', '已毕业', '0'):
        mf['enrolled'] = False
    mf.setdefault('education', mf.get('edu'))
    student = str(mf.get('student') or '').strip()
    if re.fullmatch(r'(?:小学|初中|高中|中职|大专|本科|研究生)(?:在校生|在读)', student):
        mf.setdefault('stage', re.sub(r'在校生|在读', '', student))
        mf.setdefault('enrolled', True)
    today = date.today().isoformat()
    if policy.get('expire_date') and str(policy['expire_date']) < today:
        return {'verdict': '不适用', 'miss': [], 'checks': {'valid': False}, 'note': '政策已过有效期', 'mutex': None, 'evidence_text': '', 'reason_codes': []}
    if policy.get('effect_date') and str(policy['effect_date']) > today:
        return {'verdict': '不适用', 'miss': [], 'checks': {'valid': False}, 'note': '政策尚未生效', 'mutex': None, 'evidence_text': '', 'reason_codes': []}
    checks['valid'] = True
    hh_region = str((hh or {}).get('region') or '').strip()
    region_unknown = False
    if not hh_region:
        checks['region'] = 'unknown'
        miss.append('家庭地区未知')
        region_unknown = True
    else:
        rflag, rnote = _region_eval(policy, detail, hh_region)
        if rflag == '不适':
            checks['region'] = False
            return {'verdict': '不适用', 'miss': [], 'checks': checks, 'note': '地区核验：%s' % rnote, 'mutex': None, 'evidence_text': '',
                    'reason_codes': _derive_reason_codes(checks, miss)}
        if rflag == '冲突':
            checks['region'] = 'conflict'
            miss.append('地区冲突：%s' % rnote)
            region_unknown = True
        elif rflag == '未知':
            checks['region'] = 'unknown'
            miss.append('地区判定未知：%s' % rnote)
            region_unknown = True
        else:
            checks['region'] = True
    # 收紧 v2：明细层字段为空/无限制时，并入 policy 层结构化硬条件（劳动力年龄/劳动能力/健康状况），
    # 避免"宽松明细命中"（如 lab='无限制'）导致政策级明确要求（丧失劳动力等）未被核验。
    pcond = str(policy.get('conditions') or '')
    _merged = False
    if pcond:
        m = re.search(r'(?:劳动力年龄|年龄)[:：]\s*(\d+)\s*[-~—至]\s*(\d+)(?:岁|周岁)?', pcond)
        if m and str(detail.get('age') or '').strip() in ('', '无限制', '不限'):
            if not _merged:
                detail = dict(detail); _merged = True
            detail['age'] = '%s-%s' % (m.group(1), m.group(2))
        for dim, dim_key in (('lab', '劳动能力'), ('health', '健康状况')):
            m = re.search(r'%s[:：]\s*([^；;。]+)' % dim_key, pcond)
            if m and str(detail.get(dim) or '').strip() in ('', '无限制', '不限'):
                if not _merged:
                    detail = dict(detail); _merged = True
                detail[dim] = m.group(1).strip()
    # 收紧 v2：policy 层原文硬门槛独立核验（与明细层并列，取交集）——
    # 政策原文要求的年龄/劳动能力/健康任一不满足即不适用，明细层办理口径不得放宽原文门槛。
    _p_fail = None
    if pcond:
        m = re.search(r'(?:劳动力年龄|年龄)[:：]\s*(\d+)\s*[-~—至]\s*(\d+)(?:岁|周岁)?', pcond)
        if m:
            low, high = int(m.group(1)), int(m.group(2))
            a = mf.get('age')
            try:
                a = int(str(a).strip())
            except Exception:
                a = None
            if a is not None and (a < low or a > high):
                _p_fail = '政策原文年龄要求 %s-%s 岁，成员年龄 %s 不满足' % (low, high, a)
        if not _p_fail:
            m = re.search(r'劳动能力[:：]\s*([^；;。]+)', pcond)
            if m:
                reqs = [x.strip() for x in m.group(1).split(',') if x.strip()]
                mvals = [x.strip() for x in str(mf.get('labor') or '').split(',') if x.strip()]
                if mvals and not any(v in reqs or any(r in v or v in r for r in reqs) for v in mvals):
                    if all(('普通劳动力' in v or '技能劳动力' in v) for v in mvals) and \
                       all(('丧失' in r or '无劳动力' in r or '弱劳动力' in r or '半劳动力' in r) for r in reqs):
                        _p_fail = '政策原文要求劳动能力为%s，成员为%s，不满足' % (m.group(1).strip(), mf.get('labor'))
        if not _p_fail:
            m = re.search(r'健康状况[:：]\s*([^；;。]+)', pcond)
            if m:
                reqs = [x.strip() for x in m.group(1).split(',') if x.strip()]
                mvals = [x.strip() for x in str(mf.get('health') or '').split(',') if x.strip()]
                if mvals and not any(v in reqs or any(r in v or v in r for r in reqs) for v in mvals):
                    if any('健康' in v for v in mvals) and all('残疾' in r for r in reqs):
                        _p_fail = '政策原文要求健康状况为%s，成员为%s，不满足' % (m.group(1).strip(), mf.get('health'))
    if _p_fail:
        return {'verdict': '不适用', 'miss': [], 'checks': {'policy_hard': False},
                'note': _p_fail, 'mutex': None, 'evidence_text': '', 'reason_codes': []}
    tri = _tri_vs_member(detail, mf, (hh or {}).get('risk') or [])
    checks['tri'] = tri
    hard_fail = any(v == '不满足' for v in tri.values())
    unknown = any(v == '未知' for v in tri.values()) or region_unknown
    cond_txt = str(detail.get('conditions') or '') or str(policy.get('conditions') or '')
    tgt_txt = str(detail.get('target') or '') or str(policy.get('target') or '')
    if not cond_txt.strip() and not tgt_txt.strip():
        checks['source'] = 'ambiguous'
    stage_ok, stage_note = _stage_match(cond_txt, mf)
    obj_ok, obj_note = _obj_match(tgt_txt, cond_txt, hh, mf)
    from policy_conditions import object_guard
    obj_ok, obj_note = object_guard(cond_txt, hh, mf, obj_ok, obj_note)
    house_ok, house_note = _house_match(cond_txt, hh)
    inc_ok, inc_note = _income_match(cond_txt, hh)
    enj_ok, enj_note = _enjoyed_match(cond_txt, enjoyed or [])
    cred_ok, cred_note = _credential_match(cond_txt, mf)
    ins_ok, ins_note = _insurance_match(cond_txt + ' ' + tgt_txt, mf)
    age_ok, age_note = _age_match(cond_txt + ' ' + tgt_txt, mf)
    from eligibility_gates import necessary_checks
    necessary = necessary_checks(policy, detail, mf)
    for nm, (ok, note) in (('stage', (stage_ok, stage_note)), ('obj', (obj_ok, obj_note)),
                           ('house', (house_ok, house_note)), ('income', (inc_ok, inc_note)),
                           ('enjoyed', (enj_ok, enj_note)), ('credential', (cred_ok, cred_note)),
                           ('insurance', (ins_ok, ins_note)), ('age', (age_ok, age_note)),
                           *necessary.items()):
        checks[nm] = ok
        if ok is False:
            hard_fail = True
            miss.append(note)
        elif ok == 'unknown':
            unknown = True
            miss.append(note)
    # 修复4 + 修复10：记录全部未覆盖句子，不 break
    sentences = [s.strip() for s in re.split(r'[；;。\n]', cond_txt) if s.strip()]
    dims = {'stage': (stage_ok, stage_note), 'obj': (obj_ok, obj_note), 'house': (house_ok, house_note),
            'income': (inc_ok, inc_note), 'enjoyed': (enj_ok, enj_note), 'credential': (cred_ok, cred_note),
            'insurance': (ins_ok, ins_note), 'age': (age_ok, age_note)}
    uncovered_all = []
    for sent in sentences:
        # 非条件句（需申请/无需申报/按程序/原文未说明等）不算"条件未覆盖"
        if not _is_condition_sentence(sent):
            continue
        covered, uncovered_atoms = _sentence_coverage(sent, tri, dims, detail)
        if not covered:
            uncovered_all.extend(uncovered_atoms)
    if uncovered_all:
        checks['unparsed_conditions'] = uncovered_all
        unknown = True
        miss.append('条件原文未覆盖：%s' % '；'.join(s[:40] for s in uncovered_all[:3]))
    mutex_kind, mutex_txt = is_mutex_evidence(cond_txt + ' ' + str(detail.get('benefit') or '') + ' ' + str(policy.get('benefit') or ''))
    mutex = None
    if mutex_kind:
        checks['mutex'] = '%s:%s' % (mutex_kind, mutex_txt)
        mutex = {'kind': mutex_kind, 'evidence': mutex_txt,
                 'scope': '作用对象/时间/范围未确认，仅标线索（61条原文为待核对清单，不据此硬排除）'}
        miss.append('互斥条款线索待核对：%s（作用对象/时间/范围未确认，不按名称包含硬排除，需业务核对）' % mutex_txt)
        unknown = True
    evidence_text = '；'.join(x for x in (tgt_txt[:60], cond_txt[:120], str(detail.get('benefit') or '')[:60]) if x)
    if hard_fail:
        return {'verdict': '不适用', 'checks': checks, 'miss': miss,
                'note': '存在明确不满足的原文必要条件', 'mutex': mutex, 'evidence_text': evidence_text,
                'reason_codes': _derive_reason_codes(checks, miss)}
    if unknown:
        return {'verdict': '待核实', 'checks': checks, 'miss': miss,
                'note': '存在待核实信息，不凭模型猜测', 'mutex': mutex, 'evidence_text': evidence_text,
                'reason_codes': _derive_reason_codes(checks, miss)}
    return {'verdict': '通过', 'checks': checks, 'miss': [], 'note': '原文必要条件已核验',
            'mutex': mutex, 'evidence_text': evidence_text, 'reason_codes': []}
# ---------- 7. 统一入口 ----------
def _detail_by_policy(data, pid, detail_id):
    """只查本政策的明细；找不到 → None（不得跨政策补齐）。"""
    for d in (data.get('by_policy') or {}).get(pid, []):
        if str(d.get('detail_id')) == str(detail_id):
            return d
    return None
def _exact_category_match(cat, target):
    """精确匹配分类，避免子串误命中。"""
    if not cat or not target:
        return False
    parts = re.split(r'[,，、/]', str(target))
    return any(cat == p.strip() for p in parts)
def apply_forbid_v2(recs, hh, data):
    """对候选集应用 v2。
    返回 (out, excluded, pending, errors)：
      out      = 保留候选（forbid 标注+证据核验结果写回）；
      excluded = 证据明确不适用（不得再推荐）；
      pending  = 待核实候选清单（不升级为直接适用）；
      errors   = 处理失败记录（不静默）。
    注意：调用方 smart_matcher_kb.py:632 当前对 errors 非空即 raise RuntimeError
    （整份失败）。本文件已为异常候选补 evidence 占位，但"单条隔离保留"要真正生效，
    需调用方同步改为不因 errors 非空而整份失败——请按业务决定是否同步修改。
    """
    checks = data.get('checks') or []
    risks = (hh or {}).get('risk') or []
    unverified = collect_unverified_forbid(checks, risks)
    enjoyed_hh = (hh or {}).get('enjoyed') or []
    members = (hh or {}).get('members') or []
    by_policy = data.get('by_policy') or {}
    code2pid = {p['code']: p['pid'] for p in data.get('policies', [])}
    policies_by_pid = {p['pid']: p for p in data.get('policies', [])}
    def _forbid_hit(r):
        hit = []
        for u in unverified:
            matched_cats = []
            for f in u['categories']:
                if (_exact_category_match(f, r.get('cat1'))
                        or _exact_category_match(f, r.get('name') or r.get('policy_name'))
                        or _exact_category_match(f, r.get('category1'))):
                    matched_cats.append(f)
            if matched_cats:
                hit.append({'risk': u['risk'], 'categories': matched_cats, 'forbid_raw': u['forbid_raw']})
        return hit
    def _annotate(rec, hit, ev, err=None):
        rec['forbid_v2'] = {
            'mode': 'unverified' if hit else 'none',
            'hit': hit,
            'reason': 'rule_check.forbid 无项目内来源，未作为资格硬排除依据；不删除候选、不因该配置单独降排名',
            'expression': '不表达为「该家庭禁止享受」',
        }
        if ev is not None:
            rec['forbid_v2']['evidence'] = ev
        if err:
            rec['forbid_v2']['error'] = err
    out, excluded, pending, errors = [], [], [], []
    for r in recs:
        try:
            is_kg = 'members' in r and isinstance(r.get('members'), list)
            hit = _forbid_hit(r)
            if is_kg:
                new_members = []
                for m in r.get('members') or []:
                    m = dict(m)
                    pid = r.get('pid') or code2pid.get(r.get('policy_code'))
                    det = _detail_by_policy(data, pid, m.get('detail_id')) if pid else None
                    policy = policies_by_pid.get(pid) if pid else None
                    if policy is None:
                        m['eligible'] = '待核实'
                        m['forbid_v2'] = {'evidence': {'verdict': '待核实', 'note': '明细未匹配该政策（不跨政策补齐）',
                                                       'checks': {}, 'miss': ['明细与政策对应不上'], 'mutex': None, 'evidence_text': '',
                                                       'reason_codes': ['ambiguous_policy_source']}}
                        pending.append({'policy_code': r.get('policy_code'), 'member_no': m.get('member_no'),
                                        'detail_id': m.get('detail_id'), 'verdict': '待核实', 'note': '明细未匹配该政策'})
                    else:
                        if det is None:
                            # 政策在用但无明细分支：用政策级字段构造核验单元
                            det = {'detail_id': m.get('detail_id'), 'pid': policy.get('pid'),
                                   'region': policy.get('regions'), 'conditions': policy.get('conditions'),
                                   'target': policy.get('target'), 'age': policy.get('age'),
                                   'lab': policy.get('lab'), 'health': policy.get('health'),
                                   'pov': policy.get('pov'),
                                   'benefit': policy.get('benefit'), 'process': policy.get('process')}
                        ev = verify_member_detail(policy, det, m.get('member_no'), members, hh, enjoyed_hh)
                        m['forbid_v2'] = {'evidence': ev}
                        m['eligible'] = ev['verdict']
                        if ev['verdict'] == '不适用':
                            excluded.append({'policy_code': r.get('policy_code'), 'policy_name': policy.get('name'),
                                             'member_no': m.get('member_no'), 'detail_id': m.get('detail_id'),
                                             'verdict': '不适用', 'note': ev['note'], 'evidence': ev['evidence_text']})
                            continue
                        elif ev['verdict'] == '待核实':
                            pending.append({'policy_code': r.get('policy_code'), 'policy_name': policy.get('name'),
                                            'member_no': m.get('member_no'), 'detail_id': m.get('detail_id'),
                                            'verdict': '待核实', 'note': ev['note'], 'miss': ev['miss']})
                    new_members.append(m)
                r = dict(r)
                r['members'] = new_members
                r['dedup_hit_count'] = sum(m.get('eligible') == '通过' for m in new_members)
                _annotate(r, hit, None)
                if new_members:
                    out.append(r)
            else:
                r = dict(r)
                enjoyed = r.get('enjoyed')
                if not isinstance(enjoyed, list):
                    enjoyed = enjoyed_hh
                pid = r.get('pid') or code2pid.get(r.get('code'))
                det = _detail_by_policy(data, pid, r.get('detail_id')) if pid else None
                policy = policies_by_pid.get(pid) if pid else None
                m_no = re.match(r'成员(\d+)', str(r.get('member') or ''))
                member_no = m_no.group(1) if m_no else None
                if policy is None or member_no is None:
                    _annotate(r, hit, {'verdict': '待核实', 'note': '候选结构不完整（不跨政策补齐）',
                                       'checks': {}, 'miss': ['候选结构问题'], 'mutex': None, 'evidence_text': '',
                                       'reason_codes': ['ambiguous_policy_source']})
                    pending.append({'policy_code': r.get('code'), 'member_no': member_no, 'detail_id': r.get('detail_id'),
                                    'verdict': '待核实', 'note': '候选结构不完整'})
                else:
                    if det is None:
                        # 政策在用但无明细分支：用政策级字段构造核验单元，
                        # 不因"无明细"直接放弃核验（条件在 policy 层，仍可判定或待核实）。
                        det = {'detail_id': r.get('detail_id'), 'pid': policy.get('pid'),
                               'region': policy.get('regions'), 'conditions': policy.get('conditions'),
                               'target': policy.get('target'), 'age': policy.get('age'),
                               'lab': policy.get('lab'), 'health': policy.get('health'),
                               'pov': policy.get('pov'),
                               'benefit': policy.get('benefit'), 'process': policy.get('process')}
                    ev = verify_member_detail(policy, det, member_no, members, hh, enjoyed)
                    _annotate(r, hit, ev)
                    if ev['verdict'] == '不适用':
                        excluded.append({'policy_code': r.get('code'), 'policy_name': policy.get('name'),
                                         'member_no': member_no, 'detail_id': r.get('detail_id'),
                                         'verdict': '不适用', 'note': ev['note'], 'evidence': ev['evidence_text']})
                        continue
                    elif ev['verdict'] == '待核实':
                        pending.append({'policy_code': r.get('code'), 'policy_name': policy.get('name'),
                                        'member_no': member_no, 'detail_id': r.get('detail_id'),
                                        'verdict': '待核实', 'note': ev['note'], 'miss': ev['miss']})
                out.append(r)
        except Exception as e:
            # F4b：异常候选本身可能非 dict（如 None），except 块内读取统一走 (r or {})
            _r = r if isinstance(r, dict) else {}
            errors.append({
                'policy_code': _r.get('code') or _r.get('policy_code'),
                'member_no': _r.get('member_no'),
                'detail_id': _r.get('detail_id'),
                'error': repr(e),
                'traceback': traceback.format_exc(),
            })
            # F4：异常候选补 evidence 占位，保证下游 kg_batch5_merge 读
            #      row['forbid_v2']['evidence']['verdict'] 不会 KeyError
            r_failed = dict(_r)
            r_failed['forbid_v2'] = {
                'mode': 'error',
                'hit': [],
                'reason': '核验过程异常，标记待核实',
                'error': repr(e),
                'evidence': {'verdict': '待核实', 'note': f'核验异常：{type(e).__name__}',
                             'checks': {}, 'miss': ['核验过程异常，已标记待核实'], 'mutex': None,
                             'evidence_text': ''},
            }
            pending.append({
                'policy_code': _r.get('code') or _r.get('policy_code'),
                'member_no': _r.get('member_no'),
                'detail_id': _r.get('detail_id'),
                'verdict': '待核实',
                'note': f'核验异常：{type(e).__name__}',
            })
            out.append(r_failed)
    return out, excluded, pending, errors
