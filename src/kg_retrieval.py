# -*- coding: utf-8 -*-
"""kg_retrieval.py — 知识图谱检索模块（批次4第三轮·隔离开发，不接入正式问答）
定位：面向"家庭具体成员 × policy_detail 明细分支"的图谱召回与核验。
第三轮修复（相对第二轮）：
  1. 三态严格化：'已核验条件满足' 要求全部结构化条件明确满足且无未知；未知≠满足，
     不再以"无明确不满足"充当命中。
  2. 对象资格：detail.target 与无法解析的 conditions 保留原文；未核验的对象或必要条件
     使该明细资格状态降为'待核实'，不得统计为完整资格通过。
  3. 多风险集合判断：pov 与 hh.risk 按集合关系核验（任一/全部/无法确定→未知），
     不再把"因病,因学"拼成字符串做包含匹配。
  4. 地区：过滤空分段；优先用图谱 REGION 节点做名称/行政层级解析，双向子串仅作启发式后备
     （标注'启发式'）；主表与明细任一方向矛盾均记'冲突'；明细地区为空区分'明确继承'与
     '信息缺失'，不默认无地区限制。
  5. 明细汇总与遍历顺序无关：每条明细独立保留地区与资格状态，政策级由分支聚合得出
     （有适用分支→适用；仅冲突→冲突；全不适→不适），冲突分支不计入适用命中。
  6. 缓存与确定性：refresh_cache()/cache_status()；SAME_DOC 邻居排序、SIM 邻居按权重降序
     后截断；补充信息按 (成员, 字段, 政策) 聚合，保留全部明细依据。
  7. 排序：对重复明细分支去重后计分，保留成员覆盖价值，避免"明细多/分支重复"主导排名。
只读 kg_node/kg_edge/kg_policy_edge/kg_policy_sim 与 policy/policy_detail/rule_check，不写库。
"""
import json
import re
import time
import threading
from datetime import date
from risk_vocabulary import risk_labels

import config
from sqlalchemy import create_engine, text

import smart_matcher_kb as smk

_LAB_COND = 'CONDITION:劳动能力:'
_HEALTH_COND = 'CONDITION:健康状况:'
_AGE_RE = re.compile(r'^CONDITION:劳动力年龄:(\d+)-(\d+)$')
_WIDE_OBJECT = ('所有农户', '一般农户', '家庭', '帮扶对象')      # 宽泛对象不参与强命中
_WIDE_AGE = lambda lo, hi: hi >= 100 or (hi - lo) >= 60          # 宽年龄（≈不限）不参与强命中
_SPLIT_RE = re.compile(r'[,，、;；/]')
_RISK_TERMS = ['因自然灾害', '因意外事故', '因务工就业不稳', '因产业项目失败', '住房安全问题',
               '饮水安全问题', '缺劳动力', '失学辍学', '未参保', '因病', '因学', '因残', '因灾']


# ---------------- 数据载入（进程内缓存，只读；可显式刷新） ----------------
_cache = {'t': None, 'data': None, 'ts': None, 'region_idx': None}
_cache_lock = threading.RLock()


def _load(force=False):
    with _cache_lock:
        return _load_locked(force)


def _load_locked(force=False):
    if _cache['t'] is not None and not force and time.time() - (_cache['ts'] or 0) < 30:
        return _cache['t'], _cache['data']
    e = create_engine(config.DATABASE_URL)
    with e.connect() as c:
        if c.dialect.name == 'postgresql':
            c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
        from graph_update_state import pending
        pending_rows = pending(c)
        dirty = {'POLICY:'+r['policy_code'] for r in pending_rows}
        dirty.update('POLICY:'+r[0] for r in c.execute(text("SELECT policy_code FROM policy WHERE status IS NULL OR status<>'在用'")))
        nodes = {}
        for r in c.execute(text('SELECT id, node_type, name, attrs FROM kg_node')):
            attrs=r[3] or {}
            if isinstance(attrs,str):
                attrs=json.loads(attrs)
            nodes[r[0]]={'node_type':r[1],'name':r[2],'policy_version':attrs.get('policy_version')}
        nodes = {nid:meta for nid,meta in nodes.items() if nid not in dirty}
        edges = {'IN_REGION': {}, 'APPLIES_TO': {}, 'REQUIRES': {}, 'COVERS': {}, 'BELONGS_TO': {}, 'FROM_DOC': {}, 'HAS_SUBSIDY': {}}
        for r in c.execute(text('SELECT src_id, dst_id, edge_type, evidence, source_field FROM kg_edge')):
            if r[0] not in nodes or r[1] not in nodes:
                continue
            d = edges.setdefault(r[2], {})
            d.setdefault(r[1], []).append((r[0], r[3] or '', r[4] or ''))
        same_doc = {}
        # Missing document labels never establish document identity, even after a rebuild.
        from kg_normalization import same_doc_sql
        for r in c.execute(text(same_doc_sql()), {'et': 'SAME_DOC',
                'max_policies': getattr(config, 'KG_SAME_DOC_MAX_POLICIES', 20)}):
            if r[0] not in nodes or r[1] not in nodes:
                continue
            same_doc.setdefault(r[0], []).append(r[1])
            same_doc.setdefault(r[1], []).append(r[0])
        sim = {}
        for r in c.execute(text('SELECT src_id, dst_id, weight FROM kg_policy_sim WHERE edge_type=:et'), {'et': 'SIM_ATTR'}):
            if r[0] not in nodes or r[1] not in nodes:
                continue
            sim.setdefault(r[0], []).append((r[1], float(r[2])))
            sim.setdefault(r[1], []).append((r[0], float(r[2])))
    # 确定性：邻居排序（SAME_DOC 按 id 字典序，SIM 按 weight 降序），截断一致
    for pid in same_doc:
        same_doc[pid] = sorted(set(same_doc[pid]))
    for pid in sim:
        sim[pid] = sorted(set(sim[pid]), key=lambda x: (-x[1], x[0]))
    # REGION 名称索引（仅县区级节点，供地区解析 O(109) 而非 O(12051)）
    region_idx = {}
    for nid, meta in nodes.items():
        if meta['node_type'] == 'REGION' and nid != 'REGION:江西省全省':
            region_idx[nid.split(':', 1)[1]] = nid
    e.dispose()
    data = smk.load_db()
    _cache.update(t=(nodes, edges, same_doc, sim), data=data, region_idx=region_idx,
                  ts=time.time(), pending_policies=pending_rows)
    return _cache['t'], _cache['data']


def refresh_cache():
    """显式刷新进程内图谱/政策缓存（正式数据更新后调用；隔离验证可见性）"""
    _load(force=True)
    return _cache['ts']


def invalidate_cache():
    with _cache_lock:
        _cache['t'] = None
        _cache['ts'] = None


def cache_status():
    """缓存状态：构建时间、节点/边/政策规模（供更新可见性验证）"""
    if _cache['t'] is None:
        return {'loaded': False}
    nodes, edges, same_doc, sim = _cache['t']
    return {
        'loaded': True, 'ts': _cache['ts'],
        'nodes': len(nodes),
        'edges': {k: len(v) for k, v in edges.items()},
        'same_doc_pairs': sum(len(v) for v in same_doc.values()) // 2,
        'sim_pairs': sum(len(v) for v in sim.values()) // 2,
        'policies': len(_cache['data']['policies']),
        'pending_policies': _cache.get('pending_policies',[]),
    }


# ---------------- 地区 ----------------
def _strip_prov(s):
    """去掉任意"X省"限定（仅用于名称比对，不改变归属判断）。"""
    return re.sub(r'^[\u4e00-\u9fff]{1,6}?省', '', s or '').strip() if s else s


def _prov_of(s):
    """提取文本中的省份名（"江西省全省"→"江西省"；"湖南省株洲市"→"湖南省"；
    直辖市"北京市海淀区"→"北京"）。
    "全省/全国"中的"全"不是省份名，返回 None（避免把无省份限定的"全省"误读为省份）。"""
    m = re.search(r'([\u4e00-\u9fff]{1,6}?)省', s or '')
    prov = m.group(1) if m else None
    if prov in ('全', '各'):
        return None
    if prov:
        return prov
    # 直辖市（"北京路"等街道名不触发，只用带"市"的全称）
    for full in ('北京市', '上海市', '天津市', '重庆市'):
        if full in (s or ''):
            return full[0]
    return None


# 江西省 11 个地级市（家庭地区无省份字样时的唯一识别）
JX_CITY_NAMES = ('南昌', '九江', '上饶', '抚州', '宜春', '吉安', '赣州',
                 '景德镇', '萍乡', '新余', '鹰潭')


def _is_jx_region(region, region_names=None):
    """家庭地区文本是否为可唯一识别的江西省内地区（地级市名或图谱区县节点名）。
    城市名必须是"城市名+行政区划后缀"（南昌市/南昌县/新建区等）或完整"江西省南昌"，
    街道/道路名（如"南昌路88号"）不得当作地区匹配。"""
    r = (region or '').strip()
    if not r:
        return False
    for c in JX_CITY_NAMES:
        if c + '市' in r or c + '县' in r or c + '区' in r:
            return True
        if r == c or r == '江西省' + c:
            return True
    names = region_names
    if names is None:
        try:
            _load()
            names = list((_cache.get('region_idx') or {}).keys())
        except Exception:
            names = []
    for name in names:
        if name and (name in r or (len(r) >= 2 and r in name)):
            return True
    return False


def _service_scope_mode():
    """SERVICE_SCOPE=jiangxi（默认）/national；读取失败按江西模式（保守，保持现网）。"""
    try:
        from policy_core.region import service_scope_mode
        return service_scope_mode()
    except Exception:
        return 'jiangxi'


def _national_scope_check(region):
    """全国模式的服务范围判断（T17）：能确定性解析行政区即放行，不再把外省当 blocked。

    只采用全国行政区划层的确定性结论（ambiguous 非空按无法确认处理）；解析不出仍追问，
    文案不得声称"仅支持江西"，避免全国口径下把外省家庭直接拒之门外。
    """
    got = None
    try:
        from policy_core import admin_division
        got = admin_division.canonical(region)
    except Exception:
        got = None
    if got:
        if got.get('ambiguous'):
            return ('ask', '“%s”对应多个行政区，请补充所在省／市／县（区）全称' % region)
        if got.get('national') or got.get('level') in ('province', 'city', 'county'):
            return ('ok', '')
    if any(k in region for k in ('路', '街', '巷', '大道', '号', '大厦', '小区', '村', '镇')):
        return ('ask', '“%s”缺少可核验的市/县（区）行政归属，请补充所在省／市／县（区）全称' % region)
    return ('ask', '无法确认家庭所在地区（%s）的行政归属，请补充所在省／市／县（区）全称' % region)


def service_scope_check(hh, region_names=None):
    """家庭业务范围校验 → ('ok'|'ask'|'blocked', 说明)。

    仅基于 hh['region']（家庭所属地区字段），不扫描全文地名关键词；
    江西家庭成员省外务工/就学（地区字段仍为江西省内）不受影响。
    - blocked：家庭所属地区明确在江西省外，当前系统仅支持江西省范围（仅 SERVICE_SCOPE=jiangxi）。
    - ask   ：地区缺失、街道名缺行政归属、或省市信息矛盾不足以确认，需先追问。
    - ok    ：地区可确定性识别（江西模式=江西省内；全国模式=任一可解析行政区）。
    """
    region = (hh.get('region') or '').strip()
    if not region:
        if _service_scope_mode() == 'national':
            return ('ask', '缺少家庭所在地区，无法判断政策适用于哪些地区，请补充所在省／市／县（区）')
        return ('ask', '缺少家庭所在地区，无法判断是否属于江西省服务范围，请补充所在市/县（区）')
    if _service_scope_mode() == 'national':
        return _national_scope_check(region)
    prov = _prov_of(region)
    if prov:
        if prov != '江西':
            return ('blocked', '当前系统仅支持江西省范围内的帮扶政策匹配（家庭所在地区：%s）' % region)
        # 省份为江西时，检查省与地级市矛盾："江西省长沙市"——城市不在江西11市 → 要求确认
        m = re.search(r'省([\u4e00-\u9fff]{2,6}?市)', region)
        if m:
            city = m.group(1)[:-1]  # 去掉"市"
            if city and not any(city == c or city.startswith(c) for c in JX_CITY_NAMES):
                return ('ask', '省份与地级市信息矛盾（%s），请确认家庭所在市/县（区）' % region)
        return ('ok', '')
    if _is_jx_region(region, region_names):
        return ('ok', '')
    # 街道/道路名（如"南昌路88号"）或无法唯一识别：追问，不默认江西
    if any(k in region for k in ('路', '街', '巷', '大道', '号', '大厦', '小区', '村', '镇')):
        return ('ask', '“%s”缺少可核验的市/县（区）行政归属，请补充所在市/县（区）全称' % region)
    return ('ask', '无法确认家庭所在地区（%s）属于江西省，请补充所在市/县（区）全称' % region)


def match_region_nodes(hh_region, nodes, region_idx=None):
    """家庭地区 → 图谱 REGION 节点（规范化匹配，返回节点 id 列表与命中方式）。
    region_idx 传入时用名称索引（O(109)）；否则退化遍历（兼容外部调用）。"""
    if not hh_region:
        return [], []
    hits, ways = [], []
    r0 = _strip_prov(hh_region).strip()
    if region_idx is not None:
        items = list(region_idx.items())
    else:
        items = [(nid.split(':', 1)[1], nid) for nid, meta in nodes.items()
                 if meta['node_type'] == 'REGION' and nid != 'REGION:江西省全省']
    for name, nid in items:
        if r0 == name:
            hits.append(nid); ways.append('精确')
        elif r0.endswith(name) and len(name) >= 2:
            hits.append(nid); ways.append('后缀')
        elif name.endswith(r0) and len(r0) >= 2:
            hits.append(nid); ways.append('前缀')
    return hits, ways


def _region_apply(scope, hh_nodes, nodes, hh_region, region_idx=None):
    """scope 文本 → 家庭地区判定。
    返回 ('适用'|'不适'|'未知', 说明)。
    解析优先级：空分段过滤 → 全省/全国 → REGION 节点解析（名称/行政层级）→ 双向子串启发式。
    """
    parts = [p.strip() for p in _SPLIT_RE.split(scope or '') if p.strip()]
    if not parts:
        return '未知', 'scope 为空（无地区限制信息）'
    if '全国' in scope:
        return '适用', '全国级适用'
    if '全省' in scope:
        # 关键：带省份限定的"全省"（如"江西省全省"）必须与家庭省份比对，防跨省误判
        prov = _prov_of(scope)
        if prov:
            hp = _prov_of(hh_region)
            if hp and hp == prov:
                return '适用', '省级适用（%s）' % prov
            if hp:
                return '不适', '政策限%s，家庭在%s' % (prov, hp)
            # 家庭地区文本不含"省"字（如"南昌市新建区"）时，用图谱江西 REGION 节点
            # 命中判定家庭在江西——_prov_of 依赖"省"字，不能把江西市县误判为省份未知。
            if hh_nodes:
                return '适用', '省级适用（%s）—家庭地区命中江西省内节点' % prov
            return '未知', '政策限%s，家庭省份未知' % prov
        return '未知', 'scope 含"全省"但未限定省份，需政策省份上下文'
    # 0) 图谱 REGION 节点均为江西省内；家庭省份明确且非江西 → 区县政策不适用（防跨省误判）
    #    仅当家庭文本能提取出省份且明确非江西省时触发；家庭省份缺失/为江西不拦截，
    #    全国/全省分支已在前方处理，不影响其语义。
    hp = _prov_of(hh_region)
    if hp and hp != '江西':
        return '不适', '政策地区节点均为江西省内，家庭在%s' % hp
    # 1) REGION 节点解析（权威名称清单；region_idx 加速）
    matched, unmatched = [], []
    if region_idx is not None:
        items = list(region_idx.items())
    else:
        items = [(nid.split(':', 1)[1], nid) for nid, meta in nodes.items()
                 if meta['node_type'] == 'REGION' and nid != 'REGION:江西省全省']
    for p in parts:
        np = _strip_prov(p).strip()
        hit = None
        for name, nid in items:
            if np == name or (np.endswith(name) and len(name) >= 2) or (name.endswith(np) and len(np) >= 2):
                hit = nid
                break
        if hit:
            matched.append(hit)
        else:
            unmatched.append(p)
    # 2) 节点关系：家庭地区节点 ∈ 政策地区节点（或互含，行政层级）
    for nid in matched:
        if nid in hh_nodes:
            return '适用', '节点匹配 %s' % nid
    # 3) 启发式后备（标注'启发式'，不作为最终资格依据）
    r0 = _strip_prov(hh_region).strip()
    for p in parts:
        pp = _strip_prov(p).strip()
        if pp and (r0 in pp or pp in r0):
            return '适用', '启发式子串 %s' % pp
    if matched:
        return '不适', '地区节点均不含家庭地区'
    return '不适', '无匹配地区（%s）' % '、'.join(unmatched[:3])


_LEGACY_DEFAULT_REGIONS = {'C': '全国', 'S': '江西省全省'}


def _policy_default_regions():
    """无显式地区政策的默认口径：旧链路保留历史江西默认；全国化模式只默认国家级"全国"。

    现网默认（REGION_NATIONWIDE=0 且 SERVICE_SCOPE=jiangxi）必须与历史行为完全一致，
    所以旧链路显式回落历史默认；全国化模式下去掉省级"江西省全省"默认（M10：否则全国库
    里的省级政策会被静默当成江西政策），S 级空地区当作信息缺失 → 待细化保留。
    """
    try:
        from policy_core.region import nationwide_enabled
        if nationwide_enabled():
            return {'C': '全国'}
    except Exception:
        pass
    return _LEGACY_DEFAULT_REGIONS


def _program_region_uncertain(policy, detail, hh_region):
    """REGION_SOFT_FILTER 开启且程序层无法确定性裁决（None）→ 旧层不得硬判"不适"丢弃（M3）。"""
    try:
        from policy_core import region as region_semantics
        if not region_semantics.soft_filter_enabled():
            return False
        return region_semantics.policy_region_verdict(policy, detail, hh_region) is None
    except Exception:
        return False


def _judge_legacy(policy, detail, hh_region, nodes, hh_nodes=None, region_idx=None):
    """联合地区判断旧实现 → ('适用'|'不适'|'冲突'|'地区待细化', 说明)
    主表与明细任一方向矛盾均记'冲突'；明细地区为空区分'明确继承'与'信息缺失'。
    """
    if hh_nodes is None:
        hh_nodes, _ = match_region_nodes(hh_region or '', nodes, region_idx)
    if not hh_region:
        return '地区待细化', '家庭地区未知'
    scope = (policy.get('regions') or '').strip()
    inherit = None
    if not scope:
        scope = _policy_default_regions().get(policy.get('level'), '')
        inherit = '默认规则' if scope else None
    p_ok, p_note = _region_apply(scope, hh_nodes, nodes, hh_region, region_idx)
    if p_ok == '未知' and inherit is None:
        return '地区待细化', '政策级地区判定未知（%s）且无默认规则，信息缺失（不默认无地区限制）' % p_note
    p_label = '政策级(%s)' % ('默认规则' if inherit else '原文')
    d_region = ((detail or {}).get('region') or '').strip()
    if not d_region:
        # 明细未限定地区：区分明确继承（政策级判定明确）与信息缺失（政策级也无法判定）
        if p_ok == '适用':
            return '适用', '明细未限定地区，明确继承 %s' % p_note
        if p_ok == '不适':
            return '不适', '明细未限定地区，继承 %s' % p_note
        return '地区待细化', '政策级地区无法判定且明细无地区（信息缺失）'
    d_ok, d_note = _region_apply(d_region, hh_nodes, nodes, hh_region, region_idx)
    if d_ok == '未知':
        return '地区待细化', '明细级地区无法解析：%s' % (d_region[:24])
    if p_ok == '适用' and d_ok == '适用':
        return '适用', '明细级地区：%s' % d_note
    if p_ok == '适用' and d_ok == '不适':
        return '冲突', '%s适用，但明细级限定地区不含家庭地区（%s）' % (p_label, d_note)
    if p_ok == '不适' and d_ok == '适用':
        return '冲突', '%s不适，但明细级地区含家庭地区（%s）' % (p_label, d_note)
    if p_ok == '不适' and d_ok == '不适':
        return '不适', '政策级与明细级均不含家庭地区'
    # p_ok 未知（信息缺失）且明细不适/适用 → 以明细为准，标注信息缺失
    if d_ok == '不适':
        return '不适', '政策级信息缺失，明细级不含家庭地区'
    return '冲突', '政策级信息缺失，明细级判定（%s）与政策级不一致' % d_note


def region_judge(policy, detail, hh_region, nodes, hh_nodes=None, region_idx=None):
    """联合地区判断 → ('适用'|'不适'|'冲突'|'地区待细化', 说明)。

    M3（T17）：REGION_SOFT_FILTER 开启时，程序层（region.policy_region_verdict）无法
    确定性裁决的候选不再被旧图谱逻辑硬判"不适"丢弃，降级为"地区待细化"保留待核；
    REGION_SOFT_FILTER=0 时严格旧行为（江西词表/图谱节点原判定）。
    """
    status, note = _judge_legacy(policy, detail, hh_region, nodes, hh_nodes, region_idx)
    if status == '不适' and _program_region_uncertain(policy, detail, hh_region):
        return '地区待细化', '程序层地区关系无法确定，保留候选待核（%s）' % note
    return status, note


# ---------------- 三态条件核验 ----------------
def _tri(v_det, v_member):
    """三态：('满足'|'不满足'|'未知'|'无限制', 依据值)"""
    if not v_det or str(v_det).strip() in ('无限制', '', '不限'):
        return ('无限制', None)
    if v_member is None or v_member == '':
        return ('未知', v_det)
    return ('满足' if str(v_member).strip() in [x.strip() for x in str(v_det).split(',')] else '不满足', v_det)


def _tri_age(det_age, member_age):
    if not det_age or str(det_age).strip() in ('无限制', '', '不限'):
        return ('无限制', None)
    m = re.match(r'^\s*(\d+)\s*[-~—]\s*(\d+)\s*$', str(det_age))
    if not m:
        return ('未知', det_age)          # 非区间格式，保留原文待核实
    lo, hi = int(m.group(1)), int(m.group(2))
    if not isinstance(member_age, int):
        return ('未知', det_age)
    return ('满足' if lo <= member_age <= hi else '不满足', det_age)


def _count_risk_terms(w):
    """单词中出现的风险词条数（长词优先，去除重叠），>1 表示复合风险无法确定逻辑"""
    terms = sorted(_RISK_TERMS, key=len, reverse=True)
    rest = w
    cnt = 0
    for t in terms:
        if t in rest:
            cnt += 1
            rest = rest.replace(t, '', 1)
    return cnt


def _tri_risk(pov, risks):
    """pov（风险类型要求）与家庭风险集合的核验（集合语义，不拼字符串）。
    单词→包含即满足；多词带'或'→任一；多词带'且/和/、/与'→全部；
    无分隔符复合风险（如'因病因学'）→无法确定逻辑→未知（不猜）；无连词多词→未知。"""
    from kg_forbid_v2 import _risk_match
    if not pov or str(pov).strip() in ('无限制', '', '不限'):
        return ('无限制', None)
    ok, note = _risk_match(str(pov), risks)
    return ('未知' if ok == 'unknown' else ('满足' if ok else '不满足'), note)


def _obj_pending(target):
    """对象资格是否待核实：target 非空且含非宽泛对象词 → True。
    仅含宽泛对象（所有农户/一般农户/家庭/帮扶对象，与图谱 _WIDE_OBJECT 口径一致）
    视为无对象限制；含具体对象词（脱贫人口/低保/特困/残疾等）→ 待核实，不凭关键词猜测资格。"""
    t = (target or '').strip()
    if not t:
        return False
    words = [w.strip() for w in _SPLIT_RE.split(t) if w.strip()]
    if not words:
        return False
    return any(w not in ('所有农户', '一般农户', '家庭', '帮扶对象', '无限制', '不限') for w in words)


def cond_tri_state(det, member, hh):
    """对单个明细×成员做三态核验（lab/health/age/risk 集合）。
    返回 (状态列表, verified_pass, unknown_count, obj_pending, unparsed)
      verified_pass = 全部结构化条件明确满足（无'不满足'且无'未知'）——不再以"没有失败"充当满足；
      obj_pending   = detail.target 含非宽泛对象词（对象原文未结构化核验）；
      unparsed      = conditions 有原文但四维结构化条件全空（必要条件未解析）。
    """
    f = member.get('fields', {})
    out = []
    lab, hea, age, pov = det.get('lab'), det.get('health'), det.get('age'), det.get('pov')
    for field, det_v, mem_v in (('劳动能力', lab, f.get('labor')),
                                ('健康状况', hea, f.get('health'))):
        st, ev = _tri(det_v, mem_v)
        out.append({'field': field, 'det': det_v or '', 'member': mem_v or '', 'status': st, 'evidence': ev})
    st, ev = _tri_age(age, f.get('age'))
    out.append({'field': '年龄', 'det': age or '', 'member': f.get('age', ''), 'status': st, 'evidence': ev})
    # 风险类型：家庭级共同条件，按风险集合核验
    st, ev = _tri_risk(pov, hh.get('risk') or [])
    out.append({'field': '风险类型', 'det': pov or '', 'member': ','.join(hh.get('risk') or []), 'status': st, 'evidence': ev})
    unknown = sum(1 for x in out if x['status'] == '未知')
    verified_pass = all(x['status'] in ('满足', '无限制') for x in out)
    obj_pending = _obj_pending(det.get('target'))
    # Structural fields alone never prove that the remaining original sentence was parsed.
    unparsed = bool((det.get('conditions') or '').strip()) and str(det['conditions']).strip() not in ('无限制','不限','无')
    return out, verified_pass, unknown, obj_pending, unparsed


def _eligible(states, obj_pending, unparsed, region_flag):
    """明细×成员 资格状态：'通过'|'待核实(原因)'|'不通过'。
    未核验的对象/必要条件/未知条件一律不得计为'通过'。"""
    if region_flag == '冲突':
        return '待核实(地区冲突)'
    if obj_pending:
        return '待核实(对象原文)'
    if unparsed:
        return '待核实(条件原文)'
    if any(s['status'] == '不满足' for s in states):
        return '不通过'
    if any(s['status'] == '未知' for s in states):
        return '待核实(条件未知)'
    return '通过'


# ---------------- forbid 标注（只读，不改规则） ----------------
def forbid_flags(policy, hh, checks):
    """候选政策被哪些风险 forbid（按 rule_check.forbid → 分类/名称匹配）。
    标注语义='该风险下不推荐'，不代表'家庭绝对禁止'；执行一致性由评估单独验证，
    业务合理性需人工确认（不以本注释为业务语义依据）。"""
    flags = []
    for risk in hh.get('risk') or []:
        ck = smk._risk_check(checks, risk)
        if not ck or not ck['forbid'] or ck['forbid'] == '无限制':
            continue
        parts = [re.sub(r'^A\d+', '', f.strip()) for f in str(ck['forbid']).replace('□', ' ').split()]
        for f2 in parts:
            if f2 and (f2 in (policy.get('cat1') or '') or f2 in (policy.get('name') or '')):
                flags.append({'risk': risk, 'forbid': f2, 'rule': ck['forbid']})
    return flags


# ---------------- 图谱属性命中 ----------------
def _attr_hits(hh, nodes, edges):
    """返回 {code: [('IN_REGION'|'APPLIES_TO'|'REQUIRES'|'COVERS', node_id, evidence, weight)]}"""
    out = {}
    region = hh.get('region')
    reg_nodes, _ = match_region_nodes(region, nodes)
    for nid in reg_nodes:                      # 强：县区级地区
        for src, ev, _sf in edges['IN_REGION'].get(nid, []):
            out.setdefault(src[7:], []).append(('IN_REGION', nid, ev, 10.0))
    # 对象身份：低保/特困/残疾（强）
    keys = []
    for mb in hh.get('members') or []:
        f = mb.get('fields', {})
        if f.get('dibao') == '1' or '低保' in ' '.join(hh.get('enjoyed') or []):
            keys.append('低保')
        if f.get('tegang') == '1' or '特困' in ' '.join(hh.get('enjoyed') or []):
            keys.append('特困')
        if f.get('disability') or '残疾' in ' '.join(hh.get('enjoyed') or []):
            keys.append('残疾')
    for k in set(keys):
        for nid in nodes:
            if nodes[nid]['node_type'] == 'POPULATION' and k in nid and not any(w in nid for w in _WIDE_OBJECT):
                for src, ev, _sf in edges['APPLIES_TO'].get(nid, []):
                    out.setdefault(src[7:], []).append(('APPLIES_TO', nid, ev, 10.0))
    # 成员条件：劳动能力/健康状况/窄年龄（强；宽年龄不参与）
    for mb in hh.get('members') or []:
        f = mb.get('fields', {})
        lab = f.get('labor')
        if lab:
            nid = _LAB_COND + lab
            if nid in nodes:
                for src, ev, _sf in edges['REQUIRES'].get(nid, []):
                    out.setdefault(src[7:], []).append(('REQUIRES', nid, ev, 10.0))
        he = f.get('health')
        if he and he != '健康':
            nid = _HEALTH_COND + he
            if nid in nodes:
                for src, ev, _sf in edges['REQUIRES'].get(nid, []):
                    out.setdefault(src[7:], []).append(('REQUIRES', nid, ev, 10.0))
        age = f.get('age')
        if isinstance(age, int):
            for nid in nodes:
                m = _AGE_RE.match(nid)
                if m and not _WIDE_AGE(int(m.group(1)), int(m.group(2))) \
                        and int(m.group(1)) <= age <= int(m.group(2)):
                    for src, ev, _sf in edges['REQUIRES'].get(nid, []):
                        out.setdefault(src[7:], []).append(('REQUIRES', nid, ev, 10.0))
    # 风险（弱）
    for risk in hh.get('risk') or []:
        seen = set()
        for label in sorted(risk_labels(risk)):
            nid = 'RISK:' + label
            for src, ev, _sf in edges['COVERS'].get(nid, []):
                if src not in seen:
                    seen.add(src)
                    out.setdefault(src[7:], []).append(('COVERS', nid, ev, 2.0))
    return out


# ---------------- 主流程 ----------------
def kg_search(hh, top_k=30, sim_min_w=0.75, expand=True, data=None):
    """图谱检索主流程（第三轮口径）。
    评分公式：
      score = min(attr_score, 30) + 2.0 * dedup_hit_count - 0.5 * unknown_count
             - 5.0(若地区冲突) + 0.2 * 成员覆盖数
      dedup_hit_count = 去重后的"分支×成员 完整资格通过"数（重复明细分支归一后去重，
                        避免'明细多/分支重复'主导排名）；
      unknown_count  = 全部'未知'条件数（未知≠满足，降权保留）；
      attr_score     = Σ 属性命中权重（IN_REGION/APPLIES_TO/REQUIRES=10，COVERS=2，
                        SAME_DOC=1.0，SIM_ATTR=weight≥sim_min_w），封顶 30。
    资格口径（每 成员×明细）：
      '通过' = 地区适用 + 结构化条件全部满足且无未知 + 无对象原文待核 + 无条件原文未解析；
      '待核实(…)' = 存在未知条件 / 对象原文 / 条件原文 / 地区冲突；
      '不通过' = 存在明确不满足。
    返回：candidates（含 policy_code/member_no/detail_id/原文/三态/资格/forbid/排序证据）、
          missing_info（按成员+字段聚合，保留全部明细依据）、region_conflicts、stats（新口径）。
    """
    nodes, edges, same_doc, sim = _load()[0]
    # Graph topology is cached; source status/date/details are refreshed for every request.
    data = smk.load_db() if data is None else data
    region_idx = _cache['region_idx']
    policies = {p['code']: p for p in data['policies']}
    outdated = {code for code,p in policies.items()
                if nodes.get('POLICY:'+code,{}).get('policy_version') is not None
                and nodes['POLICY:'+code]['policy_version'] != p.get('version')}
    checks = data['checks']
    today = date.today().isoformat()
    t0 = time.time()

    # 1) 图谱属性命中（强+弱）
    attr = _attr_hits(hh, nodes, edges)
    attr = {code:paths for code,paths in attr.items()
            if code in policies and policies[code].get('status')=='在用' and code not in outdated}
    # 2) 有限扩展（SAME_DOC / SIM_ATTR，仅从已命中政策出发，1 跳；邻居已确定性排序）
    if expand:
        for code in list(attr.keys()):
            pid = 'POLICY:' + code
            # Prefer the same business category; policy code order must not decide relevance.
            doc_neighbors = sorted(same_doc.get(pid, []), key=lambda nid: (
                policies.get(nid[7:], {}).get('cat1') != policies.get(code, {}).get('cat1'),
                -sum(w for _et, _nid, _ev, w in attr.get(nid[7:], [])), nid))
            for nb in doc_neighbors[:10]:
                if nb[7:] not in attr and nb[7:] not in outdated:
                    attr.setdefault(nb[7:], []).append(('SAME_DOC', pid, '同文件', 1.0))
            for nb, w in sim.get(pid, [])[:10]:          # SIM 已按 weight 降序
                if w >= sim_min_w and nb[7:] not in attr and nb[7:] not in outdated:
                    attr.setdefault(nb[7:], []).append(('SIM_ATTR', pid, '相似 %.2f' % w, w))

    # 3) 状态/有效期/地区前置过滤 + 明细核验（顺序无关聚合）
    cands, region_rejected = [], []
    hh_nodes, _ = match_region_nodes(hh.get('region') or '', nodes, region_idx)
    for code, paths in attr.items():
        p = policies.get(code)
        if not p or p['status'] != '在用' or p['level'] not in ('C', 'S', 'U', 'X'):
            continue
        if p.get('effect_date') and p['effect_date'] > today:
            continue
        if p.get('expire_date') and p['expire_date'] < today:
            continue
        details = data.get('by_policy', {}).get(p['pid'], [])
        members = []
        detail_results = []                # 每条明细独立结果（顺序无关）
        region_flags = {'适用': 0, '冲突': 0, '不适': 0, '地区待细化': 0}
        for det in details:
            rj, rnote = region_judge(p, det, hh.get('region'), nodes, hh_nodes, region_idx)
            region_flags[rj] = region_flags.get(rj, 0) + 1
            if rj not in ('适用', '冲突'):
                continue                   # 不适/待细化分支跳过核验（冲突保留待核）
            for mb in hh.get('members') or []:
                states, vpass, unk, obj_pend, unparsed = cond_tri_state(det, mb, hh)
                from kg_forbid_v2 import verify_member_detail
                verified = verify_member_detail(p, det, mb.get('member_no'), hh.get('members', []), hh, hh.get('enjoyed', []))
                elig = verified['verdict']
                # R3：明确不适用（对象冲突/地区分支不适/有效期满等）的成员×明细不再展开，
                #     从源头收敛图谱候选量，避免后续融合层重复核验与"全堆待核实"
                if elig == '不适用':
                    continue
                members.append({
                    'member_no': mb.get('member_no'), 'member_name': mb.get('name') or '',
                    'detail_id': det.get('detail_id'), 'detail_region': det.get('region') or '',
                    'region_flag': rj, 'region_note': rnote,
                    'detail_conditions': det.get('conditions') or '',
                    'target': det.get('target') or '',
                    'benefit': det.get('benefit') or '',
                    'states': states,
                    'verified_pass': vpass, 'unknown_count': unk,
                    'obj_pending': obj_pend, 'unparsed': unparsed,
                    'eligible': elig,
                    'verification': verified,
                    'matched': [s['field'] for s in states if s['status'] == '满足'],
                    'unknown': [s['field'] for s in states if s['status'] == '未知'],
                    'not_matched': [s['field'] for s in states if s['status'] == '不满足'],
                })
                detail_results.append({'detail_id': det.get('detail_id'), 'rj': rj, 'eligible': elig})
        # 政策级地区聚合（不依赖遍历顺序）：有适用分支→适用；仅冲突→冲突；全不适→不适
        if not details:
            region_info = ('待核实', '无可用明细，不能认定资格，资料待核实')
        elif region_flags['适用'] > 0:
            region_info = ('适用', '存在适用明细分支（冲突 %d 条保留待核）' % region_flags['冲突'])
        elif region_flags['冲突'] > 0:
            region_info = ('冲突', '无适用分支，仅冲突 %d 条（待核实）' % region_flags['冲突'])
        else:
            region_info = ('不适', '全部明细不适（%s）' % '、'.join('%s:%d' % (k, v) for k, v in region_flags.items() if v))
        if region_info[0] == '不适':
            region_rejected.append({'policy_code': code, 'policy_name': p['name'],
                                    'attr_score': min(sum(w for _, _, _, w in paths), 30.0),
                                    'note': region_info[1]})
            continue
        # 去重分支命中：按 (归一分支, 成员) 去重，避免重复明细分支主导排名
        dedup_hits, seen_hits = 0, set()
        for m in members:
            if m['eligible'] == '通过':
                key = (m['detail_id'], m['member_no'])
                if key in seen_hits:
                    continue
                seen_hits.add(key)
                dedup_hits += 1
        attr_score = min(sum(w for _, _, _, w in paths), 30.0)
        member_cov = len({m['member_no'] for m in members if m['eligible'] == '通过'})
        unknown_count = sum(m['unknown_count'] for m in members)
        region_conflict = region_info[0] == '冲突'
        score = attr_score + 2.0 * dedup_hits - 0.5 * unknown_count \
            - (5.0 if region_conflict else 0.0) + 0.2 * member_cov
        cands.append({
            'policy_code': code, 'policy_name': p['name'], 'level': p['level'],
            'cat1': p['cat1'], 'cat2': p['cat2'], 'attr': p['attr'],
            'kg_paths': paths, 'attr_score': round(attr_score, 2),
            'region_judge': region_info[0], 'region_note': region_info[1],
            'region_conflict': region_conflict, 'branch_count': len(details),
            'members': members, 'dedup_hit_count': dedup_hits,
            'unknown_count': unknown_count,
            'forbid': forbid_flags(p, hh, checks),
            'score': round(score, 2),
            'evidence': [x for _, _, x, _ in paths if x][:3],
        })

    # 来源不清 forbid 不参与排序；仅按地区、成员与明细核验证据排序。
    #    无成员（资料不足）场景：风险相关（COVERS 弱命中）优先于纯地区命中
    region_order = {'适用': 0, '冲突': 1, '待核实': 2}
    no_member = not hh.get('members')
    cands.sort(key=lambda c: (region_order.get(c['region_judge'], 3),
                              0 if (no_member and any(t == 'COVERS' for t, _, _, _ in c['kg_paths'])) else 1,
                              -c['score'], -c['attr_score'], -c['dedup_hit_count']))
    # 5) 资料补充清单：按 (成员, 字段, 政策) 聚合，保留全部明细原文依据
    missing_info = []
    seen = set()
    if no_member:
        missing_info.append({'policy_code': None, 'detail_id': None, 'member_no': None,
                             'field': '家庭成员信息',
                             'reason': '当前仅地区+风险，无法对任何明细做条件核验；需补充成员（人数、年龄、劳动能力、健康状况、参保情况）'})
    for c in cands[:top_k * 3]:
        if c['region_judge'] in ('地区待细化', '待核实'):
            key = ('地区', c['policy_code'])
            if key not in seen:
                seen.add(key)
                missing_info.append({'policy_code': c['policy_code'], 'detail_id': None, 'member_no': None,
                                    'field': '家庭地区', 'reason': c['region_note']})
        for m in c['members']:
            if m['eligible'] == '待核实(地区冲突)':
                key = ('地区冲突', c['policy_code'], m['detail_id'])
                if key not in seen:
                    seen.add(key)
                    missing_info.append({'policy_code': c['policy_code'], 'detail_id': m['detail_id'],
                                         'member_no': m['member_no'], 'field': '地区冲突',
                                         'reason': m['region_note']})
            if m['obj_pending']:
                key = ('对象', c['policy_code'], m['detail_id'])
                if key not in seen:
                    seen.add(key)
                    missing_info.append({'policy_code': c['policy_code'], 'detail_id': m['detail_id'],
                                         'member_no': m['member_no'], 'field': '对象资格',
                                         'reason': '对象为原文文本未核验：%s' % (m['target'][:40])})
            if m['unparsed']:
                key = ('条件原文', c['policy_code'], m['detail_id'])
                if key not in seen:
                    seen.add(key)
                    missing_info.append({'policy_code': c['policy_code'], 'detail_id': m['detail_id'],
                                         'member_no': m['member_no'], 'field': '条件原文',
                                         'reason': '必要条件未结构化，原文：%s' % (m['detail_conditions'][:40])})
            for f in m['unknown']:
                key = (f, c['policy_code'], m['member_no'], m['detail_id'])
                if key in seen:
                    continue
                seen.add(key)
                missing_info.append({'policy_code': c['policy_code'], 'detail_id': m['detail_id'],
                                     'member_no': m['member_no'], 'field': f,
                                     'reason': '明细要求%s（%s），家庭未提供' % (f, m['detail_conditions'][:30] or '原文条件')})
    # 6) 汇总（新口径）
    def _cnt(pred):
        return sum(1 for c in cands if pred(c))
    stats = {
        'attr_candidates': len(attr), 'after_filter': len(cands),
        'region_rejected': len(region_rejected),
        'region_pass': _cnt(lambda c: c['region_judge'] == '适用'),
        'region_conflict': _cnt(lambda c: c['region_conflict']),
        'struct_pass': sum(1 for c in cands for m in c['members'] if m['verified_pass']),
        'eligible_pass': sum(1 for c in cands for m in c['members'] if m['eligible'] == '通过'),
        'obj_pending': sum(1 for c in cands for m in c['members'] if m['obj_pending']),
        'unparsed': sum(1 for c in cands for m in c['members'] if m['unparsed']),
        'unknown': sum(1 for c in cands for m in c['members'] if m['unknown_count'] > 0),
        'forbid_flagged': _cnt(lambda c: bool(c['forbid'])),
        'elapsed': round(time.time() - t0, 2),
    }
    return {'candidates': cands, 'missing_info': missing_info,
            'region_conflicts': [c['policy_code'] for c in cands if c['region_conflict']],
            'region_rejected': region_rejected[:100],
            'stats': stats}


def format_candidate(c, verbose=True):
    """候选的可读展示（评估/报告用）"""
    forbid = ';'.join('%s→禁%s' % (f['risk'], f['forbid']) for f in c['forbid'])
    lines = ['%s %s [%s/%s] 地区=%s 分数=%s attr=%s 分支=%s 通过=%s 未知=%s%s' % (
        c['policy_code'], (c['policy_name'] or '')[:18], c['level'], c['cat1'],
        c['region_judge'], c['score'], c['attr_score'], c['branch_count'],
        c['dedup_hit_count'], c['unknown_count'], (' forbid=' + forbid) if forbid else '')]
    if verbose:
        lines.append('    图谱路径: ' + '; '.join('%s(%s)' % (t, n.split(':')[-1][:22]) for t, n, _, _ in c['kg_paths'][:6]))
        for m in c['members'][:5]:
            st = '/'.join('%s:%s' % (s['field'], s['status']) for s in m['states'])
            lines.append('    成员%s×明细%s[%s] %s → %s' % (
                m['member_no'], m['detail_id'], m['detail_region'] or '继承', st, m['eligible']))
    return '\n'.join(lines)
