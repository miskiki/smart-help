# -*- coding: utf-8 -*-
"""模型 provider 抽象层（docs/redesign/30-custom-model-registry.md §4.3）。

核心原则：调用点只认模型行，按行现场构造 endpoint 配置，无池化——
改完凭证/换默认，下次调用自动生效，不用重启、不用缓存失效。

P1 只提供 chat provider（openai，OpenAI 兼容，覆盖远端与本地 llama）。
embed / rerank provider 在 P2/P3 按同构扩展。

MODEL_REGISTRY 总开关：0（默认）= 问答链全走 yaml 平台（现状零变化）；
1 = 对话链从注册表取默认模型行。
"""
import logging
import os
import time
from urllib.parse import urlparse

_LOG = logging.getLogger(__name__)
_ON_VALUES = ('1', 'on', 'true', 'yes')

# 支持的 provider 清单（前端 /providers 接口 + 默认 URL 预填）
PROVIDERS = {
    'openai': {
        'label': 'OpenAI 兼容（含本地 llama-server / Ollama / 各云厂商兼容端点）',
        'chat': True, 'embed': True, 'rerank': True,
        'default_base_url': 'https://api.openai.com/v1',
    },
    'dashscope_rerank': {
        'label': 'DashScope 原生重排（百炼 rerank 网关）',
        'chat': False, 'embed': False, 'rerank': True,
        'default_base_url': 'https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank',
    },
}

VALID_TYPES = ('chat', 'embed', 'rerank')
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '0.0.0.0')


def registry_enabled():
    """MODEL_REGISTRY 总开关。默认 0 = 全走 yaml 平台，现网零行为变化。"""
    return str(os.environ.get('MODEL_REGISTRY', '') or '').strip().lower() in _ON_VALUES


def validate_base_url(url, type_):
    """SSRF 校验：仅 http/https、主机名非空。不封私网（本地 llama 合法）。
    返回 (ok, reason)；ok=False 时 reason 是可直接回前端的文案。"""
    url = (url or '').strip()
    if not url:
        return False, 'base_url 不能为空'
    try:
        p = urlparse(url)
    except Exception:
        return False, 'base_url 不是合法 URL'
    if p.scheme not in ('http', 'https'):
        return False, 'base_url 只支持 http/https'
    if not p.hostname:
        return False, 'base_url 缺少主机名'
    if type_ == 'rerank' and p.scheme == 'http' and p.hostname not in LOCAL_HOSTS:
        return False, '远端重排端点请用 https（本地 127.0.0.1 除外）'
    return True, ''


def _row_dict(row):
    """ORM 行 → dict（统一字段访问，测试可注入纯 dict）。"""
    if hasattr(row, '_mapping'):
        return dict(row)
    if isinstance(row, dict):
        return row
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def default_chat_cfg():
    """注册表默认对话行（type=chat, is_default=TRUE）→ cfg dict。
    无行/无默认/表未迁移（捕获异常）返回 None，调用方回落 yaml 平台。"""
    return _default_row_cfg('chat', row_to_chat_cfg)


def default_embed_cfg():
    """注册表默认嵌入行（type=embed, is_default=TRUE）→ cfg dict。
    无行/无默认/表未迁移（捕获异常）返回 None，调用方回落 yaml 平台。"""
    return _default_row_cfg('embed', row_to_embed_cfg)


def default_rerank_cfg():
    """注册表默认重排行（type=rerank, is_default=TRUE）→ cfg dict。
    无行/无默认/表未迁移（捕获异常）返回 None，调用方回落 yaml 平台 profile。"""
    return _default_row_cfg('rerank', row_to_rerank_cfg)


def _default_row_cfg(type_, to_cfg):
    try:
        from models import Model
        row = Model.query.filter_by(type=type_, is_default=True).first()
    except Exception as exc:
        _LOG.warning('model_providers: models 表不可用（未迁移？），回落 yaml 平台：%s', exc)
        return None
    return to_cfg(row) if row else None


def row_to_chat_cfg(row):
    """模型行 → kb_rag 现有 cfg dict 形状（load_model_cfg 的返回格式）。

    这样 kb_rag._llm_answer/_llm_stream/recognize.llm_extract 的调用代码零改动，
    只换 cfg 的来源（yaml 平台 → 注册表行）。
    """
    d = _row_dict(row)
    from model_creds import decrypt
    return {
        'kind': 'chat',
        'platform': 'registry:%s' % d.get('id', ''),
        'base_url': (d.get('base_url') or '').rstrip('/'),
        'api_key': decrypt(d.get('api_key_enc')),
        'llm_model': d.get('model_name') or '',
        'embed_model': '',
        'rerank_model': '',
        'embed_dim': 1536,
        'rerank_endpoint': '',
        'quality_tier': 'high',
        'embed_query_instruction': '',
        'json_mode': bool(d.get('json_mode')),
    }


def row_to_embed_cfg(row):
    """嵌入模型行 → kb_rag._embed/_embed_cfg 现有 cfg dict 形状（load_model_cfg 返回格式）。

    这样 kb_rag._embed/_embed_queries/sync_policy 的调用代码零改动，只换 cfg 来源
    （yaml 平台 → 注册表行）。embed_dim 取行内 dim（行级维度，废除全局 index_dim 锁）。
    """
    d = _row_dict(row)
    from model_creds import decrypt
    # 重排端点：P2 继承当前 yaml 向量平台 profile（重排端点正交于嵌入模型，是本地栈属性，
    # policy_core/retrieval.py 与 preflight 依赖它）；P3 再切到注册表默认重排行。
    try:
        from model_registry import embed_cfg as _yaml_embed_cfg
        rerank_endpoint = (_yaml_embed_cfg().get('rerank_endpoint') or '').strip()
    except Exception:
        rerank_endpoint = ''
    return {
        'kind': 'embed',
        'platform': 'registry:%s' % d.get('id', ''),
        'base_url': (d.get('base_url') or '').rstrip('/'),
        'api_key': decrypt(d.get('api_key_enc')),
        'llm_model': '',
        'embed_model': d.get('model_name') or '',
        'rerank_model': '',
        'embed_dim': int(d.get('dim') or 1536),
        'rerank_endpoint': rerank_endpoint,
        'quality_tier': 'high',
        'embed_query_instruction': d.get('query_instruction') or '',
        'json_mode': False,
    }


def row_to_rerank_cfg(row):
    """重排模型行 → 重排 cfg dict（recognize.rerank_scores / policy_core source-ranker 形状）。

    端点按 provider 构造：dashscope_rerank 的 base_url 即完整重排 URL（DashScope 原生或
    本地 serve_qwen3_rerank 兼容层，两者都说 {model,input:{query,documents}}→output.results[]）；
    openai 的 base_url 是端点根，重排地址为 base_url/rerank（OpenAI/Jina 格式）。
    """
    d = _row_dict(row)
    from model_creds import decrypt
    provider = d.get('provider') or 'openai'
    base = (d.get('base_url') or '').rstrip('/')
    endpoint = base + '/rerank' if provider == 'openai' else base
    return {
        'kind': 'rerank',
        'rerank_model': d.get('model_name') or '',
        'rerank_endpoint': endpoint,
        'api_key': decrypt(d.get('api_key_enc')),
        'provider': provider,
    }


def test_with_cfg(cfg, timeout=60):
    """按 cfg['kind'] 复用 chat/embed/rerank 的 HTTP 调用主体做连通性探测。

    cfg 键同 row_to_*_cfg 的字段集合（含 kind：chat/embed/rerank）。
    返回 {ok, latency_ms, detail}。不写库、不缓存、不触发 reindex——
    供 /test-draft 保存前探测与 test_chat/test_embed/test_rerank 薄封装共用。
    """
    import requests
    kind = cfg.get('kind')
    if kind == 'chat':
        if not cfg['llm_model']:
            return {'ok': False, 'latency_ms': 0, 'detail': '模型未填 model_name'}
        headers = {'Authorization': 'Bearer %s' % cfg['api_key'], 'Content-Type': 'application/json'}
        body = {'model': cfg['llm_model'],
                'messages': [{'role': 'user', 'content': '连通性测试，请回复 ok'}],
                'max_tokens': 16, 'temperature': 0}
        start = time.time()
        try:
            r = requests.post(cfg['base_url'] + '/chat/completions',
                              json=body, headers=headers, timeout=timeout)
            ms = int((time.time() - start) * 1000)
            if r.status_code != 200:
                return {'ok': False, 'latency_ms': ms,
                        'detail': 'HTTP %s：%s' % (r.status_code, r.text[:200])}
            data = r.json()
            content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
            if not str(content).strip():
                return {'ok': False, 'latency_ms': ms, 'detail': '响应无内容（choices 为空）'}
            return {'ok': True, 'latency_ms': ms, 'detail': '模型回复：%s' % str(content).strip()[:80]}
        except requests.exceptions.RequestException as exc:
            return {'ok': False, 'latency_ms': int((time.time() - start) * 1000),
                    'detail': '连接失败：%s' % exc}
        except Exception as exc:
            return {'ok': False, 'latency_ms': int((time.time() - start) * 1000),
                    'detail': '响应解析失败：%s' % exc}
    if kind == 'embed':
        if not cfg['embed_model']:
            return {'ok': False, 'latency_ms': 0, 'detail': '模型未填 model_name'}
        dim = cfg['embed_dim']
        headers = {'Content-Type': 'application/json'}
        if cfg['api_key'] and cfg['api_key'] != 'EMPTY':
            headers['Authorization'] = 'Bearer %s' % cfg['api_key']
        body = {'model': cfg['embed_model'], 'input': ['连通性测试'], 'dimensions': dim}
        start = time.time()
        try:
            r = requests.post(cfg['base_url'] + '/embeddings', json=body, headers=headers, timeout=timeout)
            ms = int((time.time() - start) * 1000)
            if r.status_code != 200:
                return {'ok': False, 'latency_ms': ms, 'detail': 'HTTP %s：%s' % (r.status_code, r.text[:200])}
            data = r.json().get('data') or []
            if not data:
                return {'ok': False, 'latency_ms': ms, 'detail': '响应无向量（data 为空）'}
            actual = len(data[0]['embedding'])
            if actual != dim:
                return {'ok': False, 'latency_ms': ms,
                        'detail': '实测维度 %d 与填写 dim=%d 不符，请修正 dim 后保存' % (actual, dim)}
            return {'ok': True, 'latency_ms': ms, 'detail': '实测维度 %d 一致，可用作向量模型' % actual}
        except requests.exceptions.RequestException as exc:
            return {'ok': False, 'latency_ms': int((time.time() - start) * 1000), 'detail': '连接失败：%s' % exc}
        except Exception as exc:
            return {'ok': False, 'latency_ms': int((time.time() - start) * 1000), 'detail': '响应解析失败：%s' % exc}
    if kind == 'rerank':
        if not cfg['rerank_model']:
            return {'ok': False, 'latency_ms': 0, 'detail': '模型未填 model_name'}
        query, documents = '连通性测试', ['测试文档一', '测试文档二']
        start = time.time()
        try:
            scores = rerank_scores(cfg, query, documents, timeout=timeout)
            ms = int((time.time() - start) * 1000)
            if scores is None:
                return {'ok': False, 'latency_ms': ms, 'detail': '重排调用失败（端点不可达或格式不符）'}
            if not any(s > 0 for s in scores):
                return {'ok': False, 'latency_ms': ms, 'detail': '重排返回全 0 分（模型未加载或格式不符）'}
            return {'ok': True, 'latency_ms': ms, 'detail': '重排 %d 条文档成功，最高分 %.3f' % (len(scores), max(scores))}
        except Exception as exc:
            return {'ok': False, 'latency_ms': int((time.time() - start) * 1000), 'detail': '测试异常：%s' % exc}
    return {'ok': False, 'latency_ms': 0, 'detail': '未知 kind：%s' % kind}


def test_embed(row, timeout=60):
    """嵌入连通性测试（对齐参考项目 /models/:id/debug）：实测 1 条文本，回传实际维度。
    实测维度与行内 dim 不符则 ok=False 并提示修正（防止填错维度导致索引锁死）。
    返回 {ok, latency_ms, detail}。用生产同款 OpenAI 兼容 /embeddings。
    薄封装：委托 test_with_cfg，行为不变。"""
    return test_with_cfg(row_to_embed_cfg(row), timeout)


def row_meta(row):
    """列表/详情接口的安全投影：api_key 恒空串，只回 configured 布尔。"""
    from model_creds import is_configured
    d = _row_dict(row)
    host = ''
    try:
        host = urlparse(d.get('base_url') or '').hostname or ''
    except Exception:
        host = ''
    return {
        'id': d.get('id'), 'type': d.get('type'), 'name': d.get('name'),
        'source': d.get('source'), 'provider': d.get('provider'),
        'base_url': d.get('base_url') or '', 'model_name': d.get('model_name') or '',
        'dim': d.get('dim'), 'query_instruction': d.get('query_instruction') or '',
        'json_mode': bool(d.get('json_mode')), 'is_default': bool(d.get('is_default')),
        'api_key': '', 'api_key_configured': is_configured(d.get('api_key_enc')),
        'kind': '本地' if host in LOCAL_HOSTS else '云端',
        'created_at': str(d.get('created_at') or ''), 'updated_at': str(d.get('updated_at') or ''),
    }


def test_chat(row, timeout=60):
    """连通性测试（对齐参考项目 /models/:id/debug）：对已存模型发一句实测对话。
    返回 {ok, latency_ms, detail}。用生产同款 OpenAI 兼容 /chat/completions。
    薄封装：委托 test_with_cfg，行为不变。"""
    return test_with_cfg(row_to_chat_cfg(row), timeout)


def rerank_scores(rerank_cfg, query, documents, timeout=60):
    """按 provider 调重排模型打分，返回 list[float]（与 documents 等长）；无模型/无文档/调用失败返回 None。

    dashscope_rerank（默认）：请求 {model, input:{query, documents}}，响应 output.results[].relevance_score。
    openai：请求 {model, query, documents}，响应 results[].relevance_score。
    端点留空时 dashscope_rerank 回落 DashScope 原生网关（compatible-mode /rerank 在该网关 404）。
    """
    import requests
    model = (rerank_cfg or {}).get('rerank_model')
    if not model or not documents:
        return None
    provider = (rerank_cfg or {}).get('provider') or 'dashscope_rerank'
    api_key = (rerank_cfg or {}).get('api_key', '')
    headers = {'Content-Type': 'application/json'}
    if api_key and api_key != 'EMPTY':
        headers['Authorization'] = 'Bearer %s' % api_key
    try:
        if provider == 'openai':
            url = (rerank_cfg or {}).get('rerank_endpoint')
            if not url:
                return None
            r = requests.post(url, json={'model': model, 'query': query, 'documents': documents},
                              headers=headers, timeout=timeout)
            if r.status_code != 200:
                return None
            results = r.json().get('results') or []
        else:
            url = ((rerank_cfg or {}).get('rerank_endpoint')
                   or 'https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank')
            r = requests.post(url, json={'model': model, 'input': {'query': query, 'documents': documents}},
                              headers=headers, timeout=timeout)
            if r.status_code != 200:
                return None
            results = ((r.json().get('output') or {}).get('results')) or []
        scores = [0.0] * len(documents)
        for item in results:
            idx = item.get('index')
            if isinstance(idx, int) and 0 <= idx < len(documents):
                scores[idx] = float(item.get('relevance_score', 0.0))
        return scores
    except Exception:
        return None


def test_rerank(row, timeout=60):
    """重排连通性测试（对齐参考项目 /models/:id/debug）：实测 1 组 query+2 条文档。
    按行内 provider 走对应请求/响应格式；拿到任意重排结果即 ok=True。返回 {ok, latency_ms, detail}。
    薄封装：委托 test_with_cfg，行为不变。"""
    return test_with_cfg(row_to_rerank_cfg(row), timeout)
