"""Member-facing labels and age-gating for clarification questions.

Two user-visible problems drive this module:

1. A member question was rendered as "成员1：" even when the household text had
   named the person, so the user could not tell who to answer for. When a name
   is discoverable we show it: "成员1（钱七）".
2. Education-era questions (in-school status, study stage, student recognition)
   were asked of members whose age makes it implausible, most visibly an
   elderly member being asked about 在读. The engine already returns unknown for
   those probes, but a household-level sample question still dropped in.

Both switches default to the safer behavior and can turn it back off:
  MEMBER_LABEL_NAME  default on  show a discovered name in labels
  AGE_GATED_QUESTION default on  do not ask education-era questions when no
                               member is in a plausible student-age range

This module is pure and dependency-free; callers pass the state they have.
"""
import os
import re

MEMBER_LABEL_NAME = "MEMBER_LABEL_NAME"
AGE_GATED_QUESTION = "AGE_GATED_QUESTION"

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# Words that a member record often starts with after the number; none is a
# person's name. Used to avoid treating the next standard fact as a person.
_NAME_STOPWORDS = (
    "年龄", "岁", "姓名", "性别", "职业", "文化", "健康", "残疾", "患病", "大病",
    "长期", "慢病", "就业", "务工", "劳动", "收入", "医保", "参保", "住房", "饮水",
    "学生", "在读", "在校", "上学", "学段", "幼儿园", "小学", "初中", "高中",
    "大学", "本科", "大专", "申请", "享受", "领取", "参加", "目前", "现在",
    "已经", "未", "没", "无", "有", "是", "为",
)


def _flag(name, default="1"):
    raw = os.environ.get(name, default)
    return str(raw).strip().lower() in ("1", "on", "true", "yes", "y", "open")


def member_label_name_enabled():
    """Show a discovered peer/member name in the question wording."""
    return _flag(MEMBER_LABEL_NAME, "1")


def age_gated_questions_enabled():
    """Drop schooling questions when no member is a plausible student."""
    return _flag(AGE_GATED_QUESTION, "1")


def cn2int(value):
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    return CN_NUM.get(value)


def _clean_name(text):
    if not isinstance(text, str):
        return None
    text = re.sub(r"[\s（）()、,，。;；:：]", "", text)
    if not text:
        return None
    if re.match(r"^[\u4e00-\u9fff]+$", text) and 2 <= len(text) <= 8:
        return text
    return None


def _is_stopword(name):
    return any(name == word or word in name for word in _NAME_STOPWORDS)


# 自然称谓。用户极少说“成员2”，说的是“我母亲”“张兰是我母亲”，
# 现有 _MEMBER_HEAD 只认字面量“成员N”，所以姓名发现拿不到名字，
# member_display() 的“成员2（张兰）”能力一直空转（真链路复现的缺陷 B）。
KINSHIP = ("母亲", "妈妈", "父亲", "爸爸", "配偶", "妻子", "丈夫",
           "儿子", "女儿", "孙子", "孙女", "本人", "爱人", "老婆", "老公")
_KINSHIP_SORTED = tuple(sorted(KINSHIP, key=len, reverse=True))
# 称谓前后常粘着的虚词，剥掉后才能剩下姓名。
_EDGE_PARTICLES = "是我的叫名字为跟和与，,。 .、：:;；"


def extract_kinship_roles(text):
    """Return {kinship: name_or_empty} for natural role references in user text.

    Handles "我母亲张兰58岁" (name follows) and "张兰是我母亲，60岁"
    (name precedes). A role with no discoverable name maps to "" so callers
    can still tell the role was mentioned. Pure function, no side effects.
    """
    if not isinstance(text, str) or not text:
        return {}
    body = re.sub(r"\s+", " ", text)
    found = {}
    for kin in _KINSHIP_SORTED:
        if kin in found:
            continue
        hit = None
        for m in re.finditer(re.escape(kin), body):
            tail = body[m.end():m.end() + 10]
            run = re.match(r"[\u4e00-\u9fff]{2,4}", tail)
            cand = _clean_name(run.group()) if run else None
            if cand is None or _is_stopword(cand):
                head = body[max(0, m.start() - 10):m.start()]
                head = re.split(r"[，,。;；：:！？!?\s]", head)[-1]
                head = head.rstrip(_EDGE_PARTICLES)
                run = re.search(r"[\u4e00-\u9fff]{2,4}$", head)
                cand = _clean_name(run.group()) if run else None
            if cand is not None and not _is_stopword(cand):
                if hit is None:
                    hit = cand
            elif hit is None and kin not in found:
                found[kin] = ""
        if hit:
            found[kin] = hit
    return found


# A member head is "(家庭)成员 1" where 1 is a digit or Chinese numeral, not
# immediately followed by a digit/unit that would make it a count.
_MEMBER_HEAD = re.compile(r"(?:家庭)?成员\s*([1-9]\d?|[一二三四五六七八九十])")


def extract_member_names(text, known_members=None):
    """Return {member_no: name} for explicit peer names in user text.

    Handles "成员1（张三）", "成员1:张三 年龄45岁", "成员1 张三 60岁",
    "家庭成员1 王五 热心" and "成员1（李四）都在上学". The token following
    the number is a name only when it is pure Chinese, bounded by a delimiter,
    and not one of the standard field words.
    """
    if not isinstance(text, str) or not text:
        return {}
    body = re.sub(r"\s+", " ", text)
    found = {}
    for m in _MEMBER_HEAD.finditer(body):
        no = cn2int(m.group(1))
        if no is None:
            continue
        tail = body[m.end():m.end() + 18]
        name = None
        pm = re.match(r"^\s*[（(](\S{2,8})[）)]", tail)
        if pm:
            name = _clean_name(pm.group(1))
        if name is None or _is_stopword(name):
            cm = re.match(r"^\s*[:：]\s*(\S{2,8})(?=\s|[，,。;；）)])", tail)
            if cm:
                name = _clean_name(cm.group(1))
        if name is None or _is_stopword(name):
            bm = re.match(r"^\s+(\S{2,8})(?=\s|[，,。;；）)])", tail)
            if bm:
                name = _clean_name(bm.group(1))
        if name is not None and not _is_stopword(name) and no not in found:
            found[no] = name
    # 自然称谓（"我母亲张兰"）只有在成员编号集合已知时才敢做同序映射；
    # known_members 为 None 时保持旧行为，绝不猜测。
    # 只有在"仅剩一个没编号的成员"时才绑，绝不做顺序猜测：
    # 两名成员都没编号时无法判断谁是母亲，猜错比不猜更坏。
    if known_members:
        taken = {str(k) for k in found}
        spare = [str(no) for no in known_members if str(no) not in taken]
        named = [n for n in extract_kinship_roles(text).values() if n]
        if len(spare) == 1 and len(named) == 1:
            found[spare[0]] = named[0]
    return found


def member_display(member_no, name):
    """Render a member reference for the user; name is optional."""
    key = str(member_no).strip()
    if member_label_name_enabled() and isinstance(name, str):
        name = re.sub(r"\s+", "", name)
        if 2 <= len(name) <= 12 and re.match(r"^[\u4e00-\u9fff]+$", name):
            return "成员" + key + "（" + name + "）"
    return "成员" + key


def member_subject(entity, st=None, hh=None):
    """User-facing subject before the question: 家庭, or 成员N（姓名）."""
    if entity == "household":
        return "家庭"
    key = str(entity).strip()
    no = cn2int(key)
    name = None
    if isinstance(no, int):
        names = (st or {}).get("member_names") or {}
        if isinstance(names, dict):
            for lookup in (no, key, str(no)):
                candidate = names.get(lookup)
                if isinstance(candidate, str) and candidate:
                    name = candidate
                    break
        if name is None and hh:
            by_no = {m.get("member_no"): m for m in (hh.get("members") or []) if isinstance(m, dict)}
            for lookup in (no, key, str(no)):
                member = by_no.get(lookup)
                if member and isinstance(member.get("name"), str) and member.get("name"):
                    name = member["name"]
                    break
    return member_display(no if no is not None else key, name)


# A member is a plausible student when their known age falls in the range that
# includes pre-school, compulsory, high school and degree study. Members with
# an unknown age are also excluded: the age question is what must be answered,
# not a schooling question the family already cannot answer.
# Field names that concern in-school/study status. Member questions for these
# are the ones an elderly or pre-school member should not be asked about
# unless their age makes study plausible.
EDUCATION_FIELD_TERMS = ("enrolled", "study_stage", "student_difficulty_recognized",
                         "full_time_study", "admitted_this_year")


MIN_STUDENT_AGE = 3
MAX_STUDENT_AGE = 55


def _known_age(st, entity):
    fact = (st.get("facts") or {}).get(entity + ":age")
    if not isinstance(fact, dict) or fact.get("state") != "known":
        return None
    value = fact.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def education_question_allowed(st, member_no):
    """Whether an education-era question for member_no should be asked.

    Without the age gate the caller's existing candidate selection alone
    decides; clearing the switch restores it. With the gate on, a member whose
    age is known and outside the plausible study range is excluded, and a
    member whose age is unknown is excluded because the age is the missing
    fact that should be asked first.
    """
    if not age_gated_questions_enabled():
        return True
    age = _known_age(st, str(member_no))
    return age is not None and MIN_STUDENT_AGE <= age <= MAX_STUDENT_AGE
