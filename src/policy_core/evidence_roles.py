"""片段级来源角色系统：区分"能阅读"和"能做资格条件"。

核心原则：
- 程序负责稳定分段和证据编号
- 模型只能引用程序给出的片段
- 角色不按字段名一刀切，按片段语义判断
- 未确认的关系保留 unresolved，不擅自决定 AND/OR
- resolve 和 audit 分开：解析器只给候选，审核器独立核对
"""
import re
from dataclasses import dataclass, field
from typing import Optional, Callable


# 角色枚举
ROLE_BINDING_CONDITION = 'binding_condition'      # 已确认的资格条件
ROLE_EXCLUSION = 'exclusion'                          # 明确的排除条件
ROLE_AUDIENCE_STATEMENT = 'audience_statement'   # 适用人群陈述
ROLE_CLASSIFICATION = 'classification'            # 分类/索引/标签
ROLE_BENEFIT_STANDARD = 'benefit_standard'        # 金额/周期/档次
ROLE_PROCEDURE = 'procedure'                        # 办理流程
ROLE_REFERENCE_ONLY = 'reference_only'            # 未确认关联的参考资料
ROLE_UNRESOLVED = 'unresolved'                     # 无法可靠判断

# 审核状态
AUDIT_DRAFT = 'draft'          # 解析器刚给的候选，未审核
AUDIT_ACCEPTED = 'accepted'    # 独立审核通过
AUDIT_REJECTED = 'rejected'    # 独立审核拒绝
AUDIT_FAILED = 'failed'        # 审核失败（超时、格式错误等）

# subject_scope 枚举
SCOPE_MEMBER = 'member'
SCOPE_HOUSEHOLD = 'household'
SCOPE_INSTITUTION = 'institution'
SCOPE_UNKNOWN = 'unknown'

VALID_SUBJECT_SCOPES = {SCOPE_MEMBER, SCOPE_HOUSEHOLD, SCOPE_INSTITUTION, SCOPE_UNKNOWN}

# 每种角色允许的用途（只有 accepted 状态才生效）
ALLOWED_USES = {
    ROLE_BINDING_CONDITION: ['condition', 'exclusion', 'association', 'context'],
    ROLE_EXCLUSION: ['exclusion', 'context'],
    ROLE_AUDIENCE_STATEMENT: ['association', 'context', 'source_gap'],
    ROLE_CLASSIFICATION: ['context', 'classification'],
    ROLE_BENEFIT_STANDARD: ['benefit', 'context'],
    ROLE_PROCEDURE: ['procedure', 'context'],
    ROLE_REFERENCE_ONLY: ['context', 'source_gap'],
    ROLE_UNRESOLVED: ['context', 'source_gap'],
}


@dataclass
class EvidenceFragment:
    evidence_id: str
    source_field: str
    text: str
    role: str = ROLE_UNRESOLVED
    audit_status: str = AUDIT_DRAFT  # draft / accepted / rejected / failed
    applies_to: str = 'current_detail'  # current_detail / parent_policy / compilation
    constraint_relation: str = 'unresolved'  # required / excluded / preferred / unresolved
    subject_scope: str = SCOPE_UNKNOWN  # member / household / institution / unknown
    parent_scope: str = ''  # 上级范围（如"中等职业学校毕业生"）
    reason: str = ''
    provenance: dict = field(default_factory=dict)
    children: list = field(default_factory=list)  # 子片段

    def allows_use(self, purpose: str) -> bool:
        if self.audit_status != AUDIT_ACCEPTED:
            return False
        return purpose in ALLOWED_USES.get(self.role, [])


def field_role_hint(field_name: str) -> str:
    """根据字段名给出角色提示（不是最终决定）。"""
    if field_name in ('original_conditions', 'conditions'):
        return ROLE_BINDING_CONDITION
    if field_name in ('original_subject', 'target', 'parent_target'):
        return ROLE_AUDIENCE_STATEMENT
    if field_name in ('original_title', 'title', 'original_category3') or field_name.startswith('scope_category'):
        return ROLE_CLASSIFICATION
    if field_name in ('parent_conditions', 'reference_text'):
        return ROLE_REFERENCE_ONLY
    if field_name.startswith('linked_qualification_'):
        return ROLE_BINDING_CONDITION
    if field_name.startswith('linked_'):
        return ROLE_REFERENCE_ONLY
    return ROLE_UNRESOLVED


def split_text_segments(text: str) -> list:
    """把长文本拆成片段：按句号、分号、换行。
    
    不做关键词语义判断，只做稳定的物理分段。
    """
    if not text:
        return []
    segments = []
    parts = re.split(r'[。；;\n]+', text)
    for i, part in enumerate(parts):
        part = part.strip()
        if part:
            segments.append({'segment_id': i, 'text': part})
    return segments


def build_initial_fragments(source: dict, evidence_map: dict) -> dict:
    """从 evidence_map 构建初始片段（draft 状态）。
    
    返回 {evidence_id: EvidenceFragment}
    """
    registry = {}
    for eid, ev in evidence_map.items():
        field = ev.get('source', '')
        hint = field_role_hint(field)
        registry[eid] = EvidenceFragment(
            evidence_id=eid,
            source_field=field,
            text=ev.get('quote', ''),
            role=hint,
            audit_status=AUDIT_DRAFT,
            provenance=ev,
        )
    return registry


def resolve_roles(fragments: dict, source_context: dict, resolver: Callable) -> dict:
    """解析器：只产生候选角色，审核状态仍为 draft。
    
    resolver 是依赖注入的函数：输入 (fragments, source_context)，返回 {evidence_id: {role, applies_to, constraint_relation, subject_scope, parent_scope, reason}}
    """
    raw_results = resolver(fragments, source_context)
    if not isinstance(raw_results, dict):
        return fragments
    
    for eid, result in raw_results.items():
        if eid not in fragments:
            continue
        if not isinstance(result, dict):
            continue
        # 结构校验
        valid_roles = {ROLE_BINDING_CONDITION, ROLE_EXCLUSION, ROLE_AUDIENCE_STATEMENT, 
                       ROLE_CLASSIFICATION, ROLE_BENEFIT_STANDARD, ROLE_PROCEDURE,
                       ROLE_REFERENCE_ONLY, ROLE_UNRESOLVED}
        role = result.get('role', ROLE_UNRESOLVED)
        if role not in valid_roles:
            role = ROLE_UNRESOLVED
        
        frag = fragments[eid]
        frag.role = role
        frag.applies_to = result.get('applies_to', frag.applies_to)
        frag.constraint_relation = result.get('constraint_relation', 'unresolved')
        
        # subject_scope 校验
        scope = result.get('subject_scope', SCOPE_UNKNOWN)
        if scope not in VALID_SUBJECT_SCOPES:
            scope = SCOPE_UNKNOWN
        frag.subject_scope = scope
        
        frag.parent_scope = result.get('parent_scope', '')
        frag.reason = result.get('reason', '')
        frag.audit_status = AUDIT_DRAFT  # 解析器不签署审核通过
    
    return fragments


def audit_roles(fragments: dict, source_context: dict, auditor: Callable) -> dict:
    """独立审核器：核对来源、上下文和候选角色。
    
    auditor 是依赖注入的函数：输入 (fragments, source_context)，返回 {evidence_id: {audit_status, reason}}
    只有 auditor 返回 accepted，才能用于授权。
    """
    audit_results = auditor(fragments, source_context)
    if not isinstance(audit_results, dict):
        return fragments
    
    for eid, result in audit_results.items():
        if eid not in fragments:
            continue
        if not isinstance(result, dict):
            continue
        
        frag = fragments[eid]
        status = result.get('audit_status', AUDIT_FAILED)
        valid_statuses = {AUDIT_DRAFT, AUDIT_ACCEPTED, AUDIT_REJECTED, AUDIT_FAILED}
        if status not in valid_statuses:
            status = AUDIT_FAILED
        
        frag.audit_status = status
        if 'reason' in result:
            frag.reason = result['reason']
    
    return fragments


def authorize_evidence(evidence_id: str, purpose: str, registry: dict) -> tuple:
    """检查证据是否允许用于指定用途。
    
    返回 (allowed: bool, error: dict)
    """
    frag = registry.get(evidence_id)
    if not frag:
        return False, {
            'code': 'unknown_evidence',
            'evidence_id': evidence_id,
            'purpose': purpose,
            'message': f'证据 {evidence_id} 不存在',
        }
    
    # 检查审核状态
    if frag.audit_status == AUDIT_DRAFT:
        return False, {
            'code': 'evidence_not_audited',
            'evidence_id': evidence_id,
            'purpose': purpose,
            'role': frag.role,
            'source_field': frag.source_field,
            'message': f'证据 {evidence_id} 尚未经过独立审核，不能用于 {purpose}',
        }
    if frag.audit_status == AUDIT_REJECTED:
        return False, {
            'code': 'evidence_audit_rejected',
            'evidence_id': evidence_id,
            'purpose': purpose,
            'role': frag.role,
            'source_field': frag.source_field,
            'message': f'证据 {evidence_id} 被独立审核拒绝，不能用于 {purpose}',
        }
    if frag.audit_status == AUDIT_FAILED:
        return False, {
            'code': 'evidence_audit_failed',
            'evidence_id': evidence_id,
            'purpose': purpose,
            'role': frag.role,
            'source_field': frag.source_field,
            'message': f'证据 {evidence_id} 审核失败，不能用于 {purpose}',
        }
    
    # 检查角色权限
    if not frag.allows_use(purpose):
        return False, {
            'code': 'evidence_use_not_authorized',
            'evidence_id': evidence_id,
            'purpose': purpose,
            'role': frag.role,
            'source_field': frag.source_field,
            'message': f'证据 {evidence_id}（角色={frag.role}）不允许用于 {purpose} 用途',
        }
    
    return True, None
