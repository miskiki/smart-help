# -*- coding: utf-8 -*-
"""KG 图谱更新 worker 启动门控测试：KG_WORKER_IN_FULL_MODEL 开关组合。

app.create_app 的重依赖（config/models/api/authentication/conversations/chain_trace/
policy_core/kg_source_update）全部用内存桩替换：不连数据库、不启动真实线程，
只观察 kg_source_update.start_worker 是否被调用，以及启动日志是否说明原因。
"""
import importlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from flask import Blueprint


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class KgWorkerGateTest(unittest.TestCase):
    def setUp(self):
        self.start_worker = mock.Mock(name='start_worker')
        self.upload_dir = tempfile.mkdtemp(prefix='kg_worker_gate_')

    def tearDown(self):
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    def _fakes(self, kg_graph_enabled=True, full_model=False, showcase=False):
        """构造 app.py 依赖桩；全部内存对象，禁止真实连库/线程。"""
        config = _module(
            'config',
            DATABASE_URL='sqlite://',
            SECRET_KEY='test-secret',
            UPLOAD_DIR=self.upload_dir,
            MAX_CONTENT_LENGTH=1024 * 1024,
            KG_GRAPH_ENABLED=kg_graph_enabled,
            KG_AUTO_UPDATE=True,
            AUTH_DATABASE_URL='sqlite://',
            AUTH_JWT_SECRET='x' * 32,
        )
        policy_core = _module('policy_core', enabled=mock.Mock(return_value=False))
        policy_core.showcase = _module(
            'policy_core.showcase', enabled=mock.Mock(return_value=showcase))
        policy_core.full_settings = _module(
            'policy_core.full_settings', enabled=mock.Mock(return_value=full_model))
        policy_core.routes = _module('policy_core.routes', bp=Blueprint('core', __name__))
        policy_core.policy_worker = _module(
            'policy_core.policy_worker', start=mock.Mock(name='policy_worker_start'))
        policy_core.publishing = _module(
            'policy_core.publishing', start=mock.Mock(name='publishing_start'))
        return {
            'config': config,
            'models': _module('models', db=mock.Mock(name='db')),
            'api': _module('api', bp=Blueprint('api', __name__)),
            'authentication': _module('authentication', install=mock.Mock(name='install_auth')),
            'conversations': _module('conversations', chat_bp=Blueprint('chat', __name__)),
            'chain_trace': _module('chain_trace', install=mock.Mock(name='install_trace')),
            'policy_core': policy_core,
            'policy_core.showcase': policy_core.showcase,
            'policy_core.full_settings': policy_core.full_settings,
            'policy_core.routes': policy_core.routes,
            'policy_core.policy_worker': policy_core.policy_worker,
            'policy_core.publishing': policy_core.publishing,
            'kg_source_update': _module('kg_source_update', start_worker=self.start_worker),
        }

    def _load_app(self, *, kg_graph_enabled=True, full_model=False, showcase=False,
                  worker_env=None):
        """在依赖桩下重新导入 app 模块（触发一次 create_app），返回模块。"""
        fakes = self._fakes(kg_graph_enabled=kg_graph_enabled,
                            full_model=full_model, showcase=showcase)
        with mock.patch.dict(sys.modules, fakes), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KG_WORKER_IN_FULL_MODEL', None)
            if worker_env is not None:
                os.environ['KG_WORKER_IN_FULL_MODEL'] = worker_env
            sys.modules.pop('app', None)
            return importlib.import_module('app')

    # ---- 四种组合 ----

    def test_non_full_model_starts_worker(self):
        """非 full_model 传统链路沿用现网：无条件启动，开关值不影响。"""
        for value in (None, '0', '1'):
            self.start_worker.reset_mock()
            with self.assertLogs('app', level='INFO') as logs:
                module = self._load_app(full_model=False, worker_env=value)
            self.start_worker.assert_called_once_with()
            self.assertIsNotNone(module.app)
            self.assertTrue(
                any('已启动' in line and '传统规则模式' in line for line in logs.output),
                logs.output)

    def test_full_model_switch_off_does_not_start(self):
        """full_model + 开关关闭（默认）：不启动，日志说明原因。"""
        for value in (None, '', '0', 'off', 'false', 'no'):
            self.start_worker.reset_mock()
            with self.assertLogs('app', level='INFO') as logs:
                self._load_app(full_model=True, worker_env=value)
            self.start_worker.assert_not_called()
            self.assertTrue(
                any('未启动' in line and 'KG_WORKER_IN_FULL_MODEL' in line
                    for line in logs.output),
                logs.output)

    def test_full_model_switch_on_starts_worker(self):
        """full_model + 开关打开：启动，日志标记 full_model 原因。"""
        for value in ('1', 'on', 'true', 'yes', 'TRUE'):
            self.start_worker.reset_mock()
            with self.assertLogs('app', level='INFO') as logs:
                self._load_app(full_model=True, worker_env=value)
            self.start_worker.assert_called_once_with()
            self.assertTrue(
                any('已启动' in line and 'full_model' in line for line in logs.output),
                logs.output)

    def test_showcase_does_not_start(self):
        """展示模式不启动，即使开关打开。"""
        self.start_worker.reset_mock()
        with self.assertLogs('app', level='INFO') as logs:
            self._load_app(showcase=True, full_model=False, worker_env='1')
        self.start_worker.assert_not_called()
        self.assertTrue(any('展示模式' in line for line in logs.output), logs.output)

    # ---- 门控补充 ----

    def test_kg_graph_disabled_does_not_start(self):
        """KG_GRAPH_ENABLED 关闭优先，full_model 开关打开也不启动。"""
        self.start_worker.reset_mock()
        with self.assertLogs('app', level='INFO') as logs:
            self._load_app(kg_graph_enabled=False, full_model=True, worker_env='1')
        self.start_worker.assert_not_called()
        self.assertTrue(any('KG_GRAPH_ENABLED 关闭' in line for line in logs.output),
                        logs.output)

    def test_switch_values(self):
        """开关识别 1/on/true/yes（大小写不敏感），其余（含默认空）为关闭。"""
        module = self._load_app(full_model=True, worker_env=None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KG_WORKER_IN_FULL_MODEL', None)
            self.assertFalse(module.kg_worker_in_full_model_enabled())
            for value in ('1', 'on', 'true', 'yes', 'TRUE'):
                os.environ['KG_WORKER_IN_FULL_MODEL'] = value
                self.assertTrue(module.kg_worker_in_full_model_enabled(), value)
            for value in ('0', 'off', 'false', 'no', ''):
                os.environ['KG_WORKER_IN_FULL_MODEL'] = value
                self.assertFalse(module.kg_worker_in_full_model_enabled(), value)


if __name__ == '__main__':
    unittest.main()
