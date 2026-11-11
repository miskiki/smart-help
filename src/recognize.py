# -*- coding: utf-8 -*-
"""政策识别服务：PDF → 文本 → LLM 字段抽取 → 文号/相似度匹配判定 → 结构化结果。
- 模型 API 从 config/model_settings.yaml 读取（百炼平台，OpenAI 兼容），key 运行时读取不写死
- 匹配判定：文号精确命中 > 名称相似度 > 新增；识别到废止/失效 → 停用
- 独立运行：python recognize.py <pdf路径>；供 /api/upload 调用 recognize_pdf()
"""
import json
import re
import os
import difflib
from pathlib import Path
import requests

import yaml
from sqlalchemy import create_engine, text

import config  # 触发 Kingbase 兼容补丁 + 解析 DATABASE_URL

MODEL_YAML = os.environ.get('MODEL_SETTINGS_PATH', str(Path(__file__).resolve().parents[1] / 'config' / 'model_settings.yaml'))

# 一级分类（命名规则.md，15 类）
CATEGORIES = ['综合保障', '产业帮扶', '健康帮扶', '就业帮扶', '其他教育帮扶',
              '义务教育保障', '因灾救助', '公益岗位帮扶', '住房安全保障', '金融帮扶',
              '生产生活条件改善', '饮水安全保障', '社会帮扶', '基础设施建设', '搬迁']



# 四级体系层级编码 → 中文（C=国家 / S=省级 / U=市级 / X=县级；识别/展示共用）
LEVEL_CN = {'C': '国家', 'S': '省级', 'U': '市级', 'X': '县级'}

# 江西 11 设区市（级别识别用）
JX_CITIES = ['南昌市', '九江市', '上饶市', '抚州市', '宜春市', '吉安市', '赣州市',
             '景德镇市', '萍乡市', '新余市', '鹰潭市']

# 江西地区词库（县/区/县级市/管委会/开发区/管理局，含库内 108 个实际值 + 补齐常见市区）
REGION_LEXICON = [
    '万安县', '万年县', '万载县', '三清山管委会', '上栗县', '上犹县', '上高县', '东临新区',
    '东乡区', '东湖区', '丰城市', '临川区', '乐安县', '乐平市', '于都县', '井冈山市',
    '仙女湖区', '会昌县', '余干县', '余江区', '信丰县', '信州区', '信江新区管委会',
    '修水县', '全南县', '共青城市', '兴国县', '分宜县', '南丰县', '南城县', '南康区',
    '南昌县', '南昌经济技术开发区', '吉安县', '吉州区', '吉水县', '大余县', '奉新县',
    '婺源县', '宁都县', '安义县', '安源区', '安福县', '安远县', '定南县', '宜丰县',
    '宜黄县', '寻乌县', '峡江县', '崇义县', '崇仁县', '广丰区', '广信区', '广昌县',
    '庐山市', '弋阳县', '彭泽县', '德兴市', '德安县', '抚州高新区', '新干县', '新建区',
    '昌江区', '月湖区', '柴桑区', '樟树市', '横峰县', '武功山风景名胜区', '武宁县',
    '永丰县', '永修县', '永新县', '泰和县', '浮梁县', '渝水区', '湖口县', '湘东区',
    '湾里管理局', '濂溪区', '玉山县', '珠山区', '瑞昌市', '瑞金市', '石城县', '章贡区',
    '红谷滩区', '芦溪县', '莲花县', '萍乡经济技术开发区', '蓉江新区', '袁州区', '贵溪市',
    '资溪县', '赣县区', '赣州经济技术开发区', '进贤县', '遂川县', '都昌县', '鄱阳县',
    '金溪县', '铅山县', '铜鼓县', '青原区', '靖安县', '高安市', '鹰潭高新技术产业开发区',
    '黎川县', '龙南市', '龙虎山管委会', '西湖区', '青云谱区', '青山湖区', '浔阳区',
]
_REGION_SORTED = sorted(REGION_LEXICON, key=len, reverse=True)   # 长名优先，防“赣州”吞“赣州经济技术开发区”


def detect_regions(text, filename=None):
    """地区词库确定性识别：扫描全文+文件名，返回去重地区列表（长名优先）"""
    t = (text or '') + (' ' + (filename or ''))
    found = []
    for name in _REGION_SORTED:
        if name in t and name not in found:
            found.append(name)
    return found


def detect_level(text, filename=None):
    """级别规则识别：发文机关/文号代字 → C/S/U/X；无信号返回 (None, '')。
    顺序：国家 > 省级 > 市级 > 县级（长信号优先，避免“江西省人民政府”误判县级）"""
    t = (text or '') + (' ' + (filename or ''))
    if re.search(r'国务院|国发〔|国办发|国函〔|国发改|国财社|国教督', t):
        return 'C', '文号/发文机关含“国务院/国发”等国家信号'
    if re.search(r'江西省人民政府|赣府|赣政办|赣发〔|赣人社|赣财|赣教|赣医保|赣农|赣乡振|赣建', t):
        return 'S', '文号/发文机关含省级信号'
    for city in JX_CITIES:
        if (city + '人民政府') in t or (city + '政府') in t:
            return 'U', '含“%s人民政府”' % city
    if re.search(r'[\u4e00-\u9fa5]{1,8}(县|区)人民政府', t):
        return 'X', '含县级/区级人民政府'
    return None, ''
# 完整文号模式：机关代字 + 括号年份 + 序号 + 号（如：赣人社发〔2019〕3号 / 赣财资环[2019]4号）
_DOC_FULL_PAT = re.compile(r'[\u4e00-\u9fa5A-Za-z]+[〔\[(（【][0-9]{4}[〕\]）)】]\s*\d+\s*号')


def norm_doc(s):
    """文号归一化：全角括号统一为半角、去空白，用于跨全/半角比较"""
    s = re.sub(r'[〔【［(（]', '(', s)
    s = re.sub(r'[〕】］)）]', ')', s)
    return re.sub(r'\s+', '', s)


def extract_doc_nums(text):
    """提取全文中的完整文号（含机关代字），归一化后返回集合"""
    return set(norm_doc(m.group(0)) for m in _DOC_FULL_PAT.finditer(text or ''))


def load_model_cfg(platform=None):
    """读取 yaml 中指定平台配置；平台取参：参数 > 环境变量 RECOGNIZE_PLATFORM > 默认 bailian。
    各平台仅需 LLM 即可识别（embedding/rerank 缺失时自动降级 difflib）。
    返回 dict 含 profile 能力参数：embed_dim / rerank_endpoint / quality_tier（缺省兼容旧配置）"""
    platform = platform or os.environ.get('RECOGNIZE_PLATFORM', '') or 'bailian'
    with open(MODEL_YAML, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    for plat in data.get('MODEL_PLATFORMS', []):
        if plat.get('platform_name') == platform:
            profile = plat.get('profile') or {}
            return {
                'platform': platform,
                'base_url': plat['api_base_url'].rstrip('/'),
                'api_key': plat.get('api_key', ''),
                'llm_model': (plat.get('llm_models') or [None])[0],
                'embed_model': (plat.get('embed_models') or [''])[0],
                'rerank_model': (plat.get('rerank_models') or [''])[0],
                'embed_dim': int(profile.get('embed_dim') or 1536),
                'rerank_endpoint': (profile.get('rerank_endpoint') or '').strip(),
                'quality_tier': profile.get('quality_tier') or 'standard',
                'embed_query_instruction': (profile.get('embed_query_instruction') or '').strip(),
                'json_mode': profile.get('json_mode'),
            }
    raise RuntimeError('model_settings.yaml 中未找到平台 %s（可选：%s）'
                       % (platform, '、'.join(x.get('platform_name', '?') for x in data.get('MODEL_PLATFORMS', []))))


def extract_pdf_text(path):
    """PyMuPDF 提取全文（多页合并，段落分隔保留）"""
    import fitz
    doc = fitz.open(path)
    parts = []
    for page in doc:
        parts.append(page.get_text('text'))
    doc.close()
    return '\n'.join(parts)


# ---------------- LLM 字段抽取 ----------------
_PROMPT = """你是政策台账信息提取助手。请从政策文件全文提取字段，只输出 JSON，不要任何其他内容：
{
  "policy_name": "政策名称（去掉文号，简洁准确）",
  "doc_ref": "主文号，如：莲乡振字〔2026〕18号",
  "level": "C 或 S 或 U 或 X（国家级填C，省级填S，市级填U，县级/区级填X；识别不出填空串）",
  "category1": "一级分类，只能从以下15类中选一个最合适的：综合保障、产业帮扶、健康帮扶、就业帮扶、其他教育帮扶、义务教育保障、因灾救助、公益岗位帮扶、住房安全保障、金融帮扶、生产生活条件改善、饮水安全保障、社会帮扶、基础设施建设、搬迁",
  "regions": "适用地区（县级填县名，省级填全省）",
  "status": "在用 或 停用（仅当本文件本身被废止/失效或为明确废止通知才填停用；引用旧文件或替代旧规定不代表本新文件停用）",
  "is_revision": true 或 false（是否修订/更新已有政策，如正文提到修订、代替、同时废止原办法等）",
  "referenced_doc_refs": ["正文中引用到的其他文号列表"],
  "superseded_doc_refs": ["明确被本文件替代或废止的文号；普通引用不要填写"],
  "target": "适用对象原文", "conditions": "申请条件原文", "benefit": "补贴标准原文", "process": "办理流程原文", "contact": "经办部门/联系方式原文",
  "effect_date": "生效日期 YYYY-MM-DD，没有填空", "expire_date": "失效日期 YYYY-MM-DD，没有填空",
  "details": [{"region":"适用地区", "target":"适用对象原文", "conditions":"申请条件原文", "benefit":"补贴标准原文", "currency":"币种/金额单位原文，如元，没有填空", "unit":"补贴计量单位原文，如人或亩，没有填空", "cycle":"发放周期原文，如月或一次性，没有填空", "process":"流程原文", "contact":"联系部门", "phone":"电话", "lab":"劳动能力条件，没有填空", "health":"健康条件，没有填空", "age":"年龄范围如18-60，没有填空", "pov":"风险条件，没有填空"}],
  "reason": "简短判定说明（含层级与分类判定依据）"
}"""


_PROMPT_LITE = """你是政策信息提取助手。从政策文件中提取3个字段，只输出JSON，不要任何其他内容：
{"policy_name": "政策名称（去掉文号，简洁准确）", "doc_ref": "主文号，如：莲乡振字〔2026〕18号", "status": "在用 或 停用（仅当本文件本身被废止/失效或为明确废止通知才填停用；引用旧文件或替代旧规定不代表本新文件停用）"}
如果文中没有文号，doc_ref 填空字符串。"""


def _clean_json(s):
    s = s.strip()
    if s.startswith('```'):
        s = re.sub(r'^```[a-zA-Z]*\n?', '', s)
        s = re.sub(r'\n?```$', '', s)
    return s


def llm_extract(text, cfg):
    """调 LLM 抽取字段，返回 dict（本地小模型用简化 prompt，解析失败返回空 dict）"""
    if len(text) > 100000:
        raise ValueError('文档超过 100000 字，请按政策拆分，避免遗漏后段条件')
    url = cfg['base_url'] + '/chat/completions'
    headers = {'Authorization': 'Bearer %s' % cfg['api_key'], 'Content-Type': 'application/json'}
    prompt = _PROMPT
    body = {
        'model': cfg['llm_model'],
        'messages': [
            {'role': 'system', 'content': prompt},
            {'role': 'user', 'content': text},
        ],
        'temperature': 0.1,
        'response_format': {'type': 'json_object'},
    }
    r = requests.post(url, headers=headers, json=body, timeout=180)
    r.raise_for_status()
    content = r.json()['choices'][0]['message']['content']
    try:
        return json.loads(_clean_json(content))
    except Exception:
        return {}


def normalize_fields(fields):
    """字段归一化：键兜底 + 分类/层级校验 + 状态判定"""
    for k, v in {'policy_name': '', 'doc_ref': '', 'regions': '', 'category1': '',
                 'level': '', 'status': '在用', 'is_revision': False,
                 'referenced_doc_refs': [], 'reason': ''}.items():
        fields.setdefault(k, v)
    cat = fields.get('category1', '').strip()
    if cat not in CATEGORIES:
        cat = ''
    level = str(fields.get('level', '')).strip().upper()
    if level not in ('C', 'S', 'U', 'X'):
        level = ''
    status = str(fields.get('status', '')).strip() or '在用'
    status = '停用' if ('停用' in status or '废止' in status) else '在用'
    fields.update(category1=cat, level=level, status=status)
    return fields


def get_all_policies():
    """从 Kingbase 读 policy 匹配所需的行"""
    engine = create_engine(config.DATABASE_URL)
    with engine.connect() as c:
        rows = c.execute(text(
            'SELECT p.policy_id,p.policy_code,p.policy_name,p.doc_ref,p.status,p.level,p.regions,p.category1,p.target,p.conditions,p.benefit,p.contact FROM policy p')).mappings().all()
    return [dict(r) for r in rows]


def rerank_scores(query, documents, cfg):
    """调 rerank 模型对候选重排打分；无配置或调用失败返回 None（调用方降级 difflib）。
    P3 换源：注册表开启且有默认重排行时用它（provider 决定请求/响应格式），否则回落
    本文件模块级 load_model_cfg() 的 yaml 平台 cfg（off 态行为与改造前逐字节一致）。
    请求/响应格式与打分逻辑统一委托 model_providers.rerank_scores。"""
    from model_providers import default_rerank_cfg, registry_enabled, rerank_scores as _shared
    if registry_enabled():
        rc = default_rerank_cfg()
        if rc:
            cfg = rc
    return _shared(cfg, query, documents, timeout=60)


# 名称尾部数字后缀（台账同名重复条目的典型特征，如“…办法的通知2”）→ 排序降权
_NUM_TAIL = re.compile(r'[0-9]+$')


def _name_scores(name, policies, cfg):
    """候选重排：difflib 粗筛 Top20 -> reranker 精排，融合分 = 0.6*difflib + 0.4*rerank
    （reranker 不可用时回退纯 difflib）。名称尾数字后缀的重复条目降权 x0.85。
    返回 ([(score, policy)], rr_ok)，rr_ok=False 表示已降级 difflib"""
    scored = sorted(
        ((difflib.SequenceMatcher(None, name, p['policy_name']).ratio(), p) for p in policies),
        key=lambda x: x[0], reverse=True)
    short = scored[:20]
    rr = rerank_scores(name, [p['policy_name'] for _, p in short], cfg)
    if rr is None:
        return short, False
    fused = []
    for i, (d, p) in enumerate(short):
        f = 0.6 * d + 0.4 * rr[i]
        if _NUM_TAIL.search(p['policy_name']):
            f *= 0.85
        fused.append((f, p))
    return sorted(fused, key=lambda x: x[0], reverse=True), True


def _policy_name(name):
    name = re.sub(r'(?:19|20)\d{2}年?|修订|修正|试行', '', name or '')
    return re.sub(r'[^\u4e00-\u9fa5A-Za-z0-9]', '', name)


def _same_scope(fields, policy):
    if fields.get('level') and policy.get('level') and fields['level'] != policy['level']:
        return False
    def regions(item):
        value = (item.get('regions') or '').strip() or config.DEFAULT_POLICY_REGIONS.get(item.get('level'), '')
        return set(re.split(r'[、，,；;\s]+', value.replace('江西省全省','全省').replace('江西省','全省'))) - {''}
    left, right = regions(fields), regions(policy)
    return not left or not right or bool(left & right) or '全国' in left | right or '全省' in left | right


def _set_candidates(fields, candidates, reason):
    fields['match_candidates'] = [dict({k:p.get(k) or '' for k in ('policy_id','policy_code','policy_name','level','regions','doc_ref','category1','target','conditions','benefit')}, reason=reason) for p in candidates]


'''这里是明确政策编号命中，这里的编号只能从【政策编号：xxx】行提取'''
def match_policy(fields, full_text, policies, cfg=None):
    """明确编号 > 主文号/明确替代关系 > 相似候选人工确认 > 新增。"""
    fields['match_candidates'] = []
    code = (fields.get('policy_code') or '').strip().upper()
    if code:
        hits = [p for p in policies if (p.get('policy_code') or '').upper() == code]
        if len(hits) != 1:
            raise ValueError('政策编号%s%s，未更新台账' % (code, '不存在' if not hits else '对应多个记录，请核对'))
        hit = hits[0]
        similarity = difflib.SequenceMatcher(None, _policy_name(fields.get('policy_name')), _policy_name(hit['policy_name'])).ratio()
        conflict = (fields.get('category1') and hit.get('category1') and fields['category1'] != hit['category1']) or (fields.get('level') and hit.get('level') and fields['level'] != hit['level']) or similarity < 0.4
        if conflict:
            reason = '明确编号%s与政策名称、层级或分类存在明显冲突，请核对后确认' % code
            _set_candidates(fields, hits, reason)
            return '待人工确认', hit, round(similarity,3), reason
        return '编号命中', hit, 1.0, '明确编号%s命中，按指定政策更新并保留原编号' % code


    # 主文号/替代关系命中，主要作用是防幻觉：superseded_doc_refs 里的文号必须在全文里真实出现，否则不算。这是防 LLM 编造的核心机制。
    principal = extract_doc_nums(fields.get('doc_ref') or '')
    superseded = set()
    for ref in fields.get('superseded_doc_refs') or []:
        # The old reference must actually occur in the uploaded document.
        superseded |= {number for number in extract_doc_nums(ref) if number in norm_doc(full_text)}
    related = superseded if fields.get('is_revision') is True or fields.get('status') == '停用' else set()
    refs = principal | related
    hits = [p for p in policies if extract_doc_nums(p.get('doc_ref') or '') & refs]
    if hits:
        name = _policy_name(fields.get('policy_name'))
        hits.sort(key=lambda p: difflib.SequenceMatcher(None,name,_policy_name(p['policy_name'])).ratio(),reverse=True)
        hit = hits[0]
        similarity = difflib.SequenceMatcher(None,name,_policy_name(hit['policy_name'])).ratio()
        replacement = bool(extract_doc_nums(hit.get('doc_ref') or '') & related)
        if len(hits) > 1 or (fields.get('status') != '停用' and (not _same_scope(fields,hit) or (similarity < 0.8 and not replacement))):
            reason = '文号或替代关系关联多个政策，或名称/地区差异较大，请确认更新对象'
            _set_candidates(fields,hits,reason)
            return '待人工确认', None if len(hits)>1 else hit, round(similarity,3), reason
        kind = '替代关系命中' if replacement else '文号命中'
        return kind,hit,0.97,'%s唯一对应%s，判定%s' % (kind,hit['policy_code'],'停用' if fields.get('status')=='停用' else '更新')

    candidates = []
    name = _policy_name(fields.get('policy_name'))
    content = ''.join(fields.get(k) or '' for k in ('target','conditions','benefit'))
    for p in policies:
        if not _same_scope(fields,p):
            continue
        similarity = difflib.SequenceMatcher(None,name,_policy_name(p['policy_name'])).ratio() if name else 0
        old_content = ''.join(p.get(k) or '' for k in ('target','conditions','benefit'))
        content_sim = difflib.SequenceMatcher(None,content,old_content).ratio() if min(len(content),len(old_content)) >= 30 else 0
        if similarity >= 0.72 or (similarity >= 0.35 and content_sim >= 0.85):
            candidates.append((max(similarity,content_sim),p))
    if candidates:
        candidates.sort(key=lambda pair:pair[0],reverse=True)
        reason = '发现同名或内容相似政策，但文号/更新关系不明确，请选择更新已有政策或新增'
        _set_candidates(fields,[p for _,p in candidates],reason)
        return '待人工确认', candidates[0][1] if len(candidates)==1 else None, round(candidates[0][0],3), reason
    if fields.get('status') == '停用' or related or fields.get('is_revision') is True:
        return '待人工确认',None,0.0,'识别为停用或修订，但未能唯一定位旧政策，请人工核实'
    return '新增',None,1.0,'未发现对应文号、明确替代关系或相似政策，生成新编号入库'


# ---------------- 主流程 ----------------
def recognize_pdf(path, cfg=None, document=None, input_fields=None):
    """识别一份 PDF → 结构化草稿字段（供上传接口使用）"""
    cfg = cfg or load_model_cfg()
    from policy_ingest import document_text
    text = document if document is not None else document_text(path)
    filename = os.path.basename(path)
    fields = normalize_fields(llm_extract(text, cfg))
    # Only an explicit input label may select a ledger identifier; model guesses cannot.
    fields.pop('policy_code', None)
    codes = set(re.findall(r'^\s*政策编号\s*[:：]\s*([^\s]+)\s*$', text, re.M)) if input_fields is None else set()
    if len(codes) > 1:
        raise ValueError('文件包含多个政策编号，请拆分后上传')
    if codes:
        fields['policy_code'] = codes.pop()
    fields.update(input_fields or {})
    fields = normalize_fields(fields)
    fields['document_text'] = text
    # 弱模型全文校正：状态按全文废止关键词兜底；废止类 doc_ref 语义特殊置空待人工补；
    # 其余情况 doc_ref 若不在全文文号集合中同样清空
    if fields.get('doc_ref') and any(number not in norm_doc(text) for number in extract_doc_nums(fields['doc_ref'])):
        fields['doc_ref'] = ''
    # 地区词库确定性识别（全文+文件名），命中则覆盖 LLM 初值；未命中保留 LLM 值（如“全省”）
    lex = detect_regions(text, filename)
    if lex and not fields.get('regions'):
        fields['regions'] = '、'.join(lex)
    # 级别规则识别（文号/发文机关），命中则覆盖/标注 LLM 初值
    rule_level, rule_reason = detect_level(text, filename)
    if rule_level:
        if fields.get('level') and fields['level'] != rule_level:
            fields['reason'] = (fields.get('reason') or '') + '；规则识别层级为%s（%s），覆盖 LLM 初值%s' % (
                LEVEL_CN.get(rule_level, rule_level), rule_reason, LEVEL_CN.get(fields['level'], fields['level']))
        fields['level'] = rule_level
    policies = get_all_policies()
    match_type, policy, confidence, reason = match_policy(fields, text, policies, cfg)
    return {
        'policy_name': fields['policy_name'],
        'doc_ref': fields.get('doc_ref', ''),
        'level': fields.get('level', ''),
        'category1': fields.get('category1', ''),
        'regions': fields.get('regions', ''),
        'status': fields.get('status', '在用'),
        'match_type': match_type,
        'confidence': confidence,
        'match_policy_id': policy['policy_id'] if policy else None,
        'match_policy_code': policy['policy_code'] if policy else None,
        'match_reason': reason + '｜' + fields.get('reason', ''),
        'draft_data': fields,
    }


if __name__ == '__main__':
    import sys
    cfg = load_model_cfg()
    for pdf in sys.argv[1:]:
        print('\n===== %s =====' % os.path.basename(pdf))
        try:
            rec = recognize_pdf(pdf, cfg)
            print(json.dumps(rec, ensure_ascii=False, indent=2))
        except Exception as e:
            print('识别失败:', e)
