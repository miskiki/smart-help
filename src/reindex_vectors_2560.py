# -*- coding: utf-8 -*-
"""2560 维向量重建（非破坏式）：Qwen3-Embedding-4B 重新向量化 policy_vector 全部 doc_text。

与 reindex_vectors.py（原地 UPDATE，切平台即破坏旧向量）不同，本脚本：
- 新向量写入新表 policy_vector_qwen3（同结构），旧表 policy_vector 原样保留
- 灰度对比确认检索质量后，由人工/脚本执行换表与删旧
- 查询侧 Instruct 前缀只影响查询嵌入；文档侧按官方协议不加前缀

用法（admin_panel 目录）：
    python reindex_vectors_2560.py            # 全量重建到 policy_vector_qwen3
    python reindex_vectors_2560.py --resume   # 断点续跑（进度存 app_config.reindex_cursor_2560）
"""
import argparse
import time

import numpy as np
import requests
from sqlalchemy import create_engine, text

import config

SRC_TABLE = 'policy_vector'
DST_TABLE = 'policy_vector_qwen3'
EMBED_URL = 'http://127.0.0.1:8095/v1/embeddings'
EMBED_MODEL = 'qwen3-embedding-4b'
EMBED_DIM = 2560
KEY_CURSOR = 'reindex_cursor_2560'


def _ensure_table(db):
    with db.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS %s (LIKE %s INCLUDING ALL)" % (DST_TABLE, SRC_TABLE)))


def _read_cursor(db):
    with db.connect() as c:
        r = c.execute(text("SELECT cfg_value FROM app_config WHERE cfg_key = :k"),
                      {'k': KEY_CURSOR}).fetchone()
    try:
        return int(r[0]) if r and r[0] else 0
    except (TypeError, ValueError):
        return 0


def _write_cursor(db, value):
    with db.begin() as c:
        c.execute(text(
            "INSERT INTO app_config(cfg_key,cfg_value,updated_at) VALUES(:k,:v,now()) "
            "ON CONFLICT (cfg_key) DO UPDATE SET cfg_value=:v, updated_at=now()"),
            {'k': KEY_CURSOR, 'v': str(value)})


def _embed_batch(texts):
    """文档侧原始文本直接向量化（无 Instruct 前缀），OpenAI 兼容 /embeddings"""
    out = []
    for i in range(0, len(texts), 10):
        batch = texts[i:i + 10]
        r = requests.post(EMBED_URL, json={'model': EMBED_MODEL, 'input': batch,
                                           'dimensions': EMBED_DIM},
                          headers={'Content-Type': 'application/json'}, timeout=120)
        r.raise_for_status()
        data = sorted(r.json()['data'], key=lambda x: x.get('index', 0))
        if len(data) != len(batch) or any(len(x['embedding']) != EMBED_DIM for x in data):
            raise ValueError('向量数量或维度异常，重建中断（请检查 8095 嵌入服务）')
        out.extend([x['embedding'] for x in data])
    return out


def main():
    ap = argparse.ArgumentParser(description='2560 维非破坏式重建（旧表保留）')
    ap.add_argument('--batch', type=int, default=64, help='每批条数（默认 64）')
    ap.add_argument('--resume', action='store_true', help='从上次断点继续')
    args = ap.parse_args()

    db = create_engine(config.DATABASE_URL)
    _ensure_table(db)
    with db.connect() as c:
        total = c.execute(text("SELECT count(*) FROM %s" % SRC_TABLE)).fetchone()[0]
    print('待重建向量总数: %d → 写入 %s（%d 维）' % (total, DST_TABLE, EMBED_DIM))

    cursor = _read_cursor(db) if args.resume else 0
    if cursor:
        print('断点续跑：从第 %d 条继续' % cursor)

    t0 = time.time()
    done = cursor
    while True:
        with db.connect() as c:
            rows = c.execute(text(
                "SELECT id, policy_code, policy_name, detail_id, doc_text, status, policy_version "
                "FROM %s ORDER BY id LIMIT :n OFFSET :o" % SRC_TABLE),
                {'n': args.batch, 'o': cursor}).fetchall()
        if not rows:
            break
        texts = [(r[4] or '') for r in rows]
        vecs = _embed_batch(texts)
        with db.begin() as wc:
            for r, vec in zip(rows, vecs):
                wc.execute(text(
                    "DELETE FROM %s WHERE id = :id" % DST_TABLE), {'id': r[0]})
                wc.execute(text(
                    "INSERT INTO %s (id, policy_code, policy_name, detail_id, doc_text, vector, "
                    "status, updated_at, policy_version, embed_model) "
                    "VALUES (:id, :pc, :pn, :did, :txt, :v, :st, now(), :pv, :m)" % DST_TABLE),
                    {'id': r[0], 'pc': r[1], 'pn': r[2], 'did': r[3], 'txt': r[4],
                     'v': np.array(vec, dtype=np.float32).tobytes(), 'st': r[5],
                     'pv': r[6], 'm': EMBED_MODEL})
        cursor += len(rows)
        done = cursor
        _write_cursor(db, cursor)
        print('进度 %d/%d  已用 %.0fs' % (cursor, total, time.time() - t0), flush=True)

    _write_cursor(db, 0)
    print('重建完成：共 %d 条 → %s（%d 维），耗时 %.0fs。旧表 %s 未改动。'
          % (done, DST_TABLE, EMBED_DIM, time.time() - t0, SRC_TABLE))


if __name__ == '__main__':
    main()
