# -*- coding: utf-8 -*-
"""M0 事件层单测：envelope 协议、旧字段兼容、seq/回放。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import events  # noqa: E402
import chain_trace  # noqa: E402


def _state(trace_id='t' * 32, logger=None):
    return {'id': trace_id, 'logger': logger, 'decisions': 0, 'turn_error': False,
            'agg': {}, 'spans': {}, 'subs': [], 'seq': 0, 'turn_id': None}


class EventsClassifyTest(unittest.TestCase):
    def test_span_start_envelope(self):
        env = events.build(_state(), 'span_start', {'span_id': 'a1', 'stage': 'vector.search'}, 3)
        self.assertEqual(env['v'], 'evt-2')
        self.assertEqual(env['seq'], 3)
        self.assertEqual(env['kind'], 'stage.vector.search')
        self.assertEqual(env['phase'], 'retrieve')
        self.assertEqual(env['exposure'], 'user')
        self.assertEqual(env['title'], '向量检索')
        self.assertEqual(env['status'], 'running')
        self.assertEqual(env['trace_id'], 't' * 32)

    def test_span_end_error_status(self):
        env = events.build(_state(), 'span_end', {'stage': 'eligibility.verify',
                                                 'status': 'error',
                                                 'error_type': 'ValueError',
                                                 'duration_ms': 12.5}, 4)
        self.assertEqual(env['status'], 'error')
        self.assertEqual(env['metrics']['ms'], 12.5)
        self.assertEqual(env['detail']['error_type'], 'ValueError')

    def test_model_call_title_and_phase(self):
        env = events.build(_state(), 'full_model.model_call',
                           {'purpose': 'core_full_advice', 'seconds': 1.5}, 5)
        self.assertEqual(env['kind'], 'model.call')
        self.assertEqual(env['phase'], 'answer')
        self.assertIn('生成回答', env['title'])
        self.assertEqual(env['metrics']['ms'], 1500.0)

    def test_domain_event_exposure(self):
        env = events.build(_state(), 'full_model.evidence_load', {'kept': 3, 'dropped': 1}, 6)
        self.assertEqual(env['kind'], 'evidence.load')
        self.assertEqual(env['exposure'], 'user')
        self.assertEqual(env['detail']['kept'], 3)

        env = events.build(_state(), 'full_model.reasoning', {'count': 8}, 7)
        self.assertEqual(env['exposure'], 'audit')

    def test_unknown_event_kept_as_audit(self):
        env = events.build(_state(), 'mystery.thing', {'foo': 'bar'}, 8)
        self.assertEqual(env['exposure'], 'audit')
        self.assertEqual(env['kind'], 'mystery.thing')
        self.assertEqual(env['title'], 'mystery.thing')

    def test_sensitive_fields_dropped(self):
        env = events.build(_state(), 'full_model.facts',
                           {'facts': {'household:age': {'value': '62'}},
                            'question': '我家老人 62 岁能领补贴吗',
                            'member_name': '张三', 'count': 2}, 9)
        self.assertNotIn('question', env['detail'])
        self.assertNotIn('member_name', env['detail'])
        self.assertEqual(env['detail']['facts'], 1)  # 容器只留计数
        self.assertEqual(env['detail']['count'], 2)

    def test_build_never_raises(self):
        env = events.build({}, 'span_start', {'stage': object()}, 1)
        self.assertIsInstance(env, dict)


class ProgressViewTest(unittest.TestCase):
    def test_legacy_span_fields_kept(self):
        env = events.build(_state(), 'span_start', {'stage': 'rules.match'}, 1)
        view = events.progress_view('span_start', {'stage': 'rules.match'}, env)
        self.assertEqual(view['key'], 'stage:rules.match')
        self.assertFalse(view['done'])
        self.assertIs(view['event'], env)

    def test_unknown_event_gets_seq_key(self):
        env = events.build(_state(), 'full_model.facts', {'count': 1}, 11)
        view = events.progress_view('full_model.facts', {'count': 1}, env)
        self.assertEqual(view['key'], 'evt:facts.updated:11')
        self.assertIn('event', view)

    def test_without_envelope_returns_none_for_unknown(self):
        self.assertIsNone(events.progress_view('route', {'endpoint': 'x'}, None))


class ChainTraceEmitReplayTest(unittest.TestCase):
    def setUp(self):
        chain_trace._RING.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {'CHAIN_TRACE_DIR': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_emit_assigns_seq_and_turn(self):
        state = _state()
        token = chain_trace._current.set(state)
        try:
            chain_trace.set_turn(530)
            chain_trace._emit(state, 'turn.started')
            chain_trace._emit(state, 'span_start', stage='vector.search')
        finally:
            chain_trace._current.reset(token)
        self.assertEqual(state['seq'], 2)
        rows = chain_trace.replay(state['id'])
        self.assertEqual([r['seq'] for r in rows], [1, 2])
        self.assertEqual(rows[0]['kind'], 'turn.started')
        self.assertEqual(rows[0]['turn_id'], 530)
        self.assertEqual(rows[1]['phase'], 'retrieve')

    def test_replay_since_filter(self):
        state = _state()
        for _ in range(3):
            chain_trace._emit(state, 'turn.started')
        rows = chain_trace.replay(state['id'], since=2)
        self.assertEqual([r['seq'] for r in rows], [2, 3])

    def test_replay_falls_back_to_jsonl(self):
        state = _state()
        chain_trace._emit(state, 'span_start', stage='rules.match')
        rows = list(chain_trace._RING.get(state['id']) or [])
        # 模拟进程重启：内存环清空后仍能从 JSONL 还原
        path = Path(self.tmp.name) / 'chain_trace-999.jsonl'
        with path.open('w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        chain_trace._RING.clear()
        replay = chain_trace.replay(state['id'])
        self.assertEqual(replay[0]['kind'], 'stage.rules.match')
        self.assertEqual(replay[0]['title'], '规则匹配')

    def test_subscriber_receives_envelope(self):
        state = _state()
        q = chain_trace.subscribe(state)
        chain_trace._emit(state, 'span_start', stage='vector.search')
        view = q.get_nowait()
        self.assertEqual(view['stage'], 'vector.search')
        self.assertEqual(view['event']['kind'], 'stage.vector.search')
        # internal 事件不进流
        chain_trace._emit(state, 'request_start', method='POST')
        self.assertTrue(q.empty())


class ConflictProgramEventsTest(unittest.TestCase):
    def test_program_route_is_user_visible(self):
        env = events.build(_state(), 'full_model.conflict.scope.program_route',
                           {'source_id': 'S1', 'member': 1, 'category': 'program'}, 20)
        self.assertEqual(env['kind'], 'conflict.scope.evaluated')
        self.assertEqual(env['phase'], 'decide')
        self.assertEqual(env['exposure'], 'user')
        self.assertEqual(env['detail']['source_id'], 'S1')
        self.assertEqual(env['detail']['category'], 'program')

    def test_fallback_and_cache_are_audit(self):
        env = events.build(_state(), 'full_model.conflict.scope.fallback',
                           {'reason': 'unknown_fact'}, 21)
        self.assertEqual(env['exposure'], 'audit')
        self.assertEqual(env['status'], 'ok')
        env = events.build(_state(), 'full_model.conflict.scope.audit_cached', {'count': 2}, 22)
        self.assertEqual(env['kind'], 'conflict.scope.audit_cached')
        self.assertEqual(env['detail']['count'], 2)

    def test_classified_event_is_user(self):
        env = events.build(_state(), 'full_model.conflict.scope.classified',
                           {'category': 'semantic', 'count': 3}, 23)
        self.assertEqual(env['kind'], 'conflict.scope.classified')
        self.assertEqual(env['exposure'], 'user')


if __name__ == '__main__':
    unittest.main()
