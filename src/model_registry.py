# -*- coding: utf-8 -*-
"""模型能力注册表：把"维度/端点/能力"从代码剥离到 yaml，切换模型只改配置不改代码。

- 对话平台：app_config.qa_llm_platform（沿用现有机制，前端下拉切换）
- 向量平台：app_config.qa_embed_platform（默认 yaml 顶层 DEFAULT_EMBED_PLATFORM，可被
  环境变量 EMBED_PLATFORM 覆盖；对话切换不影响向量平台，避免误触发索引重建）
- 每个平台在 model_settings.yaml 的 profile 中声明能力参数：
    embed_dim        向量维度（bailian 1536 / qwen3-embedding-4b 2560）
    rerank_endpoint  本地 rerank 端点（留空 = 走 DashScope 原生默认）
    quality_tier     能力档位（保留位，供判定策略参考）

对外接口：
    embed_dim() / embed_cfg() / rerank_cfg()     当前向量平台能力
    index_dim() / set_index_dim(dim)             存量索引维度（app_config.vector_index_dim）
    check_index_compat()                         写入/检索前校验，维度不一致即报错提示重建
"""
import os
import time

import yaml
from sqlalchemy import create_engine, text

import config
from recognize import MODEL_YAML, load_model_cfg

# app_config 键
KEY_EMBED_PLATFORM = 'qa_embed_platform'
KEY_INDEX_DIM = 'vector_index_dim'

_EMBED_PLATFORM_CACHE = {'ts': 0.0, 'platform': None}
_INDEX_DIM_CACHE = {'ts': 0.0, 'dim': None}


def _db():
    return create_engine(config.DATABASE_URL)


def _read_cfg(key):
    with _db().connect() as c:
        r = c.execute(
            text("SELECT cfg_value FROM app_config WHERE cfg_key = :k"),
            {'k': key}).fetchone()
        return r[0] if r and r[0] else None


def _write_cfg(key, value):
    with _db().connect() as c:
        c.execute(
            text("INSERT INTO app_config(cfg_key,cfg_value,updated_at) VALUES(:k,:v,now()) "
                 "ON CONFLICT (cfg_key) DO UPDATE SET cfg_value=:v, updated_at=now()"),
            {'k': key, 'v': value})
        c.commit()


def get_embed_platform():
    """当前向量平台：环境变量 EMBED_PLATFORM > app_config > yaml 顶层 DEFAULT_EMBED_PLATFORM > bailian"""
    env = os.environ.get('EMBED_PLATFORM', '').strip()
    if env:
        return env
    now = time.time()
    if _EMBED_PLATFORM_CACHE['platform'] and now - _EMBED_PLATFORM_CACHE['ts'] < 30:
        return _EMBED_PLATFORM_CACHE['platform']
    plat = None
    try:
        plat = _read_cfg(KEY_EMBED_PLATFORM)
    except Exception:
        pass
    if not plat:
        try:
            with open(MODEL_YAML, 'r', encoding='utf-8') as f:
                plat = (yaml.safe_load(f) or {}).get('DEFAULT_EMBED_PLATFORM')
        except Exception:
            plat = None
    _EMBED_PLATFORM_CACHE['ts'] = now
    _EMBED_PLATFORM_CACHE['platform'] = plat or 'bailian'
    return _EMBED_PLATFORM_CACHE['platform']


def set_embed_platform(platform):
    """切换向量平台（如切到 local-qwen3-emb）。切换后若维度变化需重建索引。"""
    load_model_cfg(platform)  # 校验平台存在
    _write_cfg(KEY_EMBED_PLATFORM, platform)
    _EMBED_PLATFORM_CACHE['platform'] = platform
    _EMBED_PLATFORM_CACHE['ts'] = time.time()


def embed_cfg():
    """当前向量平台配置（含 base_url/api_key/embed_model/embed_dim/rerank_endpoint）"""
    return load_model_cfg(get_embed_platform())


def embed_dim():
    """当前向量模型的维度（yaml profile 声明；缺省 1536 兼容旧配置）"""
    return int(embed_cfg().get('embed_dim') or 1536)


def rerank_cfg():
    """当前向量平台的重排能力：模型名 + 端点（端点空 = DashScope 原生默认）"""
    cfg = embed_cfg()
    return {
        'rerank_model': cfg.get('rerank_model', ''),
        'rerank_endpoint': cfg.get('rerank_endpoint', ''),
    }


def index_dim():
    """存量索引维度（app_config.vector_index_dim；30s 缓存；未记录返回 None）"""
    now = time.time()
    if _INDEX_DIM_CACHE['dim'] and now - _INDEX_DIM_CACHE['ts'] < 30:
        return _INDEX_DIM_CACHE['dim']
    dim = None
    try:
        v = _read_cfg(KEY_INDEX_DIM)
        dim = int(v) if v else None
    except Exception:
        dim = None
    _INDEX_DIM_CACHE['ts'] = now
    _INDEX_DIM_CACHE['dim'] = dim
    return dim


def set_index_dim(dim):
    """记录存量索引维度（向量写入成功后调用；重建完成后由脚本调用）"""
    _write_cfg(KEY_INDEX_DIM, str(int(dim)))
    _INDEX_DIM_CACHE['dim'] = int(dim)
    _INDEX_DIM_CACHE['ts'] = time.time()


def check_index_compat():
    """写入/检索前校验：存量索引维度与当前向量模型维度必须一致。
    不一致抛出明确错误提示重建，而不是静默混用或崩溃。"""
    want = embed_dim()
    have = index_dim()
    if have is not None and have != want:
        raise ValueError(
            '向量模型维度不一致：存量索引 %d 维，当前模型 %d 维。'
            '请先运行 python reindex_vectors_2560.py 重建索引后再操作。'
            % (have, want, get_embed_platform()))
