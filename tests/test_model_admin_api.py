# -*- coding: utf-8 -*-
"""模型注册表管理端 API 端到端测试（Flask test client + sqlite 临时文件库）。

把 model_bp 的 9 条路由真实走通到临时 sqlite 库，验证：
- 凭证永不回显（列表 api_key 恒 ''、只回 api_key_configured 布尔）；
- 创建：id 前缀 m-、无默认自动设默认、带 api_key 加密落库；
- 更新：type 不可改、PUT 永不覆盖凭证（改名不冲 key）；
- 凭证子资源：设置/更换/查询元数据/清空；
- 删除：默认模型拒删（需先切默认）、非默认可删；
- 连通性测试：非 chat 拒（P2/P3）、chat 走 test_chat 替身；
- 设为默认：同类型兄弟默认原子清零（部分唯一索引兜底）；
- 列表 type 过滤、非法 type 报错；providers 清单。

约束：不连生产库。登录态 authentication.require_user / model_admin.require_user
全部替身注入；凭证加密用固定 MODEL_CRED_KEY；每个用例独立建库、结束即删。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from cryptography.fernet import Fernet

# 固定测试密钥，让 encrypt/decrypt 确定可断言（不依赖 app_config 派生）
os.environ['MODEL_CRED_KEY'] = Fernet.generate_key().decode()

from flask import Flask, jsonify  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402

import authentication  # noqa: E402
import model_admin  # noqa: E402
import vector_reindex  # noqa: E402
from models import db  # noqa: E402

MODELS_DDL = """
CREATE TABLE "models" (
    "id" TEXT PRIMARY KEY, "type" TEXT NOT NULL, "name" TEXT NOT NULL,
    "source" TEXT NOT NULL DEFAULT 'remote', "provider" TEXT NOT NULL DEFAULT 'openai',
    "base_url" TEXT NOT NULL, "api_key_enc" TEXT, "model_name" TEXT NOT NULL,
    "dim" INTEGER, "query_instruction" TEXT, "json_mode" BOOLEAN,
    "is_default" BOOLEAN NOT NULL DEFAULT 0,
    "created_at" TIMESTAMP, "updated_at" TIMESTAMP
)"""

# 部分唯一索引：每类型至多一个 is_default（与 migrate_models.py 一致）
MODELS_UQ_DDL = "CREATE UNIQUE INDEX uq_models_default ON models(type) WHERE is_default"

AUDIT_DDL = """
CREATE TABLE "audit_log" (
    "log_id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "action" TEXT, "target_code" TEXT, "target_name" TEXT,
    "detail" TEXT, "operator" TEXT, "created_at" TEXT
)"""

# 向量表（复合主键 id+embed_model，与 migrate_vector_multi_model.py 对齐）
VECTOR_DDL = """
CREATE TABLE "policy_vector" (
    "id" INTEGER NOT NULL, "policy_code" TEXT, "policy_name" TEXT,
    "detail_id" INTEGER, "doc_text" TEXT, "vector" BLOB, "status" TEXT,
    "updated_at" TEXT, "policy_version" TEXT, "embed_model" TEXT NOT NULL,
    PRIMARY KEY ("id", "embed_model")
)"""

APPCONFIG_DDL = """
CREATE TABLE "app_config" (
    "cfg_key" TEXT PRIMARY KEY, "cfg_value" TEXT, "updated_at" TEXT
)"""


class _FakeUser:
    username = 'admin-tester'
    role = 'admin'
    must_change_password = False
    real_name = '测试管理员'


class ModelAdminApiTestBase(unittest.TestCase):
    """每个用例一套 Flask app + 独立 sqlite 文件库；请求只命中临时库。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='model-admin-test-')
        self.addCleanup(lambda: os.system('rm -rf %s' % self.tmpdir))
        self.db_path = os.path.join(self.tmpdir, 'models.db')
        self.engine = create_engine('sqlite:///' + self.db_path)

        @event.listens_for(self.engine, 'connect')
        def _reg_now(dbapi_conn, _rec):
            dbapi_conn.create_function('now', 0, lambda: '2026-01-01 00:00:00')

        with self.engine.begin() as conn:
            conn.execute(text(MODELS_DDL))
            conn.execute(text(MODELS_UQ_DDL))
            conn.execute(text(AUDIT_DDL))
            conn.execute(text(VECTOR_DDL))
            conn.execute(text(APPCONFIG_DDL))

        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY='test', TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite:///' + self.db_path,
            SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(model_admin.model_bp)

        @self.app.errorhandler(authentication.ApiError)
        def _api_error(exc):
            return jsonify(ok=False, msg=exc.message, code=exc.code), exc.status

        self.client = self.app.test_client()
        # admin_required 走 authentication.require_user（装饰器闭包解析模块全局）
        # _audit 走 model_admin 命名空间里 import 的 require_user
        for patch in (
                mock.patch.object(authentication, 'require_user',
                                  lambda *a, **k: _FakeUser()),
                mock.patch.object(model_admin, 'require_user',
                                  lambda *a, **k: _FakeUser())):
            patch.start()
            self.addCleanup(patch.stop)

    def _create(self, **overrides):
        payload = {'type': 'chat', 'name': '测试模型', 'provider': 'openai',
                   'base_url': 'http://127.0.0.1:8081/v1', 'model_name': 'qwen-test'}
        payload.update(overrides)
        r = self.client.post('/api/admin/models', json=payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertTrue(data['ok'], data)
        return data['model']['id']

    def _list(self, type_=None):
        url = '/api/admin/models' + ('?type=' + type_ if type_ else '')
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class TestProviders(ModelAdminApiTestBase):
    def test_providers_catalog(self):
        data = self._list()
        self.assertTrue(data['ok'])
        r = self.client.get('/api/admin/models/providers')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertGreater(len(body['providers']), 0)
        for p in body['providers']:
            for key in ('id', 'label', 'default_base_url'):
                self.assertIn(key, p)


class TestCreate(ModelAdminApiTestBase):
    def test_create_id_prefix_and_auto_default(self):
        mid = self._create()
        self.assertTrue(mid.startswith('m-'))
        # 该类型首个模型自动成为默认
        self.assertTrue(self._list()['items'][0]['is_default'])

    def test_create_stores_encrypted_key_not_echoed(self):
        mid = self._create(api_key='sk-secret-123')
        data = self._list()
        item = [i for i in data['items'] if i['id'] == mid][0]
        self.assertEqual(item['api_key'], '')
        self.assertTrue(item['api_key_configured'])
        # 库里是密文不是明文
        with self.engine.connect() as c:
            enc = c.execute(text('SELECT api_key_enc FROM models WHERE id=:i'),
                            {'i': mid}).scalar()
        self.assertTrue(enc)
        self.assertNotIn('sk-secret-123', enc)

    def test_create_requires_type(self):
        r = self.client.post('/api/admin/models', json={'name': 'x', 'model_name': 'y'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])

    def test_create_bad_base_url(self):
        r = self.client.post('/api/admin/models', json={
            'type': 'chat', 'name': 'x', 'model_name': 'y',
            'base_url': 'ftp://nope/v1'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])

    def test_create_embed_dim_range(self):
        r = self.client.post('/api/admin/models', json={
            'type': 'embed', 'name': '嵌入', 'model_name': 'emb',
            'base_url': 'http://127.0.0.1:8095/v1', 'dim': 64})
        self.assertEqual(r.status_code, 400)  # dim 低于 128


class TestUpdate(ModelAdminApiTestBase):
    def test_update_cannot_change_type(self):
        mid = self._create(type='chat', name='A')
        r = self.client.put('/api/admin/models/' + mid,
                            json={'type': 'embed', 'name': 'A改'})
        self.assertEqual(r.status_code, 200)
        item = [i for i in self._list()['items'] if i['id'] == mid][0]
        self.assertEqual(item['type'], 'chat')  # type 保持原值

    def test_update_does_not_clobber_credential(self):
        mid = self._create(api_key='sk-original')
        # Fernet 密文带时间戳，两次 encrypt 必不同；断言"密文原样未变"而非等于新值
        with self.engine.connect() as c:
            enc_before = c.execute(text('SELECT api_key_enc FROM models WHERE id=:i'),
                                   {'i': mid}).scalar()
        r = self.client.put('/api/admin/models/' + mid,
                            json={'name': '改名', 'base_url': 'http://127.0.0.1:8082/v1'})
        self.assertEqual(r.status_code, 200)
        item = [i for i in self._list()['items'] if i['id'] == mid][0]
        self.assertEqual(item['name'], '改名')
        self.assertTrue(item['api_key_configured'])  # 原 key 仍在
        with self.engine.connect() as c:
            enc_after = c.execute(text('SELECT api_key_enc FROM models WHERE id=:i'),
                                  {'i': mid}).scalar()
        self.assertEqual(enc_after, enc_before)


class TestCredentials(ModelAdminApiTestBase):
    def test_set_and_query_metadata(self):
        mid = self._create()  # 无 key
        r = self.client.put('/api/admin/models/' + mid + '/credentials', json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertFalse(body['fields']['api_key']['configured'])
        # 设置 key
        r = self.client.put('/api/admin/models/' + mid + '/credentials',
                            json={'api_key': 'sk-new'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['fields']['api_key']['configured'])

    def test_clear_credentials(self):
        mid = self._create(api_key='sk-x')
        self.assertTrue([i for i in self._list()['items'] if i['id'] == mid][0]['api_key_configured'])
        r = self.client.delete('/api/admin/models/' + mid + '/credentials')
        self.assertEqual(r.status_code, 200)
        self.assertFalse([i for i in self._list()['items'] if i['id'] == mid][0]['api_key_configured'])


class TestDelete(ModelAdminApiTestBase):
    def test_delete_default_rejected(self):
        mid = self._create()  # 自动成为默认
        r = self.client.delete('/api/admin/models/' + mid)
        self.assertEqual(r.status_code, 400)
        self.assertIn('默认模型', r.get_json()['msg'])

    def test_delete_non_default_ok(self):
        self._create()  # 第一个是默认
        second = self._create(name='第二个')
        # 第二个不是默认，可删
        r = self.client.delete('/api/admin/models/' + second)
        self.assertEqual(r.status_code, 200)
        self.assertFalse([i for i in self._list()['items'] if i['id'] == second])

    def test_delete_unknown_404(self):
        r = self.client.delete('/api/admin/models/m-not-exist')
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()['ok'])


class TestConnectivity(ModelAdminApiTestBase):
    def test_rerank_delegates_to_test_rerank(self):
        mid = self._create(type='rerank', name='重排', model_name='rr',
                           provider='dashscope_rerank',
                           base_url='http://127.0.0.1:8096/v1/rerank')
        with mock.patch.object(model_admin, 'test_rerank',
                               lambda row, timeout=60: {'ok': True, 'latency_ms': 23,
                                                        'detail': '重排 2 条文档成功，最高分 0.900'}):
            r = self.client.post('/api/admin/models/' + mid + '/test')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['latency_ms'], 23)
        self.assertIn('重排', body['detail'])

    def test_chat_delegates_to_test_chat(self):
        mid = self._create()
        with mock.patch.object(model_admin, 'test_chat',
                               lambda row: {'ok': True, 'latency_ms': 42, 'detail': 'pong'}):
            r = self.client.post('/api/admin/models/' + mid + '/test')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['latency_ms'], 42)
        self.assertEqual(body['detail'], 'pong')

    def test_embed_delegates_to_test_embed(self):
        mid = self._create(type='embed', name='嵌入', model_name='emb',
                           base_url='http://127.0.0.1:8095/v1', dim=2560)
        with mock.patch.object(model_admin, 'test_embed',
                               lambda row, timeout=60: {'ok': True, 'latency_ms': 17,
                                                        'detail': '实测维度 2560 与填写 dim=2560 一致'}):
            r = self.client.post('/api/admin/models/' + mid + '/test')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['latency_ms'], 17)
        self.assertIn('2560', body['detail'])


class TestTestDraft(ModelAdminApiTestBase):
    """Task 2：POST /test-draft 保存前连通性测试。契约（ledger 裁定，写死）：
    输入校验失败（base_url 非法/缺 model_name/embed 缺 dim/type 非法/provider 非法）→ 400
    {ok:false, latency_ms:0, detail:<原因>}；输入合法真正探测 → 200 同形状 {ok, latency_ms, detail}。
    两路 body 形状一致；不写库、不缓存、不触发 reindex。mock 掉 test_with_cfg，不连真实网络。
    create=True：RED 阶段 model_admin 尚未 import test_with_cfg，该属性尚不存在。
    """

    def test_shape_and_no_db_write(self):
        total_before = self._list()['total']
        with mock.patch.object(model_admin, 'test_with_cfg',
                               lambda cfg, timeout=60: {'ok': True, 'latency_ms': 11,
                                                        'detail': 'draft ok'},
                               create=True):
            r = self.client.post('/api/admin/models/test-draft', json={
                'type': 'chat', 'provider': 'openai',
                'base_url': 'http://127.0.0.1:8081/v1', 'model_name': 'qwen-draft'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        for key in ('ok', 'latency_ms', 'detail'):
            self.assertIn(key, body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['latency_ms'], 11)
        self.assertEqual(body['detail'], 'draft ok')
        # 不写库：total 不变
        self.assertEqual(self._list()['total'], total_before)

    def test_rejects_bad_base_url(self):
        r = self.client.post('/api/admin/models/test-draft', json={
            'type': 'chat', 'provider': 'openai',
            'base_url': 'ftp://nope/v1', 'model_name': 'x'})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['latency_ms'], 0)
        self.assertTrue(body.get('detail'))

    def test_non_numeric_dim_returns_400(self):
        # 契约：非法输入绝不 500（dim 非数字曾 KeyError/ValueError -> 500）
        resp = self.client.post('/api/admin/models/test-draft', json={
            'type': 'embed', 'provider': 'openai',
            'base_url': 'http://127.0.0.1:8095/v1', 'model_name': 'emb-x', 'dim': 'abc'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('dim 必须是整数', resp.get_json()['detail'])

    def test_dim_out_of_range_returns_400(self):
        resp = self.client.post('/api/admin/models/test-draft', json={
            'type': 'embed', 'provider': 'openai',
            'base_url': 'http://127.0.0.1:8095/v1', 'model_name': 'emb-x', 'dim': 99999})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('128', resp.get_json()['detail'])

    def test_provider_type_mismatch_returns_400(self):
        # dashscope_rerank 只支持 rerank；chat 组合直接 400，不打网络
        resp = self.client.post('/api/admin/models/test-draft', json={
            'type': 'chat', 'provider': 'dashscope_rerank',
            'base_url': 'https://dashscope.aliyuncs.com/api/v1', 'model_name': 'qwen-plus'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('不支持', resp.get_json()['detail'])

    def test_uses_stored_key_when_id_given(self):
        # 编辑态草稿测试：前端拿不到明文密钥，凭 id 让后端用库里解密后的 key 兜底
        mid = self._create(api_key='sk-stored-xyz')
        seen = {}

        def fake(cfg, timeout=60):
            seen['api_key'] = cfg['api_key']
            return {'ok': True, 'latency_ms': 1, 'detail': 'ok'}

        with mock.patch.object(model_admin, 'test_with_cfg', fake, create=True):
            r = self.client.post('/api/admin/models/test-draft', json={
                'id': mid, 'type': 'chat', 'provider': 'openai',
                'base_url': 'http://127.0.0.1:8081/v1', 'model_name': 'qwen-test'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(seen['api_key'], 'sk-stored-xyz')

    def test_explicit_key_wins_over_stored(self):
        mid = self._create(api_key='sk-stored-xyz')
        seen = {}

        def fake(cfg, timeout=60):
            seen['api_key'] = cfg['api_key']
            return {'ok': True, 'latency_ms': 1, 'detail': 'ok'}

        with mock.patch.object(model_admin, 'test_with_cfg', fake, create=True):
            self.client.post('/api/admin/models/test-draft', json={
                'id': mid, 'api_key': 'sk-typed', 'type': 'chat', 'provider': 'openai',
                'base_url': 'http://127.0.0.1:8081/v1', 'model_name': 'qwen-test'})
        self.assertEqual(seen['api_key'], 'sk-typed')

    def test_stored_key_not_sent_to_a_different_base_url(self):
        """id 兜底是"把密钥发到 base_url"，base_url 变了就必须拒绝，否则成了密钥外发原语。"""
        mid = self._create(api_key='sk-stored-xyz')
        called = []
        with mock.patch.object(model_admin, 'test_with_cfg',
                               lambda cfg, timeout=60: called.append(cfg) or
                               {'ok': True, 'latency_ms': 1, 'detail': 'ok'}, create=True):
            r = self.client.post('/api/admin/models/test-draft', json={
                'id': mid, 'type': 'chat', 'provider': 'openai',
                'base_url': 'http://evil.example.com/v1', 'model_name': 'qwen-test'})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(called, [])
        self.assertIn('base_url', r.get_json()['detail'])

    def test_stored_key_rejected_when_type_differs(self):
        mid = self._create(api_key='sk-stored-xyz')
        r = self.client.post('/api/admin/models/test-draft', json={
            'id': mid, 'type': 'embed', 'provider': 'openai',
            'base_url': 'http://127.0.0.1:8081/v1', 'model_name': 'emb', 'dim': 1024})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn('不一致', r.get_json()['detail'])

    def test_unknown_id_returns_400(self):
        r = self.client.post('/api/admin/models/test-draft', json={
            'id': 'm-does-not-exist', 'type': 'chat', 'provider': 'openai',
            'base_url': 'http://127.0.0.1:8081/v1', 'model_name': 'x'})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn('不存在', r.get_json()['detail'])

    def test_stored_key_use_is_audited(self):
        from models import AuditLog, db
        mid = self._create(api_key='sk-stored-xyz')
        with mock.patch.object(model_admin, 'test_with_cfg',
                               lambda cfg, timeout=60: {'ok': True, 'latency_ms': 1, 'detail': 'ok'},
                               create=True):
            self.client.post('/api/admin/models/test-draft', json={
                'id': mid, 'type': 'chat', 'provider': 'openai',
                'base_url': 'http://127.0.0.1:8081/v1', 'model_name': 'qwen-test'})
        with self.app.app_context():
            actions = [row.action for row in AuditLog.query.filter_by(target_code=mid).all()]

    def test_embed_requires_dim(self):
        r = self.client.post('/api/admin/models/test-draft', json={
            'type': 'embed', 'provider': 'openai',
            'base_url': 'http://127.0.0.1:8095/v1', 'model_name': 'emb'})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertTrue(body.get('detail'))


class TestSetDefault(ModelAdminApiTestBase):
    def test_set_default_clears_siblings(self):
        first = self._create(name='一号')   # 自动默认
        second = self._create(name='二号')  # 非默认
        r = self.client.post('/api/admin/models/' + second + '/default')
        self.assertEqual(r.status_code, 200)
        items = {i['id']: i['is_default'] for i in self._list()['items']}
        self.assertFalse(items[first])
        self.assertTrue(items[second])

    def test_default_invariant_single_per_type(self):
        a = self._create(type='chat', name='A')
        b = self._create(type='chat', name='B')
        c = self._create(type='chat', name='C')
        self.client.post('/api/admin/models/' + a + '/default')
        self.client.post('/api/admin/models/' + b + '/default')
        self.client.post('/api/admin/models/' + c + '/default')
        defaults = [i for i in self._list()['items'] if i['is_default']]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]['id'], c)


class TestListFilter(ModelAdminApiTestBase):
    def test_type_filter(self):
        self._create(type='chat', name='对话')
        self._create(type='embed', name='嵌入', model_name='emb',
                     base_url='http://127.0.0.1:8095/v1', dim=2560)
        chats = self._list('chat')['items']
        embeds = self._list('embed')['items']
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]['type'], 'chat')
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0]['type'], 'embed')

    def test_bad_type_rejected(self):
        r = self.client.get('/api/admin/models?type=wrong')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])


class TestIndexStatus(ModelAdminApiTestBase):
    def test_index_status_reports_embed_rows(self):
        self._create(type='embed', name='本地嵌入', model_name='emb-local',
                     base_url='http://127.0.0.1:8095/v1', dim=2560)
        # 写两条向量行到临时库的 policy_vector（走测试引擎，不是 config.DATABASE_URL）
        import numpy as np
        with self.engine.begin() as c:
            for i in (1, 2):
                c.execute(text(
                    "INSERT INTO policy_vector (id, policy_code, policy_name, detail_id, doc_text, "
                    "vector, status, updated_at, policy_version, embed_model) "
                    "VALUES (:i, :pc, :pc, :i, :t, :v, 'ok', now(), '1', :m)"),
                    {'i': i, 'pc': 'P1', 't': 't%d' % i,
                     'v': np.zeros(4, dtype=np.float32).tobytes(), 'm': 'emb-local'})
        # 端点内部 from vector_reindex import index_status，其 _engine() 走 config.DATABASE_URL，
        # 必须 patch 回临时库；vector_table 指向临时库已建的表名
        with mock.patch.object(vector_reindex, '_engine', lambda: self.engine), \
                mock.patch.object(vector_reindex, 'vector_table', lambda: 'policy_vector'):
            r = self.client.get('/api/admin/models/index-status')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(len(body['items']), 1)
        item = body['items'][0]
        self.assertEqual(item['model_name'], 'emb-local')
        self.assertEqual(item['rows'], 2)
        self.assertTrue(item['built'])
        self.assertTrue(item['is_default'])  # 唯一 embed，自动默认


class TestReindex(ModelAdminApiTestBase):
    def test_reindex_starts_background_task(self):
        mid = self._create(type='embed', name='嵌入', model_name='emb-x',
                           base_url='http://127.0.0.1:8095/v1', dim=2560)
        called = threading.Event()
        with mock.patch.object(vector_reindex, 'reindex_for_model',
                               lambda *a, **k: (called.set(),
                                                {'ok': True, 'total': 1, 'done': 1,
                                                 'seconds': 0.0, 'table': 'policy_vector',
                                                 'model_name': 'emb-x', 'dim': 2560})[1]):
            r = self.client.post('/api/admin/models/%s/reindex' % mid)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['started'])
        self.assertEqual(body['model_name'], 'emb-x')
        # 后台线程应已跑完替身
        self.assertTrue(called.wait(3.0))

    def test_reindex_rejects_non_embed(self):
        mid = self._create()  # chat
        r = self.client.post('/api/admin/models/%s/reindex' % mid)
        self.assertEqual(r.status_code, 400)
        self.assertIn('嵌入模型', r.get_json()['msg'])


class TestIdNamespace(ModelAdminApiTestBase):
    """Task 1：放宽 _ID_RE 后，seed 的中文/点号 id 可入径，且不重新放行危险字符。

    seed id 全集来自 config/model_settings.yaml → deploy/seed_models.py:_slug
    （实测最长 43 字符，含 'seed-本地Qwen3.6-35B-qwen3.6-35b-a3b' 这类中文点号 id）。
    """

    def test_cjk_dotted_id_roundtrip(self):
        seed_like = 'seed-本地Qwen3.6-35B-qwen3.6-35b-a3b'   # 实际 seed id（见 config/model_settings.yaml）
        resp = self.client.put('/api/admin/models/' + seed_like,
                               json={'name': '改名测试'})
        self.assertNotEqual(resp.status_code, 400)     # 关键：不能再被 _ID_RE 拒
        self.assertEqual(resp.status_code, 404, resp.get_data(as_text=True))  # 库中没有 → 404 而非 400

    def test_id_length_capped_at_64(self):
        bad = 'a' * 65
        resp = self.client.put('/api/admin/models/' + bad, json={'name': 'x'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('模型编号无效', resp.get_json()['msg'])

    def test_rejects_unsafe_chars(self):
        # 点号 '.' 是字符集成员（seed id 带点：seed-本地Qwen3.6-...），'..' 在
        # 正则层**应该**匹配上，不靠正则拒绝点号。实测（werkzeug 3.1.9 测试客户端）
        # 'PUT /api/admin/models/..' 会以 mid='..' 到达 handler（'../../etc' 才在
        # 路由层 404）——真正护栏是 mid 仅作不透明 DB 主键查询与游标字符串键，
        # 全程无人用 mid 拼文件路径，故 '..' 无害。
        # 正则层只断言「字符集外」的危险字符 fullmatch 不上；
        # 端点层断言引号-空白-反斜杠等会进入 <mid> 的串必须 400。
        for bad in ('/', '../../etc', '/etc/passwd', 'q"x', "q'x",
                    'a b', 'a\nb', 'a\\b'):
            self.assertIsNone(model_admin._ID_RE.fullmatch(bad), bad)
        self.assertIsNotNone(model_admin._ID_RE.fullmatch('seed-本地Qwen3.6-35B-x'))
        for bad in ('q"x', "q'x", 'a b', 'a\\b'):
            resp = self.client.put('/api/admin/models/' + bad, json={'name': 'x'})
            self.assertEqual(resp.status_code, 400, bad)


if __name__ == '__main__':
    unittest.main()
