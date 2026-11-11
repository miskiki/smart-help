# -*- coding: utf-8 -*-
"""T28 规则编译执行器测试：开关矩阵、线程启动、compile_once 语义、CLI。

全部使用 mock：不连数据库、不 import rule_worker/compile_rules 的真实编译栈、
不占用 GPU，也不启动真实后台线程。
"""
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from policy_core import policy_worker  # noqa: E402

_ENV_KEYS = (
    'POLICY_WORKER',
    'POLICY_WORKER_INTERVAL',
    'POLICY_WORKER_BATCH',
    'POLICY_WORKER_RULE_COMPILE',
    'POLICY_WORKER_RULE_COMPILE_INTERVAL',
    'POLICY_WORKER_RULE_COMPILE_BATCH',
)


class _StopLoop(Exception):
    """测试用：让 _rule_compile_loop 跑完一轮后从 sleep 退出。"""


class _FakeThread:
    """替身线程：只记录启动参数，不真正运行 target。"""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        self._alive = False

    def start(self):
        self.started = True
        self._alive = True

    def is_alive(self):
        return self._alive


class _WorkerTestCase(unittest.TestCase):
    """隔离 env 与模块级线程句柄，避免污染其它测试。"""

    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        self._saved_thread = policy_worker._thread
        self._saved_rule_thread = policy_worker._rule_thread
        policy_worker._thread = None
        policy_worker._rule_thread = None

    def tearDown(self):
        policy_worker._thread = self._saved_thread
        policy_worker._rule_thread = self._saved_rule_thread
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _capture_threads(self):
        """把 policy_worker.threading.Thread 换成 _FakeThread，返回创建列表。"""
        created = []

        def factory(**kwargs):
            thread = _FakeThread(**kwargs)
            created.append(thread)
            return thread

        patcher = mock.patch.object(policy_worker.threading, 'Thread', side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return created


class SwitchTest(_WorkerTestCase):
    def test_rule_compile_enabled_off_by_default_and_for_negative_values(self):
        self.assertFalse(policy_worker.rule_compile_enabled())
        for value in ('', '0', 'off', 'false', 'no', 'disable'):
            os.environ['POLICY_WORKER_RULE_COMPILE'] = value
            self.assertFalse(policy_worker.rule_compile_enabled(), value)

    def test_rule_compile_enabled_for_truthy_values(self):
        for value in ('1', 'on', 'true', 'yes', 'TRUE', 'Yes'):
            os.environ['POLICY_WORKER_RULE_COMPILE'] = value
            self.assertTrue(policy_worker.rule_compile_enabled(), value)

    def test_rule_compile_interval_defaults_and_fallbacks(self):
        self.assertEqual(policy_worker.rule_compile_interval(), 60)
        os.environ['POLICY_WORKER_RULE_COMPILE_INTERVAL'] = '120'
        self.assertEqual(policy_worker.rule_compile_interval(), 120)
        for value in ('', 'abc', '0', '-5'):
            os.environ['POLICY_WORKER_RULE_COMPILE_INTERVAL'] = value
            self.assertEqual(policy_worker.rule_compile_interval(), 60, value)

    def test_rule_compile_batch_defaults_and_fallbacks(self):
        self.assertEqual(policy_worker.rule_compile_batch(), 1)
        os.environ['POLICY_WORKER_RULE_COMPILE_BATCH'] = '3'
        self.assertEqual(policy_worker.rule_compile_batch(), 3)
        for value in ('', 'abc', '0', '-2'):
            os.environ['POLICY_WORKER_RULE_COMPILE_BATCH'] = value
            self.assertEqual(policy_worker.rule_compile_batch(), 1, value)


class StartTest(_WorkerTestCase):
    def test_start_all_switches_off_creates_no_thread(self):
        created = self._capture_threads()
        self.assertFalse(policy_worker.start())
        self.assertEqual(created, [])
        self.assertIsNone(policy_worker._thread)
        self.assertIsNone(policy_worker._rule_thread)

    def test_start_policy_worker_only_keeps_old_behaviour(self):
        os.environ['POLICY_WORKER'] = '1'
        created = self._capture_threads()
        self.assertTrue(policy_worker.start())
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].name, 'policy-worker')
        self.assertIs(created[0].target, policy_worker._loop)
        self.assertTrue(created[0].daemon)
        self.assertTrue(created[0].started)
        self.assertFalse(policy_worker.start(), '重复启动应幂等返回 False')
        self.assertEqual(len(created), 1)

    def test_start_rule_compile_only_starts_rule_compiler(self):
        os.environ['POLICY_WORKER_RULE_COMPILE'] = '1'
        created = self._capture_threads()
        self.assertTrue(policy_worker.start())
        self.assertEqual([thread.name for thread in created], ['rule-compiler'])
        self.assertIs(created[0].target, policy_worker._rule_compile_loop)
        self.assertTrue(created[0].daemon)
        self.assertIsNone(policy_worker._thread)
        self.assertIs(policy_worker._rule_thread, created[0])
        self.assertFalse(policy_worker.start(), 'rule-compiler 存活时重复启动应返回 False')
        self.assertEqual(len(created), 1)

    def test_start_both_switches_starts_two_threads(self):
        os.environ['POLICY_WORKER'] = '1'
        os.environ['POLICY_WORKER_RULE_COMPILE'] = '1'
        created = self._capture_threads()
        self.assertTrue(policy_worker.start())
        self.assertEqual([thread.name for thread in created], ['policy-worker', 'rule-compiler'])
        self.assertTrue(all(thread.daemon for thread in created))
        self.assertTrue(all(thread.started for thread in created))

    def test_stop_clears_both_thread_handles(self):
        policy_worker._thread = object()
        policy_worker._rule_thread = object()
        policy_worker.stop()
        self.assertIsNone(policy_worker._thread)
        self.assertIsNone(policy_worker._rule_thread)


class CompileOnceTest(_WorkerTestCase):
    def test_calls_work_once_batch_times(self):
        fake = mock.Mock(side_effect=['万安县', '吉安县', '泰和县'])
        result = policy_worker.compile_once(batch=3, work_once=fake)
        self.assertEqual(fake.call_count, 3)
        self.assertEqual(result['claimed'], ['万安县', '吉安县', '泰和县'])
        self.assertEqual(result['compiled'], 3)
        self.assertEqual(result['failed'], 0)
        self.assertNotIn('error', result)

    def test_stops_when_no_job(self):
        fake = mock.Mock(side_effect=['万安县', None, '泰和县'])
        result = policy_worker.compile_once(batch=5, work_once=fake)
        self.assertEqual(fake.call_count, 2, '空队列应立即停止本轮，不空转')
        self.assertEqual(result['claimed'], ['万安县'])
        self.assertEqual(result['compiled'], 1)
        self.assertEqual(result['failed'], 0)

    def test_exception_returns_failed_without_raise(self):
        fake = mock.Mock(side_effect=RuntimeError('boom'))
        with self.assertLogs('policy_core.policy_worker', level='ERROR'):
            result = policy_worker.compile_once(batch=5, work_once=fake)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(result['compiled'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertIn('RuntimeError: boom', result['error'])

    def test_partial_success_then_exception(self):
        fake = mock.Mock(side_effect=['万安县', RuntimeError('db down')])
        with self.assertLogs('policy_core.policy_worker', level='ERROR'):
            result = policy_worker.compile_once(batch=3, work_once=fake)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(result['claimed'], ['万安县'])
        self.assertEqual(result['compiled'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertIn('db down', result['error'])

    def test_default_batch_comes_from_env(self):
        os.environ['POLICY_WORKER_RULE_COMPILE_BATCH'] = '2'
        fake = mock.Mock(side_effect=['万安县', '吉安县', '泰和县'])
        result = policy_worker.compile_once(work_once=fake)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(result['compiled'], 2)

    def test_lazy_imports_rule_worker_work_once(self):
        fake_module = types.ModuleType('rule_worker')
        fake_module.work_once = mock.Mock(return_value='万安县')
        with mock.patch.dict(sys.modules, {'rule_worker': fake_module}):
            result = policy_worker.compile_once()
        self.assertEqual(fake_module.work_once.call_count, 1)
        self.assertEqual(result['claimed'], ['万安县'])
        self.assertEqual(result['compiled'], 1)

    def test_accepts_engine_obj_for_signature_parity(self):
        fake = mock.Mock(return_value=None)
        result = policy_worker.compile_once(engine_obj=object(), batch=1, work_once=fake)
        self.assertEqual(result, {'claimed': [], 'compiled': 0, 'failed': 0})


class StepAndLoopTest(_WorkerTestCase):
    def test_step_swallows_exception(self):
        with mock.patch.object(policy_worker, 'compile_once', side_effect=RuntimeError('import fail')):
            with self.assertLogs('policy_core.policy_worker', level='ERROR') as logs:
                summary = policy_worker._rule_compile_step()
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['compiled'], 0)
        self.assertIn('单轮失败', '\n'.join(logs.output))

    def test_step_logs_success(self):
        summary = {'claimed': ['万安县'], 'compiled': 1, 'failed': 0}
        with mock.patch.object(policy_worker, 'compile_once', return_value=summary):
            with self.assertLogs('policy_core.policy_worker', level='INFO') as logs:
                result = policy_worker._rule_compile_step()
        self.assertEqual(result, summary)
        self.assertIn('规则编译执行器', '\n'.join(logs.output))

    def test_loop_runs_one_round_then_sleeps_and_exits(self):
        with mock.patch.object(policy_worker, '_rule_compile_step') as step:
            with mock.patch.object(policy_worker.time, 'sleep', side_effect=_StopLoop) as sleep:
                with self.assertRaises(_StopLoop):
                    policy_worker._rule_compile_loop()
        step.assert_called_once_with(None)
        sleep.assert_called_once_with(60)


class CliTest(_WorkerTestCase):
    def test_rule_compile_once_disabled_returns_2_and_skips_compile(self):
        out = io.StringIO()
        with mock.patch.object(policy_worker, 'compile_once') as compile_mock:
            with redirect_stdout(out):
                code = policy_worker.main(['--rule-compile-once'])
        self.assertEqual(code, 2)
        self.assertIn('POLICY_WORKER_RULE_COMPILE 未开启', out.getvalue())
        compile_mock.assert_not_called()

    def test_rule_compile_once_enabled_compiles_and_returns_0(self):
        os.environ['POLICY_WORKER_RULE_COMPILE'] = '1'
        summary = {'claimed': ['万安县'], 'compiled': 1, 'failed': 0}
        out = io.StringIO()
        with mock.patch.object(policy_worker, 'compile_once', return_value=summary) as compile_mock:
            with redirect_stdout(out):
                code = policy_worker.main(['--rule-compile-once', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), summary)
        compile_mock.assert_called_once_with()

    def test_rule_compile_once_enabled_plain_output(self):
        os.environ['POLICY_WORKER_RULE_COMPILE'] = '1'
        summary = {'claimed': ['万安县'], 'compiled': 1, 'failed': 0}
        out = io.StringIO()
        with mock.patch.object(policy_worker, 'compile_once', return_value=summary):
            with redirect_stdout(out):
                code = policy_worker.main(['--rule-compile-once'])
        self.assertEqual(code, 0)
        self.assertIn('万安县', out.getvalue())

    def test_default_path_still_requires_policy_worker_switch(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = policy_worker.main([])
        self.assertEqual(code, 2)
        self.assertIn('POLICY_WORKER 未开启', out.getvalue())


if __name__ == '__main__':
    unittest.main()
