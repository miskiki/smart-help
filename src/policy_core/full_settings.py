"""Full-library mode and bounded per-turn configuration."""
import os

VERSION='full-model-1.0'
MODE='full-model-v1'

def enabled():
    value=os.environ.get('POLICY_MATCH_MODE','').strip()
    if value and value not in ('full_model','pilot','showcase','legacy'):
        raise ValueError('未知政策分析模式')
    if value=='full_model':
        if os.environ.get('POLICY_SHOWCASE_ENABLED','').lower() in ('1','true','yes') or os.environ.get('POLICY_CORE_ENABLED','').lower() in ('1','true','yes'):
            raise ValueError('全库模式不能同时启用试点或7项展示模式')
        return True
    return False

def limits():
    source_ranker=os.environ.get('FULL_MODEL_SOURCE_RANKER','local')
    if source_ranker not in ('local','llm'):raise ValueError('未知来源排序后端')
    return dict(max_intents=6,top_k=40,max_candidates=36,
                # P0-2：每轮审核来源数可由 FULL_MODEL_MAX_SOURCES_PER_TURN 调整。
                # P3-3（缺陷三）：默认 6→12——qa529 实测 35 条 eligible 每轮只核 8 条，3 轮最多 18 条，25 项长期"未完成分析"。
                max_sources_per_turn=int(os.environ.get('FULL_MODEL_MAX_SOURCES_PER_TURN','12')),
                batch_size=2,max_questions=4,
                # 追问轮数单点来源：规则路径与全模型路径都从这里取，不再各自硬编码。
                max_rounds=int(os.environ.get('FULL_MODEL_MAX_ROUNDS','4')),
                source_ranker=source_ranker,inline_reasoning_repair=True, canonical_questions=True, evidence_summary=True,
                # P3-2（缺陷三）：source_batch_size 真实生效（reasoning.py 原 min(2,…) 硬顶已移除）。
                source_batch_size=int(os.environ.get("FULL_MODEL_SOURCE_BATCH_SIZE","2")),
                deadline=float(os.environ.get('FULL_MODEL_DEADLINE','180')),
                max_calls=int(os.environ.get('FULL_MODEL_MAX_CALLS','36')))
