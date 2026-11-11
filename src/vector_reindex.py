# -*- coding: utf-8 -*-
"""向量索引重建（多模型泛化，docs/redesign/30-custom-model-registry.md §4.5）。

取代 reindex_vectors_2560.py 的三个硬编码常量（EMBED_URL/MODEL/DIM）：
重建目标（嵌入端点/模型名/维度）改从 models 表目标行读取（row_to_embed_cfg），
表名取当前 POLICY_VECTOR_TABLE，靠行级 embed_model 标记区分模型——
多模型共存、切换默认即回滚，旧模型向量行原样保留。

- 文档侧按官方协议不加 Instruct 前缀（与旧脚本一致）
- 断点续跑游标 app_config.reindex_cursor_<model_id>（按模型 id 命名）
- 重建期间该模型检索走已有行（可能不全），管理页提示，不阻断在线问答
"""
import logging
import os
import time

import numpy as np
import requests
from sqlalchemy import create_engine, text

import config

_LOG = logging.getLogger(__name__)
EMBED_BATCH = 10  # dashscope embedding 单次批量上限（实测 >10 报 400）


def vector_table():
    """当前向量表名：env POLICY_VECTOR_TABLE 优先，缺省 policy_vector。"""
    return (os.environ.get('POLICY_VECTOR_TABLE', 'policy_vector').strip() or 'policy_vector')


def _engine():
    return create_engine(config.DATABASE_URL)


def _cursor_key(model_id):
    return 'reindex_cursor_%s' % model_id


def _read_cursor(db, model_id):
    with db.connect() as c:
        r = c.execute(text("SELECT cfg_value FROM app_config WHERE cfg_key = :k"),
                      {'k': _cursor_key(model_id)}).fetchone()
    try:
        return int(r[0]) if r and r[0] else 0
    except (TypeError, ValueError):
        return 0


def _write_cursor(db, model_id, value):
    with db.begin() as c:
        c.execute(text(
            "INSERT INTO app_config(cfg_key,cfg_value,updated_at) VALUES(:k,:v,now()) "
            "ON CONFLICT (cfg_key) DO UPDATE SET cfg_value=:v, updated_at=now()"),
            {'k': _cursor_key(model_id), 'v': str(value)})


def _embed_batch(cfg, texts):
    """文档侧原始文本直接向量化（无 Instruct 前缀），OpenAI 兼容 /embeddings。
    cfg: row_to_embed_cfg 返回的 dict（base_url/api_key/embed_model/embed_dim）。"""
    url = cfg['base_url'] + '/embeddings'
    headers = {'Content-Type': 'application/json'}
    if cfg.get('api_key'):
        headers['Authorization'] = 'Bearer ' + cfg['api_key']
    dim = int(cfg.get('embed_dim') or 1536)
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        r = requests.post(url, json={'model': cfg['embed_model'], 'input': batch,
                                     'dimensions': dim}, headers=headers, timeout=120)
        r.raise_for_status()
        data = sorted(r.json()['data'], key=lambda x: x.get('index', 0))
        if len(data) != len(batch) or any(len(x['embedding']) != dim for x in data):
            raise ValueError('向量数量或维度异常，重建中断（请检查嵌入服务与 dim 配置）')
        out.extend([x['embedding'] for x in data])
    return out


def _source_rows(db, table, default_model_name):
    """取要重建的文档切片：优先当前默认模型的全量行（规范索引源）；
    默认模型无行时退回全表按 id 去重（每 id 取一行）。"""
    if default_model_name:
        with db.connect() as c:
            rows = c.execute(text(
                "SELECT id, policy_code, policy_name, detail_id, doc_text, status, policy_version "
                "FROM \"%s\" WHERE embed_model = :m ORDER BY id" % table),
                {'m': default_model_name}).fetchall()
        if rows:
            return rows
    with db.connect() as c:
        rows = c.execute(text(
            "SELECT id, policy_code, policy_name, detail_id, doc_text, status, policy_version "
            "FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY embed_model) rn "
            "FROM \"%s\") t WHERE rn = 1 ORDER BY id" % table)).fetchall()
    return rows


def reindex_for_model(cfg, model_id, model_name, default_model_name=None,
                      batch=64, resume=False, progress_cb=None):
    """用 cfg 描述的嵌入模型重建 model_name 的全量向量索引。

    cfg: row_to_embed_cfg 的 dict。model_name: 目标模型名（写入 embed_model 列）。
    default_model_name: 源切片取自哪个模型（缺省用全表去重）。
    返回 {ok, total, done, seconds, table, model_name}。"""
    table = vector_table()
    db = _engine()
    rows = _source_rows(db, table, default_model_name)
    total = len(rows)
    cursor = _read_cursor(db, model_id) if resume else 0
    if cursor >= total:
        _write_cursor(db, model_id, 0)
        return {'ok': True, 'total': total, 'done': total, 'seconds': 0.0,
                'table': table, 'model_name': model_name}
    t0 = time.time()
    dim = int(cfg.get('embed_dim') or 1536)
    # 预删目标模型既有行（非默认模型重建时清空旧行，避免残留脏向量）
    with db.begin() as wc:
        wc.execute(text("DELETE FROM \"%s\" WHERE embed_model = :m" % table),
                   {'m': model_name})
    done = cursor
    for offset in range(cursor, total, batch):
        chunk = rows[offset:offset + batch]
        texts = [(r[4] or '') for r in chunk]
        vecs = _embed_batch(cfg, texts)
        with db.begin() as wc:
            for r, vec in zip(chunk, vecs):
                wc.execute(text(
                    "INSERT INTO \"%s\" (id, policy_code, policy_name, detail_id, doc_text, vector, "
                    "status, updated_at, policy_version, embed_model) "
                    "VALUES (:id, :pc, :pn, :did, :txt, :v, :st, now(), :pv, :m) "
                    "ON CONFLICT (id, embed_model) DO UPDATE SET "
                    "vector=EXCLUDED.vector, doc_text=EXCLUDED.doc_text, "
                    "updated_at=now(), policy_version=EXCLUDED.policy_version" % table),
                    {'id': r[0], 'pc': r[1], 'pn': r[2], 'did': r[3], 'txt': r[4],
                     'v': np.array(vec, dtype=np.float32).tobytes(), 'st': r[5],
                     'pv': r[6], 'm': model_name})
        done += len(chunk)
        _write_cursor(db, model_id, done)
        if progress_cb:
            try:
                progress_cb(done, total, time.time() - t0)
            except Exception:
                pass
        _LOG.info('reindex %s: %d/%d  已用 %.0fs', model_name, done, total, time.time() - t0)
    _write_cursor(db, model_id, 0)
    return {'ok': True, 'total': total, 'done': done, 'seconds': round(time.time() - t0, 1),
            'table': table, 'model_name': model_name, 'dim': dim}


def index_status():
    """各 embed 模型的索引状态（行数/维度/是否已建/重建进度），供管理页轮询。
    用原生 SQL 读 models 表（不依赖 Flask app context，脚本与 API 都能调）。"""
    table = vector_table()
    db = _engine()
    out = []
    with db.connect() as c:
        rowcounts = {r[0]: (r[1], r[2], r[3]) for r in c.execute(text(
            "SELECT embed_model, count(*), min(length(vector)/4), max(length(vector)/4) "
            "FROM \"%s\" GROUP BY embed_model" % table))}
        cursors = {r[0]: int(r[1]) if r[1] else 0 for r in c.execute(text(
            "SELECT cfg_key, cfg_value FROM app_config WHERE cfg_key LIKE 'reindex_cursor_%'"))}
        for r in c.execute(text(
            "SELECT id, name, model_name, dim, is_default FROM models "
            "WHERE type='embed' ORDER BY created_at DESC")):
            mid, name, model_name, dim, is_default = r[0], r[1], r[2] or '', r[3], r[4]
            cnt, dmin, dmax = rowcounts.get(model_name, (0, None, None))
            built = cnt > 0 and (dmin is None or dmin == dmax)
            cur = cursors.get('reindex_cursor_%s' % mid, 0)
            out.append({
                'id': mid, 'name': name, 'model_name': model_name,
                'dim': dim, 'rows': cnt, 'dim_min': dmin, 'dim_max': dmax,
                'built': bool(built), 'is_default': bool(is_default),
                'reindexing': bool(cur), 'reindex_cursor': cur,
            })
    return out
