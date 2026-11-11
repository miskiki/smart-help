"""真实模型的角色解析和独立审核适配器。

两个独立请求：
- model_resolver：从原文片段判断角色（draft 候选）
- model_auditor：独立核对解析结果是否正确
- model_reaudit：修正后重新审核
"""
import json
import re
from admin_panel.kb_rag import _llm_answer


RESOLVER_SYSTEM = """你是政策来源角色解析器。你的任务是分析政策原文片段，判断每段文字的角色。

角色定义：
- binding_condition：已确认适用于当前具体待遇的资格条件（名词短语也可能是必要条件）
- exclusion：明确的排除条件（不得、除外、不重复享受等）
- audience_statement：适用人群陈述
- classification：政策分类、索引或汇编覆盖标签
- benefit_standard：金额、周期、待遇档次
- procedure：办理流程
- reference_only：尚未确认关联的参考资料
- unresolved：无法可靠判断

重要规则：
1. 名词短语也可能表达必要条件，不要求出现"必须""仅限"才识别约束
2. 不要因为有"脱贫人口"就认为是资格条件，也不要因为没有"仅限"就认为不是
3. 多字段条件可能需要组合，不能将每个片段视为独立充分条件
4. "A不能单独证明符合"不等于"A不是必要条件"
5. 来源关系不明确时保留unknown，不虚构AND/OR关系
6. 家庭身份与成员学生身份不能混成同一主体
7. 金额不能当成资格条件
8. 列表子项要保留上级范围
9. 输出纯JSON items数组，不要markdown

输出格式：
{
  "items": [
    {
      "evidence_id": "E1",
      "role": "binding_condition",
      "applies_to": "current_detail",
      "constraint_relation": "required",
      "subject_scope": "member",
      "parent_scope": "",
      "reason": "简短解释"
    }
  ]
}
"""

AUDITOR_SYSTEM = """你是政策来源角色独立审核员。你的任务是核对角色解析结果是否正确。

检查项：
1. 角色是否受原文支持
2. 上级范围是否完整保留
3. 成员、家庭、机构是否混淆
4. 金额、流程是否被错误当成资格
5. 排除条件是否遗漏
6. 是否将对象栏自动认定为倾斜标签
7. 是否忽略真实对象限制
8. 是否将未确认关联资料施加给当前待遇

要求：
1. 对每个请求的 evidence_id 都要返回一条结果
2. 允许返回unresolved
3. 不要强迫二选一
4. 如果候选错误，可以输出 correction 建议，但 correction 不等于 accepted
5. 输出纯JSON items数组，不要markdown

输出格式：
{
  "items": [
    {
      "evidence_id": "E1",
      "audit_status": "accepted",
      "reason": "审核通过",
      "correction": null
    }
  ]
}

audit_status 可选值：accepted / rejected / unresolved
correction 可以是修正后的角色建议，也可以是 null
"""


def _parse_json(text):
    """从模型返回中提取JSON。"""
    text = re.sub(r'^```json\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _items_to_dict(items):
    """把 items 数组转成 {evidence_id: result} 字典。"""
    result = {}
    for item in items:
        eid = item.get('evidence_id')
        if eid:
            result[eid] = item
    return result


def model_resolver(fragments: dict, source_context: dict) -> tuple:
    """真实模型角色解析器。
    
    返回 (result_dict, error)
    result_dict: {evidence_id: {role, applies_to, constraint_relation, subject_scope, parent_scope, reason}}
    error: None 或错误信息
    """
    policy_name = source_context.get('policy_name', '')
    region = source_context.get('region', '')
    
    prompt_parts = [f"政策：{policy_name}", f"地区：{region}", "", "原文片段："]
    for eid, frag in fragments.items():
        prompt_parts.append(f"- {eid}: {frag.text}")
    
    prompt = "\n".join(prompt_parts)
    
    try:
        result = _llm_answer(
            question=prompt,
            context="",
            system=RESOLVER_SYSTEM,
            purpose="core_role_resolve",
        )
        parsed = _parse_json(result)
        if not parsed:
            return {}, 'invalid_json'
        items = parsed.get('items', [])
        if not items:
            return {}, 'invalid_schema'
        return _items_to_dict(items), None
    except Exception as e:
        return {}, f'model_call_failed: {type(e).__name__}'


def model_auditor(fragments: dict, source_context: dict) -> tuple:
    """真实模型独立审核器。
    
    返回 (result_dict, error, coverage_info)
    result_dict: {evidence_id: {audit_status, reason, correction}}
    error: None 或错误信息
    coverage_info: {requested: [...], received: [...], missing: [...]}
    """
    policy_name = source_context.get('policy_name', '')
    expected_ids = list(fragments.keys())
    
    prompt_parts = [f"政策：{policy_name}", "", f"需要审核的片段（共{len(expected_ids)}个）："]
    for eid, frag in fragments.items():
        prompt_parts.append(f"- {eid}: 原文={frag.text}, 角色={frag.role}, 理由={frag.reason}")
    
    prompt_parts.append(f"\n请对以下所有 evidence_id 都返回审核结果：{', '.join(expected_ids)}")
    
    prompt = "\n".join(prompt_parts)
    
    try:
        result = _llm_answer(
            question=prompt,
            context="",
            system=AUDITOR_SYSTEM,
            purpose="core_role_audit",
        )
        parsed = _parse_json(result)
        if not parsed:
            return {}, 'invalid_json', {'requested': expected_ids, 'received': [], 'missing': expected_ids}
        
        items = parsed.get('items', [])
        if not items:
            return {}, 'invalid_schema', {'requested': expected_ids, 'received': [], 'missing': expected_ids}
        
        result_dict = _items_to_dict(items)
        received = list(result_dict.keys())
        missing = [eid for eid in expected_ids if eid not in result_dict]
        
        return result_dict, None, {'requested': expected_ids, 'received': received, 'missing': missing}
    except Exception as e:
        return {}, f'model_call_failed: {type(e).__name__}', {'requested': expected_ids, 'received': [], 'missing': expected_ids}
