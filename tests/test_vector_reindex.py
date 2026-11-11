# -*- coding: utf-8 -*-
"""vector_reindex 模块单测（sqlite 临时文件库，不连生产）。

覆盖：
- _source_rows：优先默认模型全量行；默认无行时退回全表按 id 去重；
- reindex_for_model：mock _embed_batch 验证 预删旧行 + ON CONFLICT 写入 +
  游标推进/完成后归零 + 返回结构；resume 断点续跑；
- index_status：原生 SQL 读 models/向量表/app_config，built 判定（行数>0 且维度一致）、
  reindexing 游标判定；
- _read_cursor / _write_cursor 往返。

约束：sqlite 没有 now()，用 SQLAlchemy event 注册一个返回固定串的连接级函数；
vector 列按字节存 float32，维度=length(vector)/4。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import numpy as np
from sqlalchemy import create_engine, event, text

import vector_reindex

VECTOR_DDL = """
CREATE TABLE policy_vector (
    id INTEGER NOT NULL,
    policy_code TEXT, policy_name TEXT, detail_id INTEGER,
    doc_text TEXT, vector BLOB, status TEXT,
    updated_at TEXT, policy_version TEXT,
    embed_model TEXT NOT NULL,
    PRIMARY KEY (id, embed_model)
)"""
APPCONFIG_DDL = """
CREATE TABLE app_config (
    cfg_key TEXT PRIMARY KEY, cfg_value TEXT, updated_at TEXT
)"""
MODELS_DDL = """
CREATE TABLE models (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
    model_name TEXT NOT NULL, dim INTEGER, is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT, updated_at TEXT
)"""


def make_engine(path):
    eng = create_engine('sqlite:///' + path)

    @event.listens_for(eng, 'connect')
    def _reg_now(dbapi_conn, _rec):
        dbapi_conn.create_function('now', 0, lambda: '2026-01-01 00:00:00')

    return eng


class VRTBase(unittest.TestCase):
    TABLE = 'policy_vector'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='vector-reindex-test-')
        self.addCleanup(lambda: os.system('rm -rf %s' % self.tmpdir))
        self.db_path = os.path.join(self.tmpdir, 'vec.db')
        self.engine = make_engine(self.db_path)
        with self.engine.begin() as c:
            c.execute(text(VECTOR_DDL))
            c.execute(text(APPCONFIG_DDL))
            c.execute(text(MODELS_DDL))
        # 所有被测函数都走 _engine() / vector_table()，统一指向临时库
        self._eng_patch = mock.patch.object(vector_reindex, '_engine', lambda: self.engine)
        self._tab_patch = mock.patch.object(vector_reindex, 'vector_table', lambda: self.TABLE)
        self._eng_patch.start()
        self._tab_patch.start()
        self.addCleanup(self._eng_patch.stop)
        self.addCleanup(self._tab_patch.stop)

    def _ins_vec(self, vid, model, dim=4, pc='P1', txt='t', status='ok', pv='1'):
        blob = np.zeros(dim, dtype=np.float32).tobytes()
        with self.engine.begin() as c:
            c.execute(text(
                "INSERT INTO policy_vector (id, policy_code, policy_name, detail_id, doc_text, "
                "vector, status, updated_at, policy_version, embed_model) "
                "VALUES (:i, :pc, :pn, :d, :t, :v, :s, now(), :pv, :m) "
                "ON CONFLICT (id, embed_model) DO UPDATE SET vector=EXCLUDED.vector"),
                {'i': vid, 'pc': pc, 'pn': pc, 'd': vid, 't': txt, 'v': blob,
                 's': status, 'pv': pv, 'm': model})

    def _ins_model(self, mid, name, model_name, dim, is_default=0):
        with self.engine.begin() as c:
            c.execute(text(
                "INSERT INTO models (id, type, name, model_name, dim, is_default, created_at) "
                "VALUES (:i, 'embed', :n, :m, :d, :def, '2026-01-01')"),
                {'i': mid, 'n': name, 'm': model_name, 'd': dim, 'def': is_default})


class TestSourceRows(VRTBase):
    def test_prefers_default_model_rows(self):
        self._ins_vec(1, 'default-emb', pc='A')
        self._ins_vec(1, 'other-emb', pc='A')
        self._ins_vec(2, 'default-emb', pc='B')
        rows = vector_reindex._source_rows(self.engine, self.TABLE, 'default-emb')
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[0] for r in rows], [1, 2])

    def test_fallback_dedup_when_default_empty(self):
        self._ins_vec(1, 'model-x', pc='A')
        self._ins_vec(1, 'model-y', pc='A')  # 同 id 不同模型，去重只留一行
        self._ins_vec(2, 'model-y', pc='B')
        rows = vector_reindex._source_rows(self.engine, self.TABLE, None)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r[0] for r in rows), [1, 2])


class TestReindexForModel(VRTBase):
    def _cfg(self, dim=4):
        return {'base_url': 'http://127.0.0.1:9/v1', 'api_key': '',
                'embed_model': 'target-emb', 'embed_dim': dim}

    def test_writes_rows_resets_cursor(self):
        self._ins_vec(1, 'default-emb', txt='doc-1')
        self._ins_vec(2, 'default-emb', txt='doc-2')
        calls = []

        def fake_batch(cfg, texts):
            calls.append(list(texts))
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        with mock.patch.object(vector_reindex, '_embed_batch', side_effect=fake_batch):
            res = vector_reindex.reindex_for_model(self._cfg(), 'm-1', 'target-emb',
                                                   default_model_name='default-emb')
        self.assertTrue(res['ok'])
        self.assertEqual(res['total'], 2)
        self.assertEqual(res['done'], 2)
        self.assertEqual(len(calls), 1)  # batch=64 一次吃完
        # 目标模型行已写入，维度正确
        with self.engine.connect() as c:
            n = c.execute(text(
                "SELECT count(*) FROM policy_vector WHERE embed_model='target-emb'")).scalar()
            dmin = c.execute(text(
                "SELECT min(length(vector)/4) FROM policy_vector WHERE embed_model='target-emb'")).scalar()
        self.assertEqual(n, 2)
        self.assertEqual(dmin, 4)
        # 完成后游标归零
        self.assertEqual(vector_reindex._read_cursor(self.engine, 'm-1'), 0)

    def test_resume_skips_done(self):
        self._ins_vec(1, 'default-emb')
        self._ins_vec(2, 'default-emb')
        self._ins_vec(3, 'default-emb')
        # 预置游标到 2（前 2 条视为已完成）
        vector_reindex._write_cursor(self.engine, 'm-2', 2)

        def fake_batch(cfg, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        with mock.patch.object(vector_reindex, '_embed_batch', side_effect=fake_batch) as mb:
            res = vector_reindex.reindex_for_model(self._cfg(), 'm-2', 'target-emb',
                                                   default_model_name='default-emb', resume=True)
        self.assertTrue(res['ok'])
        self.assertEqual(res['done'], 3)
        # 断点续跑只补剩余 1 条
        self.assertEqual(len(mb.call_args_list), 1)
        self.assertEqual(len(mb.call_args_list[0][0][1]), 1)

    def test_cursor_past_total_short_circuits(self):
        self._ins_vec(1, 'default-emb')
        vector_reindex._write_cursor(self.engine, 'm-3', 99)
        with mock.patch.object(vector_reindex, '_embed_batch') as mb:
            res = vector_reindex.reindex_for_model(self._cfg(), 'm-3', 'target-emb',
                                                   default_model_name='default-emb', resume=True)
        self.assertTrue(res['ok'])
        self.assertEqual(res['done'], 1)
        mb.assert_not_called()


class TestIndexStatus(VRTBase):
    def test_built_and_unbuilt_and_reindexing(self):
        self._ins_model('m-a', '已建', 'emb-a', 4, is_default=1)
        self._ins_model('m-b', '未建', 'emb-b', 8, is_default=0)
        self._ins_vec(1, 'emb-a', dim=4)
        self._ins_vec(2, 'emb-a', dim=4)
        # emb-b 无行 → 未建
        # 给 m-a 预置重建游标 → reindexing 标记
        vector_reindex._write_cursor(self.engine, 'm-a', 1)
        items = {i['model_name']: i for i in vector_reindex.index_status()}
        self.assertEqual(items['emb-a']['rows'], 2)
        self.assertTrue(items['emb-a']['built'])
        self.assertTrue(items['emb-a']['is_default'])
        self.assertTrue(items['emb-a']['reindexing'])
        self.assertEqual(items['emb-a']['reindex_cursor'], 1)
        self.assertEqual(items['emb-b']['rows'], 0)
        self.assertFalse(items['emb-b']['built'])
        self.assertFalse(items['emb-b']['reindexing'])

    def test_dim_mismatch_not_built(self):
        self._ins_model('m-c', '维度不齐', 'emb-c', 4, is_default=1)
        self._ins_vec(1, 'emb-c', dim=4)
        self._ins_vec(2, 'emb-c', dim=8)  # 维度不一致 → built=False
        item = vector_reindex.index_status()[0]
        self.assertEqual(item['model_name'], 'emb-c')
        self.assertEqual(item['rows'], 2)
        self.assertFalse(item['built'])
        self.assertEqual(item['dim_min'], 4)
        self.assertEqual(item['dim_max'], 8)


class TestCursorRoundTrip(VRTBase):
    def test_read_write(self):
        self.assertEqual(vector_reindex._read_cursor(self.engine, 'm-x'), 0)
        vector_reindex._write_cursor(self.engine, 'm-x', 42)
        self.assertEqual(vector_reindex._read_cursor(self.engine, 'm-x'), 42)
        vector_reindex._write_cursor(self.engine, 'm-x', 0)
        self.assertEqual(vector_reindex._read_cursor(self.engine, 'm-x'), 0)


if __name__ == '__main__':
    unittest.main()
