# -*- coding: utf-8 -*-
"""SQLAlchemy 模型。表结构按 Kingbase 设计（PostgreSQL 兼容语法），
开发阶段跑在 sqlite 上；切 Kingbase 只改连接串，模型不动。"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class AdminUser(db.Model):
    """管理员账号（当前单管理员，预留多用户扩展）"""
    __bind_key__ = 'auth'
    __tablename__ = 'admin_user'
    real_name = db.Column(db.String(64))
    active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=True)
    auth_version = db.Column(db.Integer, nullable=False, default=1)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.BigInteger, nullable=False, default=0)
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(32), default='admin')
    created_at = db.Column(db.String(32), default=_now_str)


class Policy(db.Model):
    """政策主表（对应现有 smart_policy.db 的 policy 表，新增 3 个管理字段）
    policy_id 现有主键名；新字段 updated_at/updated_by/user_id 问答侧不返回。"""
    __tablename__ = 'policy'
    policy_id = db.Column(db.Integer, primary_key=True)
    policy_code = db.Column(db.String(32))
    policy_name = db.Column(db.String(255))
    level = db.Column(db.String(8))
    category1 = db.Column(db.String(64))
    category2 = db.Column(db.String(64))
    category3 = db.Column(db.String(64))
    attr = db.Column(db.Text)
    regions = db.Column(db.Text)
    doc_ref = db.Column(db.Text)
    target = db.Column(db.Text)
    conditions = db.Column(db.Text)
    benefit = db.Column(db.Text)
    process = db.Column(db.Text)
    contact = db.Column(db.Text)
    effect_date = db.Column(db.String(32))
    expire_date = db.Column(db.String(32))
    status = db.Column(db.String(16))
    cur_version = db.Column(db.String(16))
    source = db.Column(db.String(255))
    level_note = db.Column(db.Text)
    # ---- 管理字段（仅管理界面可见，问答接口不返回） ----
    updated_at = db.Column(db.String(32))
    updated_by = db.Column(db.String(64))
    user_id = db.Column(db.Integer)   # 预留多用户


class PolicyVersion(db.Model):
    """版本留档表（已有表，确认更新时旧版快照写 content_snapshot）"""
    __tablename__ = 'policy_version'
    version_id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer)
    version_no = db.Column(db.String(16))
    content_snapshot = db.Column(db.Text)
    effect_date = db.Column(db.String(32))
    expire_date = db.Column(db.String(32))
    updated_by = db.Column(db.String(64))
    updated_at = db.Column(db.String(32))
    note = db.Column(db.Text)


class PolicyDraft(db.Model):
    """待确认草稿表：识别/上传结果先进这里，确认后才落主表"""
    __tablename__ = 'policy_draft'
    draft_id = db.Column(db.Integer, primary_key=True)
    policy_code = db.Column(db.String(32))       # 命中已有编号则带值；空 = 新增
    policy_name = db.Column(db.String(255))
    level = db.Column(db.String(8))              # 模型初值（S/X）
    category1 = db.Column(db.String(64))         # 模型初值
    doc_ref = db.Column(db.Text)                 # 文号（模型识别）
    regions = db.Column(db.Text)                 # 地区清单（模型识别/人工补充）
    status = db.Column(db.String(16), default='在用')
    version_no = db.Column(db.String(16))        # 命中时 = 当前版本+1；新增 = v1
    source = db.Column(db.String(255))           # PDF 文件名
    pdf_path = db.Column(db.String(512))
    match_type = db.Column(db.String(16))        # 文号命中 / 相似度命中 / 新增 / 待接入识别
    confidence = db.Column(db.Float)             # 置信度
    draft_data = db.Column(db.JSON)              # 模型识别完整字段集（可编辑）
    match_policy_id = db.Column(db.Integer)      # 相似度命中的候选 policy_id
    match_reason = db.Column(db.Text)            # 模型判定依据
    created_by = db.Column(db.String(64))
    created_at = db.Column(db.String(32), default=_now_str)
    confirm_status = db.Column(db.String(16), default='待确认')  # 待确认/已确认/已驳回
    confirmed_by = db.Column(db.String(64))
    confirmed_at = db.Column(db.String(32))


class QaLog(db.Model):
    """问答记录表：不脱敏全量留痕，管理界面按时间倒序展示前 N 条"""
    __bind_key__ = 'auth'
    __tablename__ = 'qa_log'
    status = db.Column(db.String(24), nullable=False, default='complete', index=True)
    selected_model = db.Column(db.String(256))
    actual_model = db.Column(db.String(1024))
    qa_id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    matched_policies = db.Column(db.Text)        # 命中政策编号列表
    match_json = db.Column(db.Text)              # 适用判定清单（问答与导出共用，保证一致）
    user_id = db.Column(db.Integer)              # 预留
    session_id = db.Column(db.String(128))
    source = db.Column(db.String(32))
    ask_time = db.Column(db.String(32), default=_now_str)


class QaTrace(db.Model):
    """按轮持久化链路 trace_id（刷新页面/事后回放的事件索引）。

    qa_log 表归数据库属主（system）所有，应用账号没有 ALTER 权限；
    而 public schema 对应用账号开放 CREATE，因此 trace 映射独立成表。
    该表只增不改，qa_log 删除历史时残留行无害。
    """
    __bind_key__ = 'auth'
    __tablename__ = 'qa_trace'
    qa_id = db.Column(db.Integer, primary_key=True)
    trace_id = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.String(32), default=_now_str)


class Model(db.Model):
    """模型注册表（docs/redesign/30-custom-model-registry.md §4.1）。
    对话/嵌入/重排模型是一等资源，不再捆绑在 yaml 平台里。
    表结构由 deploy/migrate_models.py 建（含 uq_models_default 部分唯一索引）。"""
    __tablename__ = 'models'
    id = db.Column(db.String(64), primary_key=True)
    type = db.Column(db.String(16), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    source = db.Column(db.String(16), nullable=False, default='remote')
    provider = db.Column(db.String(32), nullable=False, default='openai')
    base_url = db.Column(db.Text, nullable=False)
    api_key_enc = db.Column(db.Text)
    model_name = db.Column(db.Text, nullable=False)
    dim = db.Column(db.Integer)
    query_instruction = db.Column(db.Text)
    json_mode = db.Column(db.Boolean)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class AuditLog(db.Model):
    """操作日志：上传/确认（新增/更新/停用）/驳回，管理界面「最新修改条目」"""
    __tablename__ = 'audit_log'
    log_id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(32))
    # target_code 存 Model.id（见 model_admin._audit）。Model.id 本就是
    # String(64)，且 _ID_RE 上限 64，旧 String(32) 会在 seed CJK/点号 id 下截断；
    # 生产迁移同步见 deploy/migrate_models.py。
    target_code = db.Column(db.String(64))
    target_name = db.Column(db.String(255))
    detail = db.Column(db.Text)
    operator = db.Column(db.String(64))
    created_at = db.Column(db.String(32), default=_now_str)


class SurveyResult(db.Model):
    """农户发展意愿问卷结果（8题答案/得分/等级，Kingbase 留痕）。
    管理字段 created_at/updated_by 问答侧不返回，仅管理员可见。"""
    __bind_key__ = 'auth'
    __tablename__ = 'survey_result'
    survey_id = db.Column(db.Integer, primary_key=True)
    member_no = db.Column(db.Integer)           # 成员序号（问卷按成员作答）
    member_name = db.Column(db.String(64))
    household_id = db.Column(db.String(64))     # 户 ID（管理员分配）
    household_head = db.Column(db.String(64))   # 户主姓名
    answers = db.Column(db.Text)                # 8 题答案 JSON
    score = db.Column(db.Integer)               # 个人得分（满分 11）
    will_level = db.Column(db.String(8))        # 高/中/低
    speech = db.Column(db.Text)                 # 平台端话术（可读+【意愿标签】行）
    session_id = db.Column(db.String(128))
    user_id = db.Column(db.Integer)
    created_at = db.Column(db.String(32), default=_now_str)
    updated_by = db.Column(db.String(64))


class Conversation(db.Model):
    __bind_key__ = 'auth'
    __tablename__ = 'conversation'
    session_id = db.Column(db.String(128), primary_key=True)
    user_id = db.Column(db.Integer, index=True)
    title = db.Column(db.String(128), nullable=False)
    selected_model = db.Column(db.String(256))
    updated_at = db.Column(db.String(32), default=_now_str, index=True)


class LoginSession(db.Model):
    __bind_key__ = 'auth'
    __tablename__ = 'login_session'
    id = db.Column(db.String(128), primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    expires_at = db.Column(db.BigInteger, nullable=False)
    revoked = db.Column(db.Boolean, nullable=False, default=False)
    csrf_hash = db.Column(db.String(64), nullable=False)


class AuthAudit(db.Model):
    __bind_key__ = 'auth'
    __tablename__ = 'auth_audit'
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer)
    action = db.Column(db.String(64), nullable=False)
    target = db.Column(db.String(128))
    happened_at = db.Column(db.BigInteger, nullable=False)


class DeleteConfirmation(db.Model):
    __bind_key__ = 'auth'
    __tablename__ = 'delete_confirmation'
    token = db.Column(db.String(128), primary_key=True)
    session_id = db.Column(db.String(128), nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.BigInteger, nullable=False)


db.Index('ix_qa_session_id', QaLog.session_id, QaLog.qa_id)
db.Index('ix_qa_user_time', QaLog.user_id, QaLog.ask_time)
db.Index('ix_survey_session', SurveyResult.session_id)
