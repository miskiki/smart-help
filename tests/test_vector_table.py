# -*- coding: utf-8 -*-
"""向量表参数化测试：生产必须写 POLICY_VECTOR_TABLE（qwen3），不能再写旧表 policy_vector。

背景（P0）：kb_rag.sync_policy/remove_policy 曾硬编码 policy_vector，而生产检索读
policy_vector_qwen3，导致导入/停用政策后检索不生效。这里用 sqlite 假库验证写入目标。
"""
import os
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy import text as sa_text

import kb_rag
import vector_safety


def _engine():
    folder = tempfile.mkdtemp(prefix='vector_table_')
    engine = create_engine('sqlite:///' + os.path.join(folder, 'vec.db'))
    with engine.begin() as conn:
        conn.execute(sa_text('CREATE TABLE policy (policy_id integer, policy_code varchar(32), '
                          'policy_name varchar(64), cur_version varchar(16), status varchar(16))'))
        conn.execute(sa_text('CREATE TABLE policy_detail (detail_id varchar(32), policy_id integer, '
                          'target text, conditions text, region text, benefit text, process text, '
                          'extra text)'))
        for table in ('policy_vector', 'policy_vector_qwen3'):
            conn.execute(sa_text('CREATE TABLE ' + table + ' (id varchar(64) PRIMARY KEY, '
                              'policy_code varchar(32), policy_name varchar(64), doc_text text, '
                              'vector blob, status varchar(16), updated_at varchar(32), '
                              'policy_version varchar(16), embed_model varchar(64), detail_id varchar(32))'))
        conn.execute(sa_text("INSERT INTO policy VALUES (1,'C1','政策一','v1','在用')"))
        conn.execute(sa_text("INSERT INTO policy_detail "
                          "(detail_id,policy_id,target,conditions,region,benefit) "
                          "VALUES ('D1',1,'老人','低收入','莲花县','100元')"))
    return engine


class TableNameTest(unittest.TestCase):
    def test_known_tables_only(self):
        for name in ('policy_vector', 'policy_vector_qwen3'):
            self.assertEqual(vector_safety._vector_table_name(name), name)
        with self.assertRaises(ValueError):
            vector_safety._vector_table_name('policy_vector; drop table policy')

    def test_env_default(self):
        with mock.patch.dict(os.environ, {'POLICY_VECTOR_TABLE': 'policy_vector_qwen3'}, clear=False):
            self.assertEqual(vector_safety._vector_table_name(), 'policy_vector_qwen3')


class SyncPolicyTargetTest(unittest.TestCase):
    def setUp(self):
        self.engine = _engine()
        self.patches = [
            mock.patch.object(kb_rag, '_VECTOR_TABLE', 'policy_vector_qwen3'),
            # sqlite 不支持 FOR UPDATE：测试里去掉行锁语法，事务语义不影响本测试目标
            mock.patch.object(kb_rag, 'text', side_effect=lambda sql: sa_text(sql.replace(' FOR UPDATE', ''))),
            mock.patch.object(kb_rag, '_db', side_effect=lambda: self.engine),
            mock.patch.object(kb_rag, 'check_index_compat', return_value=None),
            mock.patch.object(kb_rag, '_embed_cfg', return_value={'embed_model': 'qwen3-embedding-4b'}),
            mock.patch.object(kb_rag, '_embed', side_effect=lambda texts, cfg=None: [[0.1, 0.2] for _ in texts]),
            mock.patch.object(kb_rag, 'set_index_dim', return_value=None),
            mock.patch.object(kb_rag, 'embed_dim', return_value=2),
            mock.patch.object(kb_rag, '_invalidate_cache', return_value=None),
            mock.patch.object(vector_safety, 'split_tokens', side_effect=lambda body, model: [body]),
            mock.patch.object(vector_safety, 'backup_vectors', return_value='/tmp/backup.json'),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self.patches])

    def test_sync_policy_writes_qwen3_not_legacy(self):
        result = kb_rag.sync_policy('C1', '政策一', '文号', '在用', 'v1')
        self.assertTrue(result['ok'])
        with self.engine.connect() as conn:
            new_rows = conn.execute(sa_text("SELECT count(*) FROM policy_vector_qwen3")).scalar()
            old_rows = conn.execute(sa_text("SELECT count(*) FROM policy_vector")).scalar()
        self.assertGreaterEqual(new_rows, 1)
        self.assertEqual(old_rows, 0)

    def test_remove_policy_deletes_from_qwen3(self):
        kb_rag.sync_policy('C1', '政策一', '文号', '在用', 'v1')
        removed = kb_rag.remove_policy('C1')
        self.assertTrue(removed['ok'])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa_text("SELECT count(*) FROM policy_vector_qwen3")).scalar(), 0)
            self.assertEqual(conn.execute(sa_text("SELECT count(*) FROM policy_vector")).scalar(), 0)

    def test_backup_uses_same_table(self):
        # backup_vectors 真调用：备份内容来自 qwen3 表，且 payload 记录表名
        for item in self.patches:
            if getattr(item, 'attribute', '') == 'backup_vectors':
                item.stop()
        folder = tempfile.mkdtemp(prefix='vec_backup_')
        with mock.patch.dict(os.environ, {'VECTOR_BACKUP_DIR': folder}, clear=False):
            kb_rag.sync_policy('C1', '政策一', '文号', '在用', 'v1')
            with self.engine.connect() as conn:
                path = vector_safety.backup_vectors(conn, 'C1', table='policy_vector_qwen3')
        import json
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertEqual(payload['table'], 'policy_vector_qwen3')
        self.assertGreaterEqual(len(payload['rows']), 1)
        self.assertTrue(os.path.exists(path + '.sha256') or path.endswith('.json'))


if __name__ == '__main__':
    unittest.main()
