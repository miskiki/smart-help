# -*- coding: utf-8 -*-
"""政策知识库同步 + RAG 问答（向量存 Kingbase policy_vector，不经 chroma）

- 持久化：Kingbase policy_vector 表（doc_text + vector BYTEA），服务进程内存缓存
- 向量：text-embedding-v4（bailian API，读取 model_settings.yaml）
- LLM：qwen-plus（bailian API）
- 文档 id：{policy_code}#d{detail_id}；policy_code 关联 Kingbase 台账
- 同步时机：草稿确认落库后（新增/更新 → 覆盖该 policy_code；停用 → 删除该 policy_code）
- 问答：检索 top3 切片 → 拼接上下文 → LLM 生成（隐藏管理字段）→ 写 Kingbase qa_log
"""
import os
import re
import json
import time
import logging
from datetime import datetime

import numpy as np
import requests
from sqlalchemy import create_engine, text
from chain_trace import _current as _ct_current, _emit as _ct_emit

import config
from recognize import MODEL_YAML, load_model_cfg, extract_pdf_text
from model_registry import (check_index_compat, embed_cfg, embed_dim,
                            index_dim, rerank_cfg, set_index_dim)
from model_providers import registry_enabled, default_embed_cfg, default_rerank_cfg

CHUNK_SIZE = 450
OVERLAP = 100
TOP_K = 10                 # 问答与导出共用检索范围（此前问答3/导出10不一致）
EMBED_BATCH = 10          # dashscope embedding 单次批量上限（实测 >10 报 400）
JUDGE_BATCH = 4       # 本地 3B 单批判定可稳定完成的上限（实测 4 条完整；8/12 条仅判 3-4 条）
JUDGE_ATTEMPTS = 3     # 首次判定及最多两次补判；仍有遗漏则整份结果失败
# 向量维度不再硬编码：由 model_registry 按当前向量平台 profile 动态读取
# （生产：qwen3-embedding-4b=2560；bailian text-embedding-v4=1536）

# 内存向量缓存：按嵌入模型名分键（多模型共存，切换默认即回滚）。
# _VEC_CACHE[model_name] = {max_ts, codes, texts, metas, mat(N,D) float32, dim, ts}
# LRU 上限 4 个模型，超出淘汰最久未用，防内存膨胀。yaml 模式只有单一模型，行为不变。
_VEC_CACHE = {}
_VEC_CACHE_MAX = 4


# ---- 回答取消机制：前端 /api/qa/cancel 设置标记，流式回答在每次模型调用前检查 ----
_CANCEL_FLAGS = {}
# 活跃回答会话注册表：回答线程开始/结束时登记，用于识别 DB 中"残留 generating"记录。
_ACTIVE_TURNS = set()


class AnswerCancelled(Exception):
    """用户主动取消当前回答。"""


def request_cancel(session_id):
    """标记某会话为已请求取消（幂等，仅内存态；进程重启后自然失效）。"""
    if session_id:
        _CANCEL_FLAGS[session_id] = True


def mark_active(session_id):
    """登记某会话正在回答（线程级）。"""
    if session_id:
        _ACTIVE_TURNS.add(session_id)


def unmark_active(session_id):
    """回答结束（无论成败）注销登记。"""
    if session_id:
        _ACTIVE_TURNS.discard(session_id)


def is_active(session_id):
    """该会话当前是否有回答线程在运行。"""
    return bool(session_id and session_id in _ACTIVE_TURNS)


def cancel_requested(session_id):
    """该会话是否已收到取消请求（用于 prepare 放行：用户已取消则不再 409）。"""
    return bool(session_id and _CANCEL_FLAGS.get(session_id))


def clear_cancel(session_id):
    """清除取消标记（新一轮回答开始时 / 回答结束时调用，防止残留误取消）。"""
    if session_id:
        _CANCEL_FLAGS.pop(session_id, None)


def _check_cancel(session_id):
    """在每次外部模型调用前检查取消标记；命中即抛 AnswerCancelled 终止本轮回答。"""
    if session_id and _CANCEL_FLAGS.get(session_id):
        raise AnswerCancelled('本次回答已取消')


# 当前问答 LLM 平台（Kingbase sys_config 持久化，30s 缓存；默认 bailian）
_LLM_PLATFORM_CACHE = {'ts': 0.0, 'platform': None}


def get_current_llm_platform():
    now = time.time()
    if _LLM_PLATFORM_CACHE['platform'] and now - _LLM_PLATFORM_CACHE['ts'] < 30:
        return _LLM_PLATFORM_CACHE['platform']
    plat = 'bailian'
    try:
        with _db().connect() as c:
            r = c.execute(text("SELECT cfg_value FROM app_config WHERE cfg_key='qa_llm_platform'")).fetchone()
            if r and r[0]:
                plat = r[0]
    except Exception:
        pass
    _LLM_PLATFORM_CACHE['ts'] = now
    _LLM_PLATFORM_CACHE['platform'] = plat
    return plat


def set_current_llm_platform(plat):
    with _db().connect() as c:
        c.execute(text("INSERT INTO app_config(cfg_key,cfg_value,updated_at) VALUES('qa_llm_platform',:v,now()) "
                       "ON CONFLICT (cfg_key) DO UPDATE SET cfg_value=:v, updated_at=now()"), {'v': plat})
        c.commit()
    _LLM_PLATFORM_CACHE['platform'] = plat
    _LLM_PLATFORM_CACHE['ts'] = time.time()


def _cfg():
    from conversations import MODEL_CONFIG
    cfg = MODEL_CONFIG.get()
    if cfg:
        return cfg
    # MODEL_REGISTRY=1：无会话上下文路径（直接 API/评测/worker）走注册表默认对话模型
    from model_providers import registry_enabled, default_chat_cfg
    if registry_enabled():
        cfg = default_chat_cfg()
        if cfg:
            return cfg
    return load_model_cfg(get_current_llm_platform())


def _embed_cfg():
    # 向量化平台：注册表模式（MODEL_REGISTRY=1）取默认 embed 模型行；
    # 否则 yaml 平台（model_registry，生产 local-qwen3-emb → 本地 8095，2560 维）。
    # 与对话平台解耦——切换对话模型不影响检索向量维度。
    if registry_enabled():
        rcfg = default_embed_cfg()
        if rcfg:
            return rcfg
    return embed_cfg()


def _judge_cfg():
    # 判定与 AI 建议均使用本轮选定模型，不自动切换平台。
    return _cfg()


def _db():
    return create_engine(config.DATABASE_URL)


# 生产向量表：Qwen3-Embedding-4B 的 2560 维索引（policy_vector 是 768 维原始数据，仅作重建来源，运行时不再读取）
_VECTOR_TABLE = os.environ.get('POLICY_VECTOR_TABLE', 'policy_vector_qwen3')
if _VECTOR_TABLE not in ('policy_vector_qwen3', 'policy_vector'):
    _VECTOR_TABLE = 'policy_vector_qwen3'


def _vec_table():
    return _VECTOR_TABLE


def _vec_table_exists():
    with _db().connect() as c:
        r = c.execute(text("SELECT to_regclass(:t)"), {'t': _VECTOR_TABLE}).fetchone()
        return r and r[0]


def _embed(texts, cfg=None, timeout=120):
    """按当前向量平台分批向量化，返回向量列表（维度随模型自适应，kb_rag 不再硬编码）。
    调用失败直接抛 ApiError（明确提示向量模型服务不可用），不降级、不静默继续。"""
    from authentication import ApiError
    cfg = cfg or _embed_cfg()
    from vector_safety import validate_inputs
    validate_inputs(texts, cfg['embed_model'])
    # 行级维度：注册表行内 dim 优先；yaml 平台 cfg 已带 embed_dim（与全局一致）
    dim = int(cfg.get('embed_dim') or embed_dim())
    url = cfg['base_url'] + '/embeddings'
    headers = {'Content-Type': 'application/json'}
    if cfg.get('api_key') and cfg['api_key'] != 'EMPTY':
        headers['Authorization'] = 'Bearer %s' % cfg['api_key']
    out = []
    try:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            r = requests.post(url, json={'model': cfg['embed_model'], 'input': batch, 'dimensions': dim},
                              headers=headers, timeout=timeout)
            r.raise_for_status()
            data = sorted(r.json()['data'], key=lambda x: x.get('index', 0))
            if len(data) != len(batch) or any(len(x['embedding']) != dim for x in data):
                raise ValueError('向量数量或维度不符合当前知识库配置，未覆盖原索引')
            out.extend([x['embedding'] for x in data])
    except requests.Timeout as exc:
        raise ApiError('向量模型服务响应超时，本次未生成匹配结果和 AI 建议，请检查模型服务后重试。', 503) from exc
    except requests.ConnectionError as exc:
        raise ApiError('向量模型服务无法连接（%s），本次未生成匹配结果和 AI 建议。请启动模型服务或切换向量平台后重试。' % cfg['base_url'], 503) from exc
    except requests.HTTPError as exc:
        raise ApiError('向量模型服务返回错误，本次未生成匹配结果和 AI 建议。请检查模型配置后重试。', 503) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError('向量模型返回结果异常，本次未生成匹配结果和 AI 建议，请重试。', 503) from exc
    return out


def preflight_local_models(cfg=None):
    """问答前模型健康自检（不降级原则）：当前重排配置了**本地** rerank 端点时，
    服务不可用直接抛 ApiError 失败提示，不进入检索/回答流程。
    P3 换源：注册表开启且有默认重排行时用其端点，否则回落 yaml 向量平台 profile。
    仅对本地端点（127.0.0.1/localhost）做健康自检；远端重排（如 DashScope 原生网关）
    无 /v1/health，跳过预检（调用失败下游本就降级 difflib）。默认 bailian（端点为空）不预检。"""
    if registry_enabled():
        rr = default_rerank_cfg()
    else:
        rr = rerank_cfg()
    if not rr:
        rr = rerank_cfg()
    endpoint = (rr.get('rerank_endpoint') or '').strip()
    if not rr.get('rerank_model') or not endpoint:
        return
    from urllib.parse import urlparse
    if urlparse(endpoint).hostname not in ('127.0.0.1', 'localhost'):
        return  # 远端重排不做本地健康自检
    from authentication import ApiError
    health_url = endpoint.split('/v1/')[0] + '/v1/health' if '/v1/' in endpoint else endpoint
    try:
        r = requests.get(health_url, timeout=5)
        ok = r.status_code == 200 and (r.json() or {}).get('ok') is True
        if not ok:
            raise RuntimeError('status=%s body=%s' % (r.status_code, r.text[:120]))
    except Exception as exc:
        raise ApiError('重排模型服务不可用（%s），本次未生成匹配结果和 AI 建议。请启动本地模型服务后重试。' % health_url, 503) from exc


def _build_doc(policy_name, doc_ref, full_text):
    """组装知识库文档：标题 + 文号 + 正文"""
    head = '%s' % policy_name
    if doc_ref:
        head += '（%s）' % doc_ref
    return '%s\n%s' % (head, full_text or '')


def _splitter():
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    return RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE,
                                          chunk_overlap=OVERLAP,
                                          separators=['\n\n', '\n', '。', '；', '，', ' ', ''])


def sync_policy(policy_code, policy_name, doc_ref, status, version_no, full_text=None):
    """同步/覆盖政策文档到知识库（新增与更新共用：先删该 policy_code 旧文档再写）。

    以政策明细为关联单位：为每条 policy_detail 生成独立文档块并写入 detail_id，
    避免不同明细的地区/对象/条件/补贴混在同一块；无明细政策退化为整体切片
    （detail_id=NULL，政策级）。full_text 存在时（单条 PDF 审核路径）追加为政策级块。
    """
    if not policy_code or not policy_name:
        return {'ok': False, 'msg': '缺少 policy_code / policy_name'}
    # 维度防护：存量索引与当前向量模型不一致时拒绝写入，防止新旧维度混库。
    # 注册表模式走行级标记（embed_model 列），不写全局 index_dim 锁，跳过该全局校验。
    if not registry_enabled():
        check_index_compat()
    db = _db()
    with db.connect() as c:
        pid = c.execute(text("SELECT policy_id FROM policy WHERE policy_code = :pc"),
                        {'pc': policy_code}).scalar()
        d_rows = []
        if pid:
            d_rows = c.execute(text(
                "SELECT detail_id, target, conditions, region, benefit, process, extra FROM policy_detail "
                "WHERE policy_id = :pid ORDER BY detail_id"), {'pid': pid}).fetchall()
    # 按明细组织文档块（不混合不同明细的地区/对象/条件/补贴）
    blocks = []  # (text, detail_id)
    for d in d_rows:
        body = '；'.join(x for x in [
            '适用地区：%s' % (d[3] or '') if d[3] else '',
            '适用对象：%s' % (d[1] or '') if d[1] else '',
            '申请条件：%s' % (d[2] or '') if d[2] else '',
            '补贴标准：%s' % (d[4] or '') if d[4] else '',
            '办理流程：%s' % (d[5] or '') if d[5] else '',
            '明细补充依据：%s' % (d[6] or '') if d[6] else '',
            '政策依据：%s' % doc_ref if doc_ref else '',
        ] if x)
        if body:
            blocks.append(('（编号 %s）%s\n%s' % (policy_code, policy_name, body), d[0]))
    if full_text and full_text.strip():
        blocks.append((_build_doc(policy_name, doc_ref, full_text), None))
    if not blocks:
        blocks.append((_build_doc(policy_name, doc_ref, ''), None))
    from vector_safety import split_tokens, backup_vectors
    cfg = _embed_cfg()
    model = cfg['embed_model']
    chunks = [(chunk, did) for body, did in blocks for chunk in split_tokens(body, model)]
    embeds = _embed([t for t, _ in chunks], cfg=cfg)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with db.begin() as c:
        current = c.execute(text("SELECT cur_version, status FROM policy WHERE policy_code=:pc FOR UPDATE"), {'pc': policy_code}).fetchone()
        if not current or str(current[0] or '') != str(version_no or '') or current[1] != status:
            raise ValueError('Policy changed during embedding; old index preserved')
        backup_path = backup_vectors(c, policy_code, table=_VECTOR_TABLE)
        # 注册表模式只删当前默认模型的行（含历史 NULL/空标记），保留其它模型的向量——
        # 否则更新一条政策会抹掉共存模型的索引，切回旧模型即丢该政策的召回。
        if registry_enabled():
            c.execute(text(
                "DELETE FROM " + _VECTOR_TABLE +
                " WHERE policy_code = :pc AND (embed_model=:m OR embed_model IS NULL OR embed_model='')"),
                {'pc': policy_code, 'm': model})
        else:
            c.execute(text("DELETE FROM " + _VECTOR_TABLE + " WHERE policy_code = :pc"), {'pc': policy_code})
        for i, ((chunk, did), emb) in enumerate(zip(chunks, embeds)):
            vec = np.array(emb, dtype=np.float32).tobytes()
            c.execute(text(
                "INSERT INTO " + _VECTOR_TABLE + " (id, policy_code, policy_name, doc_text, vector, status, updated_at, policy_version, embed_model, detail_id) "
                "VALUES (:id, :pc, :name, :doc, :vec, :st, :ts, :version, :model, :did)"),
                {'id': '%s#%d' % (policy_code, i), 'pc': policy_code, 'name': policy_name, 'doc': chunk,
                 'vec': vec, 'st': status, 'ts': now, 'version': version_no, 'model': model, 'did': did})
    # 注册表模式不写全局 index_dim 锁（行级 embed_model 标记已足够，多模型共存）
    if not registry_enabled():
        set_index_dim(embed_dim())
    _invalidate_cache()
    return {'ok': True, 'chunks': len(chunks), 'policy_code': policy_code, 'detail_blocks': len(blocks), 'backup_path': backup_path}


def remove_policy(policy_code):
    """停用/删除政策文档"""
    if not policy_code:
        return {'ok': False, 'msg': '缺少 policy_code'}
    db = _db()
    with db.begin() as c:
        r = c.execute(text("DELETE FROM " + _VECTOR_TABLE + " WHERE policy_code = :pc"),
                      {'pc': policy_code})
        n = r.rowcount
    _invalidate_cache()
    return {'ok': True, 'deleted': n or 0}


def _invalidate_cache():
    # 失效所有模型的缓存（下次检索按当前默认模型重载）
    for k in list(_VEC_CACHE):
        _VEC_CACHE[k]['max_ts'] = None


def _lru_evict():
    """LRU：模型数超上限时淘汰最久未用（防内存膨胀）"""
    if len(_VEC_CACHE) > _VEC_CACHE_MAX:
        oldest = min(_VEC_CACHE, key=lambda k: (_VEC_CACHE[k].get('ts') or 0))
        _VEC_CACHE.pop(oldest, None)


def _current_cache():
    """当前默认嵌入模型的缓存快照（浅拷贝，防迭代中被改）"""
    model = _embed_cfg()['embed_model']
    return dict(_VEC_CACHE.get(model) or {
        'max_ts': None, 'codes': [], 'texts': [], 'metas': [], 'mat': None, 'dim': None})


def _load_vectors():
    """从 Kingbase 加载在用向量到内存缓存（按嵌入模型名分键；版本变化时重载；维度随模型自适应）。

    多模型共存：每个模型各自建矩阵；新模型无索引时命中 0 行→空结果不报错（待重建），
    旧模型向量原样保留，切回旧模型即刻恢复（这就是共存+回滚）。
    维度自检从全局 index_dim 锁降级为行级：只对过滤出的本模型行校验维度。
    """
    cfg = _embed_cfg()
    model = cfg['embed_model']
    want = int(cfg.get('embed_dim') or embed_dim())
    # 全局 index_dim 锁仅 yaml 模式保留（现状行为）；注册表模式走行级维度自检
    if not registry_enabled():
        have = index_dim()
        if have is not None and have != want:
            raise ValueError(
                '向量索引维度 %d 与当前模型 %d 不一致，检索不可用。'
                '请运行 python reindex_vectors_2560.py 重建索引后再检索。' % (have, want))
    db = _db()
    with db.connect() as c:
        r = c.execute(text(
            "SELECT max(updated_at) FROM " + _VECTOR_TABLE +
            " WHERE status = '在用' AND (embed_model IS NULL OR embed_model='' OR embed_model=:model)"),
            {'model': model}).fetchone()
        max_ts = r[0]
        rows = c.execute(text(
            "SELECT v.id, v.policy_code, COALESCE(v.policy_name,p.policy_name), v.doc_text, v.vector, v.detail_id, v.policy_version FROM " + _VECTOR_TABLE + " v JOIN policy p ON p.policy_code=v.policy_code "
            "WHERE v.status = '在用' AND p.status = '在用' "
            "AND v.policy_version = p.cur_version "
            "AND (v.embed_model IS NULL OR v.embed_model='' OR v.embed_model=:model) "
            "AND (p.effect_date IS NULL OR p.effect_date='' OR p.effect_date <= :today) "
            "AND (p.expire_date IS NULL OR p.expire_date='' OR p.expire_date >= :today) ORDER BY v.id"),
            {'today': datetime.now().strftime('%Y-%m-%d'), 'model': model}).fetchall()
    now = time.time()
    if not rows:
        _VEC_CACHE[model] = {'max_ts': max_ts, 'codes': [], 'texts': [], 'metas': [],
                             'mat': None, 'dim': want, 'ts': now}
        _lru_evict()
        return
    vecs = np.array([np.frombuffer(bytes(r[4]), dtype=np.float32) for r in rows], dtype=np.float32)
    # 行级维度自检：本模型索引维度与配置不符→明确提示重建该模型（不是全局锁死）
    if vecs.shape[1] != want:
        raise ValueError('模型 %s 的向量索引维度 %d 与配置 %d 不一致，请重建该模型索引后再检索'
                         % (model, vecs.shape[1], want))
    codes, texts, metas = [], [], []
    for r in rows:
        codes.append(r[0])
        texts.append(r[3] or '')
        metas.append({'policy_code': r[1], 'policy_name': r[2] or '', 'detail_id': r[5], 'version': r[6] or ''})
    _VEC_CACHE[model] = {'max_ts': max_ts, 'codes': codes, 'texts': texts, 'metas': metas,
                         'mat': vecs, 'dim': want, 'ts': now}
    _lru_evict()


def _query_instruction():
    """查询侧任务指令（Qwen3-Embedding 官方协议：查询加 Instruct 前缀，文档不加）。
    平台未配置时返回空串（不加前缀），行为不变。"""
    cfg = _embed_cfg()
    return cfg.get('embed_query_instruction') or ''


def _embed_queries(queries, timeout=120):
    """查询侧向量化：带平台配置的 Instruct 前缀。文档入库仍走 _embed（无前缀）。"""
    instr = _query_instruction()
    if not instr:
        return _embed(queries, timeout=timeout)
    return _embed(['Instruct: %s\nQuery: %s' % (instr, q) for q in queries], timeout=timeout)


def _robust_cutoff(sims, floor):
    """分布鲁棒检索截断（漏斗级联修复）：绝对下限与"噪声中位数+δ"取高者。

    固定阈值是按 768 维 bge 的相似度分布调校的；当前生产用 Qwen3-2560（分布更平
    top1-p50 仅 0.2），固定阈值会让候选漏斗失控（17→38）。median+δ 随分布自适应，
    max(绝对下限, median+δ) 在 768 陡峭分布下与旧行为一致，平缓分布下自动收紧。
    """
    if sims is None or not len(sims):
        return float(floor)
    med = float(np.median(sims))
    return max(float(floor), med + 0.05)


def search_many(queries, top_k=TOP_K, threshold=0.5, timeout=20):
    """One current index snapshot and embedding batch for multiple factual intents."""
    _load_vectors()
    cache=_current_cache()
    if cache['mat'] is None or not len(cache['codes']):return [[] for _ in queries]
    vectors=_embed_queries(queries,timeout=timeout)
    mat=cache['mat'];norms=np.linalg.norm(mat,axis=1)
    results=[]
    for vector in vectors:
        qv=np.asarray(vector,dtype=np.float32)
        if mat.shape[1]!=len(qv):raise ValueError('查询向量与索引维度不一致')
        denom=norms*np.linalg.norm(qv)
        sims=np.divide(mat@qv,denom,out=np.zeros(len(mat),dtype=np.float32),where=denom!=0)
        cutoff=_robust_cutoff(sims,threshold)
        results.append([(cache['texts'][int(i)],float(sims[int(i)]),cache['metas'][int(i)]) for i in np.argsort(-sims)[:top_k] if sims[int(i)]>=cutoff])
    return results

def search(query, top_k=TOP_K, threshold=0.5):
    """检索最相关切片（numpy 余弦；低于阈值剔除），返回 [(document, sim, metadata)]"""
    _load_vectors()
    cache = _current_cache()
    if cache['mat'] is None or len(cache['codes']) == 0:
        return []
    qv = np.array(_embed_queries([query])[0], dtype=np.float32)
    mat = cache['mat']
    if mat.shape[1] != len(qv):
        raise ValueError('查询向量和知识库维度不一致，请检查模型配置后重建索引')
    qn = np.linalg.norm(qv)
    if qn == 0:
        return []
    sims = (mat @ qv) / (np.linalg.norm(mat, axis=1) * qn)
    cutoff = _robust_cutoff(sims, threshold)
    order = np.argsort(-sims)
    out = []
    for idx in order:
        s = float(sims[idx])
        if s < cutoff:
            break
        i = int(idx)
        out.append((cache['texts'][i], s, cache['metas'][i]))
        if len(out) >= top_k:
            break
    return out


_QA_SYSTEM = """你是"智能帮扶"政策咨询助手。请严格依据提供的知识库片段回答用户的政策问题：
1. 只回答知识库片段中明确存在的政策内容，不要编造；涉及金额、条件、流程以片段原文为准。
2. 引用政策名称、地区和原文依据；必要时说明采用的版本，不展示操作人等无关内部信息。
3. 区分未找到证据、原文条件不完整、家庭事实不足。明确说明缺哪类依据，不编造资格，不统一套用超出知识库范围的话术。个人或家庭是否符合必须调用统一核验入口，普通政策咨询不作个人资格结论。
4. 回答简洁、分点，直接给出结论。"""

# 家庭匹配的 AI 建议使用专用提示词，不复用政策问答的"超出知识库范围"兜底。
# 待核实≠知识库没有政策：必须说明已确认事项、待核实事项和下一步需补充的具体信息。
_ADVICE_SYSTEM = """你是"智能帮扶"家庭匹配助手。请依据提供的【最终匹配结果】和【家庭与政策分析】给出 AI 帮扶建议：
1. 只依据给定结果说明已确认事项、待核实事项和下一步应补充的具体信息；不得新增清单外政策、不得改变资格判定、不得编造金额、材料或联系人。
2. 存在待核实政策时，明确说明"以下政策需补齐条件或经经办部门核实后确认是否适用"，不得宣称已符合资格；
   无论待核实数量多少，都不得使用政策咨询场景的"知识库范围"兜底话术。
3. 没有匹配政策时，只说明未找到推荐政策并建议补充信息或咨询经办部门；不得把家庭资料不足表述为知识库缺失。
4. 按办理先后顺序分点列出，控制在 300 字以内，直接输出建议正文。"""


def _llm_answer(question, context, system=None, temperature=0.3, cfg=None, timeout=180, purpose=None, response_schema=None):
    cfg = cfg or _cfg()
    from conversations import record_model
    from authentication import ApiError
    url = cfg['base_url'] + '/chat/completions'
    headers = {'Authorization': 'Bearer %s' % cfg['api_key'], 'Content-Type': 'application/json'}
    body = {
        'model': cfg['llm_model'],
        'messages': [
            {'role': 'system', 'content': system or _QA_SYSTEM},
            {'role': 'user', 'content': ('任务：%s\n\n输入JSON：\n%s' % (question,context)) if (purpose or '').startswith(('core_','policy_','showcase_')) else '【知识库片段】\n%s\n\n【用户问题】\n%s' % (context, question)},
        ],
        'temperature': temperature,
    }
    if (purpose or '').startswith(('core_','policy_','showcase_')):
        from policy_core.llm_contract import request_options
        body.update(request_options(cfg,purpose))
        if response_schema is not None:
            from urllib.parse import urlparse
            if urlparse(cfg['base_url']).hostname in ('127.0.0.1','localhost') and cfg.get('json_mode') is True:
                body['response_format']={'type':'json_schema','json_schema':{'name':'policy_contract','strict':True,'schema':response_schema}}
    usage = {}
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        r.encoding = 'utf-8'
        payload = r.json()
        content = payload['choices'][0]['message']['content']
        usage = payload.get('usage') or {}
        from policy_core.full_runtime import record_transport_metrics
        timing=payload.get('timings') or {}
        if not isinstance(timing,dict):timing={}
        safe_timing={k:timing[k] for k in ('cache_n','prompt_n','prompt_ms','prompt_per_second','predicted_n','predicted_ms','predicted_per_second') if type(timing.get(k)) in (int,float)}
        record_transport_metrics(prompt_tokens=usage.get('prompt_tokens'),completion_tokens=usage.get('completion_tokens'),
                                 input_chars=len(context or '')+len(system or '')+len(question or ''),output_chars=len(content or ''),server_timings=safe_timing)
        if not isinstance(content, str) or not content.strip():
            raise ValueError('Empty model answer')
    except requests.Timeout as exc:
        raise ApiError('模型响应超时，本次未生成匹配结果和 AI 建议，请重试。', 503) from exc
    except requests.ConnectionError as exc:
        raise ApiError('当前所选模型服务无法连接，本次未生成匹配结果和 AI 建议。请启动模型服务或切换模型后重试。', 503) from exc
    except requests.HTTPError as exc:
        raise ApiError('当前所选模型服务返回错误，本次未生成匹配结果和 AI 建议。请检查模型配置后重试。', 503) from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ApiError('模型未返回有效内容，本次未生成匹配结果和 AI 建议，请重试。', 503) from exc
    record_model(cfg)
    # 第六阶段：模型调用观测（不记录问题/回答原文，只记字符数与 token 用量）
    _st = _ct_current.get()
    if _st is not None:
        _ct_emit(_st, 'model.meta', model_id=cfg.get('llm_model'), purpose=purpose or 'answer',
              input_chars=len(context or '') + len(question or ''),
              output_chars=len(content or ''),
              prompt_tokens=usage.get('prompt_tokens'),
              completion_tokens=usage.get('completion_tokens'),
              total_tokens=usage.get('total_tokens'),
              retries=0)
    return content.strip()


def _llm_stream(messages, temperature=0.3, cfg=None):
    """流式调用 qwen-plus（dashscope compatible-mode，stream=True）。
    逐块 yield 文本增量。"""
    cfg = cfg or _cfg()
    from conversations import record_model
    record_model(cfg)
    url = cfg['base_url'] + '/chat/completions'
    headers = {'Authorization': 'Bearer %s' % cfg['api_key'], 'Content-Type': 'application/json'}
    body = {'model': cfg['llm_model'], 'messages': messages,
            'temperature': temperature, 'stream': True}
    r = requests.post(url, json=body, headers=headers, stream=True, timeout=300)
    r.raise_for_status()
    # 字节行模式 + 显式 utf-8（llama.cpp 流响应不带 charset，decode_unicode 会按 latin-1 致中文乱码）
    for line in r.iter_lines():
        if not line:
            continue
        if line.startswith(b'data: '):
            payload = line[6:].decode('utf-8', errors='replace').strip()
            if payload == '[DONE]':
                break
            try:
                delta = json.loads(payload)['choices'][0]['delta'].get('content') or ''
            except Exception:
                continue
            if delta:
                yield delta


_MATCH_SYSTEM = """你是帮扶政策匹配引擎。用户提供家庭/个人情况，下面列出候选政策片段。
请逐条判断每条候选政策对该用户是否适用，只输出一个 JSON 对象（不要输出任何其他文字）：
{"policies":[{"code":"政策编号","applicable":true或false,"reason":"匹配理由（一句话）","missing":"尚缺条件（用户需补充核实的信息，无则空字符串）","next":"下一步核实事项（建议核实渠道/材料，无则空字符串）"}]}
判断标准：适用对象、申请条件与用户情况吻合才 applicable=true；明显不吻合（年龄、身份、参保情况、地区不符等）为 false。"""

_CONSULT_SYSTEM = _MATCH_SYSTEM + "\n本请求是政策内容咨询，不是家庭资格认定。applicable 在此仅表示该政策与问题主题和地区相关、可作为咨询依据；不要因未提供家庭年龄、收入等资格资料而排除相关政策。将具体资格待核实事项填入 missing，不得声称用户已符合资格。"


def _est_tokens(text):
    """最终输入长度保守估算（含标题/上下文/特殊 token 的近似预算）。
    实际部署模型为远程 API（无本地 tokenizer 权重），采用保守折算：
    1 个中文字符≈1 token，其他字符 4 字符≈1 token，并额外 +20% 余量，
    宁可高估触发显式报错，不让模型静默截断关键依据。"""
    if not text:
        return 0
    zh = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = len(text) - zh
    return int((zh + other / 4.0) * 1.2) + 64


def _complete_evidence(order, evidence):
    """Reload exact-version parent evidence; never infer complete eligibility from one chunk."""
    from authentication import ApiError
    docs = {}
    with _db().connect() as c:
        c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
        for code, (_doc, _score, meta) in order:
            p = c.execute(text("SELECT policy_id, cur_version, status, policy_name, doc_ref, "
                "regions, target, conditions, benefit, process FROM policy WHERE policy_code=:code"),
                {'code': code}).mappings().first()
            if not p or p['status'] != '在用' or str(p['cur_version'] or '') != str(meta.get('version') or ''):
                raise ApiError('政策版本已变化，请重新查询', 409)
            def render(row, fields):
                return '\n'.join('%s：%s' % (label, row.get(key) or '') for key, label in fields)
            parts = [render(p, [('policy_name','政策'),('doc_ref','依据'),('regions','政策地区'),
                ('target','政策对象'),('conditions','政策完整条件'),('benefit','政策待遇'),('process','政策流程')])]
            seen = set()
            for fragment in evidence[code]:
                did = fragment.get('detail_id')
                if did is None:
                    parts.append('【正文命中片段，非完整资格证据】\n' + fragment['doc'])
                    continue
                if did in seen:
                    continue
                seen.add(did)
                d = c.execute(text('SELECT region,target,conditions,benefit,process,extra FROM policy_detail '
                    'WHERE policy_id=:pid AND detail_id=:did'), {'pid':p['policy_id'],'did':did}).mappings().first()
                if d is None:
                    raise ApiError('政策明细已变化，请重新查询', 409)
                parts.append('【明细%s：独立分支，完整字段】\n%s' % (did, render(d,
                    [('region','地区'),('target','对象'),('conditions','完整条件'),('benefit','待遇'),
                     ('process','流程'),('extra','补充依据')])) )
            docs[code] = '\n\n'.join(parts)
    return docs


def match_policies(question, top_k=TOP_K, threshold=0.35, session_id=None):
    """检索候选政策 → LLM 判定每条适用性，返回统一适用清单。
    返回 [{'code','name','applicable','reason','missing','next','sim','excerpt'}]
    供问答与导出共用（保证回答与表格一致）。"""
    # Fetch more fragments before selecting distinct policies; retain every retrieved
    # fragment for each selected policy and preserve detail identities in the context.
    hits = search(question, top_k=top_k * 4, threshold=threshold)
    best = {}
    evidence = {}
    for d, s, m in hits:
        code = m.get('policy_code')
        if code:
            fragments = evidence.setdefault(code, [])
            if not any(x['doc'] == d and x['detail_id'] == m.get('detail_id') for x in fragments):
                fragments.append({'doc':d, 'detail_id':m.get('detail_id'), 'sim':round(s,3)})
        if code and (code not in best or s > best[code][1]):
            best[code] = (d, s, m)
    if not best:
        return []
    order = sorted(best.items(), key=lambda kv: (-kv[1][1],kv[0]))[:top_k]
    best = {code: row for code,row in order}
    docs = _complete_evidence(order, evidence)
    judged = {}
    try:
        for start in range(0,len(order),JUDGE_BATCH):
            _check_cancel(session_id)
            batch = order[start:start+JUDGE_BATCH]
            expected = {code for code,_ in batch}
            items = ['【%s】%s' % (code,docs[code]) for code,_row in batch]
            prompt = ('用户情况：%s\n\n候选政策：\n%s\n\n每条候选政策只输出一条判定记录，'
                      'code 必须与候选政策完全一致，不得按明细拆分成多条、不得新增或改写 code。'
                      '各明细是独立分支，不得拼接不同明细的条件或金额。请输出 JSON 判定。'
                      % (question, '\n'.join(items)))
            if len(prompt) > config.KG_MAX_PROMPT_CHARS:
                raise ValueError('Consultation evidence exceeds model budget; narrow the question')
            # 判定批：宽容校验 + 最多重试一次。
            # 宽容规则：同 code 多条只保留第一条；模型自创的未知 code 直接忽略；
            # 只要本批已知 code 全覆盖即通过（防止多输出一条幻觉记录导致整体 503）。
            # 只有真正漏判（已知 code 未覆盖）或两次都结构错误时才判定失败。
            batch_judged = None
            last_err = None
            for _attempt in range(2):
                try:
                    raw = _llm_answer('', prompt, system=_CONSULT_SYSTEM, temperature=0.1, cfg=_judge_cfg(), purpose='judge')
                    m = re.search(r'\{.*\}', raw, re.S)
                    data = json.loads(m.group(0)) if m else {}
                    policies = data.get('policies')
                    if not isinstance(policies, list) or any(
                            not isinstance(p, dict) or not isinstance(p.get('code'), str)
                            or not isinstance(p.get('applicable'), bool) for p in policies):
                        raise ValueError('Invalid consultation judgment')
                    bj = {}
                    for p in policies:
                        code = p.get('code')
                        if code in expected and code not in bj:
                            bj[code] = p
                    if set(bj) == expected:
                        batch_judged = bj
                        break
                    raise ValueError('Incomplete, duplicate or unknown consultation judgment')
                except AnswerCancelled:
                    raise
                except Exception as _e:
                    last_err = _e
            if batch_judged is None:
                raise last_err or ValueError('Incomplete, duplicate or unknown consultation judgment')
            judged.update(batch_judged)
    except Exception as exc:
        from authentication import ApiError
        if isinstance(exc, ApiError):
            raise
        raise ApiError('模型未返回完整有效的政策判定，本次未生成匹配结果和 AI 建议，请重试。', 503) from exc
    out = []
    for code, (d, s, meta) in order:
        d = docs[code]
        j = judged.get(code, {})
        out.append({
            'code': code,
            'name': meta.get('policy_name') or '',
            'applicable': j.get('applicable') is True,
            'judge_status': 'complete' if isinstance(j.get('applicable'), bool) else 'unavailable',
            'version': meta.get('version', ''),
            'reason': j.get('reason', '') or '',
            'missing': j.get('missing', '') or '',
            'next': j.get('next', '') or '',
            'sim': round(s, 3),
            'excerpt': d[:300],
            'doc': d,
            'evidence': evidence[code],
        })
    return out


def qa_stream(question, history=None, params=None, session_id=None, source='web', user_id=None, chat_only=False):
    """流式 RAG 问答：检索 → 模型判定适用清单 → 按适用政策流式回答 → 写 qa_log。
    返回 (generator, matched, meta)；落库在耗尽生成器后。
    chat_only=True：纯交流（寒暄/系统能力等，路由层已判定），跳过检索与政策判定，直接自然对话。"""
    params = params or {}
    if chat_only:
        from authentication import ApiError
        hist = (history or [])[-8:]
        convo = '\n'.join(('用户：' if h.get('role') == 'user' else '助手：') + (h.get('content') or '')[:600]
                          for h in hist if h.get('content'))
        prompt = (('【对话历史】\n' + convo + '\n【本轮用户消息】' + question) if convo else question)
        _check_cancel(session_id)
        try:
            reply = _llm_answer(prompt, '（无政策检索，普通交流场景）',
                                system=('你是基层政务帮扶政策助手的对话界面。用户此刻不是在描述家庭情况或查询具体政策资格，'
                                        '而是普通交流（寒暄、感谢、询问你能做什么等）。用2至4句自然中文回应，语气友好务实；'
                                        '可简要说明你可以帮忙核对家庭情况、匹配适用政策、解答办理流程，并邀请对方直接描述家庭情况或提出问题。'
                                        '不编造政策条款，不承诺任何资格。'),
                                temperature=0.4, purpose='consult_advice')
        except Exception:
            reply = ''
        if not isinstance(reply, str) or not reply.strip():
            # 模型不可用时也要给一句像样的普通交流回复，绝不把判定错误文案抛给聊天用户。
            reply = ('您好，我是帮扶政策助手：可以帮您核对家庭情况（人数、年龄、健康或残疾情况、所在地区、收入来源等），'
                     '匹配可申请的政策，也能解答申请条件、所需材料和办理流程。您可以直接描述家庭情况或提出具体政策问题。')
        return _one_shot(reply.strip()), [], {'top_chunks': 0, 'session_id': session_id,
                                              'sources': [], 'matched_list': [], 'judge_status': 'chat'}
    from policy_core import enabled
    if enabled():
        from policy_core.consultation import run
        # 上下文记忆：近期对话含助手回复（话题脉络完整），指代型追问才能接得上。
        prior_lines = []
        for h in (history or [])[-6:]:
            content = (h.get('content') or '').strip()
            if not content:
                continue
            prior_lines.append(('用户：' if h.get('role') == 'user' else '助手：') + content[:300])
        prior = '\n'.join(prior_lines)
        effective_question = '本轮问题：' + question + (
            '\n近期对话（仅作话题线索，用于理解指代，不是政策证据）：\n' + prior if prior else '')
        body,matched,meta=run(effective_question,_llm_answer,search,_complete_evidence,top_k=TOP_K,threshold=float(params.get('threshold',.35)))
        meta['session_id']=session_id
        return _one_shot(body),matched,meta
    # 全模型模式（POLICY_MATCH_MODE=full_model）下的政策咨询：只检索原文、只依据原文回答，
    # 不做家庭资格判定。历史缺陷：该模式下咨询问句被送进 legacy 资格判定流水线，
    # judge 结构校验失败后抛"模型未返回完整有效的政策判定"，普通对话直接变成报错。
    from policy_core.full_settings import enabled as _full_model
    if _full_model():
        return _consult_answer(question, history=history, params=params, session_id=session_id)
    temperature = float(params.get('temperature', 0.3))
    threshold = float(params.get('threshold', 0.35))
    history = history or []
    hist = history[-12:]

    trusted_context = '\n'.join(h['content'][:2000] for h in hist[-4:] if h.get('role') == 'user')
    effective_question = ('【本轮问题】' + question + '\n【此前已确认对话】' + trusted_context) if trusted_context else question
    # 回答阶段的上下文记忆：检索只用用户侧问题（助手长文本会污染召回），
    # 但生成回答时带上近期对话，指代与话题（"上面说的那个补贴"）才能连贯。
    convo_lines = []
    for h in hist[-6:]:
        content = (h.get('content') or '').strip()
        if content:
            convo_lines.append(('用户：' if h.get('role') == 'user' else '助手：') + content[:400])
    convo_digest = '\n'.join(convo_lines)
    answer_question = (effective_question + '\n【近期对话】\n' + convo_digest) if convo_digest else effective_question
    _check_cancel(session_id)
    try:
        matched_list = match_policies(effective_question, top_k=TOP_K, threshold=threshold, session_id=session_id)
    except Exception:
        _check_cancel(session_id)  # 用户主动取消/中断时仍然抛出，不被兜底吞掉
        # 资格判定流水线失败时退化为原文咨询答复，而不是把内部错误文案当成答案返回。
        return _consult_answer(question, history=history, params=params, session_id=session_id)
    applicable = [x for x in matched_list if x['applicable']]
    not_applicable = [x for x in matched_list if not x['applicable']]
    matched = sorted({x['code'] for x in applicable})
    sources = [{'policy_code': x['code'], 'policy_name': x['name'],
                'excerpt': x['excerpt']} for x in applicable]

    ctx_parts = ['【%s】（相关度 %.2f）\n%s' % (x['name'], x['sim'], x['doc'])
                 for x in applicable]
    if not_applicable:
        ctx_parts.append('【不适用政策（不要为用户推荐，可在回答中一句话说明不适用）】%s'
                         % '、'.join('%s(%s)' % (x['name'], x['code']) for x in not_applicable))
    context = '\n\n'.join(ctx_parts) or '（未检索到相关政策）'
    if len(context) + len(effective_question) > config.KG_MAX_PROMPT_CHARS:
        from authentication import ApiError
        raise ApiError('相关政策证据较多，请限定地区或政策主题后重试；系统未截掉条件生成回答。', 503)

    lines = ['**匹配结果**', '政策咨询参考（不代表家庭已符合申请资格；以下条目和导出表格使用同一份结果）：']
    for x in applicable:
        lines += ['\n**%s（%s）**' % (x['name'], x['code']), '判定说明：' + x['reason'],
                  '政策依据原文：\n' + x['doc'], '待补充：' + (x['missing'] or '无'), '办理核实：' + (x['next'] or '按原文联系经办部门')]
    if not applicable:
        lines.append('当前没有已确认的推荐政策。')
    _check_cancel(session_id)
    advice = _llm_answer(answer_question, context,
                         system=_QA_SYSTEM + '\n仅基于本次判定相关的政策给出办理及核实建议，不得新增政策或改变判定。若近期对话已明确当前话题，回答须延续该话题指代。',
                         temperature=temperature, purpose='consult_advice')
    if not isinstance(advice, str) or not advice.strip():
        from authentication import ApiError
        raise ApiError('模型未返回有效的 AI 建议，本次未生成完整结果，请重试。', 503)
    lines += ['\n**AI 建议**', advice]
    return _one_shot('\n'.join(lines)), matched, {'top_chunks': len(matched_list), 'session_id': session_id,
            'sources': sources, 'matched_list': matched_list,
            'judge_status': 'complete'}


def _run_match_pipeline(hh, recs, question, session_id, source, user_id, label='', graph_meta=None):
    """Eligibility belongs to evidence; LLM only explains priorities."""
    from household_flow import final_result
    return final_result(hh, recs, question, session_id, graph_meta, _llm_answer)


def ui_meta_from_match_json(mj):
    """从落库 match_json 重建对话流 UI 数据（历史会话恢复用）。
    返回 {'sources','analysis_md','table_rows','cards','questions'}。非户信息流返回空。"""
    import smart_matcher_kb as smk
    if not mj:
        return {'sources': [], 'analysis_md': '', 'table_rows': [], 'cards': [], 'questions': []}
    if not isinstance(mj, dict):
        return {}
    if mj.get('mode') == 'full-model-v1':
        from policy_core.result_model import table
        rows=mj.get('table_recs') or []
        return dict(sources=[dict(policy_code=r['code'],policy_name=r['name'],excerpt=r.get('basis','')) for r in rows],analysis_md='',
                    table_rows=mj.get('export_rows') or table(rows),cards=rows,questions=mj.get('questions',[]))
    if mj.get('mode') == 'showcase-v1':
        rows = mj.get('table_recs') or []
        return dict(sources=[dict(policy_code=r['code'],policy_name=r['name'],excerpt=r.get('basis','')) for r in rows],
                    analysis_md='',table_rows=mj.get('export_rows') or [],cards=rows,questions=mj.get('questions',[]))
    if mj.get('flow_version') in (1,2):
        rows = mj.get('table_recs') or []
        return dict(sources=[dict(policy_code=r['code'],policy_name=r['name'],excerpt=r.get('basis','')) for r in rows],
                    analysis_md='',table_rows=smk.table_rows_from_recs(rows),cards=rows,questions=mj.get('questions',[]))
    table_recs = mj.get('table_recs') or []
    hh_simple = mj.get('hh') or {}
    if not hh_simple.get('members'):
        return {'sources': [], 'analysis_md': '', 'table_rows': [], 'cards': [], 'questions': []}
    hh = smk.hh_from_simple(hh_simple)
    analysis = mj.get('analysis')
    from household_state import missing
    questions = []
    return {
        'sources': [{'policy_code': r['code'], 'policy_name': r['name'],
                     'excerpt': (r.get('reason') or '')[:200]} for r in table_recs],
        'analysis_md': '此为历史结果，尚未按新版流程复核。继续补充家庭情况可更新匹配。',
        'table_rows': smk.table_rows_from_recs(table_recs),
        'cards': table_recs[:8],
        'questions': questions,
    }


def profile_stream(hh, question, session_id, user_id):
    from policy_core import enabled
    if enabled():
        from policy_core.flow import run
        body,matched,meta=run(hh,question,session_id,user_id,_llm_answer)
        return _one_shot(body),matched,meta
    import smart_matcher_kb as smk
    from household_state import simple, missing
    # 阶段二：江西业务范围校验（家庭匹配入口）。
    # 仅基于家庭所属地区字段；江西成员省外务工/就学不受影响。
    from kg_retrieval import service_scope_check
    scope, scope_reason = service_scope_check(hh)
    if scope == 'blocked':
        # 家庭所属地区明确在江西省外：提示服务范围，不进入候选召回与资格核验。
        meta = {'sources': [], 'session_id': session_id, 'top_chunks': 0, 'questions': [],
                'table_rows': [], 'will_missing': False,
                'match_json': {'hh': simple(hh), 'table_recs': [], 'recs': []},
                'scope': {'status': 'blocked', 'reason': scope_reason}}
        return _one_shot(scope_reason), [], meta
    from household_flow import basics, conflict_questions, schedule, clarification_result, policy_questions
    questions = schedule(hh, conflict_questions(hh) + basics(hh))
    if scope == 'ask' and not any(q['key']=='region:0' for q in questions):
        questions = schedule(hh, [dict(key='region:0', label=scope_reason, kind='text', value='')])
    meta = {'will_missing': False}
    if questions:
        answer, matched, result = clarification_result(hh, questions, session_id)
        return _one_shot(answer), matched, result
    graph_meta = None
    if getattr(config, 'KG_GRAPH_ENABLED', False):
        from authentication import ApiError
        try:
            import kg_retrieval as kg
            from kg_batch5_merge import merge_candidates
            data = smk.load_db()
            rules = smk.match_profile(hh, apply_strategy=False, data=data)
            recall = kg.kg_search(hh, top_k=max(1,len(data['policies'])), data=data)
            merged = merge_candidates(rules, recall, hh, data,
                row_limit=None, kg_cap=None,
                detail_cap=None, evidence_chars=config.KG_EVIDENCE_CHARS)
            recs = merged['rows']
            graph_meta = dict(merged['stats'], enabled=True,
                new_policies=len({r['code'] for r in recs} - {r['code'] for r in rules}),
                excluded=[{'code':r['code'], 'member':r['member'], 'detail_id':r['detail_id'],
                           'evidence':r['forbid_v2']['evidence']} for r in merged['excluded']],
                dropped=[{'code':r['code'], 'member':r['member'], 'detail_id':r['detail_id'],
                          'reason':r['drop_reason']} for r in merged['dropped_kg']])
        except ApiError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception('Graph household matching failed: session=%s', session_id)
            raise ApiError('图谱检索或政策核验失败，本次未生成匹配结果和 AI 建议，请重试。', 503) from exc
    else:
        recs = smk.match_profile(hh)
    questions = schedule(hh, policy_questions(hh, recs))
    if questions:
        answer, matched, result = clarification_result(hh, questions, session_id)
        result['match_json']['recs'] = recs
        result['match_json']['graph'] = graph_meta
        return _one_shot(answer), matched, result
    answer, matched, result = _run_match_pipeline(hh, recs, question, session_id, 'web', user_id,
                                                  graph_meta=graph_meta)
    result['will_missing'] = meta['will_missing']
    return _one_shot(answer), matched, result


def qa_match_stream(question, session_id=None, source='web', user_id=None,reuse_hh=None):
    from policy_core.full_settings import enabled as full_enabled
    if full_enabled():
        from policy_core.full_flow import run
        from policy_core.facts import resolve as full_resolve
        import copy
        hh=copy.deepcopy(reuse_hh) if reuse_hh is not None else full_resolve(question,session_id,user_id)
        if reuse_hh is not None:hh.setdefault('policy_core',{}).update(round=0,pending=[])
        body,matched,meta=run(hh,question,session_id,user_id,_llm_answer)
        return _one_shot(body),matched,meta
    from household_state import resolve, latest, simple
    from household_flow import explain_policy
    from policy_core import enabled
    from policy_core.showcase import enabled as showcase_enabled
    if showcase_enabled():
        from policy_core.showcase import run
        from policy_core.facts import resolve as core_resolve
        import copy
        hh=copy.deepcopy(reuse_hh) if reuse_hh is not None else core_resolve(question,session_id,user_id)
        if reuse_hh is not None:hh.setdefault('policy_core',{}).update(round=0,pending=[])
        body,matched,meta=run(hh,question,session_id,user_id,_llm_answer)
        return _one_shot(body),matched,meta
    if enabled():
        from policy_core.flow import run
        if reuse_hh is not None:
            import copy
            hh=copy.deepcopy(reuse_hh)
            hh.setdefault('policy_core',{}).update(round=0,pending=[])
        else:
            from policy_core.facts import resolve as core_resolve
            hh=core_resolve(question,session_id,user_id)
        body,matched,meta=run(hh,question,session_id,user_id,_llm_answer)
        return _one_shot(body),matched,meta
    if re.search(r'为什么|为何|没匹配|未推荐|不推荐',question):
        hh=latest(session_id,user_id)
        if hh:
            explanation=explain_policy(hh,question)
            if explanation:
                meta=dict(sources=[],table_rows=[],cards=[],questions=[],session_id=session_id,
                          match_json=dict(hh=simple(hh),table_recs=[],questions=[],flow_version=1))
                return _one_shot(explanation),[],meta
    return profile_stream(resolve(question, session_id, user_id), question, session_id, user_id)


def refine_match_stream(session_id, answers, source='refine', user_id=None):
    import smart_matcher_kb as smk
    from household_state import latest, attach_surveys
    from authentication import ApiError
    hh = latest(session_id, user_id)
    if not hh:
        raise ApiError('请先输入家庭信息，再补充问卷')
    from policy_core.full_settings import enabled as full_enabled,MODE as FULL_MODE
    if full_enabled():
        if hh.get('policy_core',{}).get('mode')!=FULL_MODE:raise ApiError('请先按全库模型模式重新分析家庭，再回答问题',409)
        from policy_core.full_flow import run
        try:body,matched,meta=run(hh,'追问补充',session_id,user_id,_llm_answer,answers=answers)
        except (ValueError,TypeError) as exc:raise ApiError(str(exc),400) from exc
        return _one_shot(body),matched,meta
    from policy_core import enabled
    from policy_core.showcase import enabled as showcase_enabled
    if showcase_enabled() and hh.get('policy_core',{}).get('mode')=='showcase-v1':
        from policy_core.showcase import run
        try:body,matched,meta=run(hh,'追问补充',session_id,user_id,_llm_answer,answers=answers)
        except (ValueError,TypeError) as exc:raise ApiError(str(exc),400) from exc
        return _one_shot(body),matched,meta
    if enabled() and hh.get('policy_core'):
        from policy_core.flow import run
        try:body,matched,meta=run(hh,'追问补充',session_id,user_id,_llm_answer,answers=answers)
        except (ValueError,TypeError) as exc:raise ApiError(str(exc),400) from exc
        return _one_shot(body),matched,meta
    for key, value in answers.items():
        if key.startswith('age:'):
            if str(value) not in ('不清楚','暂时无法提供','未知') and (not str(value).isdigit() or not 0 <= int(value) <= 120):
                raise ApiError('年龄需为 0 到 120 的整数')
    try:
        smk.apply_answers(hh, answers)
    except (ValueError,TypeError) as exc:
        raise ApiError(str(exc),400) from exc
    smk._normalize_members(hh)
    attach_surveys(hh, session_id, user_id)
    return profile_stream(hh, '追问补充', session_id, user_id)


def _one_shot(answer):
    """一次性输出的生成器（保持 SSE 结构）。"""
    def gen():
        for i in range(0, len(answer), 256):
            yield answer[i:i + 256]
    return gen()


_CONSULT_SYSTEM = (
    '你是基层帮扶政策咨询助手，负责回答政策咨询（申请条件、需要的材料、办理流程、补助标准、适用对象、地区差异等）。'
    '硬约束：1) 只能依据【政策原文摘录】作答，原文没有写的口径、金额、时限、部门名称要明确说"原政策未明确"；'
    '2) 不得推测、不得编造条款，不得声称用户一定符合条件；'
    '3) 若摘录里没有用户问的政策或地区，直说库里没有覆盖，并说明可向当地乡镇（街道）经办部门确认；'
    '4) 用自然中文段落回答，2至5句，必要时分点；不要输出检索过程、编号、"匹配结果""核验"等内部术语。'
)


def _consult_docs(question, top_k=6):
    """政策咨询取证：向量近邻切片（按原文去重），返回 (原文档, 来源摘要)。"""
    docs, sources, seen = [], [], set()
    try:
        hits = search(question, top_k=top_k) or []
    except Exception:
        hits = []
    for item in hits:
        try:
            text, _sim, meta = item
        except Exception:
            continue
        doc = (text or '').strip()
        if not doc:
            continue
        sig = doc[:60]
        if sig in seen:
            continue
        seen.add(sig)
        meta = meta or {}
        name = meta.get('policy_name') or meta.get('name') or meta.get('policy_code') or '政策原文'
        docs.append('【%s】\n%s' % (name, doc[:1800]))
        sources.append({'policy_code': meta.get('policy_code'), 'policy_name': name, 'excerpt': doc[:300]})
    return docs, sources


def _consult_fallback(docs):
    """模型不可用时的兜底：不编造，直接把检索到的原文摘录交给用户。"""
    if not docs:
        return ('这次没有检索到与您问题直接相关的政策原文，我不做推测。'
                '您可以补充政策名称或所在县区，我再核对；也可以向当地乡镇（街道）经办部门确认现行口径。')
    return ('这次没有生成整理后的回答，我把检索到的政策原文摘录放在下面，供您直接核对：\n\n' + '\n\n'.join(docs[:3]))


def _consult_answer(question, history=None, params=None, session_id=None):
    """全模型模式的政策咨询：只依据原文摘录回答，模型失败时退回原文摘录（绝不返回判定错误文案）。"""
    _check_cancel(session_id)
    # 上下文记忆（咨询路径）：短问句/指代型追问（"它需要什么材料"）单看本身检索不到东西，
    # 把上一轮用户问题作为话题线索拼进检索查询，回答才接得上。
    retrieval_query = question
    prior_user = [(h.get('content') or '').strip() for h in (history or [])
                  if h.get('role') == 'user' and (h.get('content') or '').strip()]
    if prior_user and (len(question) <= 24 or re.search(r'(它|这个|那个|该政策|这项|上面|刚才|这政策|那政策)', question)):
        retrieval_query = prior_user[-1][:200] + ' ' + question
    docs, sources = _consult_docs(retrieval_query)
    convo = '\n'.join(('用户：' if h.get('role') == 'user' else '助手：') + (h.get('content') or '')[:400]
                      for h in (history or [])[-6:] if (h.get('content') or '').strip())
    context = '\n\n'.join(docs) or '（本次未检索到相关原文）'
    prompt = question + ('\n\n【近期对话（仅供理解指代，不是政策依据）】\n' + convo if convo else '')
    reply = ''
    try:
        reply = _llm_answer(prompt, context, system=_CONSULT_SYSTEM, temperature=0.25,
                            purpose='consult_policy')
    except Exception:
        reply = ''
    body = reply.strip() if isinstance(reply, str) else ''
    if len(body) < 12:
        body = _consult_fallback(docs)
    return _one_shot(body), [], {'top_chunks': len(docs), 'session_id': session_id,
                                 'sources': sources, 'matched_list': [], 'judge_status': 'consult'}



def qa(question, username=None, session_id=None, source='web', user_id=None):
    """RAG 问答：检索 → 拼接 → LLM → 写 qa_log（Kingbase）。返回 (answer, matched, detail)"""
    hits = search(question)
    matched = sorted({m['policy_code'] for _, _, m in hits if m.get('policy_code')})
    context = '\n\n'.join('【%s】（相关度 %.2f）\n%s' % (
        (m.get('policy_name') or ''), s, d[:3000]) for d, s, m in hits)
    answer = _llm_answer(question, context, purpose='qa')
    detail = {'top_chunks': len(hits)}
    return answer, matched, detail
