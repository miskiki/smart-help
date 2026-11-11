"""口语化输入规范化（前置层）。

背景（实测）：事实抽取的校验要求 quote 是原文逐字子串、成员必须写成"成员N"。
真实用户说"我家在莲花县，两口人，我62岁肢体残疾二级"时——
  * 模型的引文天然是转述 → 全部 unbound_fact → "所有提取项均未通过事实证据校验"
  * "我/老伴" 无法映射成员编号 → fact_scope / ambiguous_member_quote
结果：7.8 秒返回 0 条事实、0 张卡。

这里的做法不是放宽校验，而是**先把口语重排成管线本来就吃得下的结构化文本**，
再走原有的严格抽取。规范化的硬约束：
  * 只重排，不新增：用户没说的项一律不写
  * 说话人记为成员1，其配偶（老伴/爱人/配偶/丈夫/妻子）记为成员2
  * 保留用户原话里的数值与限定词（年龄、等级、人数）
审计阶段仍拿**用户原话**做证据源，用来兜住规范化可能引入的偏差。
"""
import re

NORMALIZE_SYSTEM = '''你是家庭情况口语的"结构化重排器"。把用户的原话重排成固定格式，供下游严格抽取使用。

硬性规则：
1. 只重排，绝不新增、推断、补全。用户没说的项，一个都不要写。
2. 说话人（我/本人/我自己）记为成员1；其配偶（老伴/爱人/配偶/丈夫/妻子）记为成员2。其他亲属按出现顺序记为成员3、成员4……
3. 数值、等级、人数、单位必须原样保留（"六十二"要写成"62岁"之外的信息不要臆造）。
4. 地区写到用户说的层级为止，不要补省市县。
5. 格式：
   家庭所在地X。户内家庭成员N人；成员1，A岁，B，C；成员2，D岁，E，F。
   其中 A/D 为年龄，"B/E" 用原话描述（如"肢体残疾二级"转成"二级残疾，肢体残疾"），"C/F" 写劳动能力、健康状况、参保情况等原话信息。
8. 保留用户原话用词，不要替换成业务术语："没买医保"就写"没买医保"（不要写"未参保"），"腿脚不方便"保留原话，可在后面用逗号补充用户明说的等级（如"二级残疾"）。
6. 用户没说的项直接省略（例如没说医保，就不要写"全家医保参保0人"；没说健康状况，就不要写"健康"或"非健康"）。
7. 只输出紧凑 JSON：{"structured":"重排后的那一两句话"}，不要解释。'''

NORMALIZE_PROMPT = '''重排下面的原话。输出 {"structured":"..."}。示例：
原话："我家在莲花县，两口人，我62岁肢体残疾二级，老伴60岁"
输出："家庭所在地莲花县。户内家庭成员2人；成员1，62岁，非健康，二级残疾，肢体残疾；成员2，60岁，健康。"

注意：示例里的"非健康/健康"只有在原话明确说了健康状况时才能写，否则省略。'''


def needs_normalize(text):
    """判断是否值得先规范化：口语化、没有管线要求的结构化标记。"""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if len(text) < 6:
        return False
    has_member = bool(re.search(r'(?:家庭成员|成员)\s*[1-9]', text))
    has_struct = bool(re.search(r'家庭所在地|所在地区|户内家庭成员', text))
    colloquial = bool(re.search(r'我|咱|俺|老伴|爱人|媳妇|老公|老婆|儿子|女儿|大哥|大姐', text))
    # 情况清单式录入（"年龄：62；残疾等级：二级"）不需要重排
    list_like = len(re.findall(r'[：:]', text)) >= 2 and not colloquial
    return colloquial and not (has_member and has_struct) and not list_like


def to_structured(text, model):
    """调用模型做重排。失败返回 None（调用方保持原路径）。"""
    from .full_runtime import ask
    try:
        raw = ask(model, 'core_full_text_normalize', '把口语家庭描述重排为结构化文本',
                  dict(original=text), NORMALIZE_SYSTEM + '\n' + NORMALIZE_PROMPT)
    except Exception:
        return None
    out = raw.get('structured') if isinstance(raw, dict) else raw
    if not isinstance(out, str):
        return None
    out = out.strip().strip('"“”')
    out = out.splitlines()[0].strip() if out else ''
    if len(out) < 6 or len(out) > 1200:
        return None
    # 必须真的变结构化，且不能凭空多出"整个家庭"级别的断言
    if not re.search(r'家庭所在地|所在地区', out):
        return None
    return out


# 断言类词：重排文本出现这些词时，原话必须有对应依据。
# 用别名组而不是单字匹配，避免"没买医保"→"未参保"这类合法同义被误判。
_CLAIM_ALIASES = {
    '参保': ('医保', '参保', '新农合', '合作医疗', '买保险', '交医保'),
    '医保': ('医保', '参保', '新农合', '合作医疗', '买保险', '交医保'),
    '残疾': ('残疾', '肢残', '腿脚', '行动不便', '聋', '哑', '盲', '瘫', '智障', '精神'),
    '低保': ('低保', '最低生活保障'),
    '特困': ('特困', '五保', '供养'),
    '脱贫': ('脱贫', '建档立卡'),
    '监测': ('监测', '返贫'),
    '在读': ('在读', '上学', '读书', '读书的', '学校', '读书'),
    '在校': ('在校', '上学', '读书', '学校'),
    '学前': ('学前', '幼儿园'),
    '本科': ('本科', '大学'),
    '专科': ('专科', '大专', '高职'),
    '高中': ('高中',),
    '中职': ('中职', '中专', '技校', '职高'),
    '健康': ('健康', '身体好', '没病', '正常'),
    '劳动力': ('劳动力', '能干活', '干不了活', '没劳力', '劳动能力'),
    '大病': ('大病', '重病', '癌症', '尿毒症', '白血病'),
    '慢病': ('慢病', '慢性病', '高血压', '糖尿病'),
}


def _numbers(text):
    """取数字，但剔除成员编号与人数这类结构数字（"成员1""2人"不是断言）。"""
    cleaned = re.sub(r'成员\s*\d+', '', str(text))
    cleaned = re.sub(r'家庭(?:成员)?\s*\d+\s*人', '', cleaned)
    cleaned = re.sub(r'\d+\s*人', '', cleaned)
    return set(re.findall(r'\d+(?:\.\d+)?', cleaned))


def rapid_check(structured, original):
    """廉价校验：重排文本不能出现原话没有的数字，也不能凭空多出身份类断言。"""
    added_numbers = _numbers(structured) - _numbers(original)
    if added_numbers:
        return False, 'numbers_not_in_original:' + ','.join(sorted(added_numbers)[:4])
    for term, aliases in _CLAIM_ALIASES.items():
        if term in structured and not any(a in original for a in aliases):
            return False, 'term_not_in_original:' + term
    return True, ''




def sanitize(structured, original):
    """把重排文本里"原话没有依据"的片段剔掉，保留合法部分。

    为什么不是整体拒绝：模型常把"没说健康状况"补成"健康"，若因此丢弃整次重排，
    用户就又回到 0 条事实。剔除越界片段既保住召回，也不引入幻觉。
    """
    dropped = []

    def seg_ok(seg):
        if _numbers(seg) - _numbers(original):
            return False, 'numbers'
        for term, aliases in _CLAIM_ALIASES.items():
            if term in seg and not any(a in original for a in aliases):
                return False, term
        return True, ''

    # 先按成员/分句切开，逐段剔除；地区与人数句尽量保留
    parts = re.split(r'(?<=[；;。])', structured)
    kept = []
    for part in parts:
        ok, why = seg_ok(part)
        if ok:
            kept.append(part)
            continue
        dropped.append(why + ':' + part.strip()[:24])
        # 段内再按逗号切，保留仍然合法的子句
        sub = [x for x in re.split(r'(?<=[，,])', part) if x.strip()]
        keep_sub = []
        for piece in sub:
            ok2, _ = seg_ok(piece)
            if ok2:
                keep_sub.append(piece)
        if keep_sub:
            tail = '' if part.rstrip().endswith(('；', ';', '。')) else '；'
            kept.append(''.join(keep_sub).rstrip('，,') + '；')
    out = ''.join(kept).strip()
    if not re.search(r'家庭所在地|所在地区', out):
        head = re.search(r'(家庭所在地|所在地区)[^；;。]*[；;。]?', structured)
        if head:
            out = head.group() + out
    return out, dropped

def verify_support(structured, original, model):
    """校对重排是否越界：把重排出的每条信息对照原话，找出原话没有的内容。

    返回 (ok, 越界说明)。这一步是防幻觉闸门——规范化一旦编造事实，
    下游零误放行的根就不存在了，所以宁可拒绝规范化。
    """
    ok, _ = rapid_check(structured, original)
    if not ok:
        return ok, _
    from .full_runtime import ask
    try:
        raw = ask(model, 'core_full_text_verify', '核对重排文本是否只包含原话信息',
                  dict(original=original, structured=structured),
                  '判断结构化文本里的每一条具体信息（地区、人数、年龄、残疾等级/类型、劳动能力、健康、参保、在读等）'
                  '是否都能在原话中找到依据。输出紧凑JSON {"supported":true/false,"unsupported":["原话没有的信息，最多3条"]}。'
                  '允许同义归一（如"两口人"→"2人"、"六十二"→"62岁"、"腿脚不便"→"肢体残疾"仅在原话明确时）。'
                  '只输出JSON。')
    except Exception:
        return False, 'verify_failed'
    if not isinstance(raw, dict):
        return False, 'verify_invalid'
    if raw.get('supported') is not True:
        return False, '; '.join(str(x)[:40] for x in (raw.get('unsupported') or [])[:3]) or 'unsupported'
    return True, ''
