"""Shared risk aliases. Identity aliases only; no new eligibility rules."""
RISK_ALIASES = {
    '住房出现安全问题': '因安全住房', '住房安全问题': '因安全住房',
    '饮水出现安全问题': '因安全饮水', '饮水安全问题': '因安全饮水',
    '家庭成员中有义务教育阶段适龄儿童少年失学辍学': '失学辍学',
    '家庭成员中有未参加城乡居民（职工）基本医疗保险': '未参保',
}


def canonical_risk(value):
    value = str(value or '').strip()
    return RISK_ALIASES.get(value, value)


def risk_labels(value):
    canonical = canonical_risk(value)
    return {canonical} | {alias for alias, name in RISK_ALIASES.items() if name == canonical}
