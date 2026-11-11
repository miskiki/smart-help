# -*- coding: utf-8 -*-
"""M0 第二批单测：SSE 帧协议与 trace 接线（不依赖数据库）。"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import events  # noqa: E402
import chain_trace  # noqa: E402


def _progress():
    env = events.build({'id': 'a' * 32, 'turn_id': 12}, 'span_start',
                       {'span_id': 's1', 'stage': 'vector.search'}, 7)
    return {'type': 'progress', 'key': 'stage:vector.search', 'stage': 'vector.search',
            'done': False, 'ms': None, 'error': False, 'event': env}


class SseFramesTest(unittest.TestCase):
    def test_v2_progress_frame_has_id_and_event(self):
        frames = list(events.sse_frames([_progress()], v2=True))
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertTrue(frame.startswith('id: 7\nevent: stage.vector.search\ndata: '))
        self.assertTrue(frame.endswith('\n\n'))
        payload = json.loads(frame.split('data: ', 1)[1].strip())
        self.assertEqual(payload['type'], 'progress')
        self.assertEqual(payload['event']['phase'], 'retrieve')  # 旧字段仍在 data 内

    def test_ping_is_comment_frame(self):
        frames = list(events.sse_frames([{'type': 'ping'}], v2=True))
        self.assertEqual(frames, [': ping\n\n'])

    def test_non_progress_stays_data_frame(self):
        frames = list(events.sse_frames([{'type': 'answer', 'content': 'x'}], v2=True))
        self.assertEqual(frames[0], 'data: {"type": "answer", "content": "x"}\n\n')

    def test_v1_plain_data_and_envelope_ignored(self):
        frames = list(events.sse_frames([_progress()], v2=False))
        self.assertTrue(frames[0].startswith('data: '))
        self.assertNotIn('id: ', frames[0])

    def test_switch_reads_env(self):
        with mock.patch.dict(os.environ, {'SSE_EVENTS_V2': '0'}):
            self.assertFalse(events.sse_v2_enabled())
        with mock.patch.dict(os.environ, {'SSE_EVENTS_V2': '1'}):
            self.assertTrue(events.sse_v2_enabled())


class TraceWiringTest(unittest.TestCase):
    def test_turn_events_classified(self):
        for name, status in (('turn.started', 'running'), ('turn.complete', 'ok'),
                             ('turn.error', 'error'), ('turn.interrupted', 'error')):
            env = events.build({'id': 'b' * 32, 'turn_id': 5}, name, {}, 1)
            self.assertEqual(env['kind'], name)
            self.assertEqual(env['status'], status)
            self.assertEqual(env['phase'], 'persist' if 'started' not in name else 'request')
            self.assertEqual(env['turn_id'], 5)

    def test_from_row_roundtrip(self):
        env = events.build({'id': 'c' * 32, 'turn_id': 9}, 'model.call' if False else
                           'full_model.model_call', {'purpose': 'core_full_advice', 'seconds': 2}, 11)
        row = dict(time='2026-11-07T08:00:00+0800', trace_id='c' * 32, event='full_model.model_call',
                   purpose='core_full_advice', seconds=2, seq=11, turn_id=9)
        back = events.from_row(row)
        self.assertEqual(back['seq'], 11)
        self.assertEqual(back['kind'], 'model.call')
        self.assertEqual(back['ts'], row['time'])
        self.assertIn('生成回答', back['title'])


if __name__ == '__main__':
    unittest.main()
