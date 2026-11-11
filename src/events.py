# -*- coding: utf-8 -*-
"""统一事件协议：把内部链路事件转成前端与回放共用的 envelope。

设计原则：
- 只做“翻译”，不参与任何判定，不改变流水线行为；事件构造失败绝不影响主流程。
- 隐私最小化：detail/metrics 只放白名单内的安全标量（计数、耗时、状态、政策编号），
  用户原话、家庭值、证据原文一律不进 envelope。
- 未知事件不丢弃：exposure=audit、标题用事件名兜底，保证可回放、可排查。
"""
import json
import os
import re
import time

VERSION = 'evt-2'
TEXT_MAX = 80

PHASES = ('request', 'understand', 'retrieve', 'evaluate', 'decide', 'answer', 'persist')
PHASE_LABELS = {'request': '请求', 'understand': '理解', 'retrieve': '检索',
                'evaluate': '评估', 'decide': '裁决', 'answer': '作答', 'persist': '落库'}

# 只折叠不丢：这些包裹层与一次具体模型调用同义，放内部过程，避免时间线上同一动作两行。
STAGE_INTERNAL = {'model.answer', 'model.health', 'consultation.pipeline', 'answer.pipeline'}

# 阶段名 -> (phase, 中文标题)。与 chain_trace.install() 的装饰表保持一致。
STAGES = {
    'household.parse': ('understand', '解析家庭信息'),
    'household.pipeline': ('understand', '家庭档案流水线'),
    'policy_core.facts': ('understand', '抽取家庭事实'),
    'policy_core.run': ('request', '核心流水线'),
    'policy.load': ('retrieve', '加载政策库'),
    'policy_core.catalog': ('retrieve', '加载政策目录'),
    'graph.load': ('retrieve', '加载政策图谱'),
    'graph.recall': ('retrieve', '图谱召回'),
    'vector.load': ('retrieve', '加载向量索引'),
    'model.embed': ('retrieve', '生成检索向量'),
    'vector.search': ('retrieve', '向量检索'),
    'candidates.merge': ('retrieve', '合并候选政策'),
    'rules.match': ('evaluate', '规则匹配'),
    'model.parse_judgment': ('evaluate', '解析模型判定'),
    'eligibility.verify': ('evaluate', '逐条资格核验'),
    'policy_core.questions': ('decide', '规划追问'),
    'model.answer': ('answer', '推理'),
    'policy_core.advice': ('answer', '生成建议'),
    'policy_core.consultation': ('answer', '咨询处理'),
    'consultation.pipeline': ('answer', '咨询流水线'),
    'answer.pipeline': ('answer', '作答流水线'),
    'model.health': ('request', '模型健康检查'),
}

# model purpose -> 中文标题。未知目的回退到“模型调用”。
PURPOSES = {
    'advice': '生成建议', 'consult_advice': '咨询建议', 'consult_policy': '政策咨询',
    'core_advice': '核心建议', 'core_advice_audit': '建议审核',
    'core_consultation': '咨询处理', 'core_consultation_audit': '咨询审核',
    'core_explanation': '结果解释', 'core_explanation_audit': '解释审核',
    'core_fact_audit': '事实审核', 'core_fact_extract': '事实抽取',
    'core_full_advice': '生成回答', 'core_full_advice_audit': '回答审核',
    'core_full_advice_audit_repair': '回答审核修复', 'core_full_audit_repair': '审核修复',
    'core_full_branches_match': '分支匹配', 'core_full_conflict_scope': '冲突口径裁决',
    'core_full_fact_audit': '事实审核', 'core_full_fact_extract': '事实抽取',
    'core_full_member_repair': '成员信息修复', 'core_full_policy_audit': '政策适用审核',
    'core_full_policy_compile': '政策条件编译', 'core_full_policy_repair': '政策条件修复',
    'core_full_primary_match': '初筛匹配', 'core_full_primary_phrase_audit': '初筛措辞审核',
    'core_full_primary_phrase_repair': '初筛措辞修复', 'core_full_question_bind': '追问绑定',
    'core_full_reason': '逐条核验', 'core_full_reason_audit': '核验审核',
    'core_full_reason_binding_repair': '核验绑定修复', 'core_full_reason_repair': '核验修复',
    'core_full_rerank': '来源重排', 'core_full_retrieval_plan': '检索规划',
    'core_full_review_blocking_repair': '审核阻塞修复', 'core_full_scope_audit': '口径审核',
    'core_full_scope_audit_id_repair': '口径编号修复',
    'core_full_scope_audit_repair': '口径审核修复',
    'core_full_scope_enum_repair': '口径枚举修复',
    'agent_plan': '调查计划', 'agent_summarize': '核验总结',
    'core_full_semantic_repair': '语义修复', 'core_full_text_normalize': '文本归一',
    'core_full_text_verify': '文本核验', 'core_intent': '意图分类', 'intake_classify': '意图分类',
    'core_omission_probe': '遗漏探查', 'core_question_audit': '追问审核',
    'core_question_plan': '追问规划', 'core_role_audit': '角色审核',
    'core_role_resolve': '角色识别', 'judge': '判定',
    'policy_compile': '政策编译', 'policy_direction': '方向选择',
    'policy_gap_review': '缺口复核', 'policy_source_case_audit': '情形审核',
    'policy_source_cases': '情形抽取', 'policy_subject_contract': '主体契约',
    'policy_type_repair': '类型修复', 'qa': '问答',
    'showcase_analysis': '展示分析', 'showcase_analysis_audit': '展示分析审核',
    'showcase_fact_audit': '展示事实审核', 'showcase_fact_extract': '展示事实抽取',
}

PURPOSE_PHASE = {
    'understand': ('core_full_fact_extract', 'core_full_fact_audit', 'core_full_text_normalize',
                   'core_full_text_verify', 'core_full_member_repair', 'core_full_role_resolve',
                   'core_full_role_audit', 'core_fact_extract', 'core_fact_audit', 'core_intent',
                   'intake_classify', 'showcase_fact_extract', 'showcase_fact_audit'),
    'retrieve': ('core_full_retrieval_plan', 'core_full_rerank', 'consult_policy'),
    'decide': ('core_full_question_plan', 'core_full_question_bind', 'core_question_audit',
               'core_question_plan', 'core_role_audit', 'agent_plan', 'agent_summarize'),
    'answer': ('core_full_advice', 'core_full_advice_audit', 'core_full_advice_audit_repair',
               'core_advice', 'core_advice_audit', 'core_consultation', 'core_consultation_audit',
               'core_explanation', 'core_explanation_audit', 'consult_advice', 'advice', 'qa',
               'showcase_analysis', 'showcase_analysis_audit'),
}

# full_model.<name> 领域事件 -> (kind, phase, exposure, 中文标题)
DOMAIN_EVENTS = {
    'evidence_load': ('evidence.load', 'retrieve', 'user', '加载政策证据'),
    'retrieval_plan': ('retrieval.plan', 'retrieve', 'user', '规划检索方向'),
    'retrieval': ('retrieval.candidates', 'retrieve', 'user', '检索候选政策'),
    'rerank': ('retrieval.rerank', 'retrieve', 'user', '来源重排'),
    'lexical_rerank': ('retrieval.rerank', 'retrieve', 'user', '关键词重排'),
    'source_rerank_backend': ('retrieval.rerank_backend', 'retrieve', 'audit', '重排后端'),
    'rerank_cache': ('retrieval.rerank_cache', 'retrieve', 'audit', '重排缓存'),
    'attribute_anchor': ('retrieval.anchor', 'retrieve', 'user', '属性锚定'),
    'facts': ('facts.updated', 'understand', 'user', '更新家庭事实'),
    'questions': ('questions.planned', 'decide', 'user', '规划追问'),
    'model_call': ('model.call', 'evaluate', 'user', '模型调用'),
    'reasoning': ('eligibility.reasoning', 'evaluate', 'audit', '逐条核验过程'),
    'assessment': ('eligibility.assessment', 'evaluate', 'audit', '判定汇总'),
    'interpretation': ('eligibility.interpretation', 'evaluate', 'audit', '口径解释'),
    'result': ('policy.result', 'answer', 'audit', '结果汇总'),
    'name_gate_rejected': ('gate.name_rejected', 'evaluate', 'audit', '名称门拦截'),
    'name_gate_prefiltered': ('gate.name_prefiltered', 'evaluate', 'audit', '名称预筛'),
    'category_scope_mismatch': ('gate.category_mismatch', 'evaluate', 'audit', '类目范围不一致'),
    'special_object_missing': ('gate.special_object_missing', 'evaluate', 'audit', '特殊对象缺失'),
    'conflict_scope_rejected': ('conflict.scope.rejected', 'evaluate', 'audit', '冲突口径评测失败'),
    'source_scope_cache': ('cache.scope', 'evaluate', 'audit', '来源口径缓存'),
    # T5 程序化冲突口径（reasoning._conflict_scope_plan 发出）。
    'conflict.scope.classified': ('conflict.scope.classified', 'decide', 'user', '冲突口径分类'),
    'conflict.scope.program_route': ('conflict.scope.evaluated', 'decide', 'user', '程序化裁决冲突口径'),
    'conflict.scope.fallback': ('conflict.scope.fallback', 'decide', 'audit', '冲突口径回落模型'),
    'conflict.scope.audit_cached': ('conflict.scope.audit_cached', 'evaluate', 'audit', '复用口径审核'),
    # P3 调查式 agent：对看过但未出卡的政策做限定轮次只读核验。
    'agent_thought': ('agent.thought', 'decide', 'user', '调查思路'),
    'agent_tool_call': ('agent.tool.call', 'decide', 'user', '调用只读工具'),
    'agent_tool_result': ('agent.tool.result', 'decide', 'user', '工具核验结果'),
    'agent_finding': ('agent.finding', 'decide', 'user', '核验说明'),
    'region.gate': ('region.gate', 'decide', 'user', '家庭地区待确认'),
    # 全国化：policy_scope 结构化地区范围过滤（开关 POLICY_SCOPE，默认关）。
    'scope.filter': ('scope.filter', 'decide', 'user', '结构化地区范围过滤'),
    # 只读全国模式（开关 NATIONAL_READONLY，默认关）：核对政策库是否收录该地区。
    'region.coverage': ('region.coverage', 'decide', 'user', '地区政策收录检查'),
}

# 顶层链路事件。
TOP_EVENTS = {
    'request_start': ('request.started', 'request', 'internal', '请求开始'),
    'route': ('request.route', 'request', 'internal', '请求路由'),
    'request_end': ('request.completed', 'request', 'internal', '请求结束'),
    'trace_summary': ('request.summary', 'request', 'internal', '链路汇总'),
    'stream_error': ('request.stream_error', 'request', 'internal', '流式传输错误'),
    'span_start': ('stage', None, 'user', None),
    'span_end': ('stage', None, 'user', None),
    'turn.started': ('turn.started', 'request', 'user', '开始处理问题'),
    'turn.complete': ('turn.complete', 'persist', 'user', '本轮处理完成'),
    'turn.error': ('turn.error', 'persist', 'user', '本轮处理失败'),
    'turn.interrupted': ('turn.interrupted', 'persist', 'user', '本轮被中断'),
    # 规则快速路径（毫秒级求值）同样必须让用户看到真实分析过程。
    'region.normalized': ('region.normalized', 'understand', 'user', '确认家庭地区'),
    'extraction.completed': ('facts.extracted', 'understand', 'user', '家庭事实抽取完成'),
    'rule.engine.loaded': ('rule.loaded', 'evaluate', 'user', '加载地区规则库'),
    'rule.evaluate': ('rule.evaluated', 'evaluate', 'user', '规则逐条求值'),
    'rule.cards': ('rule.cards', 'decide', 'user', '汇总政策卡片'),
    'rule.card': ('rule.card', 'decide', 'user', '生成政策卡片'),
    # P2 匹配核心 v2：条件原子三值求值后的 A/B/C/D 分档（规则路径与全模型路径共用）。
    'match.graded': ('match.graded', 'decide', 'user', '条件匹配分档'),
    'rule.questions': ('questions.planned', 'decide', 'user', '规划待补问题'),
    'rule.not_ready': ('rule.not_ready', 'evaluate', 'user', '规则库未就绪（改用全模型核验）'),
    'answer.compose': ('answer.composed', 'answer', 'user', '组织回答'),
    'region_soft_filter': ('region.soft_filter', 'retrieve', 'user', '地区过滤结果'),
    'scope.filter': ('scope.filter', 'decide', 'user', '结构化地区范围过滤'),
    'region.coverage': ('region.coverage', 'decide', 'user', '地区政策收录检查'),
    # P1 输入理解层：先甄别意图/主体/已给判据，再决定是否进入匹配。
    'intake.parsed': ('intake.parsed', 'understand', 'user', '理解输入'),
    'intake.clarify': ('intake.clarify', 'understand', 'user', '需要补充家庭信息'),
}

# envelope detail 里允许出现的字段（只读安全标量）。不在白名单的字段一律丢弃。
SAFE_KEYS = {
    'count', 'total', 'kept', 'dropped', 'ms', 'duration_ms', 'seconds',
    'candidates', 'details', 'sources', 'rows', 'members', 'policies', 'items',
    'unique_candidates', 'retrieved_chunks', 'source_keyword_hits', 'intent_count',
    'eligible_candidates', 'selected_sources', 'candidate_budget_truncated',
    'inactive', 'stale_index', 'missing_detail', 'date', 'region', 'region_dropped',
    'per_policy_budget', 'policy_branch_budget', 'region_unconfirmed_kept',
    'verdict', 'status', 'reason', 'error_type', 'stage', 'purpose', 'policy_code',
    'detail_id', 'decision_no', 'span_id', 'parent_id', 'http_status', 'stream_status',
    'model_calls', 'batch', 'hit', 'hit_rate', 'top_k', 'threshold', 'score',
    'region_status', 'kind', 'phase', 'member_no', 'question_count', 'source_count',
    'facts', 'fields', 'intents', 'branches', 'conflicts', 'unknowns',
    'question_len', 'matched', 'qa_id', 'replayed',
    'candidate_count', 'evidence_count', 'policy_count', 'rule_count', 'kept_count',
    'dropped_count', 'unknown_count', 'elapsed_ms', 'call_count', 'retry',
    'rules', 'local_rules', 'province_rules', 'readiness', 'cards', 'member', 'chars', 'county',
    'relation', 'source', 'unknown', 'category', 'source_id',
    'route', 'subject', 'topic', 'confidence', 'provided', 'missing',
    'a', 'b', 'c', 'd', 'version', 'tier', 'tier_label',
    'tool', 'step', 'thought', 'quote_ok', 'finding_count', 'tool_state',
    'focus', 'quotes_verified',
}
MAX_STR = 64


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _scalar(value):
    """把字段值压成安全标量：数字/布尔/短字符串；容器只留计数。"""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR]
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return None


def sanitize(fields, extra=()):
    out = {}
    for key in tuple(SAFE_KEYS) + tuple(extra):
        if key not in fields:
            continue
        value = _scalar(fields.get(key))
        if value is not None:
            out[key] = value
    return out


def _phase_for_purpose(purpose):
    if not purpose:
        return 'evaluate'
    for phase, names in PURPOSE_PHASE.items():
        if purpose in names:
            return phase
    if 'advice' in purpose or 'explanation' in purpose or purpose.startswith('qa'):
        return 'answer'
    if 'retrieval' in purpose or 'rerank' in purpose:
        return 'retrieve'
    if 'fact' in purpose or 'role' in purpose or 'text' in purpose or 'intent' in purpose:
        return 'understand'
    if 'question' in purpose:
        return 'decide'
    return 'evaluate'


def _metrics(fields):
    metrics = {}
    ms = fields.get('duration_ms')
    if ms is None:
        ms = fields.get('ms')
    if ms is None:
        seconds = fields.get('seconds')
        if isinstance(seconds, (int, float)):
            ms = round(seconds * 1000, 1)
    if isinstance(ms, (int, float)):
        metrics['ms'] = round(float(ms), 1)
    return metrics


def _int(value):
    """安全取整数；容器返回长度，字符串不猜。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return None


def _duration_text(fields):
    ms = fields.get('duration_ms')
    if ms is None:
        ms = fields.get('ms')
    if ms is None:
        seconds = fields.get('seconds')
        if isinstance(seconds, (int, float)):
            ms = float(seconds) * 1000
    if not isinstance(ms, (int, float)):
        return ''
    if ms >= 1000:
        return '%.1fs' % (ms / 1000.0)
    return '%dms' % round(ms)


def _clip(text, limit=TEXT_MAX):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def narrative(event, fields, kind, title, status):
    """把内部事件翻译成一句用户能读懂的动作描述（≤80 字）。

    只使用白名单标量（计数/耗时/状态/政策编号），不含用户原话与家庭值。
    未知事件回退标题；任何异常回退标题，绝不影响主流程。
    """
    try:
        fields = fields if isinstance(fields, dict) else {}
        title = title or kind or event
        duration = _duration_text(fields)
        if event == 'span_start':
            return _clip('正在%s…' % title)
        if event == 'span_end':
            if status == 'error':
                return _clip('%s失败（%s）' % (title, fields.get('error_type') or '内部错误'))
            return _clip('%s完成%s' % (title, '（%s）' % duration if duration else ''))
        if event in ('full_model.model_call', 'full_model.model_call_start'):
            label = title[3:] if title.startswith('模型：') else title
            if event.endswith('_start'):
                return _clip('正在调用模型：%s…' % label)
            if status == 'error':
                return _clip('调用模型失败：%s（%s）' % (label, fields.get('error_type') or '错误'))
            return _clip('调用模型：%s%s' % (label, '（%s）' % duration if duration else ''))

        if kind == 'retrieval.candidates':
            unique = _int(fields.get('unique_candidates'))
            intents = _int(fields.get('intent_count'))
            chunks = _int(fields.get('retrieved_chunks'))
            if unique is None:
                return _clip('检索候选政策')
            text = '检索到 %d 条候选政策' % unique
            if intents:
                text += '（%d 个检索意图）' % intents
            if chunks:
                text += '，%d 个片段' % chunks
            return _clip(text)
        if kind == 'retrieval.rerank':
            accepted = _int(fields.get('accepted'))
            returned = _int(fields.get('returned'))
            positive = _int(fields.get('positive_sources'))
            if accepted is not None:
                return _clip('来源重排：保留 %d 条%s' % (
                    accepted, '，返回 %d 条' % returned if returned is not None else ''))
            if positive is not None:
                return _clip('关键词重排：命中 %d 条来源' % positive)
            return _clip(title)
        if kind == 'retrieval.plan':
            count = _int(fields.get('count'))
            return _clip('规划 %d 个检索方向' % count if count is not None else title)
        if kind == 'retrieval.anchor':
            promoted = fields.get('promoted')
            count = _int(promoted)
            return _clip('按属性锚定 %d 条政策' % count if count is not None else title)
        if kind == 'evidence.load':
            kept = _int(fields.get('selected_sources'))
            if kept is None:
                kept = _int(fields.get('kept'))
            eligible = _int(fields.get('eligible_candidates'))
            dropped = fields.get('dropped')
            dropped_n = sum(v for v in dropped.values() if isinstance(v, int)) if isinstance(dropped, dict) else None
            if kept is None:
                return _clip('加载政策证据')
            text = '筛出 %d 条可用证据' % kept
            if eligible is not None:
                text += '（候选 %d 条' % eligible
                text += '，排除 %d 条）' % dropped_n if dropped_n else '）'
            elif dropped_n:
                text += '（排除 %d 条）' % dropped_n
            return _clip(text)
        if kind == 'facts.updated':
            accepted = _int(fields.get('accepted'))
            members = _int(fields.get('members'))
            if accepted is not None:
                text = '抽取家庭事实：采纳 %d 条' % accepted
                if members is not None:
                    text += '（成员 %d 人）' % members
                return _clip(text)
            count = _int(fields.get('facts'))
            return _clip('更新家庭事实（%d 项）' % count if count is not None else '更新家庭事实')
        if kind == 'facts.extracted':
            calls = _int(fields.get('model_calls'))
            return _clip('家庭事实抽取完成%s' % ('（%d 次模型调用）' % calls if calls else ''))
        if kind == 'questions.planned':
            count = _int(fields.get('question_count'))
            if count is None:
                count = _int(fields.get('count'))
            return _clip('规划 %d 个待补充问题' % count if count is not None else '规划待补充问题')
        if kind == 'rule.loaded':
            rules = _int(fields.get('rules'))
            local = _int(fields.get('local_rules'))
            if rules is None:
                return _clip(title)
            return _clip('加载地区规则库 %d 条%s' % (rules, '（本地 %d 条）' % local if local else ''))
        if kind == 'rule.not_ready':
            readiness = str(fields.get('readiness') or fields.get('state') or '')
            label = {'pending': '等待编译', 'running': '编译中', 'failed': '编译失败',
                     'no_rules': '无可用的省级规则', 'disabled': '开关关闭'}.get(readiness, readiness)
            return _clip('规则库未就绪（%s），本轮改用全模型逐条核验' % label if label
                         else '规则库未就绪，本轮改用全模型逐条核验')
        if kind == 'rule.evaluated':
            rules = _int(fields.get('rules'))
            cards = _int(fields.get('cards'))
            if rules is None:
                return _clip(title)
            return _clip('逐条求值 %d 条规则，生成 %d 张卡片%s' % (
                rules, cards or 0, '（%s）' % duration if duration else ''))
        if kind == 'rule.cards':
            rows = _int(fields.get('rows'))
            return _clip('汇总政策卡片：%d 张' % rows if rows is not None else title)
        if kind == 'rule.card':
            code = fields.get('policy_code')
            return _clip('生成政策卡片 %s' % code if code else title)
        if kind == 'match.graded':
            a, b = _int(fields.get('a')), _int(fields.get('b'))
            c, d = _int(fields.get('c')), _int(fields.get('d'))
            if a is None:
                return _clip(title)
            return _clip('条件匹配分档：A %d · B %d · C %d · D %d' % (a, b or 0, c or 0, d or 0))
        if kind == 'region.normalized':
            status_text = fields.get('region_status')
            zh = {'known': '已确认', 'unknown': '待确认', 'outside_jiangxi': '省外',
                  'ambiguous': '待确认'}.get(status_text)
            if zh:
                return _clip('家庭地区：%s' % zh)
            province = fields.get('province')
            return _clip('家庭地区%s' % ('：' + province if province else ''))
        if kind == 'region.soft_filter':
            kept = _int(fields.get('kept'))
            dropped = _int(fields.get('dropped'))
            unknown = _int(fields.get('unknown'))
            if kept is None:
                return _clip(title)
            text = '地区过滤：保留 %d 条' % kept
            if dropped:
                text += '，排除 %d 条' % dropped
            if unknown:
                text += '，%d 条待确认' % unknown
            return _clip(text)
        if kind in ('gate.name_rejected', 'gate.name_prefiltered'):
            reason = fields.get('reason') or fields.get('code')
            label = '名称门拦截' if kind == 'gate.name_rejected' else '名称预筛'
            return _clip('%s%s' % (label, '（%s）' % reason if reason else ''))
        if kind == 'gate.category_mismatch':
            return _clip('类目范围不一致，拦截 1 条')
        if kind == 'gate.special_object_missing':
            return _clip('特殊对象缺失，拦截 1 条')
        if kind == 'conflict.scope.classified':
            category = fields.get('category')
            count = _int(fields.get('count'))
            return _clip('冲突口径分类：%s%s' % (category or '已分类', '（%d 条）' % count if count else ''))
        if kind == 'conflict.scope.evaluated':
            return _clip('程序化裁决冲突口径')
        if kind == 'conflict.scope.fallback':
            return _clip('冲突口径回落模型%s' % ('（%s）' % fields.get('reason') if fields.get('reason') else ''))
        if kind == 'conflict.scope.audit_cached':
            count = _int(fields.get('count'))
            return _clip('复用口径审核%s' % ('（%d 条）' % count if count else ''))
        if kind == 'eligibility.reasoning':
            accepted = _int(fields.get('accepted'))
            rejected = _int(fields.get('rejected'))
            if accepted is None:
                return _clip(title)
            return _clip('逐条核验：通过 %d 条，排除 %d 条' % (accepted, rejected or 0))
        if kind == 'policy.result':
            rows = _int(fields.get('rows'))
            questions = _int(fields.get('questions'))
            return _clip('结果汇总：%d 张卡片，%d 个追问' % (rows or 0, questions or 0))
        if kind == 'answer.composed':
            chars = _int(fields.get('chars'))
            return _clip('组织回答%s' % ('（%d 字）' % chars if chars else ''))
        if kind == 'intake.parsed':
            route = fields.get('route') or ''
            labels = {'eligibility': '资格预判', 'consultation': '政策咨询',
                      'chat': '普通交流', 'clarify': '需要补充信息'}
            subjects = {'household': '家庭', 'self': '本人', 'third_party': '家庭成员',
                        'policy_general': '政策', 'unknown': ''}
            text = '理解为：%s' % labels.get(route, route or '未知')
            parts = [x for x in (subjects.get(fields.get('subject'), ''), fields.get('topic') or '') if x]
            if parts:
                text += ' · ' + ' · '.join(parts)
            provided = fields.get('provided')
            if route in ('eligibility', 'clarify') and isinstance(provided, list) and provided:
                text += '，已提供 %d 类信息' % len(provided)
            return _clip(text)
        if kind == 'intake.clarify':
            labels = {'region': '地区', 'members': '成员', 'age': '年龄', 'health': '健康',
                      'labor': '劳动', 'income': '收入', 'insurance': '医保'}
            missing = fields.get('missing')
            names = [labels.get(k, k) for k in missing[:4]] if isinstance(missing, list) else []
            return _clip('需要补充家庭信息才能判断（%s）' % '/'.join(names) if names else '需要补充家庭信息才能判断')
        if kind == 'agent.thought':
            step = _int(fields.get('step'))
            thought = fields.get('thought') or ''
            if status == 'error':
                return _clip('调查思路规划失败%s' % ('（第 %d 步）' % step if step else ''))
            return _clip('调查第 %d 步：%s' % (step or 1, thought or '决定下一步核验动作'))
        if kind == 'agent.tool.call':
            tool = fields.get('tool') or '工具'
            code = fields.get('policy_code')
            return _clip('调用只读工具 %s%s' % (tool, '（%s）' % code if code else ''))
        if kind == 'agent.tool.result':
            tool = fields.get('tool') or '工具'
            tool_state = fields.get('tool_state')
            if status == 'error':
                return _clip('工具 %s 执行失败' % tool)
            zh = {'satisfied': '条件成立', 'violated': '条件不符', 'unknown': '条件待定'}.get(tool_state)
            return _clip('工具 %s 返回%s' % (tool, '：' + zh if zh else '核验结果'))
        if kind == 'agent.finding':
            count = _int(fields.get('finding_count'))
            code = fields.get('policy_code')
            if status == 'error':
                return _clip('核验说明生成失败')
            return _clip('核验说明%s' % ('：%s%s' % (code or '', '（共 %d 条）' % count if count else '')))
        if kind == 'region.coverage':
            policies = _int(fields.get('policies'))
            if policies is None:
                return _clip('核对政策库地区收录范围')
            if not policies:
                return _clip('政策库暂无该地区的政策条目')
            return _clip('政策库收录该地区政策 %d 条' % policies)
        if kind == 'region.gate':
            # 江西模式下明示服务范围（历史文案）；全国化模式不再等于"只支持江西"。
            scope = os.environ.get('SERVICE_SCOPE', 'jiangxi').strip().lower()
            where = '江西省服务范围' if scope != 'national' else '服务范围'
            return _clip('家庭所在地区还不能确认属于%s：%s' % (
                where, fields.get('reason') or '需要补充市／县（区）全称'))
        if kind == 'scope.filter':
            kept = _int(fields.get('kept'))
            dropped = _int(fields.get('dropped'))
            unknown = _int(fields.get('unknown'))
            if kept is None:
                return _clip(title)
            text = '按地区范围过滤候选政策：保留 %d 条' % kept
            if dropped:
                text += '，排除 %d 条不适用' % dropped
            if unknown:
                text += '，%d 条待核' % unknown
            return _clip(text)
        if kind == 'turn.started':
            return '开始分析您的问题'
        if kind == 'turn.complete':
            rows = _int(fields.get('rows'))
            return _clip('分析完成%s' % ('，匹配 %d 项政策' % rows if rows else ''))
        if kind == 'turn.error':
            return _clip('本轮处理失败%s' % ('（%s）' % fields.get('reason') if fields.get('reason') else ''))
        if kind == 'turn.interrupted':
            return '本轮处理被中断'
        return _clip(title if status != 'running' else '正在%s…' % title)
    except Exception:
        return _clip(title or event)


def answer_segments(text, max_len=160):
    """把完整回答切成可逐段推送的片段；片段按序拼接必须逐字节等于原文。"""
    if not isinstance(text, str) or not text:
        return []
    if max_len < 40:
        max_len = 40
    segments = []
    for block in re.split(r'(\n\s*\n)', text):
        if not block:
            continue
        if len(block) <= max_len:
            segments.append(block)
            continue
        buf = ''
        for piece in re.split(r'(?<=[。！？；!?;])', block):
            if not piece:
                continue
            if buf and len(buf) + len(piece) > max_len:
                segments.append(buf)
                buf = ''
            buf += piece
            while len(buf) > max_len:
                segments.append(buf[:max_len])
                buf = buf[max_len:]
        if buf:
            segments.append(buf)
    merged = []
    for segment in segments:
        if merged and len(segment) < 8:
            merged[-1] += segment
        else:
            merged.append(segment)
    return merged


def classify(event, fields):
    """返回 (kind, phase, exposure, title, status)。"""
    if event == 'span_start' or event == 'span_end':
        stage = fields.get('stage')
        stage = stage if isinstance(stage, str) and stage else 'unknown'
        phase, title = STAGES.get(stage, ('evaluate', stage))
        kind = 'stage.' + stage
        status = 'running' if event == 'span_start' else (
            'error' if fields.get('error_type') else 'ok')
        exposure = 'internal' if stage in STAGE_INTERNAL else 'user'
        return kind, phase, exposure, title, status
    if event in ('full_model.model_call', 'full_model.model_call_start'):
        purpose = fields.get('purpose') or ''
        title = '模型：' + PURPOSES.get(purpose, purpose or '调用')
        if event.endswith('_start'):
            return 'model.call', _phase_for_purpose(purpose), 'user', title, 'running'
        status = 'error' if fields.get('error') else 'ok'
        return 'model.call', _phase_for_purpose(purpose), 'user', title, status
    if event.startswith('full_model.'):
        name = event[len('full_model.'):]
        kind, phase, exposure, title = DOMAIN_EVENTS.get(
            name, ('domain.' + name, 'evaluate', 'audit', name))
        status = 'error' if (name.endswith('rejected') or fields.get('error')) else 'ok'
        return kind, phase, exposure, title, status
    if event in TOP_EVENTS:
        kind, phase, exposure, title = TOP_EVENTS[event]
        if event == 'span_start':
            return kind, phase, exposure, title, 'running'
        turn_status = {'turn.started': 'running', 'turn.complete': 'ok',
                       'turn.error': 'error', 'turn.interrupted': 'error'}.get(event)
        status = turn_status or fields.get('status') or 'ok'
        return kind, phase, exposure, title, status
    # 未知事件：保留为 audit（回放可见），标题用事件名兜底。
    return event, 'evaluate', 'audit', event, fields.get('status') or 'ok'


def build(state, event, fields, seq):
    """构造 envelope；任何异常返回 None，绝不影响主流程。"""
    try:
        kind, phase, exposure, title, status = classify(event, fields)
        return {
            'v': VERSION,
            'seq': int(seq),
            'trace_id': state.get('id'),
            'turn_id': state.get('turn_id'),
            'kind': kind,
            'phase': phase,
            'exposure': exposure,
            'title': title,
            'text': narrative(event, fields, kind, title, status),
            'status': status,
            'ts': _now(),
            'detail': sanitize(fields),
            'metrics': _metrics(fields),
        }
    except Exception:
        return None


def legacy_fields(event, fields):
    """旧前端字段：key/stage/purpose/done/ms/error。没有语义的事件返回 None。"""
    if event in ('span_start', 'span_end'):
        stage = fields.get('stage')
        if not stage:
            return None
        ms = fields.get('duration_ms')
        return {'key': 'stage:' + stage, 'stage': stage, 'purpose': fields.get('purpose'),
                'done': event == 'span_end', 'ms': ms,
                'error': bool(fields.get('error_type'))}
    if event == 'full_model.model_call':
        purpose = fields.get('purpose')
        ms = fields.get('duration_ms')
        if ms is None:
            seconds = fields.get('seconds')
            if isinstance(seconds, (int, float)):
                ms = round(seconds * 1000, 1)
        return {'key': 'call:' + (purpose or 'model_call'), 'stage': None,
                'purpose': purpose, 'done': True, 'ms': ms,
                'error': bool(fields.get('error'))}
    return None


def progress_view(event, fields, envelope):
    """合并旧字段与 envelope，供 SSE progress 使用；返回 None 表示不进流。"""
    if not envelope:
        return legacy_fields(event, fields)
    base = legacy_fields(event, fields) or {
        'key': 'evt:%s:%s' % (envelope.get('kind'), envelope.get('seq')),
        'stage': None, 'purpose': fields.get('purpose'),
        'done': envelope.get('status') != 'running',
        'ms': (envelope.get('metrics') or {}).get('ms'),
        'error': envelope.get('status') == 'error',
    }
    base['event'] = envelope
    return base


def from_row(row):
    """从 JSONL 行还原 envelope（回放用）；行内没有 seq 时按调用方给序号。"""
    fields = {k: v for k, v in row.items()
              if k not in ('time', 'trace_id', 'event', 'seq', 'turn_id')}
    state = {'id': row.get('trace_id'), 'turn_id': row.get('turn_id')}
    envelope = build(state, row.get('event'), fields, row.get('seq') or 0)
    if envelope and row.get('time'):
        envelope['ts'] = row.get('time')
    return envelope

def sse_v2_enabled():
    """SSE 事件协议 v2 开关（默认开）；关闭后退回纯 data 帧。"""
    import os
    return os.environ.get('SSE_EVENTS_V2', '1').lower() not in ('0', 'false', 'no')


def sse_frames(events, v2=None):
    """把 run_turn 事件编码成 SSE 帧。

    v2 时 progress 帧带 id/event 行，data 仍保留全部旧字段（旧前端不受影响）；
    ping 编码为 SSE 注释帧，避免长模型调用期间空闲连接被代理断开。
    """
    if v2 is None:
        v2 = sse_v2_enabled()
    for ev in events:
        if ev.get('type') == 'ping':
            yield ': ping\n\n'
            continue
        payload = json.dumps(ev, ensure_ascii=False)
        envelope = ev.get('event') if isinstance(ev.get('event'), dict) else None
        if v2 and envelope and ev.get('type') == 'progress':
            yield 'id: %s\nevent: %s\ndata: %s\n\n' % (
                envelope.get('seq'), envelope.get('kind') or 'progress', payload)
        else:
            yield 'data: ' + payload + '\n\n'
