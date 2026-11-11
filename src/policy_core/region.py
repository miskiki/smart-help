# -*- coding: utf-8 -*-
"""地区语义层（M0 T4）：确定性行政区划归一 + 家庭/政策地区关系判定。

定位：把三套各自为政的地区判断（图谱节点/子串、SQL LIKE、facts 字符串等价）收敛到一个
纯函数模块，供 catalog/evidence_loader/retrieval/facts 复用。全部无数据库/网络 IO，
同一输入同一输出，便于单测与回放。

判定口径（FP=0 红线）：
- 只有 same / ancestor（政策地区覆盖或等于家庭地区）才给出"适用"；
- 只有双方地区都能确定性解析且互不包含（disjoint）才给出"不适用"；
- 政策地区比家庭地区更具体（descendant）、写法无法解析、区县归属有歧义一律 unknown，
  保留候选并标注，绝不因为地区写法差异把候选误放行或误丢弃。
"""
import os
import re

from . import geo

JX_PROVINCE = '江西省'
# "赣"是江西简称，但"赣州/赣县"是地名，只有独立成词时才算省份（见 _jx_marker）。
_JX_PROVINCE_ALIASES = ('江西', '江西省', '赣', '赣省')

# 全国/国家级不限地区（按覆盖任意家庭地区处理）。
_NATION_WORDS = ('全国', '国家', '全国范围', '全国通用')

# 其他省级行政区全称（全称出现即视为明确外省）。
_OTHER_PROVINCE_FULL = (
    '河北省', '山西省', '辽宁省', '吉林省', '黑龙江省', '江苏省', '浙江省', '安徽省',
    '福建省', '山东省', '河南省', '湖北省', '湖南省', '广东省', '海南省', '四川省',
    '贵州省', '云南省', '陕西省', '甘肃省', '青海省', '台湾省',
    '内蒙古自治区', '广西壮族自治区', '西藏自治区', '宁夏回族自治区', '新疆维吾尔自治区',
    '北京市', '上海市', '天津市', '重庆市', '香港特别行政区', '澳门特别行政区',
)
# 简称只在"整词/带行政后缀/带户籍语境"时判定，避免"南昌市上海路"这类街道名误判外省。
_OTHER_PROVINCE_SHORT = {
    '河北': '河北省', '山西': '山西省', '辽宁': '辽宁省', '吉林': '吉林省', '黑龙江': '黑龙江省',
    '江苏': '江苏省', '浙江': '浙江省', '安徽': '安徽省', '福建': '福建省', '山东': '山东省',
    '河南': '河南省', '湖北': '湖北省', '湖南': '湖南省', '广东': '广东省', '海南': '海南省',
    '四川': '四川省', '贵州': '贵州省', '云南': '云南省', '陕西': '陕西省', '甘肃': '甘肃省',
    '青海': '青海省', '台湾': '台湾省', '内蒙古': '内蒙古自治区', '广西': '广西壮族自治区',
    '西藏': '西藏自治区', '宁夏': '宁夏回族自治区', '新疆': '新疆维吾尔自治区',
    '北京': '北京市', '上海': '上海市', '天津': '天津市', '重庆': '重庆市',
    '香港': '香港特别行政区', '澳门': '澳门特别行政区',
}

_PUNCT_RE = re.compile(r'[\s，,。;；、·()（）\-—_/]+')
# 明确外省的说法：必须有户籍/居住语境，或整词就是"外省/省外"。
_OUTSIDE_PHRASE_RE = re.compile(
    r'(?:户籍|户口|籍贯|老家|所在地|现居|居住|家住|家庭)[^，。；;]{0,8}'
    r'(?:省外|外省|江西以外|不是江西|非江西|不在江西)'
    r'|(?:省外|外省)(?:户籍|户口|籍贯|老家)'
)

# 11 个设区市的标准名/去掉"市"的简称 -> 标准名。
_CITY_CANON = {city[:-1]: city for city in geo.CITY_NAMES}
# 县区 -> 所属设区市列表（同名县区可能多市，保持不猜）。
_CITY_OF_COUNTY = {}
for _city, _counties in geo.JIANGXI_COUNTY.items():
    for _county in _counties:
        _CITY_OF_COUNTY.setdefault(_county, []).append(_city)
# 功能区按县区级参与判定（政策库 regions 里实际出现的写法）；
# 建区级简称表只用民政口径县区，避免"赣州经济技术开发"这类截断词被当成简称。
_CITY_OF_COUNTY_ALL = dict(_CITY_OF_COUNTY)
for _zone, _city in geo.FUNCTIONAL_ZONES.items():
    _CITY_OF_COUNTY_ALL.setdefault(_zone, []).append(_city)


def _strip_admin_suffix(name):
    """去掉县区级行政区后缀，用于"新建"↔"新建区"这类常见写法。"""
    for suffix in ('自治县', '区', '县', '市', '旗'):
        if len(name) > len(suffix) and name.endswith(suffix):
            return name[:-len(suffix)]
    return name


# 县区简称 -> 标准县区名集合；与设区市简称同名（吉安/南昌）时不走简称匹配，避免歧义。
_COUNTY_CANON = {}
for _county in _CITY_OF_COUNTY:
    _canon = _strip_admin_suffix(_county)
    if _canon in _CITY_CANON or len(_canon) < 2:
        continue
    _COUNTY_CANON.setdefault(_canon, set()).add(_county)


def functional_zones_enabled():
    """功能区（开发区/新区/管委会）地区归一的开关（REGION_FUNCTIONAL_ZONES，默认开）。
    关闭后功能区写法回到旧行为（无法解析 → unknown → 保留候选并标注）。"""
    return os.environ.get('REGION_FUNCTIONAL_ZONES', '1').lower() not in ('0', 'false', 'no', 'off')


def soft_filter_enabled():
    """REGION_SOFT_FILTER 开关（默认开）；关闭时调用方严格回退旧地区行为。"""
    return os.environ.get('REGION_SOFT_FILTER', '1').lower() not in ('0', 'false', 'no', 'off')


def service_scope_mode():
    """SERVICE_SCOPE=jiangxi（默认）/national：服务范围口径。

    national 表示不再默认"政策库只有江西"：明确外省不再直接拦截，地区判定回落全国区划层。
    只有显式写 national/nation/全国 才切模式，避免拼写错误把生产切到全国口径。
    """
    mode = os.environ.get('SERVICE_SCOPE', 'jiangxi').strip().lower()
    return 'national' if mode in ('national', 'nation', '全国') else 'jiangxi'


def nationwide_enabled():
    """REGION_NATIONWIDE 开关（默认 0）；SERVICE_SCOPE=national 时强制开启。

    开启后江西词表解析不出的地区回落 admin_division（全国行政区划，纯函数、无 IO）。
    默认关闭，保证现网江西口径与历史单测完全不变。
    """
    if service_scope_mode() == 'national':
        return True
    return os.environ.get('REGION_NATIONWIDE', '0').strip().lower() in ('1', 'on', 'true', 'yes')


def _compact(text):
    return _PUNCT_RE.sub('', str(text or '')).strip()


def _jx_marker(text):
    """文本是否出现江西省份标识；"赣"只在独立成词时算省份。"""
    return '江西' in text or text in ('赣', '赣省')


def _outside_phrase(text):
    return bool(_OUTSIDE_PHRASE_RE.search(text)) or text in ('省外', '外省', '外省人', '江西以外')


def _other_province_in(text):
    """文本中明确出现的其他省级行政区（规范全称）；未出现返回 None。"""
    for full in _OTHER_PROVINCE_FULL:
        if full in text:
            return full
    for short, full in _OTHER_PROVINCE_SHORT.items():
        if short in text and _short_other_hit(text, short):
            return full
    return None


def _short_other_hit(text, short):
    if text in (short, short + '人'):
        return True
    if text.startswith(short) and len(text) > len(short) and text[len(short)] in '省市县区人':
        return True
    # 语境词与省名之间常隔一两个字（"户籍在湖南"），允许 0-4 个非分句字符。
    if re.search(r'(?:户籍|户口|籍贯|老家|来自|我是|家在|住在|现居|出生)[^，。；;、]{0,4}' + re.escape(short), text):
        return True
    return bool(re.search(re.escape(short) + r'(?:户籍|户口|籍贯|老家|人)', text))


def explicit_other(household):
    """家庭地区是否明确在江西省外（只认明确写法，模糊文本一律 False）。

    全国化模式（SERVICE_SCOPE=national / REGION_NATIONWIDE=1）下"不在江西"不再等于
    "不适配任何政策"：适用性由政策自身适用范围判定（grader 的 policy_region_disjoint）。
    这里一律返回 False，避免全国模式把外省家庭硬判成"明确不符"。
    """
    if nationwide_enabled():
        return False
    text = _compact(household)
    if not text:
        return False
    if _outside_phrase(text):
        return True
    other = _other_province_in(text)
    if not other:
        return False
    # 同时出现江西地名时视为混合描述，不判明确外省（留给证据层保守处理）。
    return not _jx_marker(text)


_LEVEL_RANK = {'unknown': 0, 'nation': 1, 'province': 2, 'city': 3, 'county': 4}


def _admin_division():
    """懒加载全国区划层；不可用/数据缺失时返回 None，调用方严格回退江西词表行为。"""
    try:
        from . import admin_division
        return admin_division
    except Exception:
        return None


def _nationwide_fill(out, text):
    """全国化回落（REGION_NATIONWIDE/SERVICE_SCOPE=national）：只补江西词表解析不出的层级。

    FP=0：只采用 admin_division 的确定性结论（ambiguous 非空即视为无法确定），
    不覆盖江西词表已给出的标准名（词表带行政后缀校准，比通用匹配更贴近政策库写法）。
    """
    admin = _admin_division()
    if admin is None:
        return out
    try:
        got = admin.canonical(text)
    except Exception:
        return out
    if not got:
        return out
    for key in ('province', 'city', 'county'):
        if not out.get(key) and got.get(key):
            out[key] = got[key]
    if got.get('ambiguous'):
        out['ambiguous'] = True
    if got.get('national'):
        out['nation'] = True
    if out.get('province') and out['province'] != JX_PROVINCE:
        out['outside'] = True
    computed = ('county' if out['county'] else 'city' if out['city']
                else 'province' if out['province'] else 'nation' if out['nation'] else 'unknown')
    if _LEVEL_RANK.get(computed, 0) > _LEVEL_RANK.get(out.get('level'), 0):
        out['level'] = computed
    return out


def canonical_levels(text):
    """地区文本 -> 确定性行政层级（无 IO）。

    返回 dict(province, city, county, level, nation, ambiguous, outside)：
      province/city/county 用标准名（"江西省"/"南昌市"/"新建区"）；
      level 取最具体已知层级 province|city|county|nation|unknown；
      ambiguous 表示同一段文本里出现多个互不兼容的行政区（不猜，只保留确定的上层）；
      outside 表示明确外省（不猜具体省市）。
    """
    out = dict(province=None, city=None, county=None, level='unknown',
               nation=False, ambiguous=False, outside=False)
    t = _compact(text)
    if not t:
        return out
    if any(word in t for word in _NATION_WORDS):
        out['nation'] = True
        out['level'] = 'nation'
        return out

    outside = _outside_phrase(t)
    other = _other_province_in(t)
    if other:
        out['province'] = other
        out['outside'] = True
    elif outside:
        out['outside'] = True
    elif _jx_marker(t):
        out['province'] = JX_PROVINCE

    # 0) "江西省全省"这类写法：明确是省级，不再往下猜县区。
    if t.endswith('全省') and _jx_marker(t):
        out['province'] = JX_PROVINCE
        out['level'] = 'province'
        return out

    # 1) 县区：标准全名（含功能区，开关可关）优先（子串匹配），其次县区简称（仅当整段文本以它结尾）。
    county_pool = _CITY_OF_COUNTY_ALL if functional_zones_enabled() else _CITY_OF_COUNTY
    counties = [county for county in county_pool if county in t]
    if not counties:
        canon_hits = [(canon, names) for canon, names in _COUNTY_CANON.items() if t.endswith(canon)]
        if len(canon_hits) == 1:
            counties = sorted(canon_hits[0][1])
        elif len(canon_hits) > 1:
            out['ambiguous'] = True
    if len(counties) == 1:
        out['county'] = counties[0]
        cities = _CITY_OF_COUNTY_ALL.get(counties[0]) or _CITY_OF_COUNTY[counties[0]]
        if len(cities) == 1:
            out['city'] = cities[0]
        else:
            out['ambiguous'] = True
    elif len(counties) > 1:
        out['ambiguous'] = True
        cities = {city for county in counties for city in (_CITY_OF_COUNTY_ALL.get(county) or _CITY_OF_COUNTY[county])}
        if len(cities) == 1:
            out['city'] = next(iter(cities))

    # 2) 设区市：标准全名（含"市"）优先；简称只在整词/"江西省+简称"时匹配，
    #    避免"南昌路88号"这类道路名被当成城市。
    direct = None
    for city in geo.CITY_NAMES:
        if city in t:
            direct = city
            break
    if direct is None:
        for canon, city in _CITY_CANON.items():
            if t == canon or t == '江西' + canon or t == '江西省' + canon or t.endswith(canon):
                direct = city
                break
    if direct is not None and out['city'] is not None and out['city'] != direct:
        # 县区反查市与直接出现的市矛盾：以直接出现的市为准，但不再猜县区归属。
        out['ambiguous'] = True
        out['county'] = None
        out['city'] = direct
    elif direct is not None:
        out['city'] = direct

    if out['outside'] and (out['city'] or out['county']):
        # 同时命中外省与江西地名：混合描述，不据此裁决。
        out['ambiguous'] = True
    if out['province'] is None and (out['city'] or out['county']) and not out['outside']:
        out['province'] = JX_PROVINCE
    out['level'] = ('county' if out['county'] else 'city' if out['city']
                    else 'province' if out['province'] else 'unknown')
    if nationwide_enabled():
        out = _nationwide_fill(out, t)
    return out


def _has_place(canon):
    return bool(canon.get('province') or canon.get('city') or canon.get('county') or canon.get('outside'))


def _covers(outer, inner):
    """outer 是否覆盖 inner（outer 是 inner 的祖先或同一行政区）；不猜不确定层级。"""
    if outer.get('nation'):
        return True
    if not _has_place(inner):
        return False
    if outer.get('county'):
        if inner.get('county'):
            return outer['county'] == inner['county'] and (
                not (outer.get('city') and inner.get('city')) or outer['city'] == inner['city'])
        return False
    if outer.get('city'):
        if inner.get('county'):
            return bool(inner.get('city')) and inner['city'] == outer['city']
        if inner.get('city'):
            return inner['city'] == outer['city']
        return False
    if outer.get('province'):
        return bool(inner.get('province')) and inner['province'] == outer['province']
    return False


def _same_place(a, b):
    if a.get('county') and b.get('county'):
        return a['county'] == b['county']
    if a.get('county') or b.get('county'):
        return False
    if a.get('city') and b.get('city'):
        return a['city'] == b['city']
    if a.get('city') or b.get('city'):
        return False
    return bool(a.get('province')) and a['province'] == b['province'] and not a.get('outside')


def _known_conflict(a, b):
    """双方都在同一层级给出明确且不同的行政区 → 必然互不包含（disjoint）。"""
    for level in ('province', 'city', 'county'):
        if a.get(level) and b.get(level) and a[level] != b[level]:
            return True
    return False


def _relation_from_parsed(household, policy_region):
    if not _has_place(household):
        return 'unknown', '家庭地区无法确定性解析'
    if policy_region.get('nation'):
        return 'ancestor', '政策地区为全国（覆盖任意家庭地区）'
    if not _has_place(policy_region):
        return 'unknown', '政策地区无法确定性解析'
    if household.get('outside') and not (household.get('province') or household.get('city') or household.get('county')):
        return 'disjoint', '家庭地区明确在江西省外，政策地区限定江西省内'
    if _known_conflict(household, policy_region):
        return 'disjoint', '家庭与政策地区指向不同行政区'
    if _same_place(household, policy_region):
        return 'same', '家庭与政策地区为同一行政区'
    if _covers(policy_region, household):
        return 'ancestor', '政策地区覆盖家庭地区'
    if _covers(household, policy_region):
        return 'descendant', '政策地区比家庭地区更具体，无法确认家庭是否在其中'
    return 'unknown', '地区层级信息不足'


def _confidence(household, policy_region, relation):
    if relation == 'unknown':
        return 'low'
    if household.get('ambiguous') or policy_region.get('ambiguous'):
        return 'medium'
    if relation == 'same':
        return 'high'
    if household.get('county') or policy_region.get('county'):
        return 'high'
    return 'medium'


def _nationwide_relation(household_c, policy_c):
    """全国区划层关系（入参是 canonical_levels 结果，避免重复解析）；不可用返回 None。"""
    admin = _admin_division()
    if admin is None:
        return None
    try:
        got = admin.relation(policy_c, household_c)
    except Exception:
        return None
    relation = (got or {}).get('relation')
    if relation not in ('same', 'ancestor', 'descendant', 'disjoint', 'unknown'):
        return None
    return relation, str((got or {}).get('reason') or '')


def _merge_nationwide_relation(household_c, policy_c, relation, note):
    """江西词表结论与全国区划层结论合并（FP=0：只补信息，不放大适用）。

    - 江西词表判 unknown（解析不出）时采用全国层确定性结论；
    - 江西词表判适用、全国层却未确认（unknown/disjoint）时降级 unknown 保留待核，
      避免江西词表的历史启发式在跨省文本上误放行。
    """
    fallback = _nationwide_relation(household_c, policy_c)
    if not fallback:
        return relation, note
    admin_relation, admin_reason = fallback
    if relation == 'unknown' and admin_relation != 'unknown':
        return admin_relation, '全国区划层：%s' % (admin_reason or admin_relation)
    if relation_is_applicable(relation) and not relation_is_applicable(admin_relation):
        return 'unknown', '江西词表与全国区划层不一致，保留待核'
    return relation, note


def region_relation(household, policy_region):
    """家庭地区 与 政策适用地区 的关系（纯函数）。

    返回 dict(relation, confidence, note)：
      same      —— 同一行政区（政策地区 = 家庭地区）；
      ancestor  —— 政策地区是家庭地区的上级/覆盖（政策省 vs 家庭县、政策市 vs 家庭县）；
      descendant—— 政策地区比家庭地区更具体（家庭只有省、政策到县），无法确认 → 不裁决；
      disjoint  —— 双方都明确且互不包含（同市不同县、不同市、外省 vs 江西）；
      unknown   —— 信息不足/无法解析/区县归属歧义，保留候选并标注。
    """
    household_c = canonical_levels(household)
    policy_c = canonical_levels(policy_region)
    relation, note = _relation_from_parsed(household_c, policy_c)
    if nationwide_enabled():
        relation, note = _merge_nationwide_relation(household_c, policy_c, relation, note)
    return dict(relation=relation, confidence=_confidence(household_c, policy_c, relation), note=note)


def relation_is_applicable(relation):
    """关系是否可直接判"地区适用"（只认 same/ancestor；其余一律不裁决）。"""
    if isinstance(relation, dict):
        relation = relation.get('relation')
    return relation in ('same', 'ancestor')


def _combine_relations(relations):
    """多值政策地区（"赣州市、吉安市"）合并：任一覆盖即适用；全部明确不交叠才不适用。"""
    if not relations:
        return None
    if any(relation_is_applicable(relation) for relation in relations):
        return 'ancestor'
    if all(relation == 'disjoint' for relation in relations):
        return 'disjoint'
    return 'unknown'


def _policy_scopes(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = re.split(r'[,，、;；/|]+', str(value))
    return [item.strip() for item in items if item and item.strip()]


def _zone_overlap(a, b):
    """同设区市内一方是功能区、另一方是普通县区：空间上可能交叉，不能硬判不适用。

    全国化口径（policy_scope 并集语义）：功能区与 host 县区可能重叠，家庭在同市其他
    县区时保留待核（不误放行、也不误丢）。江西默认口径不走这里（保持历史判定）。
    """
    if not a.get('city') or a.get('city') != b.get('city'):
        return False
    if not (a.get('county') and b.get('county')):
        return False
    zone_a = a['county'] in geo.FUNCTIONAL_ZONES
    zone_b = b['county'] in geo.FUNCTIONAL_ZONES
    return zone_a != zone_b


def policy_region_verdict(policy, detail, household):
    """政策地区 vs 家庭地区 → True(适用) / False(明确不适用) / None(无法确定)。

    只做程序化确定性裁决；None 表示调用方应保留原 kg_retrieval.region_judge 结果。
    明细地区优先级高于政策级；政策级与明细级矛盾时返回 None（交给原判定按"冲突"保守处理）。
    """
    household_c = canonical_levels(household)
    if not _has_place(household_c):
        return None
    scopes = _policy_scopes((policy or {}).get('regions'))
    detail_region = (detail or {}).get('region')
    detail_region = str(detail_region).strip() if detail_region is not None else ''
    if not scopes and not detail_region:
        return None
    policy_relation = _combine_relations([
        region_relation(household, scope)['relation'] for scope in scopes]) if scopes else None
    detail_relation = region_relation(household, detail_region)['relation'] if detail_region else None
    if nationwide_enabled():
        # 全国化：功能区政策与同市普通县区家庭共现时降级为待核，不硬判不适用。
        if policy_relation == 'disjoint' and any(
                _zone_overlap(household_c, canonical_levels(scope)) for scope in scopes):
            policy_relation = None
        if detail_relation == 'disjoint' and _zone_overlap(household_c, canonical_levels(detail_region)):
            detail_relation = None
    if detail_region:
        if relation_is_applicable(detail_relation):
            return True if policy_relation is None or relation_is_applicable(policy_relation) else None
        if detail_relation == 'disjoint':
            return False if policy_relation is None or policy_relation == 'disjoint' else None
        # 明细地区无法确定性解析：不越过原判定（原逻辑按"地区待细化"保留）。
        return None
    if policy_relation is None:
        return None
    if relation_is_applicable(policy_relation):
        return True
    if policy_relation == 'disjoint':
        return False
    return None


def equivalent_regions(a, b):
    """两个地区值是否为同一事实的粗细写法（facts 地区字段专用等价）。"""
    ca, cb = canonical_levels(a), canonical_levels(b)
    if ca.get('nation') or cb.get('nation'):
        return False
    if not _has_place(ca) or not _has_place(cb):
        return False
    if (ca.get('outside') or cb.get('outside')) and not (ca.get('outside') and cb.get('outside')):
        return False
    if _known_conflict(ca, cb):
        return False
    return _covers(ca, cb) or _covers(cb, ca)


def more_specific_region(a, b):
    """两个等价地区值中保留更具体（层级更深/文本更长）的写法；只返回原值，不改写。"""
    ca, cb = canonical_levels(a), canonical_levels(b)
    depth = {'nation': 0, 'province': 1, 'city': 2, 'county': 3}
    da, db = depth.get(ca['level'], 0), depth.get(cb['level'], 0)
    if da != db:
        return b if db > da else a
    return b if len(_compact(b)) > len(_compact(a)) else a
