# -*- coding: utf-8 -*-
"""江西省行政区划确定性标准化。

设计原则（对应阶段一"行政区划标准化"要求）：
- 只用标准行政区划表做精确匹配，不凭模型常识补全模糊地名；
- 同名/简称映射不唯一时保持未知，不强行猜；
- 不把居住地当户籍；户籍地标准化与家庭所在地标准化分开；
- 不新建区、不某个家庭写特例分支；表是公开行政区划数据。
"""
import re

# 江西省 11 个设区市及其下辖区县（标准名称）。
# key=设区市标准名，value=下辖区县/县级市标准名列表。
JIANGXI_COUNTY = {
    "南昌市": ["东湖区", "西湖区", "青云谱区", "青山湖区", "新建区", "红谷滩区",
              "南昌县", "安义县", "进贤县"],
    "九江市": ["濂溪区", "浔阳区", "柴桑区", "武宁县", "修水县", "永修县",
              "德安县", "都昌县", "湖口县", "彭泽县", "瑞昌市", "共青城市", "庐山市"],
    "上饶市": ["信州区", "广丰区", "广信区", "玉山县", "铅山县", "横峰县",
              "弋阳县", "余干县", "鄱阳县", "万年县", "婺源县", "德兴市"],
    "抚州市": ["临川区", "东乡区", "南城县", "黎川县", "南丰县", "崇仁县",
              "乐安县", "宜黄县", "金溪县", "资溪县", "广昌县"],
    "宜春市": ["袁州区", "奉新县", "万载县", "上高县", "宜丰县", "靖安县",
              "铜鼓县", "丰城市", "樟树市", "高安市"],
    "吉安市": ["吉州区", "青原区", "吉安县", "吉水县", "峡江县", "新干县",
              "永丰县", "泰和县", "遂川县", "万安县", "安福县", "永新县", "井冈山市"],
    "赣州市": ["章贡区", "南康区", "赣县区", "信丰县", "大余县", "上犹县",
              "崇义县", "安远县", "龙南市", "定南县", "全南县", "宁都县",
              "于都县", "兴国县", "会昌县", "寻乌县", "石城县", "瑞金市"],
    "景德镇市": ["昌江区", "珠山区", "浮梁县", "乐平市"],
    "萍乡市": ["安源区", "湘东区", "莲花县", "上栗县", "芦溪县"],
    "新余市": ["渝水区", "分宜县"],
    "鹰潭市": ["月湖区", "余江区", "贵溪市"],
}

# 功能区（开发区/新区/管委会/风景名胜区）：政策库 regions 里实际存在的地区写法。
# 它们不是民政口径的县区，但政策按这个粒度收录，所以按"县区级 + 归属设区市"处理：
# 家庭与政策都写同一个功能区时判 same；只到市/省时按上级覆盖判定。
FUNCTIONAL_ZONES = {
    '南昌经济技术开发区': '南昌市',
    '湾里管理局': '南昌市',
    '赣州经济技术开发区': '赣州市',
    '蓉江新区': '赣州市',
    '萍乡经济技术开发区': '萍乡市',
    '武功山风景名胜区': '萍乡市',
    '鹰潭高新技术产业开发区': '鹰潭市',
    '信江新区管委会': '鹰潭市',
    '龙虎山管委会': '鹰潭市',
    '抚州高新区': '抚州市',
    '东临新区': '抚州市',
    '三清山管委会': '上饶市',
    '仙女湖区': '新余市',
}

# 设区市标准名集合（供枚举值校验）
CITY_NAMES = tuple(JIANGXI_COUNTY.keys())

# 县区 -> 所属设区市（反查表，构建一次）。
_COUNTY_TO_CITY = {}
for _city, _counties in JIANGXI_COUNTY.items():
    for _c in _counties:
        # 若同名县区出现在多个市，不做归属猜测，保持未知。
        _COUNTY_TO_CITY.setdefault(_c, set()).add(_city)


def _has_keyword(text, keywords):
    for kw in keywords:
        if kw in text:
            return True
    return False


def normalize_address(text):
    """从一段原文中确定性提取江西省行政区划。

    返回 dict:
      {
        "province": "江西省" | None,
        "city": "南昌市" | None,        # 设区市标准名
        "county": "新建区" | None,      # 县区标准名
        "county_ambiguous": bool,       # 县区名归属不唯一
        "outside_jiangxi": bool,        # 原文明确说外省/省外
      }
    未识别到的层级返回 None，不补全。
    """
    out = dict(province=None, city=None, county=None,
               county_ambiguous=False, outside_jiangxi=False)
    if not text:
        return out
    t = str(text)

    # 明确外省："江西省以外"/"外省户籍"/"省外"（业务条件里的"省外务工"不算户籍外省，
    # 这里只在出现"户籍/户口/籍贯"语境时判定；纯"省外务工"不触发 outside）。
    if re.search(r'(户籍|户口|籍贯|老家).{0,8}(省外|外省|江西以外|不是江西)', t):
        out["outside_jiangxi"] = True
    if "江西省" in t:
        out["province"] = "江西省"

    # 1) 先匹配县区（县区名更长、更具体），反查所属市。
    found_county = None
    for county, cities in _COUNTY_TO_CITY.items():
        if county in t:
            if found_county is not None:
                # 同一句出现多个县区名，不猜测
                found_county = None
                break
            found_county = county
    if found_county is not None:
        out["county"] = found_county
        owners = _COUNTY_TO_CITY[found_county]
        if len(owners) == 1:
            out["city"] = next(iter(owners))
        else:
            out["county_ambiguous"] = True

    # 2) 直接出现设区市名（"南昌市"等）。
    for city in CITY_NAMES:
        # 精确匹配"南昌市"，避免"南昌县"误命中"南昌市"。
        if re.search(re.escape(city), t):
            # 若已经由县区反查出市，不冲突就保留；若冲突（县区反查的市≠直接出现的市），
            # 以直接出现的市为准，但标记 county 归属存疑。
            if out["city"] is not None and out["city"] != city:
                out["county_ambiguous"] = True
            out["city"] = city
            break

    return out


def apply_geo_to_facts(st, text, entity="household"):
    """对一段本轮原文跑确定性行政区划标准化，补录/校验事实。

    st 是 facts.state(hh) 返回的状态对象；text 是本轮原文；entity 是主体。
    只补录尚未 known 的字段；已 known 的不覆盖（用户更正走 correcting 路径）。
    返回 dict：本次补录了哪些字段（供审计/追踪）。
    """
    from .facts import record
    norm = normalize_address(text)
    changes = []
    facts = st["facts"]

    def _known(key):
        f = facts.get(key, {})
        return f.get("state") == "known"

    # 户籍设区市：hukou_city（member 级）。仅当原文出现"户籍/户口"语境才补，
    # 避免把家庭所在地当户籍。
    hukou_ctx = re.search(r'(户籍|户口|籍贯|老家)', text or "")
    if hukou_ctx and norm["city"]:
        key = f"{entity}:hukou_city"
        if not _known(key):
            city_val = norm["city"]
            if city_val not in (None,) and not norm["outside_jiangxi"]:
                record(st, key, city_val,
                       quote=text[max(hukou_ctx.start()-4, 0):hukou_ctx.start()+12],
                       origin='geo', definition=_field_def('hukou_city'))
                changes.append(key)
        # 户籍地县
        if norm["county"] and not norm["county_ambiguous"]:
            key2 = f"{entity}:hukou_region"
            if not _known(key2):
                record(st, key2, norm["county"],
                       quote=text[max(hukou_ctx.start()-4, 0):hukou_ctx.start()+12],
                       origin='geo', definition=_field_def('hukou_region'))
                changes.append(key2)

    return changes


def _field_def(name):
    try:
        from .fields import dictionary
        return dictionary().get(name, {})
    except Exception:
        return {}
