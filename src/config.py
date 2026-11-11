# -*- coding: utf-8 -*-
"""管理后台配置。
数据源优先级：
  1) 环境变量 ADMIN_DB_URL（显式指定完整连接串）
  2) USE_KINGBASE=1（环境变量或注册表 HKCU\\Environment）→ 连 KingbaseES policy_db，
     密码从 KINGBASE_PASSWORD 读取（环境变量或注册表，不写明文）
  3) 默认：sqlite（现有 smart_policy.db）
KingbaseES 兼容 PostgreSQL 协议，SQLAlchemy 用 postgresql+psycopg2 方言。
"""
import os
try:
    import winreg
except ImportError:
    winreg = None
from urllib.parse import quote

# ---- KingbaseES 兼容补丁：SQLAlchemy PG 方言解析不了 Kingbase 版本串 ----
# Kingbase V8 wire 协议兼容 PostgreSQL 12（psycopg2 server_version=120001），
# 这里让方言按 PG 12 处理（JSONB 等特性正常），不影响普通 PostgreSQL。
import re as _re
from sqlalchemy.dialects.postgresql import base as _pg_base

_orig_get_ver = _pg_base.PGDialect._get_server_version_info


def _kb_server_version(self, connection):
    try:
        return _orig_get_ver(self, connection)
    except AssertionError:
        return (12, 0, 1)


_pg_base.PGDialect._get_server_version_info = _kb_server_version

# Kingbase 把 PostgreSQL 的"非保留关键字" level 当保留字 -> 强制 SQLAlchemy 加引号
_pg_base.PGIdentifierPreparer.reserved_words.add('level')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 无显式地区政策时的默认口径（江西库历史语义，保持不变）：
# 国家级政策默认"全国"，省级政策默认"江西省全省"。
# 全国化模式（SERVICE_SCOPE=national / REGION_NATIONWIDE=1）下，省级政策不再默认
# 归属江西（否则全国库里的省级政策会被静默当成江西政策，误放行）；
# 该分流由 kg_retrieval._policy_default_regions() 在运行期按开关处理，不在本常量里做。
DEFAULT_POLICY_REGIONS = {'C': '全国', 'S': '江西省全省'}

_SQLITE_PATH = os.path.join(BASE_DIR, '..', 'data', 'policy_db', 'smart_policy.db')
DEFAULT_DB_URL = 'sqlite:///' + _SQLITE_PATH.replace('\\', '/')

KB_HOST, KB_PORT, KB_USER, KB_DB = '127.0.0.1', 54321, 'system', 'policy_db'


def _reg_get(name):
    if winreg is None:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment')
        v, _ = winreg.QueryValueEx(key, name)
        return v
    except OSError:
        return None


def _env_or_reg(name, default=None):
    return os.environ.get(name) or _reg_get(name) or default


def _resolve_db_url():
    explicit = os.environ.get('ADMIN_DB_URL')
    if explicit:
        return explicit
    use_kb = str(_env_or_reg('USE_KINGBASE', '')).strip()
    if use_kb == '1':
        pw = _env_or_reg('KINGBASE_PASSWORD', '')
        return 'postgresql+psycopg2://%s:%s@%s:%s/%s' % (KB_USER, quote(pw), KB_HOST, KB_PORT, KB_DB)
    return DEFAULT_DB_URL


DATABASE_URL = _resolve_db_url()
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
EXPORT_PATH = os.environ.get('POLICY_EXPORT_PATH', os.path.join(BASE_DIR, '..', 'data', 'policy_db', '政策编号台账.xlsx'))
# SECRET_KEY 必须由部署环境提供（deploy/web.env 的 ADMIN_SECRET）。
# 缺失时用进程级随机密钥兜底：不写死默认口令，重启后旧会话失效但不会静默使用弱密钥。
SECRET_KEY = _env_or_reg('ADMIN_SECRET') or __import__('secrets').token_hex(32)
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200MB


# Independent account/history database; absence is a startup error, never fallback.
AUTH_DATABASE_URL = _env_or_reg('SMART_HELP_AUTH_DATABASE_URL', '')
AUTH_JWT_SECRET = _env_or_reg('SMART_HELP_AUTH_JWT_SECRET', '')

# forbid 策略开关：正式使用证据核验，可通过环境变量回退进行对照。
# 'v1' = 旧类别硬排除（rule_check.forbid 按 category1/名称删候选，逐风险并集，默认保留）
# 'v2' = 证据化策略（来源不可追溯的 forbid 不删除候选、不降排名，仅标注；资格排除/互斥需政策原文依据）
FORBID_STRATEGY = os.environ.get('FORBID_STRATEGY', 'v2')
if FORBID_STRATEGY not in ('v1', 'v2'):
    raise ValueError('FORBID_STRATEGY must be v1 or v2')

# ---- 图谱辅助家庭政策匹配（已接线，通过隔离与真实模型冒烟验证）----
# 关闭（False）= 原链路（规则候选 → LLM 判定），回归一致；
# 开启（True） = 规则候选 + 图谱候选 融合 → 统一核验（kg_batch5_merge）。
KG_GRAPH_ENABLED = os.environ.get('KG_GRAPH_ENABLED', '1').strip().lower() in ('1', 'true', 'yes')
KG_ROW_LIMIT = 50          # 每轮最多进入模型判断的行数；其余完整保留为待核实
KG_GRAPH_CAP = 20          # 图谱候选全局补位上限（规则候选不截断）
KG_DETAIL_CAP = None       # 不按明细数截断；整轮模型预算超限时明确失败
KG_SAME_DOC_MAX_POLICIES = 20  # 大型共同依据文件不生成政策间扩散边
KG_EVIDENCE_CHARS = 120    # 每行核验证据文本截断长度
KG_MAX_MODEL_CALLS = 40    # 含判定重试与最终 AI 建议；运行时硬限制
KG_MAX_PROMPT_CHARS = 24000 # 每次请求原文输入字符上限，超限失败，不截掉资格条件
KG_AUTO_UPDATE = os.environ.get('KG_AUTO_UPDATE', '1').strip().lower() in ('1','true','yes')
# 追踪日志级别（第五阶段）：summary=只输出请求总览/阶段耗时/缓存统计/判定及原因分布/错误；
# debug=追加逐明细核验与候选排除/丢弃详情。默认 summary 以降噪（缓存命中不再逐次记事件）。
TRACE_LOG_LEVEL = os.environ.get('TRACE_LOG_LEVEL', 'summary').strip().lower()
if TRACE_LOG_LEVEL not in ('summary', 'debug'):
    raise ValueError('TRACE_LOG_LEVEL must be summary or debug')
