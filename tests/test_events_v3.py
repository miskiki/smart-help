# -*- coding: utf-8 -*-
"""P0 事件流 v3 单测：envelope 人话文本与答案分段（不依赖数据库）。"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import events  # noqa: E402

STATE = {'id': 'a' * 32, 'turn_id': 12}


def _build(event, fields=None):
    return events.build(STATE, event, fields or {}, 1)


class NarrativeTest(unittest.TestCase):
    def test_envelope_version(self):
        self.assertEqual(events.VERSION, 'evt-2')
        env = _build('turn.started', {'question_len': 12})
        self.assertEqual(env['v'], 'evt-2')
        self.assertIn('text', env)
        self.assertIn('title', env)

    def test_stage_running_and_done_for_humans(self):
        start = _build('span_start', {'stage': 'policy.load'})
        self.assertEqual(start['status'], 'running')
        self.assertEqual(start['text'], '正在加载政策库…')
        done = _build('span_end', {'stage': 'policy.load', 'duration_ms': 320})
        self.assertEqual(done['status'], 'ok')
        self.assertIn('加载政策库完成', done['text'])
        self.assertIn('320ms', done['text'])

    def test_stage_error_is_visible(self):
        env = _build('span_end', {'stage': 'vector.search', 'error_type': 'ReadTimeout'})
        self.assertEqual(env['status'], 'error')
        self.assertIn('失败', env['text'])
        self.assertIn('ReadTimeout', env['text'])

    def test_model_call_uses_chinese_purpose(self):
        env = _build('full_model.model_call', {'purpose': 'core_full_reason', 'duration_ms': 2100})
        self.assertEqual(env['kind'], 'model.call')
        self.assertIn('调用模型：逐条核验', env['text'])
        self.assertIn('2.1s', env['text'])
        self.assertNotIn('core_full_', env['text'])

    def test_wrapper_stage_is_internal(self):
        start = _build('span_start', {'stage': 'model.answer'})
        self.assertEqual(start['exposure'], 'internal')
        self.assertEqual(start['text'], '正在推理…')
        load = _build('span_start', {'stage': 'policy.load'})
        self.assertEqual(load['exposure'], 'user')

    def test_model_call_start_is_running(self):
        env = _build('full_model.model_call_start', {'purpose': 'core_full_fact_extract'})
        self.assertEqual(env['kind'], 'model.call')
        self.assertEqual(env['status'], 'running')
        self.assertEqual(env['text'], '正在调用模型：事实抽取…')

    def test_retrieval_counts(self):
        env = _build('full_model.retrieval', {'intent_count': 6, 'retrieved_chunks': 140,
                                              'source_keyword_hits': 3, 'unique_candidates': 35})
        self.assertIn('35 条候选政策', env['text'])
        self.assertIn('6 个检索意图', env['text'])

    def test_evidence_load_shows_dropped(self):
        env = _build('full_model.evidence_load', {
            'selected_sources': 10, 'eligible_candidates': 35,
            'dropped': {'inactive': 3, 'region': 12, 'missing_detail': 10}})
        self.assertIn('10 条可用证据', env['text'])
        self.assertIn('排除 25 条', env['text'])

    def test_rule_path_texts(self):
        loaded = _build('rule.engine.loaded', {'rules': 98, 'local_rules': 59})
        self.assertIn('98 条', loaded['text'])
        evaluated = _build('rule.evaluate', {'rules': 453, 'cards': 6, 'seconds': 0.012})
        self.assertIn('453 条规则', evaluated['text'])
        self.assertIn('6 张卡片', evaluated['text'])
        cards = _build('rule.cards', {'rows': 6})
        self.assertIn('6 张', cards['text'])

    def test_rule_not_ready_is_visible(self):
        env = _build('rule.not_ready', {'county': '上犹', 'local_rules': 0, 'readiness': 'pending'})
        self.assertEqual(env['exposure'], 'user')
        self.assertIn('等待编译', env['text'])
        self.assertIn('全模型', env['text'])
        sparse = _build('rule.not_ready', {'readiness': 'running'})
        self.assertIn('编译中', sparse['text'])

    def test_facts_and_region_texts(self):
        facts = _build('full_model.facts', {'accepted': 12, 'rejected': 1, 'members': 2})
        self.assertIn('采纳 12 条', facts['text'])
        self.assertIn('成员 2 人', facts['text'])
        region = _build('region.normalized', {'region_status': 'known'})
        self.assertEqual(region['text'], '家庭地区：已确认')
        region2 = _build('region.normalized', {'province': '江西省'})
        self.assertEqual(region2['text'], '家庭地区：江西省')

    def test_match_graded_text(self):
        env = _build('match.graded', {'a': 1, 'b': 2, 'c': 3, 'd': 4})
        self.assertEqual(env['kind'], 'match.graded')
        self.assertEqual(env['exposure'], 'user')
        self.assertIn('条件匹配分档：A 1 · B 2 · C 3 · D 4', env['text'])

    def test_gate_and_conflict_texts(self):
        gate = _build('full_model.category_scope_mismatch', {'code': 'JX01', 'items': ['x']})
        self.assertEqual(gate['exposure'], 'audit')
        self.assertIn('类目范围不一致', gate['text'])
        conflict = _build('full_model.conflict.scope.classified', {'category': 'region_scope', 'count': 2})
        self.assertIn('region_scope', conflict['text'])

    def test_turn_complete_and_unknown_fallback(self):
        env = _build('turn.complete', {'matched': 6, 'rows': 6})
        self.assertIn('匹配 6 项政策', env['text'])
        unknown = events.build(STATE, 'strange.event', {'foo': 'bar'}, 2)
        self.assertEqual(unknown['text'], unknown['title'])

    def test_text_is_always_short(self):
        fields = {'selected_sources': 10, 'eligible_candidates': 35,
                  'dropped': {'region': 12, 'inactive': 3, 'missing_detail': 10,
                              'date': 2, 'stale_index': 1}}
        env = _build('full_model.evidence_load', fields)
        self.assertLessEqual(len(env['text']), events.TEXT_MAX)

    def test_envelope_has_no_user_content(self):
        # 用户原话、家庭值即使误传进 fields，也不得出现在 text/detail 里。
        env = _build('full_model.retrieval', {'intent_count': 2, 'unique_candidates': 9,
                                              'question': '我叫张三，62岁，住新建区'})
        blob = json.dumps(env, ensure_ascii=False)
        self.assertNotIn('张三', blob)
        self.assertNotIn('62岁', blob)


class AnswerSegmentsTest(unittest.TestCase):
    def test_join_is_byte_exact(self):
        samples = [
            '短回答。',
            '第一段。' * 40 + '\n\n第二段没有句号也不长' + '！' * 200 + '\n\n最后一段。',
            '换行\n测试\n\n\n多空行\n' + '句。' * 90,
            '  前后空白也要保留。  ',
        ]
        for text in samples:
            segments = events.answer_segments(text)
            self.assertEqual(''.join(segments), text)
            self.assertTrue(all(s for s in segments))
            self.assertLessEqual(max(len(s) for s in segments), 200)

    def test_long_answer_is_split(self):
        text = '这是一条逐条引用的回答。' * 30
        segments = events.answer_segments(text)
        self.assertGreater(len(segments), 1)
        self.assertTrue(any(len(s) > 40 for s in segments))

    def test_short_answer_single_segment(self):
        self.assertEqual(events.answer_segments('好的。'), ['好的。'])
        self.assertEqual(events.answer_segments(''), [])

    def test_paragraph_boundary_preferred(self):
        text = '第一段。' * 20 + '\n\n' + '第二段。' * 20
        segments = events.answer_segments(text, max_len=160)
        self.assertTrue(any(s.endswith('\n\n') or s.endswith('第一段。') for s in segments))
        self.assertEqual(''.join(segments), text)


class AnswerDeltaFrameTest(unittest.TestCase):
    def test_answer_delta_is_plain_data_frame(self):
        ev = {'type': 'answer.delta', 'event_id': 't:a', 'segment_id': 0,
              'content': '第一段。', 'done': False}
        frame = list(events.sse_frames([ev], v2=True))[0]
        self.assertTrue(frame.startswith('data: '))
        payload = json.loads(frame.split('data: ', 1)[1].strip())
        self.assertEqual(payload['type'], 'answer.delta')
        self.assertEqual(payload['event_id'], 't:a')

    def test_answer_full_frame_still_last(self):
        events_in = [{'type': 'answer.delta', 'event_id': 't:a', 'segment_id': 0,
                      'content': 'a', 'done': True},
                     {'type': 'answer', 'content': 'a'}]
        frames = list(events.sse_frames(events_in, v2=True))
        self.assertEqual(len(frames), 2)
        self.assertIn('"answer"', frames[1])

class AgentEventTest(unittest.TestCase):
    """P3：调查循环的思考/工具/结论必须作为用户可见事件，而不是内部日志。"""

    def test_thought_is_user_visible(self):
        env = _build('full_model.agent_thought', {'step': 2, 'thought': '先核对地区再读原文'})
        self.assertEqual(env['kind'], 'agent.thought')
        self.assertEqual(env['phase'], 'decide')
        self.assertEqual(env['exposure'], 'user')
        self.assertIn('调查第 2 步', env['text'])
        self.assertIn('先核对地区', env['text'])

    def test_tool_call_and_result_are_visible(self):
        call = _build('full_model.agent_tool_call', {'tool': 'read_clause', 'policy_code': 'S-ZB-090'})
        self.assertEqual(call['kind'], 'agent.tool.call')
        self.assertIn('read_clause', call['text'])
        self.assertIn('S-ZB-090', call['text'])
        done = _build('full_model.agent_tool_result', {'tool': 'evaluate_condition',
                                                       'tool_state': 'satisfied'})
        self.assertEqual(done['kind'], 'agent.tool.result')
        self.assertIn('条件成立', done['text'])

    def test_finding_summary_is_visible(self):
        env = _build('full_model.agent_finding', {'policy_code': 'S-ZB-090', 'finding_count': 2,
                                                  'quote_ok': True})
        self.assertEqual(env['kind'], 'agent.finding')
        self.assertEqual(env['exposure'], 'user')
        self.assertIn('核验说明', env['text'])
        self.assertIn('S-ZB-090', env['text'])

    def test_unknown_agent_event_falls_back_to_audit_not_lost(self):
        env = _build('full_model.some_new_agent_event', {'step': 1})
        self.assertEqual(env['kind'], 'domain.some_new_agent_event')
        self.assertEqual(env['exposure'], 'audit')


if __name__ == '__main__':
    unittest.main()
