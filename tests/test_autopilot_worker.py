# -*- coding: utf-8 -*-
"""政策自动管理定时 worker（src/policy_core/autopilot.py worker 段）单测。

覆盖：
- 开关：POLICY_AUTOPILOT_WORKER 默认关 / 真值矩阵；POLICY_AUTOPILOT_INTERVAL 兜底；
- tick：默认 dry-run 不写库、POLICY_AUTOPILOT=1 时按 apply 写库、run_all 异常被捕获、
  任务摘要结构（不带样本明细）、conn=None 时自开短事务；
- 线程：start 幂等 + 线程名/daemon、stop 收敛、开关关闭时不建线程不连库、
  run_all 抛异常线程仍存活；
- __main__：--once/--json 输出；app.py 接线顺序。

约束：全部用 sqlite 内存/临时文件库，不连生产库、不启真实定时循环。
"""
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from sqlalchemy import create_engine, text

from policy_core import autopilot

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

POLICY_DDL = (
    'CREATE TABLE policy (policy_id INTEGER PRIMARY KEY, policy_code TEXT, '
    'policy_name TEXT, "level" TEXT, regions TEXT, "status" TEXT, '
    'effect_date TEXT, expire_date TEXT, updated_at TEXT, cur_version TEXT)'
)

_ENV_KEYS = ('POLICY_AUTOPILOT', 'POLICY_AUTOPILOT_WORKER', 'POLICY_AUTOPILOT_INTERVAL')
_TASK_NAMES = ['coverage_report', 'expire_scan', 'health_check',
               'outbox_drain', 'rule_requeue', 'stale_draft']


def clean_env(**overrides):
    """隔离 autopilot 相关环境变量（其余环境保持），可附加覆盖值。"""
    env = dict(os.environ)
    for key in _ENV_KEYS:
        env.pop(key, None)
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


class SwitchTest(unittest.TestCase):
    def tearDown(self):
        autopilot.stop()

    def test_worker_disabled_by_default(self):
        with clean_env():
            self.assertFalse(autopilot.enabled())
            self.assertFalse(autopilot.start())
        # apply 开关（POLICY_AUTOPILOT）不会顺带拉起 worker
        with clean_env(POLICY_AUTOPILOT='1'):
            self.assertFalse(autopilot.enabled())
            self.assertFalse(autopilot.start())
        self.assertIsNone(autopilot._thread)

    def test_worker_disabled_values(self):
        for value in ('0', 'off', 'false', 'no', '', '2', 'disable', 'yes-no'):
            with clean_env(POLICY_AUTOPILOT_WORKER=value):
                self.assertFalse(autopilot.enabled(), repr(value))
        with clean_env():
            self.assertFalse(autopilot.enabled())

    def test_worker_enabled_truthy_matrix(self):
        for value in ('1', 'on', 'true', 'yes', ' ON ', 'TRUE', 'Yes', 'true '):
            with clean_env(POLICY_AUTOPILOT_WORKER=value):
                self.assertTrue(autopilot.enabled(), repr(value))

    def test_interval_default_when_unset(self):
        with clean_env():
            self.assertEqual(autopilot.interval(), 3600)

    def test_interval_fallback(self):
        cases = {
            'bad': 3600,
            '': 3600,
            '0': 3600,
            '-5': 3600,
            '  ': 3600,
            '60': 60,
            ' 120 ': 120,
            '1': 1,
        }
        for raw, expected in cases.items():
            with clean_env(POLICY_AUTOPILOT_INTERVAL=raw):
                self.assertEqual(autopilot.interval(), expected, repr(raw))


class TickTest(unittest.TestCase):
    def setUp(self):
        autopilot.stop()
        self.tmp = tempfile.TemporaryDirectory(prefix='autopilot-worker-')
        self.url = 'sqlite:///' + os.path.join(self.tmp.name, 'worker.db')
        self.engine = create_engine(self.url)
        with self.engine.begin() as conn:
            conn.execute(text(POLICY_DDL))
            conn.execute(text(
                'INSERT INTO policy (policy_id, policy_code, policy_name, "status", '
                "expire_date) VALUES (1, 'X-1', '已过期政策', '在用', '2020-01-01')"))

    def tearDown(self):
        autopilot.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def status(self):
        with self.engine.begin() as conn:
            return conn.execute(
                text('SELECT "status" FROM policy WHERE policy_id = 1')).scalar()

    def tick(self):
        with self.engine.begin() as conn:
            return autopilot.tick(conn)

    def test_dry_run_does_not_write(self):
        with clean_env():
            result = self.tick()
        self.assertTrue(result['ok'])
        self.assertEqual(result['mode'], 'dry_run')
        self.assertEqual(sorted(result['tasks']), _TASK_NAMES)
        self.assertEqual(result['tasks']['expire_scan']['mode'], 'dry_run')
        self.assertEqual(result['tasks']['expire_scan']['counts'],
                         {'expired': 1, 'expiring': 0})
        # dry-run 只读：已过期政策仍是「在用」
        self.assertEqual(self.status(), '在用')

    def test_apply_writes_only_when_switch_on(self):
        with clean_env(POLICY_AUTOPILOT='1'):
            result = self.tick()
        self.assertTrue(result['ok'])
        self.assertEqual(result['mode'], 'apply')
        self.assertEqual(result['tasks']['expire_scan']['mode'], 'apply')
        self.assertEqual(self.status(), '停用')

    def test_tick_passes_dry_run_to_run_all(self):
        seen = []

        def fake_run_all(conn, dry_run=True, **kwargs):
            seen.append(dry_run)
            return {}

        with clean_env():
            with mock.patch.object(autopilot, 'run_all', side_effect=fake_run_all):
                result = autopilot.tick(conn=object())
        self.assertEqual(seen, [True])
        self.assertTrue(result['ok'])
        self.assertEqual(result['tasks'], {})

    def test_tick_passes_apply_to_run_all(self):
        seen = []

        def fake_run_all(conn, dry_run=True, **kwargs):
            seen.append(dry_run)
            return {}

        with clean_env(POLICY_AUTOPILOT='1'):
            with mock.patch.object(autopilot, 'run_all', side_effect=fake_run_all):
                autopilot.tick(conn=object())
        self.assertEqual(seen, [False])

    def test_tick_catches_run_all_error(self):
        with clean_env():
            with mock.patch.object(autopilot, 'run_all',
                                   side_effect=RuntimeError('boom')), \
                    mock.patch.object(autopilot._log, 'exception') as log_exc:
                result = autopilot.tick(conn=object())
        self.assertTrue(log_exc.called)
        self.assertFalse(result['ok'])
        self.assertEqual(result['tasks'], {})
        self.assertIn('RuntimeError', result['error'])
        self.assertIn('boom', result['error'])

    def test_tick_summaries_drop_sample_lists(self):
        reports = {
            'expire_scan': {'task': 'expire_scan', 'mode': 'dry_run',
                            'counts': {'expired': 2, 'expiring': 1},
                            'expired': [{'policy_code': 'A'}]},
            'health_check': {'task': 'health_check', 'mode': 'dry_run',
                             'issue_total': 3, 'checks': {'a': {'count': 1}}},
            'stale_draft': {'task': 'stale_draft', 'mode': 'dry_run',
                            'stale_count': 2, 'items': [1, 2]},
            'coverage_report': {'task': 'coverage_report', 'mode': 'dry_run',
                                'supported': False, 'reason': '缺表'},
        }
        with clean_env():
            with mock.patch.object(autopilot, 'run_all', return_value=reports):
                result = autopilot.tick(conn=object())
        tasks = result['tasks']
        self.assertEqual(tasks['expire_scan']['counts'],
                         {'expired': 2, 'expiring': 1})
        self.assertNotIn('expired', tasks['expire_scan'])
        self.assertEqual(tasks['health_check']['issue_total'], 3)
        self.assertNotIn('checks', tasks['health_check'])
        self.assertNotIn('items', tasks['stale_draft'])
        self.assertFalse(tasks['coverage_report']['supported'])

    def test_tick_opens_own_connection_for_dry_run(self):
        with clean_env():
            with mock.patch.object(autopilot, 'engine', return_value=self.engine):
                result = autopilot.tick()
        self.assertTrue(result['ok'])
        self.assertEqual(result['mode'], 'dry_run')
        self.assertEqual(self.status(), '在用')


class ThreadTest(unittest.TestCase):
    def tearDown(self):
        autopilot.stop()

    def test_start_disabled_does_not_connect(self):
        with clean_env():
            with mock.patch.object(autopilot, 'engine',
                                   side_effect=AssertionError('开关关闭时不应连库')):
                self.assertFalse(autopilot.start())
        self.assertIsNone(autopilot._thread)

    def test_start_thread_name_daemon_and_idempotent(self):
        calls = []

        def fake_loop():
            calls.append(threading.current_thread().name)
            autopilot._stop_event.wait(5)

        with clean_env(POLICY_AUTOPILOT_WORKER='1'):
            with mock.patch.object(autopilot, '_loop', fake_loop):
                try:
                    self.assertTrue(autopilot.start())
                    thread = autopilot._thread
                    self.assertIsNotNone(thread)
                    self.assertEqual(thread.name, 'policy-autopilot')
                    self.assertTrue(thread.daemon)
                    # 幂等：第二次 start 不再建线程，返回 False
                    self.assertFalse(autopilot.start())
                    self.assertIs(autopilot._thread, thread)
                finally:
                    self.assertTrue(autopilot.stop())

    def test_stop_without_start_is_safe(self):
        with clean_env():
            self.assertTrue(autopilot.stop())
            self.assertIsNone(autopilot._thread)

    def test_thread_survives_run_all_error(self):
        attempts = []

        def boom(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError('boom')

        memory_engine = create_engine('sqlite://')
        with clean_env(POLICY_AUTOPILOT_WORKER='1', POLICY_AUTOPILOT_INTERVAL='60'):
            with mock.patch.object(autopilot, 'run_all', side_effect=boom), \
                    mock.patch.object(autopilot, 'engine', return_value=memory_engine), \
                    mock.patch.object(autopilot._log, 'exception') as log_exc:
                self.assertTrue(autopilot.start())
                thread = autopilot._thread
                deadline = time.time() + 3
                while not attempts and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(attempts, 'worker 未执行第一轮 tick')
                # run_all 抛异常被 tick 兜住，线程继续存活
                self.assertTrue(log_exc.called)
                self.assertTrue(thread.is_alive())
                self.assertTrue(autopilot.stop())
        self.assertFalse(thread.is_alive())
        self.assertIsNone(autopilot._thread)


class MainTest(unittest.TestCase):
    def tearDown(self):
        autopilot.stop()

    def test_main_once_json(self):
        payload = {'ok': True, 'mode': 'dry_run', 'generated_at': '2026-11-08 00:00:00',
                   'tasks': {'expire_scan': {'mode': 'dry_run',
                                             'counts': {'expired': 1}}}}
        out = io.StringIO()
        with clean_env(), mock.patch.object(autopilot, 'tick', return_value=payload):
            with contextlib.redirect_stdout(out):
                code = autopilot.main(['--once', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), payload)

    def test_main_error_returns_nonzero(self):
        payload = {'ok': False, 'mode': 'dry_run', 'tasks': {},
                   'error': 'RuntimeError: boom'}
        out = io.StringIO()
        with clean_env(), mock.patch.object(autopilot, 'tick', return_value=payload):
            with contextlib.redirect_stdout(out):
                code = autopilot.main(['--once'])
        self.assertEqual(code, 1)
        self.assertIn('boom', out.getvalue())

    def test_main_text_output(self):
        payload = {'ok': True, 'mode': 'dry_run', 'generated_at': '2026-11-08 00:00:00',
                   'tasks': {'expire_scan': {'mode': 'dry_run',
                                             'counts': {'expired': 1}}}}
        out = io.StringIO()
        with clean_env(), mock.patch.object(autopilot, 'tick', return_value=payload):
            with contextlib.redirect_stdout(out):
                code = autopilot.main([])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn('mode: dry_run', text)
        self.assertIn('expire_scan', text)


class AppWiringTest(unittest.TestCase):
    def test_app_starts_worker_after_policy_worker(self):
        with open(os.path.join(BASE, 'src', 'app.py'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('from policy_core import autopilot', source)
        self.assertIn('autopilot.start()', source)
        self.assertLess(source.index('policy_worker.start()'),
                        source.index('autopilot.start()'))
        # 接线必须在 create_app() 内，测试/CLI 导入模块时不会启动线程
        self.assertGreater(source.index('autopilot.start()'),
                           source.index('def create_app'))


if __name__ == '__main__':
    unittest.main()
