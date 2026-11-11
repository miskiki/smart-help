"""政策条款离线编译器（C1）。

把一条政策明细条款编译成 engine.py 可直接求值的三值规则：
  {association, eligibility, eligibility_complete, gaps, unresolved, benefits}

设计约束（来自体检报告的实测结论）：
  * 编译发生在离线，不在查询路径上；因此可以重试、可以慢、可以用更严的提示词。
  * 每个原子必须绑定原文引用（source/segment_id/quote），复用 reasoning._bind 校验。
  * 不确定一律写 unresolved，绝不硬编阈值——三值求值会把未知变成追问而不是错误结论。
  * 规则存 SQLite（原型阶段），按 (code, detail_id, version, text_hash) 去重。
"""
import json, hashlib, os, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .fields import dictionary, model_fields, valid_value
from .engine import canonical, validate, atoms as engine_atoms, evaluate
from .publishing import parse_json

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'eval', 'compiled_rules.db')

OPS_DOC = '可用比较符：eq(等于) ne(不等于) in(属于列表) gte gt lte lt(数值比较) between(区间[lo,hi]) exists(有该项事实且非空)'

SYSTEM = '''你是政策条款编译器：把一条政策明细条款翻译成机器可求值的资格规则。你不判断任何家庭，只做"原文 → 逻辑"的忠实翻译。

输出必须是单个 JSON 对象，字段如下：
{
 "subject": "household" 或 "member",
 "association": 表达式,          // 政策"对象/方向"条件：哪些人群或事件属于本政策范围
 "eligibility": 表达式,          // 全部资格条件（字面要求）
 "eligibility_complete": true/false,  // 原文是否把资格条件写全（含糊只是未定义，不是不全）
 "gaps": ["原文缺失或前后矛盾之处，最多2条"],
 "unresolved": ["无法用给定字段表达的要求，最多3条"],
 "benefits": "待遇/标准原文摘录，最多80字",
 "notes": "一句话说明本规则的核心判断路径"
}

表达式语法（严格）：
 原子：{"field":"字段名","op":"比较符","value":值,"evidence":["E编号"]}
 与：{"all":[子表达式,...]}    或：{"any":[子表达式,...]}    无法表达：{"unresolved":"说明"}

硬性规则：
1. field 只能取给定 fields 字典里的键，不得发明字段。字典缺的写进 unresolved，不要硬塞近似字段。
2. 每个原子必须带 evidence，值来自给定 evidence 的 E 编号，且该 E 的原文必须真的支持这个比较（不得用标题、政策名、上级文件代替资格条款）。
3. 金额、年龄、等级、年限等阈值必须逐字出现在原文；原文没写就不写这个原子。
4. 多个并列必要条件用 all；并列待遇档次/人群档位/可选路线用 any。不要把 any 写成 all。
5. 范围类表述（如"义务教育阶段"）用 in 列表表达。
6. 布尔字段用 eq true/false；数值字段用 gte/lte/between。
7. 原文出现"困难""生活困难""无生活来源"等未定义标准时，把该条件放进 unresolved，不要自己编标准。
8. 数值区间必须给全双边界：原文写"3至6岁""3-6周岁""16-60周岁"，必须编成两个原子（gte 下界 + lte 上界）或用 between，不得只编下界。只有原文确实无上界（如"60周岁以上"）时才允许单独 gte。
9. 教育类政策（学杂费/助学金/免学费/学前教育/助学/教科书/寄宿/就读）：若原文限定在校学生或某学段，必须编入 enrolled 或 study_stage；只在对象上写"残疾人/脱贫户"而漏掉在学要求是错误编译。
10. 岗位/就业类政策（公益性岗位/就业帮扶/技能培训/务工）：必须编入年龄上下界与对象身份（脱贫人口/监测对象/就业困难人员/弱劳动力等）；只写 labor 而不写年龄区间是错误编译。
8. 只输出 JSON，不要输出思考过程、不要解释。
'''

PROMPT = '''编译这条政策条款。要求：
- subject：逐成员判定用 member；对户判定用 household。
- association：只表达"哪些人/户属于本政策对象"的限定。若原文没有明确对象限定（如"所有农户""全体居民"），写 {"unresolved":"原文无对象限定"}，不要硬凑原子。
- eligibility：表达原文每一个必要条件。并列必要条件用 all；待遇档次/人群档位/可选路线用 any。
- 每个原子必须带 evidence（E 编号数组），且该 E 的原文必须真的支持这个比较，不能用政策名或上级文件标题冒充资格条款。
- 金额/年龄/等级/年限阈值必须逐字出现在原文；原文没写就不写这个原子。
- 原文未定义的标准（如"生活困难""无生活来源"）写进 unresolved，不要自己编阈值。
- **不要为地区、行政层级、户籍所在地等地理条件建原子**：系统已在装载阶段按县过滤地区。此类条件写进 notes，不要放进 association 或 eligibility。
- 只输出 JSON。'''


def _compact_fields(fields):
    """字段字典压缩成 [scope, type, label] 三元组，减少编译 prompt 体积。"""
    out = {}
    for name, d in fields.items():
        item = [d.get('scope'), d.get('type'), d.get('label') or name]
        if d.get('values'):
            item.append(list(d['values'])[:12])
        out[name] = item
    return out


def build_payload(pack, fields):
    """从 pack 组装编译输入：政策文本 + 证据编号表 + 字段字典。"""
    from .reasoning import _evidence_map, current_source_view
    source = current_source_view(pack['source'])
    evidence = _evidence_map(source)
    policy = pack['policy']
    detail = pack['detail']
    return dict(
        policy=dict(code=policy.get('code'), name=policy.get('name'), level=policy.get('level'),
                    region=(detail.get('region') or policy.get('region') or ''),
                    category=' '.join(str(x) for x in (policy.get('cat2'), policy.get('cat3')) if x)),
        target=str(detail.get('target') or policy.get('target') or ''),
        conditions=str(detail.get('conditions') or ''),
        benefit=str(detail.get('benefit') or '')[:300],
        evidence=evidence,
        fields=_compact_fields(fields),
    ), evidence


def _geo_string_fields(fields):
    """地理类字符串字段：地区/所在地。它们由装载阶段预先过滤，不得编译成原子。"""
    out = set()
    for name, d in fields.items():
        if d.get('type') != 'string':
            continue
        label = str(d.get('label') or '')
        if any(w in label for w in ('地区', '所在地', '户籍地')):
            out.add(name)
    return out


def validate_rule(rule, pack, evidence, fields):
    """结构校验 + 原文引用校验。返回 (cleaned_rule, errors)。"""
    errors = []
    geo = _geo_string_fields(fields)
    if not isinstance(rule, dict):
        return None, ['not_object']
    subject = rule.get('subject')
    if subject not in ('household', 'member'):
        subject = 'member'
        errors.append('subject_defaulted')
    cleaned = dict(subject=subject, benefits=str(rule.get('benefits') or '')[:200],
                   gaps=[str(g)[:120] for g in (rule.get('gaps') or []) if str(g).strip()][:2],
                   unresolved=[str(u)[:120] for u in (rule.get('unresolved') or []) if str(u).strip()][:3],
                   notes=str(rule.get('notes') or '')[:120])

    def clean_expr(expr, path):
        if not isinstance(expr, dict):
            errors.append('expr_not_object:' + path); return None
        if 'unresolved' in expr:
            text = str(expr['unresolved'])[:120]
            if not text.strip():
                errors.append('empty_unresolved:' + path); return None
            return {'unresolved': text}
        if 'all' in expr or 'any' in expr:
            key = 'all' if 'all' in expr else 'any'
            children = expr.get(key)
            if not isinstance(children, list) or not children:
                errors.append('empty_logic:' + path); return None
            kept = [c for c in (clean_expr(x, path + '/' + key) for x in children[:20]) if c]
            if not kept:
                errors.append('logic_all_dropped:' + path); return None
            return {key: kept}
        if 'not' in expr:
            child = clean_expr(expr['not'], path + '/not')
            return {'not': child} if child else None
        field = expr.get('field'); op = expr.get('op'); value = expr.get('value')
        if field not in fields:
            errors.append('unknown_field:%s' % field); return None
        if field in geo:
            # 地区已由 evidence_loader 按县过滤；把它编成字符串等值原子必然求值失败。
            errors.append('geo_atom_dropped:%s' % field); return None
        definition = fields[field]
        if op not in ('eq', 'ne', 'in', 'gte', 'gt', 'lte', 'lt', 'between', 'exists'):
            errors.append('bad_op:%s' % op); return None
        if op in ('gte', 'gt', 'lte', 'lt', 'between') and definition['type'] != 'number':
            errors.append('comparison_on_nonnumeric:%s' % field); return None
        if op in ('in', 'between'):
            if not isinstance(value, list) or not value:
                errors.append('bad_range:%s' % field); return None
            if op == 'between' and (len(value) != 2 or value[0] > value[1]):
                errors.append('bad_between:%s' % field); return None
            if not all(valid_value(v, definition) for v in value):
                errors.append('bad_value:%s' % field); return None
        elif op != 'exists':
            if value is None or not valid_value(value, definition):
                errors.append('bad_value:%s' % field); return None
        ev_ids = expr.get('evidence')
        ev_ids = [e for e in ev_ids if isinstance(e, str)] if isinstance(ev_ids, list) else []
        ev_ids = [e for e in ev_ids if e in evidence]
        if not ev_ids:
            # 无原文引用的原子不可信：直接丢弃，不让它参与求值。
            errors.append('atom_without_evidence_dropped:%s' % field); return None
        atom = {'field': field, 'op': op}
        if op != 'exists':
            atom['value'] = value
        atom['evidence'] = ev_ids[:2]
        return atom

    association = clean_expr(rule.get('association'), 'association')
    eligibility = clean_expr(rule.get('eligibility'), 'eligibility')
    if association is None:
        errors.append('association_missing')
    if eligibility is None:
        errors.append('eligibility_missing')
    if association is None or eligibility is None:
        return None, errors
    # engine 原生校验（字段/比较符/取值类型）
    try:
        canonical(association, fields); validate(association, fields)
        canonical(eligibility, fields); validate(eligibility, fields)
    except ValueError as exc:
        errors.append('engine_invalid:%s' % exc)
        return None, errors
    cleaned['association'] = canonical(association, fields)
    cleaned['eligibility'] = canonical(eligibility, fields)
    complete = rule.get('eligibility_complete')
    cleaned['eligibility_complete'] = bool(complete) and not cleaned['unresolved']
    cleaned['atom_count'] = len(engine_atoms(cleaned['association'])) + len(engine_atoms(cleaned['eligibility']))
    # 弱规则：资格侧全部是 exists（"有该事实"），没有真实阈值/取值比较——信息量低，A/B 时单独标注。
    elig_atoms = engine_atoms(cleaned['eligibility'])
    cleaned['weak'] = bool(elig_atoms) and all(a.get('op') == 'exists' for a in elig_atoms)
    cleaned['cited'] = sorted({e for a in engine_atoms(cleaned['eligibility']) + engine_atoms(cleaned['association'])
                               for e in (a.get('evidence') or [])})
    status = 'complete' if cleaned['eligibility_complete'] and not errors else 'partial'
    if not cleaned['atom_count']:
        status = 'failed'
        errors.append('no_atoms')
    return dict(rule=cleaned, status=status, errors=errors), errors


def compile_pack(pack, fields, model, retries=2):
    """编译一条明细；失败重试 retries 次。返回记录 dict。"""
    from .full_runtime import ask
    payload, evidence = build_payload(pack, fields)
    last = dict(status='failed', errors=['not_attempted'], rule=None)
    for attempt in range(retries + 1):
        try:
            raw = ask(model, 'core_full_policy_compile', '把条款编译成可求值规则', payload,
                      SYSTEM + '\n' + PROMPT + '\n' + OPS_DOC)
            checked, errors = validate_rule(raw, pack, evidence, fields)
            if checked and checked['status'] != 'failed':
                return dict(ok=True, status=checked['status'], rule=checked['rule'], errors=errors,
                            attempts=attempt + 1)
            last = dict(status='failed', errors=errors or ['validate_failed'], rule=None)
        except Exception as exc:
            last = dict(status='failed', errors=[type(exc).__name__ + ':' + str(exc)[:120]], rule=None)
    return dict(ok=False, attempts=retries + 1, **last)


# ---------------------------------------------------------------- 存储

def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('''CREATE TABLE IF NOT EXISTS policy_rule(
        key TEXT PRIMARY KEY, policy_code TEXT, detail_id TEXT, version TEXT,
        text_hash TEXT, county TEXT, status TEXT, rule_json TEXT, errors TEXT,
        compiler_model TEXT, compiled_at TEXT, attempts INTEGER)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_code ON policy_rule(policy_code)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_hash ON policy_rule(text_hash)')
    conn.commit()
    return conn


def text_hash_of(pack):
    detail = pack['detail']
    raw = ' '.join(str(x or '') for x in (pack['policy'].get('name'), detail.get('target'),
                                          detail.get('conditions'), detail.get('benefit')))
    return hashlib.sha256(raw.encode()).hexdigest()


def save(record):
    conn = _conn()
    try:
        conn.execute('INSERT OR REPLACE INTO policy_rule VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            record['key'], record['policy_code'], str(record['detail_id']), record['version'],
            record['text_hash'], record['county'], record['status'],
            json.dumps(record['rule'], ensure_ascii=False) if record['rule'] else '',
            json.dumps(record['errors'], ensure_ascii=False), record['compiler_model'],
            record['compiled_at'], record['attempts']))
        conn.commit()
    finally:
        conn.close()


def load_rules(county=None):
    conn = _conn()
    try:
        sql = 'SELECT * FROM policy_rule WHERE status!="failed"'
        args = ()
        if county:
            sql += ' AND (county=? OR county="*")'
            args = (county,)
        cur = conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for row in rows:
        row['rule'] = json.loads(row['rule_json']) if row['rule_json'] else None
    return rows


def stats(county=None):
    conn = _conn()
    try:
        sql = 'SELECT status, COUNT(*) FROM policy_rule'
        args = ()
        if county:
            sql += ' WHERE county=?'
            args = (county,)
        sql += ' GROUP BY status'
        return dict(conn.execute(sql, args).fetchall())
    finally:
        conn.close()
