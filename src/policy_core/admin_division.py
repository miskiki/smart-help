# -*- coding: utf-8 -*-
"""全国行政区划确定性归一与地区关系判定（P4 地区维度层）。

数据资产：data/regions/china_divisions.json（deploy/build_china_divisions.py 生成，
来源 modood/Administrative-divisions-of-China，MIT）。纯函数、无数据库/网络 IO。

口径：
- 只按标准行政区划名称精确匹配，不猜简称、不凭常识补全；
- 同名县区（如"朝阳区"北京/长春）在没有市级上下文时保持歧义，返回候选而不是硬选；
- 直辖市（北京/天津/上海/重庆）没有地级层，市辖区/县 两个伪地级节点视为"直辖"；
- 省直辖县级行政区划（河南济源、湖北 4 个、海南 15 个）同样视为"省直辖县级市"；
- 功能区（开发区/新区/管委会/风景名胜区/高新区/管理局）按名称前缀归到设区市，
  前缀不唯一或匹配不到时保持未知，不猜；
- 只有明确可判定时才给 disjoint；无法解析或上下级关系不清一律 unknown（FP=0 红线）。

层级：nation > province > city > county。
关系（政策地区 vs 家庭地区）：
  ancestor  （上级覆盖下级，政策适用于该家庭）
  same
  descendant（政策比家庭更具体，不能确认适用，保留候选）
  disjoint  （明确互不覆盖）
  unknown
"""
import json
import os
import re
from functools import lru_cache

NATION = 'nation'
PROVINCE = 'province'
CITY = 'city'
COUNTY = 'county'
UNKNOWN = 'unknown'

LEVEL_RANK = {NATION: 4, PROVINCE: 3, CITY: 2, COUNTY: 1, UNKNOWN: 0}

NATION_WORDS = ('全国', '国家', '全国范围', '全国通用', '全国性')
# 源数据里的伪地级节点：直辖市辖区/县、省直辖县级行政区划。
PSEUDO_CITIES = ('市辖区', '县', '省直辖县级行政区划')
ZONE_SUFFIXES = ('经济技术开发区', '高新技术产业开发区', '高新技术开发区', '产业开发区',
                 '经济开发区', '开发区', '新区', '管委会', '管理局', '风景名胜区',
                 '工业园区', '产业园', '旅游区', '高新区')
_PROVINCE_STRIP = ('壮族自治区', '回族自治区', '维吾尔自治区', '自治区', '特别行政区', '省', '市')

_DEFAULT_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', '..', 'data', 'regions', 'china_divisions.json')


def data_path():
    return os.environ.get('REGION_DIVISION_DATA') or _DEFAULT_DATA


@lru_cache(maxsize=2)
def _load(path=None):
    path = path or data_path()
    with open(path, encoding='utf-8') as handle:
        raw = json.load(handle)
    provinces = [p['name'] for p in raw['provinces']]
    province_set = set(provinces)
    cities = {}       # province -> [city names]
    city_index = {}   # city -> [province]
    county_index = {}  # county -> [(province, city)]
    county_of = {}     # (province, city) -> [counties]
    for prov in raw['provinces']:
        pname = prov['name']
        cities[pname] = []
        for city in prov['cities']:
            cname = city['name']
            if cname in PSEUDO_CITIES:
                # 直辖/省直辖：伪地级节点，市县都记到省级下。
                for county in city['counties']:
                    county_index.setdefault(county, []).append((pname, None))
                    county_of.setdefault((pname, None), []).append(county)
                continue
            cities[pname].append(cname)
            city_index.setdefault(cname, []).append(pname)
            county_of[(pname, cname)] = list(city['counties'])
            for county in city['counties']:
                county_index.setdefault(county, []).append((pname, cname))
    aliases = {}
    for name in provinces:
        base = name
        for suffix in _PROVINCE_STRIP:
            if base.endswith(suffix) and len(base) > len(suffix) + 1:
                base = base[:-len(suffix)]
                break
        aliases.setdefault(base, name)
        aliases.setdefault(name, name)
    city_short = {}
    for city, provinces_of in city_index.items():
        city_short.setdefault(_strip_city_suffix(city), set()).update(provinces_of)
    return {
        'provinces': provinces,
        'province_set': province_set,
        'province_aliases': aliases,
        'cities': cities,
        'city_index': city_index,
        'city_short': city_short,
        'county_index': county_index,
        'county_of': county_of,
    }


def _strip_city_suffix(name):
    return name[:-1] if name.endswith('市') else name


def reset_cache():
    _load.cache_clear()


def provinces():
    return tuple(_load()['provinces'])


def cities_of(province):
    return tuple(_load()['cities'].get(province, ()))


def counties_of(city, province=None):
    data = _load()
    if province:
        return tuple(data['county_of'].get((province, city), ()))
    out = []
    for prov in data['city_index'].get(city, ()):
        out.extend(data['county_of'].get((prov, city), ()))
    return tuple(out)


def is_zone(name):
    name = (name or '').strip()
    if not name:
        return False
    if name in known_functional_zones():
        return True
    return any(name.endswith(suffix) for suffix in ZONE_SUFFIXES)


def _find_province(text, data):
    hits = [alias for alias in data['province_aliases'] if alias in text]
    if not hits:
        return None, []
    # 全称优先；短名（去后缀）只在词边界上成立，避免"上海路/海南路"这类街道名误判。
    full_hits = [h for h in hits if h.endswith(('省', '市', '自治区', '特别行政区'))]
    if full_hits:
        full_hits.sort(key=lambda x: (-len(x), x))
        best = full_hits[0]
        same_len = [h for h in full_hits if len(h) == len(best)]
        values = sorted({data['province_aliases'][h] for h in same_len})
        return (values[0] if len(values) == 1 else None), values
    short_hits = []
    for alias in hits:
        for match in re.finditer(re.escape(alias), text):
            before = text[match.start() - 1] if match.start() > 0 else ''
            after = text[match.end():match.end() + 1]
            if after and after in '路街巷号村镇厦':
                continue
            if before and before in '路街巷号':
                continue
            short_hits.append(alias)
            break
    if not short_hits:
        return None, []
    short_hits.sort(key=lambda x: (-len(x), x))
    best = short_hits[0]
    same_len = [h for h in short_hits if len(h) == len(best)]
    values = sorted({data['province_aliases'][h] for h in same_len})
    return (values[0] if len(values) == 1 else None), values


def _find_city(text, data, province=None):
    """返回 (city, ambiguous, provinces)。province 给定时只在该省匹配。"""
    pool = data['cities'].get(province, ()) if province else tuple(data['city_index'])
    hits = [city for city in pool if len(city) >= 2 and city in text]
    if not hits:
        # 允许"赣州市"写成"赣州"（仅当该简称在候选池中只对应一个市）。
        short_pool = {}
        for city in pool:
            short_pool.setdefault(_strip_city_suffix(city), []).append(city)
        matched = []
        for short, group in short_pool.items():
            if len(short) < 2 or short not in text:
                continue
            # 简称后面必须跟"市/地区/盟/州"或是整段文本，否则"朝阳区"会被当成"朝阳市"。
            tail = text.split(short, 1)[1]
            if tail and not tail.startswith(('市', '地区', '盟', '州')):
                continue
            matched.extend(c for c in group if c not in matched)
        hits = matched
    if not hits:
        return None, [], []
    hits.sort(key=lambda x: (-len(x), x))
    best = hits[0]
    same_len = [h for h in hits if len(h) == len(best)]
    if len(same_len) > 1:
        return None, same_len, []
    return best, [], list(data['city_index'].get(best, []))


def _find_county(text, data, province=None, city=None):
    if province and city:
        pool = set(data['county_of'].get((province, city), ()))
        hits = [county for county in pool if county in text]
        ambiguous = []
    elif province:
        pairs = {county: [p for p in data['county_index'].get(county, []) if p[0] == province]
                 for county in data['county_index']}
        hits = [county for county, ps in pairs.items() if ps and county in text]
        if city:
            hits = [county for county in hits if any(p[1] == city for p in pairs[county])]
        ambiguous = []
    else:
        hits = [county for county in data['county_index'] if county in text]
        ambiguous = []
    if not hits:
        return None, [], []
    hits.sort(key=lambda x: (-len(x), x))
    best = hits[0]
    same_len = [h for h in hits if len(h) == len(best)]
    if len(same_len) > 1:
        return None, same_len, []
    county = best
    places = data['county_index'].get(county, [])
    if province:
        places = [p for p in places if p[0] == province]
    if city:
        places = [p for p in places if p[1] == city]
    if len(places) > 1:
        return None, [county], sorted({p[0] for p in places})
    if not places:
        return None, [county], []
    return county, [], [places[0]]


_ZONE_CITY = None


def known_functional_zones():
    """已知功能区 -> 设区市（当前来自江西政策库实际写法；全国化后建议迁到数据文件）。"""
    global _ZONE_CITY
    if _ZONE_CITY is None:
        _ZONE_CITY = {}
        try:
            from . import geo
            _ZONE_CITY = dict(getattr(geo, 'FUNCTIONAL_ZONES', {}) or {})
        except Exception:
            _ZONE_CITY = {}
    return _ZONE_CITY


def _zone_city(name, data):
    """功能区名归设区市：先查已知功能区表，再按前缀猜（唯一才返回）。

    返回 (设区市标准名, 省份列表, 功能标准名)；识别不了时 (None, [], '')。
    """
    if not is_zone(name):
        return None, [], ''
    for zone_name, city in known_functional_zones().items():
        if name == zone_name or name.endswith(zone_name):
            provs = data['city_index'].get(city)
            if not provs:
                return None, [], ''
            return city, [provs[0]], zone_name
    best = None
    best_len = 0
    for short, provs in data['city_short'].items():
        if len(short) >= 2 and name.startswith(short) and len(short) > best_len:
            best, best_len = short, len(short)
    if not best:
        return None, [], ''
    provs = sorted(data['city_short'][best])
    if len(provs) != 1:
        return None, provs, ''
    return best + '市', provs, ''


def canonical(text, default_province=None):
    """把一段地区文本归一为 {level, province, city, county, ambiguous, zone, matched}。

    level 取 nation/province/city/county/unknown；ambiguous 非空表示同名或多义，
    调用方必须按 unknown 处理（可用 ambiguous 展示候选）。
    """
    text = (text or '').strip()
    out = {'level': UNKNOWN, 'province': None, 'city': None, 'county': None,
           'ambiguous': [], 'zone': False, 'matched': '', 'national': False}
    if not text:
        return out
    data = _load()
    if any(word in text for word in NATION_WORDS) and not any(
            alias in text for alias in data['province_aliases'] if len(alias) >= 2
            and alias not in ('全国',)):
        out.update(level=NATION, national=True, matched=text)
        return out
    province, prov_hits = _find_province(text, data)
    if len(prov_hits) > 1 and not province:
        out.update(ambiguous=prov_hits, matched=text)
        return out
    if not province and default_province:
        province = default_province
    if province and text.replace(province, '') in ('全省', '', '省', '范围内', '辖区'):
        out.update(level=PROVINCE, province=province, matched=province)
        return out
    if '全省' in text and province:
        out.update(level=PROVINCE, province=province, matched=province)
        return out
    # 功能区必须先于市级匹配：功能区名常含"赣州"等市名前缀，若按市处理会放大成整市适用。
    if is_zone(text):
        county, county_hits, county_places = _find_county(text, data, province=province)
        if county and not county_hits:
            place = county_places[0]
            out.update(level=COUNTY, province=place[0], city=place[1], county=county,
                       zone=True, matched=county)
            return out
        zone_city, zone_provs, known_zone = _zone_city(text, data)
        if zone_city and (not province or province in zone_provs):
            prov = province if province in zone_provs else zone_provs[0]
            # 已知功能区用标准名；否则兼容"赣州市经开区"这类插了市名的写法。
            # 只剥完整市名（带「市」），不剥简称：简称本身可能正是功能区名前缀
            #（如「赣州经济技术开发区」的「赣州」），剥了会破坏区名。
            zone_name = known_zone or text
            if not known_zone and zone_city and zone_name.startswith(zone_city) and len(zone_name) > len(zone_city):
                zone_name = zone_name[len(zone_city):]
            out.update(level=COUNTY, province=prov, city=zone_city, county=zone_name,
                       zone=True, matched=zone_name)
            return out
        out.update(zone=True, matched=text)
        return out
    city, city_hits, city_provs = _find_city(text, data, province=province)
    if city_hits:
        out.update(ambiguous=city_hits, province=province, matched=text)
        return out
    if city and not province and len(city_provs) == 1:
        province = city_provs[0]
    if city and '全市' in text:
        out.update(level=CITY, province=province, city=city, matched=city)
        return out
    county, county_hits, county_places = _find_county(text, data, province=province, city=city)
    if county_hits and not county:
        if len(county_hits) == 1 and county_places and len(set(county_places)) == 1:
            place = county_places[0]
            out.update(level=COUNTY, province=place[0], city=place[1], county=county_hits[0],
                       matched=county_hits[0])
            return out
        out.update(ambiguous=county_hits, province=province, matched=text)
        return out
    if county:
        place = county_places[0]
        out.update(level=COUNTY, province=place[0], city=place[1], county=county,
                   matched=county, zone=is_zone(county))
        return out
    if city:
        out.update(level=CITY, province=province, city=city, matched=city)
        return out
    if province:
        out.update(level=PROVINCE, province=province, matched=province)
        return out
    if is_zone(text):
        zone_city, _, known_zone = _zone_city(text, data)
        if zone_city:
            prov = data['city_index'][zone_city][0]
            out.update(level=COUNTY, province=prov, city=zone_city,
                       county=known_zone or text, zone=True, matched=known_zone or text)
            return out
        out.update(zone=True, matched=text)
        return out
    out['matched'] = text
    return out


def _scope_of(value):
    if isinstance(value, dict):
        return value
    return canonical(value)


def _cmp_place(policy, household):
    """同一省份内比较市/县。返回 ancestor/same/descendant/disjoint/unknown。"""
    p_level, h_level = policy['level'], household['level']
    p_city, h_city = policy.get('city'), household.get('city')
    p_county, h_county = policy.get('county'), household.get('county')
    if p_level == COUNTY and h_level == COUNTY:
        if p_county and h_county:
            return 'same' if p_county == h_county else 'disjoint'
        return 'unknown'
    if p_level == CITY and h_level == CITY:
        if p_city and h_city:
            return 'same' if p_city == h_city else 'disjoint'
        return 'unknown'
    if p_level == CITY and h_level == COUNTY:
        if p_city and h_city:
            return 'ancestor' if p_city == h_city else 'disjoint'
        return 'unknown'
    if p_level == COUNTY and h_level == CITY:
        if p_city and h_city and p_city != h_city:
            return 'disjoint'
        if p_city and h_city and p_city == h_city and p_county:
            return 'descendant'
        return 'unknown'
    return 'unknown'


def relation(policy_region, household_region):
    """政策地区 vs 家庭地区 → dict(relation, reason, policy, household)。

    relation: ancestor/same/descendant/disjoint/unknown。applicable=True 仅 ancestor/same。
    """
    policy = _scope_of(policy_region)
    household = _scope_of(household_region)
    base = {'policy': policy, 'household': household, 'applicable': None}
    if policy['level'] == NATION:
        base.update(relation='ancestor', reason='national', applicable=True)
        return base
    if policy['level'] == UNKNOWN or household['level'] == UNKNOWN:
        base.update(relation='unknown', reason='unresolved')
        return base
    if policy['level'] == PROVINCE:
        if policy['province'] == household['province']:
            base.update(relation='ancestor', reason='same_province', applicable=True)
        else:
            base.update(relation='disjoint', reason='province_mismatch')
        return base
    if household['level'] == PROVINCE or household['province'] != policy['province']:
        if household['province'] and policy['province'] and household['province'] != policy['province']:
            base.update(relation='disjoint', reason='province_mismatch')
        elif household['level'] == PROVINCE and policy['level'] in (CITY, COUNTY) \
                and household['province'] == policy['province']:
            # 家庭只到省级、政策更具体（直辖市/省直辖县的区县也走这里）：政策地区是家庭
            # 地区的子集，按 descendant 供参考；但无法确认家庭是否落在该市/县，
            # 不判 applicable（FP=0），由上层保留候选或追问。
            base.update(relation='descendant', reason='household_too_broad')
        else:
            base.update(relation='unknown', reason='household_too_broad')
        return base
    rel = _cmp_place(policy, household)
    base.update(relation=rel, reason='place_compare',
                applicable=True if rel in ('ancestor', 'same') else None)
    return base
