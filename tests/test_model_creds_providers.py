# -*- coding: utf-8 -*-
"""P1 模型注册表层单测：凭证加密（model_creds）+ provider 投影/校验（model_providers）。

不连生产库、不启动服务：model_creds 走 MODEL_CRED_KEY 环境变量注入；
model_providers 的 row_meta/row_to_chat_cfg 注入纯 dict 行。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

# 固定测试密钥，避免依赖 app_config 派生
from cryptography.fernet import Fernet
_TEST_KEY = Fernet.generate_key()
os.environ['MODEL_CRED_KEY'] = _TEST_KEY.decode()

import model_creds
import model_providers


class TestModelCreds(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        enc = model_creds.encrypt('sk-test-12345')
        self.assertTrue(enc)
        self.assertNotIn('sk-test-12345', enc)
        self.assertEqual(model_creds.decrypt(enc), 'sk-test-12345')

    def test_empty_or_sentinel_not_stored(self):
        self.assertEqual(model_creds.encrypt(''), '')
        self.assertEqual(model_creds.encrypt(None), '')
        self.assertFalse(model_creds.is_configured(''))
        self.assertFalse(model_creds.is_configured(None))

    def test_decrypt_garbage_degrades_to_empty(self):
        # is_configured 只答"存了没有"（非空即 True）；垃圾串解密时才降级空串
        self.assertTrue(model_creds.is_configured('not-a-fernet-token'))
        self.assertEqual(model_creds.decrypt('not-a-fernet-token'), '')

    def test_decrypt_valid_token(self):
        enc = model_creds.encrypt('secret')
        self.assertTrue(model_creds.is_configured(enc))
        self.assertEqual(model_creds.decrypt(enc), 'secret')


class TestValidateBaseUrl(unittest.TestCase):
    def test_empty_rejected(self):
        ok, reason = model_providers.validate_base_url('', 'chat')
        self.assertFalse(ok)
        self.assertIn('不能为空', reason)

    def test_bad_scheme_rejected(self):
        ok, reason = model_providers.validate_base_url('ftp://example.com/v1', 'chat')
        self.assertFalse(ok)
        self.assertIn('http/https', reason)

    def test_no_host_rejected(self):
        ok, _ = model_providers.validate_base_url('http://', 'chat')
        self.assertFalse(ok)

    def test_http_https_ok(self):
        self.assertTrue(model_providers.validate_base_url('http://127.0.0.1:8081/v1', 'chat')[0])
        self.assertTrue(model_providers.validate_base_url('https://api.openai.com/v1', 'chat')[0])

    def test_rerank_remote_requires_https(self):
        ok, reason = model_providers.validate_base_url('http://10.0.0.5/rerank', 'rerank')
        self.assertFalse(ok)
        self.assertIn('https', reason)
        # 本地 127.0.0.1 的 http 重排合法（serve_qwen3_rerank 兼容层）
        self.assertTrue(model_providers.validate_base_url('http://127.0.0.1:8096/v1/rerank', 'rerank')[0])
        self.assertTrue(model_providers.validate_base_url('https://dashscope.aliyuncs.com/api/v1', 'rerank')[0])

    def test_private_host_allowed_for_chat(self):
        # 不封私网：本地 llama-server 合法
        self.assertTrue(model_providers.validate_base_url('http://192.168.1.10:8080/v1', 'chat')[0])


class TestRowMeta(unittest.TestCase):
    def _row(self, **kw):
        row = {
            'id': 'm-abc123', 'type': 'chat', 'name': '测试模型', 'source': 'remote',
            'provider': 'openai', 'base_url': 'https://api.openai.com/v1',
            'api_key_enc': model_creds.encrypt('sk-x'), 'model_name': 'gpt-4o-mini',
            'dim': None, 'query_instruction': '', 'json_mode': True,
            'is_default': True, 'created_at': '2026-11-09 10:00:00',
            'updated_at': '2026-11-09 10:00:00',
        }
        row.update(kw)
        return row

    def test_api_key_never_echoed(self):
        meta = model_providers.row_meta(self._row())
        self.assertEqual(meta['api_key'], '')
        self.assertTrue(meta['api_key_configured'])

    def test_local_kind(self):
        meta = model_providers.row_meta(self._row(base_url='http://127.0.0.1:8081/v1', api_key_enc=''))
        self.assertEqual(meta['kind'], '本地')
        self.assertFalse(meta['api_key_configured'])

    def test_cloud_kind(self):
        meta = model_providers.row_meta(self._row())
        self.assertEqual(meta['kind'], '云端')

    def test_passthrough_fields(self):
        meta = model_providers.row_meta(self._row())
        self.assertEqual(meta['id'], 'm-abc123')
        self.assertEqual(meta['provider'], 'openai')
        self.assertTrue(meta['json_mode'])
        self.assertTrue(meta['is_default'])
        self.assertEqual(meta['model_name'], 'gpt-4o-mini')


class TestRowToChatCfg(unittest.TestCase):
    def test_shape_matches_load_model_cfg(self):
        row = {
            'id': 'm-xyz', 'type': 'chat', 'name': '本地 Qwen', 'source': 'remote',
            'provider': 'openai', 'base_url': 'http://127.0.0.1:8081/v1/',
            'api_key_enc': model_creds.encrypt('local-key'), 'model_name': 'qwen3.6-35b-a3b',
            'dim': None, 'query_instruction': '', 'json_mode': True,
            'is_default': True,
        }
        cfg = model_providers.row_to_chat_cfg(row)
        # 与 recognize.load_model_cfg 返回同形状（kb_rag/recognize 零改动）
        for key in ('platform', 'base_url', 'api_key', 'llm_model', 'embed_model',
                    'rerank_model', 'embed_dim', 'rerank_endpoint', 'quality_tier',
                    'embed_query_instruction', 'json_mode'):
            self.assertIn(key, cfg)
        self.assertEqual(cfg['platform'], 'registry:m-xyz')
        self.assertEqual(cfg['base_url'], 'http://127.0.0.1:8081/v1')  # 尾部斜杠已去
        self.assertEqual(cfg['api_key'], 'local-key')
        self.assertEqual(cfg['llm_model'], 'qwen3.6-35b-a3b')
        self.assertTrue(cfg['json_mode'])

    def test_no_key_gives_empty_api_key(self):
        row = {'id': 'm-2', 'base_url': 'http://127.0.0.1:8081/v1', 'api_key_enc': '',
               'model_name': 'qwen', 'json_mode': False}
        cfg = model_providers.row_to_chat_cfg(row)
        self.assertEqual(cfg['api_key'], '')


class TestRegistryEnabled(unittest.TestCase):
    def test_default_off(self):
        self._saved = os.environ.pop('MODEL_REGISTRY', None)
        self.assertFalse(model_providers.registry_enabled())
        if self._saved is not None:
            os.environ['MODEL_REGISTRY'] = self._saved

    def test_on_values(self):
        self._saved = os.environ.get('MODEL_REGISTRY')
        try:
            for val in ('1', 'on', 'true', 'yes', 'TRUE'):
                os.environ['MODEL_REGISTRY'] = val
                self.assertTrue(model_providers.registry_enabled(), val)
            for val in ('0', 'off', 'no', ''):
                os.environ['MODEL_REGISTRY'] = val
                self.assertFalse(model_providers.registry_enabled(), val)
        finally:
            if self._saved is None:
                os.environ.pop('MODEL_REGISTRY', None)
            else:
                os.environ['MODEL_REGISTRY'] = self._saved


class TestDefaultChatCfg(unittest.TestCase):
    def test_returns_none_when_no_model_table(self):
        # 无 Flask 应用上下文 → Model.query 抛错 → 捕获后回落 None
        self.assertIsNone(model_providers.default_chat_cfg())


class TestRowToRerankCfg(unittest.TestCase):
    def test_dashscope_endpoint_is_base_url(self):
        # dashscope_rerank 的 base_url 即完整重排 URL，不再拼 /rerank
        row = {'id': 'rr-1', 'provider': 'dashscope_rerank',
               'base_url': 'http://127.0.0.1:8096/v1/rerank/',
               'api_key_enc': '', 'model_name': 'qwen3-reranker-4b'}
        cfg = model_providers.row_to_rerank_cfg(row)
        self.assertEqual(cfg['rerank_endpoint'], 'http://127.0.0.1:8096/v1/rerank')
        self.assertEqual(cfg['rerank_model'], 'qwen3-reranker-4b')
        self.assertEqual(cfg['provider'], 'dashscope_rerank')
        self.assertEqual(cfg['api_key'], '')

    def test_openai_endpoint_appends_rerank(self):
        # openai 的 base_url 是端点根，重排地址 = base_url/rerank
        row = {'id': 'rr-2', 'provider': 'openai',
               'base_url': 'https://api.openai.com/v1',
               'api_key_enc': model_creds.encrypt('sk-rr'), 'model_name': 'rerank-1'}
        cfg = model_providers.row_to_rerank_cfg(row)
        self.assertEqual(cfg['rerank_endpoint'], 'https://api.openai.com/v1/rerank')
        self.assertEqual(cfg['api_key'], 'sk-rr')
        self.assertEqual(cfg['provider'], 'openai')

    def test_empty_provider_defaults_to_openai(self):
        # row_to_rerank_cfg 的 `or 'openai'`：空 provider 缺省 openai，端点拼 /rerank
        row = {'provider': '', 'base_url': 'https://x.example/v1',
               'api_key_enc': '', 'model_name': 'm'}
        cfg = model_providers.row_to_rerank_cfg(row)
        self.assertEqual(cfg['provider'], 'openai')
        self.assertEqual(cfg['rerank_endpoint'], 'https://x.example/v1/rerank')


class TestDefaultRerankCfg(unittest.TestCase):
    def test_returns_none_when_no_model_table(self):
        # 无 Flask 应用上下文 → Model.query 抛错 → 捕获后回落 None
        self.assertIsNone(model_providers.default_rerank_cfg())


class _FakeRerankResponse(object):
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class TestRerankScores(unittest.TestCase):
    def _cfg(self, **kw):
        base = {'rerank_model': 'm', 'rerank_endpoint': 'http://127.0.0.1:8096/v1/rerank',
                'api_key': '', 'provider': 'dashscope_rerank'}
        base.update(kw)
        return base

    def test_dashscope_format_and_scores(self):
        import requests as _rq
        from unittest import mock
        resp = _FakeRerankResponse(200, {'output': {'results': [
            {'index': 0, 'relevance_score': 0.9}, {'index': 1, 'relevance_score': 0.1}]}})
        with mock.patch.object(_rq, 'post', return_value=resp) as mp:
            scores = model_providers.rerank_scores(self._cfg(), 'q', ['a', 'b'])
        self.assertEqual(scores, [0.9, 0.1])
        _, kwargs = mp.call_args
        # dashscope 请求体 {model, input:{query, documents}}
        self.assertEqual(kwargs['json']['input'], {'query': 'q', 'documents': ['a', 'b']})
        self.assertEqual(kwargs['json']['model'], 'm')

    def test_openai_format_and_scores(self):
        import requests as _rq
        from unittest import mock
        resp = _FakeRerankResponse(200, {'results': [
            {'index': 0, 'relevance_score': 0.7}, {'index': 1, 'relevance_score': 0.3}]})
        with mock.patch.object(_rq, 'post', return_value=resp) as mp:
            scores = model_providers.rerank_scores(
                self._cfg(provider='openai', rerank_endpoint='https://x/v1/rerank'), 'q', ['a', 'b'])
        self.assertEqual(scores, [0.7, 0.3])
        _, kwargs = mp.call_args
        # openai 请求体 {model, query, documents}
        self.assertEqual(kwargs['json'], {'model': 'm', 'query': 'q', 'documents': ['a', 'b']})

    def test_no_model_returns_none(self):
        self.assertIsNone(model_providers.rerank_scores(self._cfg(rerank_model=''), 'q', ['a']))

    def test_no_documents_returns_none(self):
        self.assertIsNone(model_providers.rerank_scores(self._cfg(), 'q', []))

    def test_non_200_returns_none(self):
        import requests as _rq
        from unittest import mock
        resp = _FakeRerankResponse(500, {})
        with mock.patch.object(_rq, 'post', return_value=resp):
            self.assertIsNone(model_providers.rerank_scores(self._cfg(), 'q', ['a', 'b']))


class TestRerank(unittest.TestCase):
    def _row(self, **kw):
        row = {'id': 'rr-t', 'provider': 'dashscope_rerank',
               'base_url': 'http://127.0.0.1:8096/v1/rerank',
               'api_key_enc': '', 'model_name': 'qwen3-reranker-4b'}
        row.update(kw)
        return row

    def test_success_when_any_positive_score(self):
        from unittest import mock
        with mock.patch.object(model_providers, 'rerank_scores', return_value=[0.0, 0.42]):
            r = model_providers.test_rerank(self._row())
        self.assertTrue(r['ok'])
        self.assertIn('0.420', r['detail'])

    def test_all_zero_scores_fails(self):
        from unittest import mock
        with mock.patch.object(model_providers, 'rerank_scores', return_value=[0.0, 0.0]):
            r = model_providers.test_rerank(self._row())
        self.assertFalse(r['ok'])
        self.assertIn('全 0 分', r['detail'])

    def test_none_result_fails(self):
        from unittest import mock
        with mock.patch.object(model_providers, 'rerank_scores', return_value=None):
            r = model_providers.test_rerank(self._row())
        self.assertFalse(r['ok'])
        self.assertIn('重排调用失败', r['detail'])

    def test_no_model_name_fails(self):
        r = model_providers.test_rerank(self._row(model_name=''))
        self.assertFalse(r['ok'])
        self.assertIn('model_name', r['detail'])


if __name__ == '__main__':
    unittest.main()
